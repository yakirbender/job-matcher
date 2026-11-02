"""Search configuration (YAML) and runtime settings (environment / .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, model_validator

from job_matcher.models import SearchSpec

DEFAULT_CONFIG_PATH = Path("config/searches.example.yaml")
DEFAULT_DB_PATH = Path("data/jobs.db")


class Defaults(BaseModel):
    max_days_old: int | None = Field(14, ge=1)
    max_pages: int = Field(1, ge=1)
    results_per_page: int = Field(50, ge=1, le=50)
    title_only: bool = False


class Limits(BaseModel):
    per_minute: int = Field(20, ge=1)
    per_day: int = Field(200, ge=1)  # rolling 24 hours
    per_month: int | None = Field(None, ge=1)  # rolling 30 days


class RegionConfig(BaseModel):
    country: Literal["ca", "us"]
    term_groups: list[str] = Field(min_length=1)
    where: str | None = None
    distance_km: int | None = Field(None, ge=1)
    remote: bool = False
    max_pages: int | None = Field(None, ge=1)
    max_days_old: int | None = Field(None, ge=1)
    title_only: bool | None = None  # None = use defaults.title_only
    enabled: bool = True

    @model_validator(mode="after")
    def _check_location(self) -> RegionConfig:
        if self.remote and self.where:
            raise ValueError("a remote region searches the whole country; remove 'where'")
        if not self.remote and not self.where:
            raise ValueError("a non-remote region needs 'where'")
        return self


class SearchConfig(BaseModel):
    source: str = "adzuna"
    defaults: Defaults = Defaults()
    limits: Limits = Limits()
    terms: dict[str, list[str]]
    remote_keywords: list[str] = []
    regions: dict[str, RegionConfig]

    @model_validator(mode="after")
    def _check_term_groups(self) -> SearchConfig:
        for name, region in self.regions.items():
            missing = [g for g in region.term_groups if g not in self.terms]
            if missing:
                raise ValueError(f"region {name!r} uses undefined term groups: {missing}")
        return self

    def specs(
        self,
        regions: list[str] | None = None,
        max_pages: int | None = None,
        terms: list[str] | None = None,
        title_only: bool | None = None,
    ) -> list[SearchSpec]:
        """Expand regions x term groups into individual searches.

        With `regions` given, those regions run even if disabled in the file.
        With `terms` given, only searches for those exact terms are kept.
        `title_only` overrides the file's setting for every search.
        """
        if regions:
            unknown = sorted(set(regions) - set(self.regions))
            if unknown:
                raise ValueError(f"unknown regions {unknown}; known: {sorted(self.regions)}")
            selected = {name: self.regions[name] for name in regions}
        else:
            selected = {name: r for name, r in self.regions.items() if r.enabled}

        specs = []
        for name, region in selected.items():
            for group in region.term_groups:
                for term in self.terms[group]:
                    specs.append(
                        SearchSpec(
                            region=name,
                            country=region.country,
                            term=term,
                            term_group=group,
                            where=region.where,
                            distance_km=region.distance_km,
                            remote=region.remote,
                            remote_keywords=tuple(self.remote_keywords) if region.remote else (),
                            title_only=_first_set(
                                title_only, region.title_only, self.defaults.title_only
                            ),
                            max_days_old=region.max_days_old or self.defaults.max_days_old,
                            max_pages=max_pages or region.max_pages or self.defaults.max_pages,
                            results_per_page=self.defaults.results_per_page,
                        )
                    )
        if terms:
            specs = [s for s in specs if s.term in terms]
            if not specs:
                raise ValueError(f"no selected region searches for any of {terms}")
        return specs


def _first_set(*values: bool | None) -> bool:
    return next((v for v in values if v is not None), False)


def load_search_config(path: Path = DEFAULT_CONFIG_PATH) -> SearchConfig:
    with path.open(encoding="utf-8") as f:
        return SearchConfig.model_validate(yaml.safe_load(f))


@dataclass(frozen=True)
class Settings:
    adzuna_app_id: str | None = field(default=None, repr=False)
    adzuna_app_key: str | None = field(default=None, repr=False)
    db_path: Path = DEFAULT_DB_PATH

    @classmethod
    def from_env(cls, env_file: Path | None = Path(".env")) -> Settings:
        if env_file is not None:
            load_dotenv(env_file, override=False)
        return cls(
            adzuna_app_id=os.getenv("ADZUNA_APP_ID") or None,
            adzuna_app_key=os.getenv("ADZUNA_APP_KEY") or None,
            db_path=Path(os.getenv("JM_DB_PATH") or DEFAULT_DB_PATH),
        )
