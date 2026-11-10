"""Company job boards: Ashby and Greenhouse public APIs (free, no key).

Unlike Adzuna, a board isn't searched: it lists every job one company has, worldwide, with
the full description. The pipeline decides which jobs to keep, so a reader works in two
steps: list_jobs() gives every job, cheaply, and complete() fills in what's needed for a
job worth keeping. On Greenhouse that matters: a big board's descriptions run to 10 MB,
its plain list to under 500 KB.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from job_matcher.location import metro_for
from job_matcher.models import Job
from job_matcher.places import Place, parse_place, parse_places
from job_matcher.sources.base import SourceError

ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board"
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards"
TEMPORARY_STATUSES = frozenset({429, 500, 502, 503, 504})


class BoardError(SourceError):
    """A board couldn't be read: retries ran out, or its answer was unusable."""


class BoardGone(BoardError):
    """The board doesn't exist (any more): HTTP 404."""


class BoardClient:
    """Polite GETs to board APIs: one at a time, a pause before each, and retries."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        delay: float = 1.0,
        backoff: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self.client = client
        self.sleep = sleep
        self.delay = delay
        self.backoff = backoff
        self.max_retries = max_retries
        self.requests_made = 0

    def get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        for attempt in range(self.max_retries + 1):
            self.sleep(self.delay)
            self.requests_made += 1
            try:
                response = self.client.get(url, params=params)
            except httpx.TransportError as exc:
                problem = f"connection failed ({type(exc).__name__})"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        raise BoardError(f"not JSON: {response.text[:100]!r}") from None
                if response.status_code == 404:
                    raise BoardGone("board not found (HTTP 404)")
                if response.status_code not in TEMPORARY_STATUSES:
                    raise BoardError(f"HTTP {response.status_code}")
                problem = f"HTTP {response.status_code}"
            if attempt < self.max_retries:
                self.sleep(self.backoff)
        raise BoardError(f"gave up after {self.max_retries + 1} attempts: {problem}")


# --- shared helpers ---------------------------------------------------------------------

_BLOCK_END = re.compile(r"<\s*(?:br|/p|/div|/h\d|/li|/ul|/ol|/tr)\b[^>]*>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(value: str) -> str:
    """Plain text from HTML, one line per paragraph, heading or list item."""
    text = html.unescape(_TAG.sub("", _BLOCK_END.sub("\n", value)))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def utc_timestamp(value: str | None) -> str | None:
    """"2026-04-09T20:28:23-04:00" -> "2026-04-10T00:28:23Z", like Adzuna's dates."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def choose_place(places: list[Place]) -> Place:
    """The place that matters most for this search: a target metro, then remote in the US
    or Canada, then remote anywhere, then the first listed."""
    for place in places:
        if metro_for(place.country or "", place.area):
            return place
    for place in places:
        if place.remote and place.country in ("us", "ca"):
            return place
    for place in places:
        if place.remote and place.country is None:
            return place
    return places[0] if places else Place(None, (), False, "")


def stated_work_mode(job: Job) -> str | None:
    """The work mode the board itself states, or None to infer it from the text.

    Worked out from the stored record each time, so rescreening can't lose it.
    Ashby states it per job. Greenhouse only says "remote" in its location text.
    """
    if job.source == "ashby":
        mode = _ASHBY_WORK_MODES.get(job.raw.get("workplaceType") or "")
        return mode or ("remote" if job.raw.get("isRemote") else None)
    if job.source == "greenhouse":
        return "remote" if choose_place(parse_places(job.location_display or "")).remote else None
    return None


def _without(raw: dict[str, Any], *keys: str) -> dict[str, Any]:
    """The raw record minus its bulky description fields."""
    return {k: v for k, v in raw.items() if k not in keys}


# --- Ashby ------------------------------------------------------------------------------

_ASHBY_WORK_MODES = {"Remote": "remote", "Hybrid": "hybrid", "OnSite": "onsite"}
_ASHBY_EMPLOYMENT = {  # employmentType -> (contract_time, contract_type)
    "FullTime": ("full_time", None),
    "PartTime": ("part_time", None),
    "Contract": (None, "contract"),
    "Temporary": (None, "contract"),
}


def _ashby_place(entry: dict[str, Any], remote: bool) -> Place | None:
    """A place from Ashby's structured address, or from its location text."""
    address = (entry.get("address") or {}).get("postalAddress") or {}
    parts = [address.get(k) for k in ("addressLocality", "addressRegion", "addressCountry")]
    text = ", ".join(p for p in parts if p)
    label = entry.get("location") or text
    if text:
        place = parse_place(text)
    elif places := parse_places(label or ""):
        place = places[0]
    else:
        return None
    return Place(place.country, place.area, remote or place.remote, label)


def ashby_job(raw: dict[str, Any], token: str) -> Job:
    work_mode = stated_work_mode(Job("ashby", "", "", "", "", raw=raw))
    entries = [raw, *(raw.get("secondaryLocations") or [])]
    places = [p for e in entries if (p := _ashby_place(e, work_mode == "remote"))]
    place = choose_place(places)

    salary = next(
        (c for c in (raw.get("compensation") or {}).get("summaryComponents") or []
         if c.get("compensationType") == "Salary" and c.get("interval") == "1 YEAR"),
        {},
    )
    contract_time, contract_type = _ASHBY_EMPLOYMENT.get(raw.get("employmentType") or "",
                                                         (None, None))
    return Job(
        source="ashby",
        source_id=f"{token}:{raw['id']}",
        title=raw["title"].strip(),
        url=raw.get("jobUrl") or f"https://jobs.ashbyhq.com/{token}/{raw['id']}",
        country=place.country or "",
        company=token,  # Ashby's board API doesn't give the company's display name
        description=(raw.get("descriptionPlain") or html_to_text(raw.get("descriptionHtml") or "")
                     ).strip(),
        location_display=" | ".join(p.text for p in places) or None,
        location_area=list(place.area),
        salary_min=salary.get("minValue"),
        salary_max=salary.get("maxValue"),
        salary_currency=salary.get("currencyCode"),
        contract_time=contract_time,
        contract_type=contract_type,
        category=raw.get("department"),
        posted_at=utc_timestamp(raw.get("publishedAt")),
        raw=_without(raw, "descriptionHtml", "descriptionPlain"),
        work_mode=work_mode,
    )


class AshbyReader:
    ats = "ashby"

    def __init__(self, client: BoardClient) -> None:
        self.client = client

    def list_jobs(self, token: str) -> list[Job]:
        data = self.client.get_json(f"{ASHBY_API}/{token}", {"includeCompensation": "true"})
        return [ashby_job(raw, token) for raw in _records(data)
                if raw.get("isListed", True)]

    def complete(self, job: Job) -> Job:
        return job  # the listing already has the description


# --- Greenhouse -------------------------------------------------------------------------


def greenhouse_job(raw: dict[str, Any], token: str) -> Job:
    """A Greenhouse job, from the board listing (no description) or a job page (with one).

    The "Location Type" field isn't used: each company defines it, and on one board it
    called a remote job on-site and a London job remote.
    """
    text = (raw.get("location") or {}).get("name") or ""
    place = choose_place(parse_places(text))
    return Job(
        source="greenhouse",
        source_id=f"{token}:{raw['id']}",
        title=raw["title"].strip(),
        url=raw.get("absolute_url") or f"https://job-boards.greenhouse.io/{token}/jobs/{raw['id']}",
        country=place.country or "",
        company=raw.get("company_name") or token,
        description=html_to_text(html.unescape(raw.get("content") or "")),
        location_display=text or None,
        location_area=list(place.area),
        category=", ".join(d["name"] for d in raw.get("departments") or [] if d.get("name"))
        or None,
        posted_at=utc_timestamp(raw.get("first_published") or raw.get("updated_at")),
        raw=_without(raw, "content"),
        work_mode="remote" if place.remote else None,
    )


class GreenhouseReader:
    ats = "greenhouse"

    def __init__(self, client: BoardClient) -> None:
        self.client = client

    def list_jobs(self, token: str) -> list[Job]:
        data = self.client.get_json(f"{GREENHOUSE_API}/{token}/jobs")
        return [greenhouse_job(raw, token) for raw in _records(data)]

    def complete(self, job: Job) -> Job:
        token, job_id = job.source_id.split(":", 1)
        return greenhouse_job(self.client.get_json(f"{GREENHOUSE_API}/{token}/jobs/{job_id}"),
                              token)


def _records(data: Any) -> list[dict[str, Any]]:
    """The usable job records in a board's answer; skips any without an id or title."""
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise BoardError("answer has no job list")
    return [r for r in data["jobs"]
            if isinstance(r, dict) and r.get("id") is not None and isinstance(r.get("title"), str)]


READERS: dict[str, type[AshbyReader] | type[GreenhouseReader]] = {
    "ashby": AshbyReader,
    "greenhouse": GreenhouseReader,
}
