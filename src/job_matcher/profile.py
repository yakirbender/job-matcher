"""The job-search profile (config/profile.example.yaml) that taggers and filters read."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_PROFILE_PATH = Path("config/profile.example.yaml")


class Seniority(BaseModel):
    exclude_title: list[str] = []
    entry_level_title: list[str] = []
    entry_level_description: list[str] = []
    stretch_title: list[str] = []
    max_years_required: int | None = Field(2, ge=0)


class Exclusions(BaseModel):
    title: list[str] = []
    text: list[str] = []
    companies: list[str] = []


class Locations(BaseModel):
    metros: list[str] = []


class Salary(BaseModel):
    min: dict[str, float] | None = None


class Clearance(BaseModel):
    mode: Literal["tag", "exclude_active"] = "tag"


class Suspicious(BaseModel):
    mode: Literal["tag", "exclude"] = "tag"
    min_template_titles: int = Field(5, ge=2)


class ClearancePoints(BaseModel):
    obtainable: int = 0
    required: int = -5
    active: int = -15
    polygraph: int = -5


class Ranking(BaseModel):
    french_required: int = 8
    french_asset: int = 4
    metro: dict[str, int] = {}
    clearance: ClearancePoints = ClearancePoints()
    suspicious: int = -15
    relevant_experience: dict[str, list[str]] = {}


class Profile(BaseModel):
    role_families: dict[str, list[str]] = Field(min_length=1)
    seniority: Seniority = Seniority()
    exclude: Exclusions = Exclusions()
    locations: Locations = Locations()
    salary: Salary = Salary()
    clearance: Clearance = Clearance()
    suspicious: Suspicious = Suspicious()
    ranking: Ranking = Ranking()

    def fingerprint(self) -> str:
        """Changes whenever any setting changes; stored with each filter result."""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:16]


def load_profile(path: Path = DEFAULT_PROFILE_PATH) -> Profile:
    with path.open(encoding="utf-8") as f:
        return Profile.model_validate(yaml.safe_load(f))
