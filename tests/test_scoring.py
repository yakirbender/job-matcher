from pathlib import Path

from job_matcher import db, pipeline
from job_matcher.cv import load_vocabulary, parse_cv
from job_matcher.profile import load_profile
from job_matcher.scoring import KeywordScorer
from job_matcher.tagging import Tag, tag_job

from .conftest import make_job
from .test_cv import CV_TEXT

ROOT = Path(__file__).parents[1]
VOCAB = load_vocabulary(ROOT / "config" / "skills.yaml")
PROFILE = load_profile(ROOT / "config" / "profile.example.yaml")
CV = parse_cv(CV_TEXT, VOCAB)
SCORER = KeywordScorer(CV, PROFILE, VOCAB)


def score(job, extra_tags=()):
    return SCORER.score(job, tag_job(job, PROFILE) + list(extra_tags))


def test_full_skill_match_with_entry_title():
    job = make_job(title="Junior Java Developer", metro="orlando",
                   description="Spring Boot, SQL and REST APIs.")
    result = score(job)
    assert result.matched == ["java", "rest", "spring", "sql"]
    assert result.missing == []
    # 4 of 4: 30 + 30 * (2*1 - 1) * 4/6 = 50
    assert result.components == {"skills": 50, "level": 30, "location": 3}
    assert result.score == 83
    assert result.explanation[0].startswith("Skills (4 named): you have")
    assert "java (Skills, Projects)" in result.explanation[0]


def test_partial_match_lists_missing_skills():
    job = make_job(title="Software Developer", metro=None,
                   description="Python, Kubernetes and Terraform on AWS.")
    result = score(job)
    assert result.matched == ["python"]
    assert result.missing == ["aws", "kubernetes", "terraform"]
    # 1 of 4: 30 + 30 * (0.5 - 1) * 4/6 = 20
    assert result.components["skills"] == 20
    assert "not in your CV: aws, kubernetes, terraform." in result.explanation[0]


def test_one_named_skill_cannot_swing_the_score_far():
    # Real case that exposed the old formula: one named skill, present in the CV.
    hit = score(make_job(title="Embedded Software Engineer", metro=None,
                         description="Embedded C software for train control."))
    assert hit.matched == ["c"]
    assert hit.components["skills"] == 40  # 30 + 30 * 1 * 1/3, not the old 60
    miss = score(make_job(title="Software Developer", metro=None,
                          description="Terraform pipelines."))
    assert miss.components["skills"] == 20  # 30 - 30 * 1/3, not the old 0
    entry = score(make_job(title="Software Engineer I", metro=None, description="Join us."))
    assert entry.score > hit.score  # an entry-level title outranks one lucky keyword


def test_related_skills_earn_half_credit():
    # Real French Angular posting: the CV has React and Vue, not Angular.
    result = score(make_job(title="Développeur Angular", metro=None, description="Angular."))
    assert result.matched == [] and result.missing == []
    assert result.components["skills"] == 30  # half credit on 1 skill = neutral
    assert "related, half credit: angular (you have react, vue)" in result.explanation[0]


def test_no_named_skills_is_neutral_and_unknown_level_is_middling():
    result = score(make_job(title="Software Developer", metro=None, description="Join us."))
    assert result.components == {"skills": 30, "level": 15}
    assert result.score == 45


def test_stretch_title_scores_lower_than_entry_level():
    entry = score(make_job(title="Software Engineer I", metro=None, description="Join us."))
    stretch = score(make_job(title="Software Engineer II", metro=None, description="Join us."))
    assert entry.components["level"] == 30
    assert stretch.components["level"] == 10
    assert "1-3 years" in stretch.explanation[1]


def test_adjustments_french_clearance_suspicious():
    job = make_job(title="Développeur junior", metro="montreal",
                   description="Maîtrise du français requise. Must have an active Secret "
                               "clearance and pass a polygraph.")
    result = score(job, [Tag("suspicious", "mass_template", "same text")])
    assert result.components["french"] == 8
    assert result.components["location"] == 3
    assert result.components["clearance"] == -20  # active -15, polygraph -5
    assert result.components["suspicious"] == -15
    text = " ".join(result.explanation)
    assert "French required" in text and "polygraph" in text and "verify before applying" in text


def test_obtainable_clearance_costs_nothing():
    job = make_job(title="Junior Developer", description="Ability to obtain a Secret clearance.")
    result = score(job)
    assert result.components["clearance"] == 0
    assert "must be able to obtain one" in " ".join(result.explanation)


def test_related_experience_is_shown_but_not_scored():
    job = make_job(title="Technical Trainer", metro=None, description="Teach our tools.")
    result = score(job)
    assert any("Related experience" in line and "tutor" in line for line in result.explanation)
    assert set(result.components) == {"skills", "level"}


def test_scores_are_clamped():
    job = make_job(title="Software Engineer II", metro=None,
                   description="Kubernetes. Must have an active TS/SCI clearance; polygraph.")
    result = score(job, [Tag("suspicious", "mass_template", "x")])
    assert result.score == 0


def test_pipeline_scores_passed_jobs_and_orders_the_list(conn):
    good = db.upsert_job(conn, pipeline.enrich(make_job(
        "1", title="Junior Java Developer", description="Java and SQL.")))
    meh = db.upsert_job(conn, pipeline.enrich(make_job(
        "2", title="Junior Developer", description="Kubernetes and Terraform.")))
    db.upsert_job(conn, pipeline.enrich(make_job("3", title="Senior Java Developer")))
    conn.commit()
    pipeline.screen(conn, PROFILE)

    report = pipeline.score(conn, SCORER, cv_hash=CV.fingerprint, profile=PROFILE)
    assert report.scored == 2  # the senior job was rejected, so it isn't scored
    rows = db.screened_jobs(conn, passed=True)
    assert [r["id"] for r in rows] == [good.job_id, meh.job_id]
    assert rows[0]["score"] > rows[1]["score"]

    stored = db.job_score(conn, good.job_id)
    assert stored["scorer"] == "keyword" and stored["cv_hash"] == CV.fingerprint

    # Re-scoring replaces old rows instead of piling up.
    pipeline.score(conn, SCORER, cv_hash=CV.fingerprint, profile=PROFILE)
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 2

    # A profile change that rejects a scored job drops its score at filter time,
    # instead of leaving a stale one for `jm list --rejected` / `jm show`.
    strict = PROFILE.model_copy(deep=True)
    strict.seniority.exclude_title.append("junior")
    strict.seniority.entry_level_title.remove("junior")
    pipeline.screen(conn, strict)
    assert db.job_score(conn, good.job_id) is None
    assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


def test_stated_mid_level_scores_as_a_stretch():
    result = score(make_job(title="Software Engineer", metro=None,
                            description="Experience Level: Middle / Senior"))
    assert result.components["level"] == 10
    assert "posting says mid level" in result.explanation[1]


def test_level_wording_for_stated_years():
    result = score(make_job(title="Software Developer", metro=None,
                            description="0-2 years of experience."))
    assert result.components["level"] == 26
    assert "asks for 0+ years of experience" in result.explanation[1]
