"""Discover company job boards from the Common Crawl URL index.

The idea: Common Crawl archives web addresses like
`https://boards.greenhouse.io/acme/jobs/123`. The first path segment ("acme") is the
company's board token. Collect those tokens, and the tool can ask each company's public
board API for its jobs, without anyone keeping a list of companies by hand.

Only Greenhouse and Ashby are supported here: Lever's robots.txt blocks Common Crawl's
crawler (CCBot), so Lever addresses aren't in the index, and we respect that opt-out.

The module is in two parts:

  Part A (pure functions, no network): turn addresses into board references.
  Part B (network): page through the Common Crawl index politely. Specified below,
          tests come once Part A is reviewed.

Index API facts (checked against the docs on 2026-11-03):
  - Crawl list: https://index.commoncrawl.org/collinfo.json, newest first. Each entry has
    an "id" like "CC-MAIN-2026-43" and a "cdx-api" URL like
    "https://index.commoncrawl.org/CC-MAIN-2026-43-index".
  - Query: GET <cdx-api>?url=boards.greenhouse.io/*&output=json
    A trailing * means "every address starting with this". output=json returns one JSON
    object per line (NDJSON), each with a "url" key among others.
  - Paging: add showNumPages=true to get {"pages": N, ...}; then request page=0..N-1.
  - Rate limits: heavily limited. Sleep between calls, never run requests in parallel,
    and treat HTTP 503 as "slow down" (wait, then retry a few times at most).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

# --- Part A: addresses -> board references --------------------------------------------

# Host name -> which system it belongs to.
BOARD_HOSTS = {
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.ashbyhq.com": "ashby",
}

# First path segments that are pages of the site itself, not a company's board.
RESERVED_SEGMENTS = {"", "embed", "api", "b", "meeting", "robots.txt", "favicon.ico",
                     "sitemap.xml"}


@dataclass(frozen=True)
class BoardRef:
    """One company's job board: which system, and its token (the name in the address)."""

    ats: str  # "greenhouse" | "ashby"
    token: str  # lowercase, e.g. "acme"


def board_from_url(url: str) -> BoardRef | None:
    """The board an address belongs to, or None if it isn't a company board page.

    Rules:
      - The host must be one of BOARD_HOSTS, compared case-insensitively, ignoring a port
        ("BOARDS.GREENHOUSE.IO:443" counts).
      - The token is the first path segment, lowercased: ".../Acme/jobs/1" -> "acme".
      - A token must be non-empty and only contain letters, digits, "-" and "_".
      - First segments in RESERVED_SEGMENTS are not boards -> None.
      - Query strings and fragments ("?gh_jid=1", "#apply") are ignored.
      - Anything else (other hosts, unparsable text) -> None. Never raise.

    Hint: urllib.parse.urlsplit splits an address into scheme, host, path, query, ...
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None

    ats = BOARD_HOSTS.get(parts.hostname)
    if ats is None:
        return None

    segments = parts.path.split("/")
    if len(segments) < 2:
        return None
    token = segments[1].lower()

    if token in RESERVED_SEGMENTS:
        return None
    if not re.fullmatch(r"[a-z0-9_-]+", token):
        return None

    return BoardRef(ats, token)


def boards_from_cdx_lines(lines: Iterable[str]) -> dict[BoardRef, int]:
    """Count captures per board in Common Crawl index output (one JSON object per line).

    Each line looks like {"urlkey": "...", "timestamp": "...", "url": "https://...", ...}.
    Use board_from_url on the "url" value. The same board appears many times (one line per
    captured page), so return how many lines pointed at each board.

    Skip, without raising: blank lines, lines that aren't valid JSON, lines without a
    "url", and addresses that aren't boards.
    """
    counts = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(data, dict):
            continue
        url = data.get("url")
        if not isinstance(url, str):
            continue

        board = board_from_url(url)
        if board is None:
            continue
        counts[board] = counts.get(board, 0) + 1
    return counts


# --- Part B: paging through the index (specified now, tests after Part A review) -------
#
# class CdxClient:
#     def __init__(self, client: httpx.Client, *, sleep=time.sleep, delay: float = 2.0,
#                  max_retries: int = 3): ...
#
#     def num_pages(self, cdx_api: str, url_pattern: str) -> int:
#         """GET with showNumPages=true; return the "pages" number."""
#
#     def pages(self, cdx_api: str, url_pattern: str) -> Iterator[list[str]]:
#         """Yield each page's lines, page=0..N-1, sleeping `delay` seconds between
#         requests. On HTTP 503, sleep longer and retry, up to max_retries; then raise."""
#
# def latest_indexes(collinfo: list[dict], count: int) -> list[str]:
#     """The cdx-api URLs of the `count` newest crawls (collinfo is newest first)."""
