"""In-memory source for tests and offline runs: canned pages keyed by (region, term)."""

from __future__ import annotations

from collections.abc import Iterator

from job_matcher.models import Job, SearchSpec
from job_matcher.sources.base import SourceQuotaError


class FakeSource:
    name = "fake"

    def __init__(
        self,
        pages: dict[tuple[str, str], list[list[Job]]] | None = None,
        errors: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self.pages = pages or {}
        self.errors = errors or {}
        self.requests_made = 0
        self.max_requests: int | None = None

    def search(self, spec: SearchSpec) -> Iterator[list[Job]]:
        key = (spec.region, spec.term)
        for page in self.pages.get(key, [[]])[: spec.max_pages]:
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise SourceQuotaError("request budget used up")
            self.requests_made += 1
            if key in self.errors:
                raise self.errors[key]
            yield page
