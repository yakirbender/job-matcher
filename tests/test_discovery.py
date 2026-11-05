import json

import pytest

from job_matcher.discovery import BoardRef, board_from_url, boards_from_cdx_lines


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Greenhouse, classic and newer host.
        ("https://boards.greenhouse.io/acme/jobs/123?gh_jid=123", BoardRef("greenhouse", "acme")),
        ("https://job-boards.greenhouse.io/acme/jobs/456", BoardRef("greenhouse", "acme")),
        ("https://boards.greenhouse.io/acme", BoardRef("greenhouse", "acme")),
        ("https://boards.greenhouse.io/acme/", BoardRef("greenhouse", "acme")),
        # Case and ports don't matter; tokens come back lowercase.
        ("http://BOARDS.GREENHOUSE.IO:443/Acme/jobs/1", BoardRef("greenhouse", "acme")),
        # Tokens may contain digits, hyphens, underscores.
        ("https://boards.greenhouse.io/acme-2_labs/jobs/9", BoardRef("greenhouse", "acme-2_labs")),
        # Fragments are ignored.
        ("https://jobs.ashbyhq.com/onebrief/abc-123#apply", BoardRef("ashby", "onebrief")),
        ("https://jobs.ashbyhq.com/onebrief", BoardRef("ashby", "onebrief")),
    ],
)
def test_board_pages_give_a_board(url, expected):
    assert board_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/",                          # the site's home page
        "https://boards.greenhouse.io",                           # no path at all
        "https://boards.greenhouse.io/embed/job_board?for=acme",  # reserved segment
        "https://boards.greenhouse.io/robots.txt",
        "https://jobs.ashbyhq.com/api/non-user-graphql",
        "https://www.greenhouse.io/careers",                      # Greenhouse's own site
        "https://jobs.lever.co/acme",                             # Lever: not supported
        "https://example.com/acme/jobs/1",
        "https://boards.greenhouse.io/acme<script>/jobs",         # invalid token characters
        "not a url at all",
        "",
        "http://[bad",                                            # urlsplit itself raises on this
    ],
)
def test_everything_else_gives_none(url):
    assert board_from_url(url) is None


def cdx_line(url: str) -> str:
    return json.dumps({"urlkey": "io,greenhouse,boards)/x", "timestamp": "20261010000000",
                       "url": url, "status": "200"})


def test_counts_captures_per_board_and_skips_junk():
    lines = [
        cdx_line("https://boards.greenhouse.io/acme/jobs/1"),
        cdx_line("https://boards.greenhouse.io/acme/jobs/2"),
        cdx_line("https://BOARDS.greenhouse.io/Acme"),
        cdx_line("https://jobs.ashbyhq.com/onebrief/x"),
        cdx_line("https://boards.greenhouse.io/robots.txt"),  # not a board
        "",                                                   # blank line
        "   ",
        "{not json",                                          # broken line
        json.dumps({"timestamp": "20261010000000"}),          # no "url"
    ]
    assert boards_from_cdx_lines(lines) == {
        BoardRef("greenhouse", "acme"): 3,
        BoardRef("ashby", "onebrief"): 1,
    }


def test_no_lines_gives_no_boards():
    assert boards_from_cdx_lines([]) == {}
