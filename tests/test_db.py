import json
from datetime import UTC, datetime, timedelta

from job_matcher import db

from .conftest import make_job, make_spec


def test_migrations_are_idempotent(conn):
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_upsert_new_then_unchanged_then_updated(conn):
    first = db.upsert_job(conn, make_job("1"), now="2026-11-01T00:00:00+00:00")
    assert first.status == "new"

    again = db.upsert_job(conn, make_job("1"), now="2026-11-02T00:00:00+00:00")
    assert again == db.UpsertResult(first.job_id, "unchanged")

    changed = db.upsert_job(conn, make_job("1", description="Now with Rust."))
    assert changed.status == "updated"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (first.job_id,)).fetchone()
    assert row["description"] == "Now with Rust."
    assert row["first_seen"] == "2026-11-01T00:00:00+00:00"
    assert json.loads(row["location_area_json"])[-1] == "Orlando"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_unchanged_jobs_still_get_fresh_derived_fields(conn):
    job = make_job("1")
    db.upsert_job(conn, job)
    job.metro = "orlando"
    db.upsert_job(conn, job)
    assert conn.execute("SELECT metro FROM jobs").fetchone()[0] == "orlando"


def test_same_title_company_city_is_flagged_as_duplicate(conn):
    original = db.upsert_job(conn, make_job("1"))
    repost = db.upsert_job(conn, make_job("2", title="Software  Developer!"))
    third = db.upsert_job(conn, make_job("3"))
    other = db.upsert_job(conn, make_job("4", company="Other Co"))

    assert repost.duplicate_of == original.job_id
    assert third.duplicate_of == original.job_id
    assert other.duplicate_of is None


def test_jobs_without_a_company_are_never_flagged(conn):
    db.upsert_job(conn, make_job("1", company=None))
    second = db.upsert_job(conn, make_job("2", company=None))
    assert second.duplicate_of is None


def test_hits_are_recorded_once_per_run_and_search(conn):
    run_id = db.start_run(conn, "fake")
    result = db.upsert_job(conn, make_job("1"))
    spec = make_spec()
    db.record_hit(conn, result.job_id, run_id, spec)
    db.record_hit(conn, result.job_id, run_id, spec)
    db.record_hit(conn, result.job_id, run_id, make_spec(region="orlando"))
    assert conn.execute("SELECT COUNT(*) FROM job_hits").fetchone()[0] == 2


def test_requests_in_last_sums_runs_inside_the_window(conn):
    for requests in (3, 5, 7):
        run_id = db.start_run(conn, "adzuna")
        db.finish_run(conn, run_id, status="ok", requests=requests,
                      new=0, updated=0, unchanged=0, errors=[])
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
    conn.execute("UPDATE runs SET started_at = '2020-01-01T00:00:00+00:00' WHERE id = 1")
    conn.execute("UPDATE runs SET started_at = ? WHERE id = 2", (old,))
    assert db.requests_in_last(conn, "adzuna", days=1) == 7
    assert db.requests_in_last(conn, "adzuna", days=30) == 12
    assert db.requests_in_last(conn, "other", days=30) == 0
