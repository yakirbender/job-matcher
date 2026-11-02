"""Source-agnostic data types passed between pipeline stages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from job_matcher.text import fold_key


@dataclass(frozen=True)
class SearchSpec:
    """One query against one source: a single term in a single region."""

    region: str  # label from searches.example.yaml, e.g. "montreal"
    country: str  # "ca" | "us"
    term: str
    term_group: str
    where: str | None = None
    distance_km: int | None = None
    remote: bool = False
    remote_keywords: tuple[str, ...] = ()
    title_only: bool = False  # match the term against titles instead of the full posting
    max_days_old: int | None = None
    max_pages: int = 1
    results_per_page: int = 50


@dataclass
class Job:
    """A posting normalized from any source."""

    source: str
    source_id: str
    title: str
    url: str
    country: str
    description: str = ""
    company: str | None = None
    location_display: str | None = None
    location_area: list[str] = field(default_factory=list)  # country down to town
    latitude: float | None = None
    longitude: float | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_is_predicted: bool = False
    contract_type: str | None = None
    contract_time: str | None = None
    category: str | None = None
    posted_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Derived by the pipeline, the same way for every source.
    metro: str | None = None
    work_mode: str | None = None  # "remote" | "hybrid" | "onsite" | None (unknown)

    @property
    def city(self) -> str | None:
        if len(self.location_area) > 1:
            return self.location_area[-1]
        return self.location_display

    def content_hash(self) -> str:
        """Changes when the posting itself changes, so later stages know to redo their work."""
        payload = [
            self.title,
            self.company,
            self.description,
            self.location_display,
            self.location_area,
            self.salary_min,
            self.salary_max,
            self.contract_type,
            self.contract_time,
        ]
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def dedupe_key(self) -> str:
        """Soft identity across sources and reposts.

        Matching keys flag a likely duplicate; they never merge rows.
        """
        return "|".join(fold_key(part or "") for part in (self.title, self.company, self.city))
