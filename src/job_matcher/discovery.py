"""Discover company job boards from the Common Crawl URL index.

Common Crawl archives addresses like `https://boards.greenhouse.io/acme/jobs/123`, whose
first path segment ("acme") is the company's board token. Collecting tokens gives the
companies whose public board APIs can be read, with no hand-kept company list.

Covers Greenhouse and Ashby only: Lever's robots.txt blocks Common Crawl's crawler (CCBot),
so Lever addresses aren't in the index.

Index API (from the docs, 2026-11-03):
  - Crawl list: https://index.commoncrawl.org/collinfo.json, newest first. Each entry has
    an "id" like "CC-MAIN-2026-43" and a "cdx-api" URL like
    "https://index.commoncrawl.org/CC-MAIN-2026-43-index".
  - Query: GET <cdx-api>?url=boards.greenhouse.io/*&output=json
    A trailing * means "every address starting with this". output=json returns one JSON
    object per line (NDJSON), each with a "url" key among others.
  - Paging: add showNumPages=true to get {"pages": N, ...}; then request page=0..N-1.
  - Rate limits: heavily limited. Sleep between calls, never run requests in parallel,
    and treat HTTP 503 as "slow down" (wait, then retry a few times at most). The server
    also returns occasional 504s under load, which are worth retrying the same way.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

# --- Addresses to board references ------------------------------------------------------

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

    The host must be in BOARD_HOSTS (case and port are ignored). The token is the first
    path segment, lowercased, and may only contain letters, digits, "-" and "_". Reserved
    segments, other hosts, and malformed addresses give None; this never raises.
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

    Blank lines, invalid JSON, lines without a usable "url", and addresses that aren't
    boards are skipped rather than raising: one bad line shouldn't end a long run.
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


# --- Reading the index ------------------------------------------------------------------

# Statuses worth retrying. The real index returns 504s on requests that work a moment later.
TEMPORARY_STATUSES = frozenset({500, 502, 503, 504})


class CdxError(Exception):
    """The index couldn't be read: retries ran out, or its answer was unusable."""


def latest_indexes(collinfo: list[dict[str, Any]], count: int) -> list[str]:
    """The "cdx-api" URLs of the `count` newest crawls.

    `collinfo` is the parsed collinfo.json list, already newest first.
    """
    result = []
    for item in collinfo[:count]:
        result.append(item["cdx-api"])
    return result


class CdxClient:
    """Reads the Common Crawl index politely: one request at a time, with pauses.

    `client` and `sleep` are injected so tests can use a fake server and skip real waits.
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        delay: float = 2.0,
        backoff: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self.delay = delay  # seconds to pause before every request
        self.backoff = backoff  # extra seconds to pause after a temporary failure
        self.max_retries = max_retries  # retries after the first try

    def _get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        """One polite GET: every attempt pauses `delay` seconds first.

        A server error or dropped connection may be temporary: pause `backoff` seconds and
        retry, up to `max_retries` times, then raise CdxError. Any other non-200 status
        raises at once.
        """
        for attempt in range(self.max_retries + 1):
            self.sleep(self.delay)

            try:
                response = self.client.get(url, params=params)
            except httpx.TransportError as exc:
                # Name only the error type: its message can include the full request URL.
                problem = f"connection failed ({type(exc).__name__})"
            else:
                if response.status_code == 200:
                    return response
                if response.status_code not in TEMPORARY_STATUSES:
                    raise CdxError(f"index returned HTTP {response.status_code}")
                problem = f"HTTP {response.status_code}"

            if attempt < self.max_retries:
                self.sleep(self.backoff)

        raise CdxError(f"gave up after {self.max_retries + 1} attempts: {problem}")

    def num_pages(self, cdx_api: str, url_pattern: str) -> int:
        """How many pages of results a pattern has."""
        params = {
            "url": url_pattern,
            "output": "json",
            "showNumPages": "true",
        }
        response = self._get(cdx_api, params)
        try:
            return int(response.json()["pages"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CdxError(f"unreadable page count: {response.text[:100]!r}") from exc

    def pages(self, cdx_api: str, url_pattern: str) -> Iterator[list[str]]:
        """Yield each page's lines in turn, ready for boards_from_cdx_lines.

        A generator, so a caller that stops looping stops the requests too.
        """
        for page in range(self.num_pages(cdx_api, url_pattern)):
            params = {
                "url": url_pattern,
                "output": "json",
                "page": page,
            }
            response = self._get(cdx_api, params)
            yield response.text.splitlines()
