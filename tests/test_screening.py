import json
from pathlib import Path

from job_matcher import db, pipeline
from job_matcher.profile import load_profile

from .conftest import make_job

PROFILE = load_profile(Path(__file__).parents[1] / "config" / "profile.example.yaml")


def test_screen_stores_tags_and_results(conn):
    good = db.upsert_job(conn, pipeline.enrich(make_job("1", title="Junior Developer")))
    senior = db.upsert_job(conn, pipeline.enrich(make_job("2", title="Senior Developer")))
    conn.commit()

    report = pipeline.screen(conn, PROFILE)
    assert (report.total, report.passed) == (2, 1)
    assert report.reasons == {"seniority": 1}

    passed = db.screened_jobs(conn, passed=True)
    assert [r["id"] for r in passed] == [good.job_id]
    assert passed[0]["entry_level"] == "title"
    assert passed[0]["role_family"] == "core_dev"

    rejected = db.screened_jobs(conn, passed=False, reason="seniority")
    assert [r["id"] for r in rejected] == [senior.job_id]
    assert json.loads(rejected[0]["reasons_json"])[0]["code"] == "seniority"

    summary = db.screening_summary(conn)
    assert summary == {"total": 2, "passed": 1, "reasons": [("seniority", 1)]}


def test_screened_jobs_can_be_split_by_employment(conn):
    full = db.upsert_job(conn, pipeline.enrich(
        make_job("1", title="Junior Developer", description="Full-time role.")))
    part = db.upsert_job(conn, pipeline.enrich(
        make_job("2", title="Junior Programmer", description="Part-time role.")))
    db.upsert_job(conn, pipeline.enrich(make_job("3", title="Junior Engineer, Software")))
    conn.commit()
    pipeline.screen(conn, PROFILE)

    rows = db.screened_jobs(conn, passed=True)
    assert sorted(r["employment"] for r in rows) == ["full_time", "part_time", "unknown"]
    only_part = db.screened_jobs(conn, passed=True, employment="part_time")
    assert [r["id"] for r in only_part] == [part.job_id]
    assert [r["id"] for r in db.screened_jobs(conn, passed=True, employment="full_time")] == [
        full.job_id
    ]


def test_screening_refreshes_stale_work_mode(conn):
    job = make_job("1", title="Software Engineer I (Onsite)", metro=None,
                   location_area=["US", "Ohio", "Columbus"])
    result = db.upsert_job(conn, job)  # stored without enrich(): work_mode is None
    conn.commit()
    pipeline.screen(conn, PROFILE)
    row = conn.execute("SELECT work_mode FROM jobs WHERE id = ?", (result.job_id,)).fetchone()
    assert row[0] == "onsite"
    assert db.screened_jobs(conn, passed=False, reason="location")


def test_rescreening_replaces_old_results(conn):
    db.upsert_job(conn, pipeline.enrich(make_job("1", title="Senior Developer")))
    conn.commit()
    pipeline.screen(conn, PROFILE)

    relaxed = PROFILE.model_copy(deep=True)
    relaxed.seniority.exclude_title = []
    report = pipeline.screen(conn, relaxed)
    assert report.passed == 1
    assert conn.execute("SELECT COUNT(*) FROM filter_results").fetchone()[0] == 1
    hashes = conn.execute("SELECT DISTINCT profile_hash FROM filter_results").fetchall()
    assert [h[0] for h in hashes] == [relaxed.fingerprint()]
    assert conn.execute(
        "SELECT COUNT(*) FROM tags WHERE name = 'seniority'"
    ).fetchone()[0] == 0


def test_fetch_records_why_a_run_stopped(conn):
    from job_matcher.sources.fake import FakeSource

    from .conftest import make_spec

    specs = [make_spec(term=f"t{i}") for i in range(2)]
    source = FakeSource({("florida", f"t{i}"): [[make_job(str(i))]] for i in range(2)})
    pipeline.fetch(conn, source, specs, daily_limit=100, run_limit=1)
    stored = conn.execute("SELECT stopped_reason FROM runs").fetchone()[0]
    assert stored == "reached the --limit for this run"
