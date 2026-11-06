"""Reading reliably from an index that can silently cut responses short."""

import json

from job_matcher.discovery import (
    PREFIX_CHARS,
    BoardRef,
    discover_host,
    prefix_patterns,
    read_until_stable,
)

API = "https://index.example/x-index"
HOST = "jobs.ashbyhq.com"


def lines_for(*tokens: str) -> list[str]:
    return [json.dumps({"url": f"https://{HOST}/{token}/job"}) for token in tokens]


class FakeIndex:
    """Stands in for CdxClient: each pattern answers from a queue of reads, in order.

    A pattern's last read repeats once the queue runs out; unknown patterns are empty.
    """

    def __init__(self, reads: dict[str, list[list[str]]]) -> None:
        self.reads = reads
        self.asked: list[str] = []

    def pages(self, cdx_api: str, url_pattern: str):
        self.asked.append(url_pattern)
        queue = self.reads.get(url_pattern, [[]])
        lines = queue.pop(0) if len(queue) > 1 else queue[0]
        half = len(lines) // 2
        yield lines[:half]  # two pages, to prove pages are combined
        yield lines[half:]


def test_prefix_patterns_cover_every_starting_character():
    patterns = prefix_patterns(HOST)
    assert len(patterns) == len(PREFIX_CHARS)
    assert f"{HOST}/a*" in patterns and f"{HOST}/0*" in patterns and f"{HOST}/-*" in patterns
    assert f"{HOST}/ab*" in prefix_patterns(HOST, "a")


def test_two_matching_reads_confirm_a_query():
    index = FakeIndex({"p": [lines_for("acme", "acme", "apex")]})
    result = read_until_stable(index, API, "p")
    assert result.confirmed
    assert result.line_counts == [3, 3]
    assert result.boards == {BoardRef("ashby", "acme"): 2, BoardRef("ashby", "apex"): 1}


def test_a_short_read_is_noticed_and_outvoted():
    # The real pattern seen on Greenhouse: 68, then 4703, 4703.
    full = lines_for("acme", "apex", "aura")
    index = FakeIndex({"p": [full[:1], full]})
    result = read_until_stable(index, API, "p")
    assert result.confirmed
    assert result.line_counts == [1, 3, 3]
    assert len(result.boards) == 3


def test_reads_that_never_agree_stay_unconfirmed_but_keep_every_board():
    # The real pattern seen on the newer Greenhouse host: 5823, 6795, 1251.
    index = FakeIndex({"p": [lines_for("a"), lines_for("a", "j"), lines_for("c", "d", "e"),
                             lines_for("f", "g", "h", "i")]})
    result = read_until_stable(index, API, "p", max_reads=4)
    assert not result.confirmed
    assert result.line_counts == [1, 2, 3, 4]
    assert {b.token for b in result.boards} == set("acdefghij")


def test_counts_are_not_doubled_across_reads():
    index = FakeIndex({"p": [lines_for("acme", "acme")]})
    assert read_until_stable(index, API, "p").boards == {BoardRef("ashby", "acme"): 2}


def test_an_empty_query_is_confirmed_with_no_boards():
    result = read_until_stable(FakeIndex({}), API, "p")
    assert result.confirmed
    assert result.boards == {}


def test_discover_host_reads_every_prefix_once_when_all_is_stable():
    index = FakeIndex({f"{HOST}/a*": [lines_for("acme")]})
    results = list(discover_host(index, API, HOST))
    assert [r.pattern for r in results] == prefix_patterns(HOST)
    assert all(r.confirmed for r in results)


def test_discover_host_splits_a_prefix_whose_reads_disagree():
    unstable = [lines_for("ab"), lines_for("ab", "ac"), lines_for("ad"),
                lines_for("ae", "af", "ag")]
    index = FakeIndex({f"{HOST}/a*": unstable, f"{HOST}/ab*": [lines_for("ab")]})
    results = {r.pattern: r for r in discover_host(index, API, HOST)}
    assert not results[f"{HOST}/a*"].confirmed
    assert results[f"{HOST}/ab*"].confirmed
    assert set(prefix_patterns(HOST, "a")) <= set(results)
    assert f"{HOST}/ba*" not in results  # stable prefixes aren't split


def test_discover_host_stops_splitting_at_max_depth():
    def unstable():
        return [lines_for("a"), lines_for("a", "j"), lines_for("c", "d", "e"),
                lines_for("f", "g", "h", "i")]

    index = FakeIndex({f"{HOST}/a*": unstable(), f"{HOST}/ab*": unstable()})
    results = {r.pattern: r for r in discover_host(index, API, HOST, max_depth=2)}
    assert not results[f"{HOST}/ab*"].confirmed
    assert f"{HOST}/aba*" not in results


def test_discover_host_is_lazy():
    index = FakeIndex({})
    results = discover_host(index, API, HOST)
    next(results)
    assert len(index.asked) == 2  # one query, read twice; nothing else requested yet
