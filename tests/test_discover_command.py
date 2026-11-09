"""The discover command: choosing crawls, running several, and the known-board check."""

import httpx
import pytest
from typer.testing import CliRunner

from job_matcher import cli, db, pipeline
from job_matcher.discovery import (
    BoardRef,
    CdxClient,
    CdxError,
    Crawl,
    QueryResult,
    latest_crawls,
    prefix_patterns,
)
from tests.test_discovery_storage import FakeIndex, lines_for

HOST = "jobs.ashbyhq.com"
ALL_PREFIXES = len(prefix_patterns(HOST))
NEWER = Crawl("CC-MAIN-2026-43", "https://index.example/43-index")
OLDER = Crawl("CC-MAIN-2026-39", "https://index.example/39-index")

COLLINFO = [
    {"id": "CC-MAIN-2026-43", "cdx-api": "https://x/43-index"},
    {"id": "CC-MAIN-2026-39", "cdx-api": "https://x/39-index"},
    {"id": "CC-MAIN-2026-34", "cdx-api": "https://x/34-index"},
]


# --- choosing crawls --------------------------------------------------------------------


def test_latest_crawls_keeps_the_id_with_the_address():
    assert latest_crawls(COLLINFO, 2) == [
        Crawl("CC-MAIN-2026-43", "https://x/43-index"),
        Crawl("CC-MAIN-2026-39", "https://x/39-index"),
    ]
    assert len(latest_crawls(COLLINFO, 10)) == 3


def test_crawls_reads_the_crawl_list():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=COLLINFO)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = CdxClient(http, sleep=lambda seconds: None)
    assert [c.id for c in client.crawls(2)] == ["CC-MAIN-2026-43", "CC-MAIN-2026-39"]
    assert seen == ["https://index.commoncrawl.org/collinfo.json"]


@pytest.mark.parametrize("body", ["not json", "{}", "[{\"id\": \"x\"}]"])
def test_an_unreadable_crawl_list_is_a_cdx_error(body):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)))
    with pytest.raises(CdxError, match="unreadable crawl list"):
        CdxClient(http, sleep=lambda seconds: None).crawls(3)


# --- several crawls ---------------------------------------------------------------------


def discover(conn, index, crawls=(NEWER, OLDER), **options):
    options.setdefault("sleep", lambda seconds: None)
    return pipeline.discover(conn, index, list(crawls), [HOST], **options)


def test_every_crawl_is_read_in_the_order_given(conn):
    index = FakeIndex()
    results = discover(conn, index)
    assert [crawl for crawl, _ in results] == [NEWER, OLDER]
    assert all(report.finished == ALL_PREFIXES for _, report in results)
    assert len(index.asked) == 2 * 2 * ALL_PREFIXES  # every pattern, twice, per crawl


def test_the_query_limit_covers_the_whole_run(conn):
    results = discover(conn, FakeIndex(), limit=ALL_PREFIXES + 3)
    assert [report.finished for _, report in results] == [ALL_PREFIXES, 3]
    assert results[-1][1].stopped_reason == "reached the query limit"


def test_failures_in_a_row_stop_the_older_crawls_too(conn):
    patterns = prefix_patterns(HOST)
    index = FakeIndex(failures={p: 99 for p in patterns})
    results = discover(conn, index, stop_after_failures=5)
    assert [crawl for crawl, _ in results] == [NEWER]
    assert results[0][1].stopped_reason == "5 failed queries in a row"


def test_each_query_is_reported_as_it_finishes(conn):
    seen = []
    index = FakeIndex({f"{HOST}/a*": [lines_for("acme", "apex")]},
                      failures={f"{HOST}/b*": 1})
    discover(conn, index, crawls=[NEWER], on_query=lambda *args: seen.append(args))
    assert (NEWER.id, f"{HOST}/a*", "confirmed", "2 boards") in seen
    assert (NEWER.id, f"{HOST}/b*", "failed", "gave up after 3 attempts: HTTP 504") in seen
    assert (NEWER.id, f"{HOST}/b*", "confirmed", "0 boards") in seen  # the retry


# --- known boards -----------------------------------------------------------------------

ONEBRIEF = BoardRef("ashby", "onebrief")


def confirm(conn, pattern, *tokens, crawl=NEWER.id, confirmed=True):
    boards = {BoardRef("ashby", token): 1 for token in tokens}
    db.save_query_result(conn, crawl, QueryResult(pattern, boards, confirmed, [1, 1]))


def test_a_board_that_was_saved_is_found(conn):
    confirm(conn, f"{HOST}/o*", "onebrief")
    assert pipeline.check_known_boards(conn, [ONEBRIEF]) == {ONEBRIEF: "found"}


def test_a_board_is_not_checked_until_a_query_covering_it_is_confirmed(conn):
    confirm(conn, f"{HOST}/a*", "acme")
    confirm(conn, f"{HOST}/o*", "other", confirmed=False)  # its reads never agreed
    assert pipeline.check_known_boards(conn, [ONEBRIEF]) == {ONEBRIEF: "not checked yet"}


def test_a_board_is_missing_when_a_confirmed_query_should_have_found_it(conn):
    confirm(conn, f"{HOST}/o*", "other")
    assert pipeline.check_known_boards(conn, [ONEBRIEF]) == {ONEBRIEF: "missing"}


def test_a_smaller_prefix_covers_the_board_too(conn):
    confirm(conn, f"{HOST}/on*", "onward")
    assert pipeline.check_known_boards(conn, [ONEBRIEF]) == {ONEBRIEF: "missing"}


def test_a_confirmed_query_in_an_older_crawl_counts(conn):
    confirm(conn, f"{HOST}/o*", "onebrief", crawl=OLDER.id)
    assert pipeline.check_known_boards(conn, [ONEBRIEF]) == {ONEBRIEF: "found"}


def test_a_greenhouse_board_is_missing_only_once_both_its_hosts_are_confirmed(conn):
    anthropic = BoardRef("greenhouse", "anthropic")
    db.save_query_result(conn, NEWER.id, QueryResult("boards.greenhouse.io/a*", {}, True, [0, 0]))
    assert pipeline.check_known_boards(conn, [anthropic]) == {anthropic: "not checked yet"}
    db.save_query_result(conn, NEWER.id,
                         QueryResult("job-boards.greenhouse.io/a*", {}, True, [0, 0]))
    assert pipeline.check_known_boards(conn, [anthropic]) == {anthropic: "missing"}


# --- the command ------------------------------------------------------------------------


class FakeCommandIndex(FakeIndex):
    """FakeIndex plus the crawl list, for the command."""

    def crawls(self, count):
        return [NEWER, OLDER][:count]


@pytest.fixture
def run_command(tmp_path, monkeypatch):
    monkeypatch.setenv("JM_DB_PATH", str(tmp_path / "jobs.db"))

    def invoke(index, *args):
        monkeypatch.setattr(cli, "_index_client", lambda: index)
        return CliRunner().invoke(cli.app, ["discover", *args])

    return invoke, tmp_path / "jobs.db"


def test_the_command_reads_the_crawls_asked_for_and_saves_boards(run_command):
    invoke, path = run_command
    index = FakeCommandIndex({f"{HOST}/o*": [lines_for("onebrief")]})
    result = invoke(index, "--crawls", "1", "--ats", "ashby", "--retry-pause", "0")
    assert result.exit_code == 0, result.output
    assert "CC-MAIN-2026-43" in result.output
    assert "CC-MAIN-2026-39" not in result.output
    assert "ashby:onebrief  found" in result.output
    assert "greenhouse:anthropic  not checked yet" in result.output
    assert all(p.startswith(HOST) for p in index.asked)

    conn = db.connect(path)
    try:
        assert db.board_count(conn) == 1
    finally:
        conn.close()


def test_the_command_fails_when_a_known_board_is_missing(run_command):
    invoke, _ = run_command
    result = invoke(FakeCommandIndex(), "--crawls", "1", "--ats", "ashby")
    assert result.exit_code == 1
    assert "ashby:onebrief  missing" in result.output


def test_the_command_rejects_an_unknown_ats(run_command):
    invoke, _ = run_command
    result = invoke(FakeCommandIndex(), "--ats", "lever")
    assert result.exit_code == 2
    assert "lever" in result.output


def test_the_command_reports_an_unreachable_index(run_command):
    invoke, _ = run_command

    class Down(FakeCommandIndex):
        def crawls(self, count):
            raise CdxError("gave up after 4 attempts: HTTP 504")

    result = invoke(Down(), "--crawls", "1")
    assert result.exit_code == 1
    assert "HTTP 504" in result.output


def test_the_command_says_progress_is_kept_when_interrupted(run_command):
    invoke, _ = run_command

    class Interrupted(FakeCommandIndex):
        def pages(self, cdx_api, url_pattern):
            raise KeyboardInterrupt

    result = invoke(Interrupted(), "--crawls", "1", "--ats", "ashby")
    assert result.exit_code == 130
    assert "run the command again to resume" in result.output.lower()
