from pathlib import Path

import pytest

from job_matcher.profile import load_profile
from job_matcher.tagging import (
    clearance_tags,
    employment_tag,
    experience_years,
    french_tags,
    posting_language,
    role_family,
    tag_job,
)
from job_matcher.text import fold_title

from .conftest import make_job

PROFILE = load_profile(Path(__file__).parents[1] / "config" / "profile.example.yaml")


# Real titles seen in fetched results.
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Développeur.euse principal.e, Développement d’applications IA",
         "developpeur principal, developpement d'applications ia"),
        ("Développeur · euse généraliste (full stack) senior",
         "developpeur generaliste (full stack) senior"),
        ("Concepteur(trice) logiciel sénior", "concepteur logiciel senior"),
        ("Développeur.se senior", "developpeur senior"),
        ("Ingénieur(e) logiciel", "ingenieur logiciel"),
        ("Développeur/euse back-end", "developpeur back-end"),
        # Things that must not be mangled.
        ("Développeur .NET", "developpeur .net"),
        ("Front-end Developer", "front-end developer"),
        ("Sr./Lead Engineer", "sr./lead engineer"),
    ],
)
def test_fold_title_removes_inclusive_endings(title, expected):
    assert fold_title(title) == expected


@pytest.mark.parametrize(
    ("title", "family"),
    [
        ("Software Developer, Junior", "core_dev"),
        ("Développeur.euse principal.e, Développement d’applications IA", "core_dev"),
        ("Concepteur(trice) logiciel sénior", "core_dev"),
        ("Systems Test Engineer with Security Clearance", "qa"),
        ("QA Analyst", "qa"),
        ("Testeur logiciel junior", "qa"),
        ("Technical Support Engineer", "support"),
        ("Analyste support technique", "support"),
        ("Implementation Specialist", "implementation"),
        ("Enterprise Instructional Designer / Technical Trainer", "training"),
        ("MLOps Engineer ID72409", "core_dev"),
        ("Site Reliability Engineer || Remote (Must be from CST)", "core_dev"),
        ("Senior Highway Engineer", None),
        ("ServiceNow Administrator", None),
        ("Inside Sales Consulant", None),
        ("Systems Engineer", None),
    ],
)
def test_role_family(title, family):
    tag = role_family(title, PROFILE.role_families)
    assert (tag.value if tag else None) == family


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("Salary Range: $100,000–$150,000 Annually Experience: 6 years Sp…", 6),
        ("A level 4 employee is typically senior and has 7 to 9 years of professional…", 7),
        ("3-5 years of experience with Java", 3),
        ("5+ years of Java experience and 2 years of AWS experience", 5),
        ("0-2 years of experience and a CS degree", 0),
        ("Minimum of three years experience required", 3),
        ("3 ans d'expérience en développement", 3),
        ("Au moins 5 ans d’expérience pertinente", 5),
        ("Expérience minimale de 2 ans", 2),
        ("3-5 years of experience What would…", 3),
        # No requirement stated.
        ("22 month contract, 1–2 days onsite per week, 401(k) match", None),
        ("Founded 25 years ago, we build simulation software.", None),
    ],
)
def test_experience_years(text, years):
    found = experience_years(text)
    assert (found[0] if found else None) == years


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Software Engineer II with Security Clearance", "unspecified:required"),
        ("Must have an active Secret clearance.", "secret:active"),
        ("Active TS/SCI clearance required.", "ts_sci:active"),
        ("Ability to obtain a Secret clearance.", "secret:obtainable"),
        ("Must be able to obtain and maintain a Top Secret clearance", "top_secret:obtainable"),
        ("Must have active Secret; ability to obtain TS/SCI.", "ts_sci:active"),
        ("Cote de fiabilité requise", "reliability:required"),
        ("Être en mesure d'obtenir une cote de sécurité de niveau Secret", "secret:obtainable"),
        ("Public Trust clearance", "public_trust:required"),
    ],
)
def test_clearance(text, value):
    tags = clearance_tags(text)
    assert [t.value for t in tags if t.name == "clearance"] == [value]


def test_no_clearance_without_clearance_context():
    assert clearance_tags("Protect trade secrets; secret sauce of our product") == []


def test_polygraph_and_citizenship():
    tags = {t.name: t.value for t in clearance_tags(
        "Active TS/SCI with Full Scope Poly. US citizenship required."
    )}
    assert tags == {"clearance": "ts_sci:active", "clearance_polygraph": "yes",
                    "citizenship_required": "us"}


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("Maîtrise du français requise.", "required"),
        ("Le français est obligatoire", "required"),
        ("Fluent in French and English", "required"),
        ("Bilingual (English/French) is required", "required"),
        ("Bilingue (français et anglais)", "required"),
        ("French is an asset", "asset"),
        ("Le bilinguisme est un atout", "asset"),
        ("Bilingual preferred", "asset"),
        ("Maîtrise du français; l'espagnol est un atout", "required"),
        ("Python, Django and PostgreSQL. Spanish is an asset.", None),
    ],
)
def test_french(text, value):
    tags = french_tags(text)
    assert next((t.value for t in tags if t.name == "french"), None) == value


def test_posting_written_in_french_counts_as_french_required():
    text = ("Le candidat doit être domicilié au Québec. Nous sommes à la recherche d'un "
            "développeur pour notre équipe et vous aurez l'occasion de travailler avec nous.")
    assert posting_language(text) == "fr"
    tags = {t.name: (t.value, t.evidence) for t in french_tags(text)}
    assert tags["french"] == ("required", "posting is written in French")
    assert posting_language("We are looking for a developer to join our team.") == "en"


@pytest.mark.parametrize(
    ("overrides", "value"),
    [
        # Real cases from stored jobs.
        ({"description": "Example Co is seeking Part-Time, Software Developer, Junior, to join"},
         "part_time"),
        ({"description": "Job Type Full-Time Career Level Mid Level", "contract_time": "part_time"},
         "full_time"),  # posting text beats Adzuna's (wrong) field
        ({"description": "22 month contract with potential for conversion to full time",
          "contract_time": None}, "unknown"),
        ({"description": "Position Type: Full-time, Direct W2"}, "full_time"),
        ({"description": "Poste à temps partiel, 20 h par semaine"}, "part_time"),
        ({"description": "Emploi permanent à temps plein"}, "full_time"),
        ({"description": "Full-time or part-time schedules available"}, "either"),
        ({"description": "Build APIs.", "contract_time": "full_time"}, "full_time"),
        ({"description": "Build APIs.", "contract_time": None}, "unknown"),
    ],
)
def test_employment(overrides, value):
    assert employment_tag(make_job(**overrides)).value == value


def test_tag_job_entry_level_beats_seniority_words():
    tags = {t.name: t.value for t in tag_job(make_job(title="Associate / Staff Analyst"), PROFILE)}
    assert tags["entry_level"] == "title"
    assert "seniority" not in tags

    senior = {t.name: t.evidence for t in tag_job(make_job(title="Sr Software Engineer"), PROFILE)}
    assert senior["seniority"] == "sr"


def test_tag_job_entry_level_from_description_and_salary_kinds():
    job = make_job(title="Software Engineer", description="Great for recent graduates.",
                   salary_min=70000, salary_is_predicted=True)
    tags = {t.name: t.value for t in tag_job(job, PROFILE)}
    assert tags["entry_level"] == "description"
    assert tags["salary"] == "estimated"
    assert tags["experience_years"] == "unknown"
    assert tags["work_mode"] == "unknown"
