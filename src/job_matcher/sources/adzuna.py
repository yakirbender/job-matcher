"""Adzuna job search API (https://developer.adzuna.com).

Adzuna only returns a ~500-character description snippet, and titles and snippets
can contain <strong> highlight tags, which are stripped here.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import httpx

from job_matcher.models import Job, SearchSpec
from job_matcher.sources.base import SourceAuthError, SourceError, SourceQuotaError, register

if TYPE_CHECKING:
    from job_matcher.config import SearchConfig, Settings

BASE_URL = "https://api.adzuna.com/v1/api/jobs"
CURRENCY = {"ca": "CAD", "us": "USD"}
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_TAG = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(html.unescape(_TAG.sub("", value)).split())


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class RateLimiter:
    """Spaces requests evenly to stay under a per-minute limit."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = 0.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next_allowed:
            self._sleep(self._next_allowed - now)
            now = self._next_allowed
        self._next_allowed = now + self.interval


class AdzunaSource:
    name = "adzuna"

    def __init__(
        self,
        app_id: str | None,
        app_key: str | None,
        *,
        per_minute: int = 20,
        max_attempts: int = 4,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not app_id or not app_key:
            raise SourceAuthError("ADZUNA_APP_ID and ADZUNA_APP_KEY must be set (see .env.example)")
        self._app_id = app_id
        self._app_key = app_key
        self._client = client or httpx.Client(
            timeout=30.0,
            headers={"Accept": "application/json", "User-Agent": "job-matcher/0.1"},
        )
        self._sleep = sleep
        self._limiter = RateLimiter(per_minute, clock=clock, sleep=sleep)
        self.max_attempts = max_attempts
        self.requests_made = 0
        self.max_requests: int | None = None

    def __repr__(self) -> str:  # never show credentials
        return f"AdzunaSource(requests_made={self.requests_made})"

    # --- public API ---------------------------------------------------------

    def search(self, spec: SearchSpec) -> Iterator[list[Job]]:
        for page in range(1, spec.max_pages + 1):
            data = self._get(spec.country, page, self.build_params(spec))
            results = data.get("results") or []
            yield [self.normalize(r, spec) for r in results if r.get("id") is not None]
            if len(results) < spec.results_per_page:
                return

    @staticmethod
    def build_params(spec: SearchSpec) -> dict[str, Any]:
        """Query parameters for a search, excluding credentials."""
        params: dict[str, Any] = {
            "what": spec.term,
            "results_per_page": spec.results_per_page,
            "sort_by": "date",
            "content-type": "application/json",
        }
        if spec.where:
            params["where"] = spec.where
        if spec.distance_km:
            params["distance"] = spec.distance_km
        if spec.max_days_old:
            params["max_days_old"] = spec.max_days_old
        if spec.remote and spec.remote_keywords:
            params["what_or"] = " ".join(spec.remote_keywords)
        return params

    def normalize(self, raw: dict[str, Any], spec: SearchSpec) -> Job:
        location = raw.get("location") or {}
        company = clean_text((raw.get("company") or {}).get("display_name"))
        return Job(
            source=self.name,
            source_id=str(raw["id"]),
            title=clean_text(raw.get("title")),
            url=raw.get("redirect_url") or "",
            country=spec.country,
            description=clean_text(raw.get("description")),
            company=company or None,
            location_display=clean_text(location.get("display_name")) or None,
            location_area=[str(a) for a in location.get("area") or []],
            latitude=_float(raw.get("latitude")),
            longitude=_float(raw.get("longitude")),
            salary_min=_float(raw.get("salary_min")),
            salary_max=_float(raw.get("salary_max")),
            salary_currency=CURRENCY.get(spec.country),
            salary_is_predicted=str(raw.get("salary_is_predicted", "0")) == "1",
            contract_type=raw.get("contract_type"),
            contract_time=raw.get("contract_time"),
            category=(raw.get("category") or {}).get("tag"),
            posted_at=raw.get("created"),
            raw=raw,
        )

    # --- HTTP ---------------------------------------------------------------

    def _get(self, country: str, page: int, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{BASE_URL}/{country}/search/{page}"
        for attempt in range(1, self.max_attempts + 1):
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise SourceQuotaError("request budget used up")
            self._limiter.wait()
            self.requests_made += 1
            try:
                resp = self._client.get(
                    url, params={**params, "app_id": self._app_id, "app_key": self._app_key}
                )
            except httpx.TransportError as exc:
                # The exception text can include the request URL, which holds the key.
                problem, delay = f"network error ({type(exc).__name__})", None
            else:
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        raise SourceError("Adzuna returned a response that isn't JSON") from None
                if resp.status_code in (401, 403):
                    raise SourceAuthError(
                        f"Adzuna rejected the credentials (HTTP {resp.status_code}); "
                        "check ADZUNA_APP_ID and ADZUNA_APP_KEY in .env"
                    )
                if resp.status_code not in _RETRY_STATUSES:
                    raise SourceError(
                        f"Adzuna returned HTTP {resp.status_code}: {self._body(resp)}"
                    )
                problem, delay = f"HTTP {resp.status_code}", _retry_after(resp)

            if attempt == self.max_attempts:
                raise SourceError(f"gave up after {attempt} attempts ({problem})")
            self._sleep(delay if delay is not None else min(2.0**attempt, 30.0))
        raise AssertionError("unreachable")

    def _body(self, resp: httpx.Response) -> str:
        text = resp.text[:200].replace(self._app_key, "***").replace(self._app_id, "***")
        return " ".join(text.split())


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return min(float(resp.headers["Retry-After"]), 120.0)
    except (KeyError, ValueError):
        return None


@register("adzuna")
def build_adzuna(settings: Settings, config: SearchConfig) -> AdzunaSource:
    return AdzunaSource(
        settings.adzuna_app_id, settings.adzuna_app_key, per_minute=config.limits.per_minute
    )
