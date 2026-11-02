import pytest

from job_matcher import db, pipeline
from job_matcher.sources.base import SourceAuthError, SourceError
from job_matcher.sources.fake import FakeSource

from .conftest import make_job, make_spec


def test_fetch_stores_jobs_hits_and_derived_fields(conn):
    spec = make_spec(max_pages=2)
    source = FakeSource({
        ("florida", spec.term): [
            [make_job("1"), make_job("2", title="QA Analyst", description="Fully remote.")],
            [make_job("3", title="Tester")],
        ]
    })
    report = pipeline.fetch(conn, source, [spec], daily_limit=100)

    assert (report.new, report.requests, report.searches_run) == (3, 2, 1)
    assert dict(report.new_by_region) == {"florida": 3}
    rows = conn.execute("SELECT source_id, metro, work_mode FROM jobs ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("1", "orlando", None), ("2", "orlando", "remote"),
                                        ("3", "orlando", None)]
    run = conn.execute("SELECT status, requests, jobs_new FROM runs").fetchone()
    assert tuple(run) == ("ok", 2, 3)


def test_early_stop_skips_older_pages_once_nothing_is_new(conn):
    spec = make_spec(max_pages=3)
    pages = {("florida", spec.term): [[make_job("1")], [make_job("2")], [make_job("3")]]}
    pipeline.fetch(conn, FakeSource({("florida", spec.term): [[make_job("1")]]}), [spec],
                   daily_limit=100)

    source = FakeSource(pages)
    report = pipeline.fetch(conn, source, [spec], daily_limit=100)
    assert report.requests == 1  # page 1 was all known, so pages 2-3 were never fetched
    assert report.unchanged == 1

    full = pipeline.fetch(conn, FakeSource(pages), [spec], daily_limit=100, early_stop=False)
    assert full.requests == 3
    assert full.new == 2


def test_one_failing_search_does_not_stop_the_others(conn):
    bad, good = make_spec(term="bad"), make_spec(term="good")
    source = FakeSource(
        {("florida", "good"): [[make_job("1")]]},
        errors={("florida", "bad"): SourceError("HTTP 400")},
    )
    report = pipeline.fetch(conn, source, [bad, good], daily_limit=100)
    assert report.new == 1
    assert report.errors == ["florida / bad: HTTP 400"]
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "partial"


def test_daily_budget_stops_the_run(conn):
    run_id = db.start_run(conn, "fake")
    db.finish_run(conn, run_id, status="ok", requests=9, new=0, updated=0, unchanged=0,
                  errors=[])
    specs = [make_spec(term=f"t{i}") for i in range(3)]
    source = FakeSource({("florida", f"t{i}"): [[make_job(str(i))]] for i in range(3)})

    report = pipeline.fetch(conn, source, specs, daily_limit=10)
    assert report.requests == 1
    assert report.searches_run == 1
    assert report.stopped_reason == "reached the daily limit"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"daily_limit": 100, "monthly_limit": 2}, "reached the monthly limit"),
        (
            {"daily_limit": 100, "monthly_limit": 50, "run_limit": 2},
            "reached the --limit for this run",
        ),
    ],
)
def test_tightest_budget_wins(conn, kwargs, reason):
    specs = [make_spec(term=f"t{i}") for i in range(4)]
    source = FakeSource({("florida", f"t{i}"): [[make_job(str(i))]] for i in range(4)})
    report = pipeline.fetch(conn, source, specs, **kwargs)
    assert report.requests == 2
    assert report.stopped_reason == reason


def test_auth_errors_abort_and_are_recorded(conn):
    spec = make_spec()
    source = FakeSource(
        {("florida", spec.term): [[make_job("1")]]},
        errors={("florida", spec.term): SourceAuthError("rejected")},
    )
    with pytest.raises(SourceAuthError):
        pipeline.fetch(conn, source, [spec], daily_limit=100)
    assert conn.execute("SELECT status FROM runs").fetchone()[0] == "failed"
