from pathlib import Path

import pytest
from pydantic import ValidationError

from job_matcher.config import SearchConfig, Settings, load_search_config

REPO_CONFIG = Path(__file__).parents[1] / "config" / "searches.example.yaml"


def test_repo_config_loads_and_expands():
    cfg = load_search_config(REPO_CONFIG)
    specs = cfg.specs()
    assert {s.region for s in specs} == {"montreal", "florida", "orlando", "remote_ca", "remote_us"}
    assert any(s.term == "développeur junior" and s.region == "montreal" for s in specs)
    remote = [s for s in specs if s.region == "remote_ca"]
    assert all(s.remote and s.where is None and s.remote_keywords for s in remote)
    # Keep a default run comfortably inside the daily budget.
    assert sum(s.max_pages for s in specs) <= cfg.limits.per_day


def test_region_selection_and_page_override():
    cfg = load_search_config(REPO_CONFIG)
    specs = cfg.specs(regions=["orlando"], max_pages=3)
    assert {s.region for s in specs} == {"orlando"}
    assert all(s.max_pages == 3 for s in specs)
    with pytest.raises(ValueError, match="unknown regions"):
        cfg.specs(regions=["tokyo"])


def test_term_selection():
    cfg = load_search_config(REPO_CONFIG)
    specs = cfg.specs(regions=["remote_ca", "remote_us"], terms=["développeur junior"])
    assert [(s.region, s.term) for s in specs] == [("remote_ca", "développeur junior")]
    with pytest.raises(ValueError, match="no selected region searches"):
        cfg.specs(regions=["florida"], terms=["développeur junior"])


def _config(**region):
    return {"terms": {"core": ["dev"]}, "regions": {"r": {"term_groups": ["core"], **region}}}


@pytest.mark.parametrize(
    "region",
    [
        {"country": "us"},  # neither where nor remote
        {"country": "us", "where": "X", "remote": True},
        {"country": "fr", "where": "Paris"},
        {"country": "us", "where": "X", "term_groups": ["missing"]},
    ],
)
def test_invalid_configs_are_rejected(region):
    with pytest.raises(ValidationError):
        SearchConfig.model_validate(_config(**region))


def test_settings_read_env_and_hide_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("ADZUNA_APP_ID", "id-1")
    monkeypatch.setenv("ADZUNA_APP_KEY", "secret-key")
    monkeypatch.setenv("JM_DB_PATH", str(tmp_path / "x.db"))
    settings = Settings.from_env(env_file=None)
    assert settings.adzuna_app_key == "secret-key"
    assert settings.db_path == tmp_path / "x.db"
    assert "secret-key" not in repr(settings)
