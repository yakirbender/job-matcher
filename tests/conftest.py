from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from job_matcher import db
from job_matcher.models import Job, SearchSpec

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def adzuna_page() -> dict[str, Any]:
    return json.loads((FIXTURES / "adzuna_search_page.json").read_text(encoding="utf-8"))


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def make_spec(**overrides: Any) -> SearchSpec:
    values: dict[str, Any] = {
        "region": "florida",
        "country": "us",
        "term": "software developer",
        "term_group": "core_en",
        "where": "Florida",
    }
    return SearchSpec(**(values | overrides))


def make_job(source_id: str = "1", **overrides: Any) -> Job:
    values: dict[str, Any] = {
        "source": "fake",
        "source_id": source_id,
        "title": "Software Developer",
        "url": f"https://example.test/{source_id}",
        "country": "us",
        "company": "Acme",
        "location_area": ["US", "Florida", "Orange County", "Orlando"],
        "description": "Build things.",
    }
    return Job(**(values | overrides))
