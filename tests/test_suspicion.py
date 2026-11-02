from pathlib import Path

import pytest

from job_matcher import db, pipeline
from job_matcher.filters import evaluate
from job_matcher.profile import load_profile
from job_matcher.suspicion import mass_template_tags, scam_phrase_tag
from job_matcher.tagging import tag_job

from .conftest import make_job

PROFILE = load_profile(Path(__file__).parents[1] / "config" / "profile.example.yaml")
TEMPLATE = ("– Remote Example Staffing is a technology consulting and software development "
            "company delivering cloud, AI, data, and enterprise solutions across the US.")
BOILERPLATE = ("Company Overview Example Defense offers a wide range of services in the military "
               "modeling, simulation, and training industry from research efforts onward.")
TITLES = ["SAP Backend Developer", "Rust Developer", "React Developer",
          "Blockchain Developer", "ETL Developer"]


def mail_merged(n: int, company: str = "Example Staffing"):
    return [(i, make_job(str(i), title=t, company=company, description=f"{t} {TEMPLATE}"))
            for i, t in enumerate(TITLES[:n])]


def test_mail_merged_postings_are_marked():
    tags = mass_template_tags(mail_merged(5), min_titles=5)
    assert sorted(tags) == [0, 1, 2, 3, 4]
    assert all(t.value == "mass_template" for t in tags.values())
    assert "5 different titles" in tags[0].evidence


def test_template_variants_from_a_flagged_company_are_marked_too():
    variant = (99, make_job("99", title="Boomi Middleware Developer", company="Example Staffing",
                            description=TEMPLATE.removeprefix("– Remote ")))
    unrelated = (98, make_job("98", title="Lab Supervisor", company="Example Staffing",
                              description="A forward-thinking company hiring a supervisor."))
    tags = mass_template_tags(mail_merged(5) + [variant, unrelated], min_titles=5)
    assert 99 in tags
    assert 98 not in tags


def test_fewer_titles_than_the_threshold_are_not_marked():
    assert mass_template_tags(mail_merged(4), min_titles=5) == {}


def test_company_boilerplate_is_not_a_template():
    # Big employers open every posting with the same "about us" paragraph; that's normal.
    jobs = [(i, make_job(str(i), title=t, company="Example Defense", description=BOILERPLATE))
            for i, t in enumerate(TITLES)]
    assert mass_template_tags(jobs, min_titles=5) == {}


def test_same_template_from_different_companies_is_not_pooled():
    jobs = mail_merged(3, "Company A") + [
        (10 + i, job) for i, (_, job) in enumerate(mail_merged(3, "Company B"))
    ]
    assert mass_template_tags(jobs, min_titles=5) == {}


@pytest.mark.parametrize(
    "text",
    [
        "Training fee of $99 is refunded after your first week.",
        "Interviews are conducted over Telegram.",
        "You will receive a check deposit to buy equipment with gift cards.",
        "Des frais de formation sont exigés.",
    ],
)
def test_scam_phrases(text):
    tag = scam_phrase_tag(text)
    assert tag is not None and tag.value == "scam_phrase"


def test_ordinary_posting_has_no_scam_phrase():
    assert scam_phrase_tag("Build REST APIs in Python. Health and dental benefits.") is None


def test_suspicious_jobs_are_noted_by_default_and_rejected_on_request():
    job = make_job(metro="orlando", title="Junior Developer",
                   description="Interviews are conducted over WhatsApp.")
    tags = tag_job(job, PROFILE) + [scam_phrase_tag(job.description)]
    result = evaluate(job, tags, PROFILE)
    assert result.passed
    assert "suspicious" in [n.code for n in result.notes]

    strict = PROFILE.model_copy(deep=True)
    strict.suspicious.mode = "exclude"
    assert [r.code for r in evaluate(job, tags, strict).reasons] == ["suspicious"]


def test_screen_marks_templates_across_stored_jobs(conn):
    for _, job in mail_merged(5):
        db.upsert_job(conn, pipeline.enrich(job))
    conn.commit()
    pipeline.screen(conn, PROFILE)
    rows = db.screened_jobs(conn, passed=True)
    assert len(rows) == 5
    assert {r["suspicious"] for r in rows} == {"mass_template"}
