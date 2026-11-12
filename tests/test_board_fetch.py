"""Fetching jobs from discovered boards: what's kept, what's saved, and resuming."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from job_matcher import cli, db, pipeline
from job_matcher.discovery import BoardRef, QueryResult
from job_matcher.models import Job
from job_matcher.profile import load_profile
from job_matcher.sources.boards import BoardError, BoardGone, ashby_job

PROFILE = load_profile(Path(__file__).parents[1] / "config" / "profile.example.yaml")
FIXTURES = Path(__file__).parent / "fixtures"
ASHBY = json.loads((FIXTURES / "ashby_board.json").read_text(encoding="utf-8"))["jobs"]


def board_job(n: int, title="Software Engineer", **fields) -> Job:
    """A board job, normalized from a real Ashby record, with a new id and title."""
    raw = dict(ASHBY[0], id=f"job-{n}", title=title, **fields)
    return ashby_job(raw, "acme")


REMOTE_US = {}  # ASHBY[0] is "United States | Remote"
ORLANDO_ON_SITE = {"workplaceType": "OnSite", "isRemote": False, "location": "Orlando, FL",
                   "address": {"postalAddress": {"addressLocality": "Orlando",
                                                 "addressRegion": "Florida",
                                                 "addressCountry": "USA"}}}
VIRGINIA_ON_SITE = {"workplaceType": "OnSite", "isRemote": False,
                    "location": "Hampton, VA", "address": {}}
REMOTE_GERMANY = {"location": "Germany | Remote",
                  "address": {"postalAddress": {"addressCountry": "Germany"}}}
REMOTE_ANYWHERE = {"location": "Remote", "address": {}}


# --- which jobs are kept ----------------------------------------------------------------


@pytest.mark.parametrize(("fields", "title", "wanted"), [
    (REMOTE_US, "Software Engineer", True),
    (ORLANDO_ON_SITE, "Software Engineer", True),
    (ORLANDO_ON_SITE, "QA Tester", True),  # every role family counts
    (REMOTE_ANYWHERE, "Software Engineer", True),  # country unknown: kept for review
    (REMOTE_US, "Corporate FP&A Manager", False),  # not a software role
    (VIRGINIA_ON_SITE, "Software Engineer", False),  # on-site outside the targets
    (REMOTE_GERMANY, "Software Engineer", False),  # remote, but not US or Canada
])
def test_board_job_wanted(fields, title, wanted):
    assert pipeline.board_job_wanted(board_job(1, title, **fields), PROFILE) is wanted


# --- fetching ---------------------------------------------------------------------------


class FakeReader:
    """Stands in for AshbyReader: each token answers with jobs, or raises."""

    ats = "ashby"

    def __init__(self, boards):
        self.boards = boards
        self.listed: list[str] = []
        self.completed: list[str] = []
        self.client = type("Client", (), {"requests_made": 0})()

    def list_jobs(self, token):
        self.listed.append(token)
        self.client.requests_made += 1
        answer = self.boards[token]
        if isinstance(answer, Exception):
            raise answer
        return [replace(job) for job in answer]

    def complete(self, job):
        self.completed.append(job.source_id)
        if job.title.startswith("Closed meanwhile"):
            raise BoardGone("board not found (HTTP 404)")
        return replace(job, description=job.description + " Full text.")


def add_boards(conn, *tokens, ats="ashby"):
    boards = {BoardRef(ats, token): 1 for token in tokens}
    db.save_query_result(conn, "CC-MAIN-2026-43", QueryResult("x/*", boards, True, [1, 1]))


def fetch(conn, reader, **options):
    return pipeline.fetch_boards(conn, {"ashby": reader}, PROFILE, **options)


def board_row(conn, token):
    return conn.execute("SELECT * FROM boards WHERE token = ?", (token,)).fetchone()


def test_only_wanted_jobs_are_completed_and_saved(conn):
    add_boards(conn, "acme")
    reader = FakeReader({"acme": [
        board_job(1), board_job(2, "Corporate FP&A Manager"), board_job(3, **VIRGINIA_ON_SITE),
    ]})
    report = fetch(conn, reader)
    assert reader.completed == ["acme:job-1"]
    assert (report.boards, report.jobs_listed, report.jobs_kept, report.new) == (1, 3, 1, 1)
    saved = conn.execute("SELECT source, source_id, description, work_mode FROM jobs").fetchall()
    assert [tuple(r)[:2] for r in saved] == [("ashby", "acme:job-1")]
    assert saved[0]["description"].endswith("Full text.")
    assert saved[0]["work_mode"] == "remote"
    row = board_row(conn, "acme")
    assert (row["fetch_status"], row["jobs_listed"], row["jobs_kept"]) == ("ok", 3, 1)
    assert row["last_fetched"]


def test_fetching_again_finds_nothing_new(conn):
    add_boards(conn, "acme")
    fetch(conn, FakeReader({"acme": [board_job(1)]}))
    report = fetch(conn, FakeReader({"acme": [board_job(1)]}), refresh_days=0)
    assert (report.new, report.unchanged) == (0, 1)


def test_recently_fetched_boards_wait(conn):
    add_boards(conn, "acme")
    fetch(conn, FakeReader({"acme": []}))
    later = FakeReader({"acme": []})
    assert fetch(conn, later, refresh_days=7).boards == 0
    assert later.listed == []


def test_never_fetched_boards_come_first_and_limit_counts_boards(conn):
    add_boards(conn, "old")
    fetch(conn, FakeReader({"old": []}))
    add_boards(conn, "new1", "new2")
    reader = FakeReader({"old": [], "new1": [], "new2": []})
    report = fetch(conn, reader, refresh_days=0, limit=2)
    assert reader.listed == ["new1", "new2"]
    assert report.stopped_reason == "reached the board limit"


def test_a_gone_board_is_marked_and_skipped_later(conn):
    add_boards(conn, "closed")
    report = fetch(conn, FakeReader({"closed": BoardGone("board not found (HTTP 404)")}))
    assert report.gone == 1
    assert board_row(conn, "closed")["fetch_status"] == "gone"
    later = FakeReader({"closed": []})
    fetch(conn, later, refresh_days=0)
    assert later.listed == []


def test_a_failing_board_is_recorded_and_tried_next_time(conn):
    add_boards(conn, "flaky")
    report = fetch(conn, FakeReader({"flaky": BoardError("gave up after 4 attempts: HTTP 503")}))
    assert report.failed == ["ashby:flaky"]
    row = board_row(conn, "flaky")
    assert row["fetch_status"] == "error"
    assert row["fetch_error"] == "gave up after 4 attempts: HTTP 503"
    later = FakeReader({"flaky": []})
    fetch(conn, later)  # an error isn't a fetch: no waiting for refresh_days
    assert later.listed == ["flaky"]


def test_failures_in_a_row_stop_the_run(conn):
    add_boards(conn, *(f"b{i}" for i in range(8)))
    reader = FakeReader({f"b{i}": BoardError("HTTP 503") for i in range(8)})
    report = fetch(conn, reader, stop_after_failures=3)
    assert len(reader.listed) == 3
    assert report.stopped_reason == "3 failed boards in a row"


def test_a_job_closed_between_listing_and_fetching_is_skipped(conn):
    add_boards(conn, "acme")
    report = fetch(conn, FakeReader({"acme": [board_job(1, "Closed meanwhile Software Engineer"),
                                              board_job(2)]}))
    assert report.new == 1


def test_the_run_is_recorded(conn):
    add_boards(conn, "acme")
    fetch(conn, FakeReader({"acme": [board_job(1)]}))
    run = conn.execute("SELECT * FROM runs").fetchone()
    assert (run["source"], run["status"]) == ("boards", "ok")
    assert (run["requests"], run["jobs_new"]) == (1, 1)


def test_readers_sharing_a_client_count_its_requests_once(conn):
    add_boards(conn, "acme")
    ashby = FakeReader({"acme": []})
    greenhouse = FakeReader({})
    greenhouse.ats, greenhouse.client = "greenhouse", ashby.client
    report = pipeline.fetch_boards(conn, {"ashby": ashby, "greenhouse": greenhouse}, PROFILE)
    assert report.requests == 1


def test_a_stated_work_mode_survives_rescreening(conn):
    # The description says "remote", but Ashby says on-site in Orlando.
    job = board_job(1, **ORLANDO_ON_SITE)
    job.description = "Fully remote team, but this role is on site."
    db.upsert_job(conn, pipeline.enrich(job))
    conn.commit()
    pipeline.screen(conn, PROFILE)
    row = conn.execute("SELECT metro, work_mode FROM jobs").fetchone()
    assert (row["metro"], row["work_mode"]) == ("orlando", "onsite")


# --- the command ------------------------------------------------------------------------


def test_the_command_fetches_and_reports(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    monkeypatch.setenv("JM_DB_PATH", str(path))
    conn = db.connect(path)
    add_boards(conn, "acme")
    conn.close()
    reader = FakeReader({"acme": [board_job(1), board_job(2, "Corporate FP&A Manager")]})
    monkeypatch.setattr(cli, "_board_readers", lambda: {"ashby": reader})

    result = CliRunner().invoke(cli.app, ["fetch-boards", "--ats", "ashby"])
    assert result.exit_code == 0, result.output
    assert "ashby:acme" in result.output
    assert "2 listed, 1 kept" in result.output
    assert "1 new" in result.output


# --- closed jobs ------------------------------------------------------------------------


def closed_ids(conn):
    rows = conn.execute("SELECT source_id FROM jobs WHERE closed_at IS NOT NULL ORDER BY id")
    return [r[0] for r in rows]


def test_jobs_missing_from_their_board_are_closed_and_reopened(conn):
    add_boards(conn, "acme", "acme2")
    fetch(conn, FakeReader({"acme": [board_job(1), board_job(2)], "acme2": []}))
    # Another board whose token starts the same way: its job must stay open.
    other = ashby_job(dict(ASHBY[0], id="job-9", title="Software Engineer"), "acme2")
    db.upsert_job(conn, other)

    report = fetch(conn, FakeReader({"acme": [board_job(1)], "acme2": [other]}),
                   refresh_days=0)
    assert report.closed == 1
    assert closed_ids(conn) == ["acme:job-2"]

    fetch(conn, FakeReader({"acme": [board_job(1), board_job(2)], "acme2": [other]}),
          refresh_days=0)
    assert closed_ids(conn) == []  # it came back


def test_a_listed_job_no_longer_kept_stays_open_and_seen(conn):
    add_boards(conn, "acme")
    fetch(conn, FakeReader({"acme": [board_job(1)]}))
    conn.execute("UPDATE jobs SET last_seen = '2026-01-01T00:00:00+00:00'")
    # Still on the board, but the pre-filter no longer keeps it: open, and seen just now.
    report = fetch(conn, FakeReader({"acme": [board_job(1, "Corporate FP&A Manager")]}),
                   refresh_days=0)
    assert (report.closed, closed_ids(conn)) == (0, [])
    assert conn.execute("SELECT last_seen FROM jobs").fetchone()[0] > "2026-11"


def test_a_gone_board_closes_its_jobs(conn):
    add_boards(conn, "acme")
    fetch(conn, FakeReader({"acme": [board_job(1)]}))
    report = fetch(conn, FakeReader({"acme": BoardGone("board not found (HTTP 404)")}),
                   refresh_days=0)
    assert (report.gone, report.closed) == (1, 1)
    assert closed_ids(conn) == ["acme:job-1"]
