"""The interface every job source implements, plus a name -> factory registry."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from job_matcher.models import Job, SearchSpec

if TYPE_CHECKING:
    from job_matcher.config import SearchConfig, Settings


class SourceError(Exception):
    """A search failed; the pipeline logs it and moves on to the next search."""


class SourceAuthError(SourceError):
    """Credentials are missing or rejected; every later search would fail too."""


class SourceQuotaError(SourceError):
    """The request budget is used up; stop fetching for this run."""


@runtime_checkable
class JobSource(Protocol):
    name: str
    requests_made: int
    max_requests: int | None  # set by the pipeline from the daily budget; None = unlimited

    def search(self, spec: SearchSpec) -> Iterator[list[Job]]:
        """Yield one list of normalized jobs per result page, newest first.

        Pages are fetched lazily, so a caller that stops iterating stops making requests.
        """
        ...


SourceFactory = Callable[["Settings", "SearchConfig"], JobSource]
_REGISTRY: dict[str, SourceFactory] = {}


def register(name: str) -> Callable[[SourceFactory], SourceFactory]:
    def decorator(factory: SourceFactory) -> SourceFactory:
        _REGISTRY[name] = factory
        return factory

    return decorator


def build_source(name: str, settings: Settings, config: SearchConfig) -> JobSource:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise SourceError(f"unknown source {name!r}; available: {sorted(_REGISTRY)}") from None
    return factory(settings, config)
