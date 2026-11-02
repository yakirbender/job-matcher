from pathlib import Path

import pytest

from job_matcher.filters import evaluate
from job_matcher.profile import Profile, load_profile
from job_matcher.tagging import tag_job

from .conftest import make_job

PROFILE = load_profile(Path(__file__).parents[1] / "config" / "profile.example.yaml")


def run(job, profile: Profile = PROFILE, duplicate_of=None):
    return evaluate(job, tag_job(job, profile), profile, duplicate_of=duplicate_of)


def codes(result):
    return [r.code for r in result.reasons]


def test_a_good_junior_job_passes_with_notes():
    job = make_job(title="Software Developer, Junior", metro="orlando")
    result = run(job)
    assert result.passed
    assert [n.code for n in result.notes] == ["experience_unknown"]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"title": "Senior Highway Engineer"}, "not_software"),
        ({"title": "Sr Software Engineer"}, "seniority"),
        ({"title": "Programmer 3 - .NET Angular"}, "seniority"),
        ({"title": "Développeur.euse principal.e"}, "seniority"),
        ({"title": "Software Engineering Intern"}, "internship"),
        ({"title": "Stagiaire en développement logiciel"}, "internship"),
        ({"description": "Commission only role."}, "excluded_keyword"),
        ({"description": "Requires 5+ years of experience."}, "experience"),
        ({"metro": None, "work_mode": "onsite", "location_display": "Dallas, TX"}, "location"),
        ({"metro": None, "work_mode": "hybrid"}, "location"),
    ],
)
def test_rejections(overrides, code):
    job = make_job(**({"metro": "orlando"} | overrides))
    assert code in codes(run(job))


def test_remote_outside_metros_passes_and_unknown_is_noted():
    remote = make_job(metro=None, work_mode="remote")
    assert run(remote).passed
    unknown = run(make_job(metro=None, work_mode=None))
    assert unknown.passed
    assert "remote_unconfirmed" in [n.code for n in unknown.notes]


def test_onsite_inside_a_target_metro_passes():
    assert run(make_job(metro="montreal", work_mode="onsite")).passed


def test_duplicates_are_rejected():
    assert codes(run(make_job(metro="orlando"), duplicate_of=7)) == ["duplicate"]


def test_company_blocklist():
    profile = PROFILE.model_copy(deep=True)
    profile.exclude.companies = ["Example Staffing"]
    job = make_job(company="Example Staffing Inc.", metro="orlando")
    assert codes(run(job, profile)) == ["company"]


def test_salary_floor_ignores_estimates():
    profile = PROFILE.model_copy(deep=True)
    profile.salary.min = {"USD": 60000}
    low = make_job(metro="orlando", salary_max=50000, salary_currency="USD")
    assert codes(run(low, profile)) == ["salary"]
    estimated = make_job(metro="orlando", salary_max=50000, salary_currency="USD",
                         salary_is_predicted=True)
    result = run(estimated, profile)
    assert result.passed
    assert "salary_estimated" in [n.code for n in result.notes]


def test_clearance_is_only_tagged_by_default():
    job = make_job(metro="orlando", description="Must have an active Secret clearance.")
    assert run(job).passed
    profile = PROFILE.model_copy(deep=True)
    profile.clearance.mode = "exclude_active"
    assert codes(run(job, profile)) == ["clearance"]
    obtainable = make_job(metro="orlando", description="Ability to obtain a Secret clearance.")
    assert run(obtainable, profile).passed
