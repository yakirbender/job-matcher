"""Saving discovery results, resuming, and surviving failed queries."""

import json

from job_matcher import db, pipeline
from job_matcher.discovery import CdxError, prefix_patterns

API = "https://index.example/x-index"
CRAWL = "CC-MAIN-2026-43"
HOST = "jobs.ashbyhq.com"
ALL_PREFIXES = len(prefix_patterns(HOST))


def lines_for(*tokens: str) -> list[str]:
    return [json.dumps({"url": f"https://{HOST}/{token}/job"}) for token in tokens]


class FakeIndex:
    """Stands in for CdxClient. A pattern answers from its queue of reads, repeating the
    last one; `failures` makes a pattern raise that many times first."""

    def __init__(self, reads=None, failures=None) -> None:
        self.reads = reads or {}
        self.failures = failures or {}
        self.asked: list[str] = []

    def pages(self, cdx_api: str, url_pattern: str):
        self.asked.append(url_pattern)
        if self.failures.get(url_pattern, 0) > 0:
            self.failures[url_pattern] -= 1
            raise CdxError("gave up after 3 attempts: HTTP 504")
        queue = self.reads.get(url_pattern, [[]])
        yield queue.pop(0) if len(queue) > 1 else queue[0]


def run(conn, index, crawl=CRAWL, **options):
    options.setdefault("sleep", lambda seconds: None)
    return pipeline.discover_crawl(conn, index, crawl, API, [HOST], **options)


def statuses(conn, crawl=CRAWL):
    return {p: row["status"] for p, row in db.discovery_queries(conn, crawl).items()}


def test_results_are_saved_with_their_status(conn):
    index = FakeIndex({f"{HOST}/a*": [lines_for("acme", "acme", "apex")]})
    report = run(conn, index)
    assert report.finished == ALL_PREFIXES
    assert report.new_boards == 2
    board = conn.execute("SELECT * FROM boards WHERE token = 'acme'").fetchone()
    assert (board["ats"], board["captures"], board["last_crawl"]) == ("ashby", 2, CRAWL)
    assert set(statuses(conn).values()) == {"confirmed"}
    row = db.discovery_queries(conn, CRAWL)[f"{HOST}/a*"]
    assert json.loads(row["line_counts_json"]) == [3, 3]


def test_rerunning_a_finished_crawl_makes_no_requests(conn):
    run(conn, FakeIndex())
    index = FakeIndex()
    report = run(conn, index)
    assert index.asked == []
    assert report.finished == 0


def test_a_stopped_run_resumes_without_repeating_finished_queries(conn):
    first = FakeIndex()
    report = run(conn, first, limit=5)
    assert report.finished == 5
    assert report.stopped_reason == "reached the query limit"
    done = set(first.asked)

    second = FakeIndex()
    run(conn, second)
    assert not done & set(second.asked)
    assert len(statuses(conn)) == ALL_PREFIXES


def test_a_failed_query_does_not_stop_the_others_and_is_retried(conn):
    index = FakeIndex({f"{HOST}/c*": [lines_for("cobalt")]}, failures={f"{HOST}/b*": 1})
    report = run(conn, index)
    assert report.failed == []
    assert statuses(conn)[f"{HOST}/b*"] == "confirmed"
    assert statuses(conn)[f"{HOST}/c*"] == "confirmed"
    row = db.discovery_queries(conn, CRAWL)[f"{HOST}/b*"]
    assert row["attempts"] == 2


def test_retries_wait_for_the_pause(conn):
    pauses: list[float] = []
    index = FakeIndex(failures={f"{HOST}/b*": 1})
    run(conn, index, sleep=pauses.append, retry_pause=60.0)
    assert pauses == [60.0]


def test_no_pause_when_nothing_failed(conn):
    pauses: list[float] = []
    run(conn, FakeIndex(), sleep=pauses.append)
    assert pauses == []


def test_a_query_that_keeps_failing_is_recorded_and_tried_on_the_next_run(conn):
    pattern = f"{HOST}/b*"
    index = FakeIndex(failures={pattern: 99})
    report = run(conn, index, max_attempts=3)
    assert report.failed == [pattern]
    assert index.asked.count(pattern) == 3
    row = db.discovery_queries(conn, CRAWL)[pattern]
    assert row["status"] == "failed"
    assert "HTTP 504" in row["last_error"]

    later = FakeIndex()
    assert run(conn, later).failed == []
    assert later.asked == [pattern, pattern]  # only it, read twice
    assert statuses(conn)[pattern] == "confirmed"


def test_failures_in_a_row_stop_the_run_with_everything_saved(conn):
    patterns = prefix_patterns(HOST)
    index = FakeIndex({patterns[0]: [lines_for("acme")]},
                      failures={p: 99 for p in patterns[1:]})
    report = run(conn, index, stop_after_failures=5)
    assert report.stopped_reason == "5 failed queries in a row"
    assert len(index.asked) == 2 + 5  # the good query read twice, then 5 failures
    assert conn.execute("SELECT COUNT(*) FROM boards").fetchone()[0] == 1


def test_a_success_resets_the_failure_count(conn):
    patterns = prefix_patterns(HOST)
    failing = {p: 1 for i, p in enumerate(patterns) if i % 2 == 0}  # never 2 in a row
    report = run(conn, FakeIndex(failures=failing), stop_after_failures=2)
    assert report.stopped_reason is None


def test_the_same_board_in_two_crawls_is_one_row(conn):
    run(conn, FakeIndex({f"{HOST}/a*": [lines_for("acme")]}), crawl="CC-MAIN-2026-39")
    report = run(conn, FakeIndex({f"{HOST}/a*": [lines_for("acme", "acme")]}), crawl=CRAWL)
    assert report.new_boards == 0
    rows = conn.execute("SELECT * FROM boards").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_crawl"] == "CC-MAIN-2026-39"
    assert rows[0]["last_crawl"] == CRAWL
    assert rows[0]["captures"] == 2  # the most seen in any one read, not a running sum


def unstable_reads():
    return [lines_for("aa"), lines_for("aa", "ac"), lines_for("ad", "ae", "af"),
            lines_for("ag", "ah", "ai", "aj")]


def test_a_prefix_that_never_settles_is_split_and_keeps_its_boards(conn):
    index = FakeIndex({f"{HOST}/a*": unstable_reads()})
    run(conn, index)
    assert statuses(conn)[f"{HOST}/a*"] == "unconfirmed"
    assert statuses(conn)[f"{HOST}/ab*"] == "confirmed"
    assert f"{HOST}/ba*" not in statuses(conn)
    tokens = {r["token"] for r in conn.execute("SELECT token FROM boards")}
    assert {"aa", "ac", "ad", "aj"} <= tokens


def test_a_stopped_run_resumes_inside_a_split_prefix(conn):
    run(conn, FakeIndex({f"{HOST}/a*": unstable_reads()}), limit=ALL_PREFIXES + 3)
    finished = len(statuses(conn))
    assert finished == ALL_PREFIXES + 3

    later = FakeIndex()
    run(conn, later)
    assert f"{HOST}/a*" not in later.asked
    assert len(statuses(conn)) == 2 * ALL_PREFIXES


def test_splitting_stops_at_max_depth(conn):
    index = FakeIndex({f"{HOST}/a*": unstable_reads(), f"{HOST}/ab*": unstable_reads()})
    run(conn, index, max_depth=2)
    assert statuses(conn)[f"{HOST}/ab*"] == "unconfirmed"
    assert f"{HOST}/aba*" not in statuses(conn)


def test_crawls_are_tracked_separately(conn):
    run(conn, FakeIndex(), crawl="CC-MAIN-2026-39")
    index = FakeIndex()
    run(conn, index, crawl=CRAWL)
    assert len(index.asked) == 2 * ALL_PREFIXES
