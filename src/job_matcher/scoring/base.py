"""The interface every scorer implements, so a local LLM or API scorer can replace the
keyword scorer later without touching the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from job_matcher.models import Job
from job_matcher.tagging import Tag


@dataclass
class ScoreResult:
    score: int  # 0-100
    components: dict[str, int] = field(default_factory=dict)  # name -> points
    explanation: list[str] = field(default_factory=list)  # one line per factor
    matched: list[str] = field(default_factory=list)  # job skills you have
    missing: list[str] = field(default_factory=list)  # job skills not in your CV


class Scorer(Protocol):
    name: str
    version: str  # bump when scoring logic changes, so stored scores are traceable

    def score(self, job: Job, tags: list[Tag]) -> ScoreResult: ...
