"""Reading postings with a local model: quotes, the Ollama client, and the run loop.

Ollama is never called: answers come from httpx.MockTransport.
"""

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from job_matcher import cli, db, pipeline
from job_matcher.extraction import (
    QUOTE_FIELDS,
    UNCHECKED_FIELDS,
    Extraction,
    check_quotes,
    quote_found,
    user_message,
)
from job_matcher.ollama import ModelMissing, OllamaClient, OllamaUnavailable, TooLong
from job_matcher.profile import load_profile

from .conftest import make_job

PROFILE = load_profile(Path(__file__).parents[1] / "config" / "profile.example.yaml")
POSTING = (
    "Software Engineer (Controls)\nRequirements: Bachelor's degree in computer science or "
    "engineering, OR 2+ years of professional experience. Must be a U.S. citizen or lawful "
    "permanent resident. Python and C++ required; Rust is a plus."
)

ANSWER = {
    "years_required": 2, "years_with_degree": 0,
    "years_quote": "OR 2+ years of professional experience",
    "degree_substitutes_for_years": True,
    "degree_substitute_quote": ("Bachelor's degree in computer science or engineering, "
                                "OR 2+ years of professional experience"),
    "entry_level_band": False, "entry_level_quote": "",
    "level": "entry", "must_have_skills": ["Python", "C++"], "nice_to_have_skills": ["Rust"],
    "degree_required": "none",
    "work_authorization": "us_citizen_or_pr",
    "work_authorization_quote": "Must be a U.S. citizen or lawful permanent resident",
    "clearance": "none", "clearance_quote": "",
}


def answer(**changes):
    return Extraction.model_validate(ANSWER | changes)


# --- quotes -----------------------------------------------------------------------------


@pytest.mark.parametrize(("quote", "found"), [
    ("OR 2+ years of professional experience", True),
    ("or 2+ YEARS of professional experience.", True),  # case and punctuation ignored
    ('"Bachelor\'s degree in computer science" ... U.S. citizen', True),  # pieces
    ("an active Secret clearance", False),  # not in the posting
    ("experience", False),  # one word proves nothing
    ("", False),
])
def test_quote_found(quote, found):
    assert quote_found(quote, POSTING) is found


def test_claims_with_quotes_in_the_posting_are_kept():
    checked = check_quotes(answer(), POSTING)
    assert checked.unverified == []
    assert (checked.fields["years_required"], checked.fields["years_with_degree"]) == (2, 0)
    assert checked.fields["work_authorization"] == "us_citizen_or_pr"


def test_an_invented_claim_is_dropped():
    invented = answer(clearance="must_hold", clearance_quote="an active Secret clearance")
    checked = check_quotes(invented, POSTING)
    assert checked.unverified == ["clearance"]
    assert (checked.fields["clearance"], checked.fields["clearance_quote"]) == ("none", "")


def test_a_bad_years_quote_drops_both_year_fields():
    checked = check_quotes(answer(years_quote="3+ years required"), POSTING)
    assert checked.unverified == ["years_required"]
    assert (checked.fields["years_required"], checked.fields["years_with_degree"]) == (None,
                                                                                        None)


def test_fewer_years_with_a_degree_needs_a_provable_substitute():
    # Real trial error: "1-2 years+ professional experience" read as 0 years with a degree.
    posting = "Requirements: 1-2 years+ professional software engineering experience."
    no_substitute = answer(years_required=1, years_with_degree=0,
                           years_quote="1-2 years+ professional software engineering",
                           degree_substitutes_for_years=False, degree_substitute_quote="")
    assert check_quotes(no_substitute, posting).fields["years_with_degree"] == 1

    claimed = answer(years_required=1, years_with_degree=0,
                     years_quote="1-2 years+ professional software engineering",
                     degree_substitutes_for_years=True,
                     degree_substitute_quote="professional software engineering experience")
    checked = check_quotes(claimed, posting)  # the quote has no "or": not a substitute
    assert "degree_substitutes_for_years" in checked.unverified
    assert checked.fields["years_with_degree"] == 1


@pytest.mark.parametrize(("field", "value", "quote"), [
    # Real trial error: a work permit inferred from the location.
    ("work_authorization", "canada", "Software Engineer (Controls)"),
    ("clearance", "must_hold", "Python and C++ required"),
    ("years_required", 2, "Python and C++ required"),
])
def test_a_real_quote_about_something_else_is_dropped(field, value, quote):
    quote_field = {"work_authorization": "work_authorization_quote",
                   "clearance": "clearance_quote", "years_required": "years_quote"}[field]
    checked = check_quotes(answer(**{field: value, quote_field: quote}), POSTING)
    assert field in checked.unverified


def test_saying_nothing_is_stated_needs_no_quote():
    quiet = answer(years_required=None, years_with_degree=None, years_quote="",
                   work_authorization="none_stated", work_authorization_quote="")
    assert check_quotes(quiet, POSTING).unverified == []


def test_the_model_sees_title_company_location_and_description():
    job = make_job(title="Data Engineer", company="Acme", location_display="Remote in US",
                   description="Build pipelines.")
    assert user_message(job) == (
        "Title: Data Engineer\nCompany: Acme\nLocation: Remote in US\n\nBuild pipelines.")


# --- the Ollama client ------------------------------------------------------------------


def ollama_reply(content, prompt_tokens=500):
    return httpx.Response(200, json={"message": {"content": content},
                                     "prompt_eval_count": prompt_tokens, "eval_count": 120})


def client_for(handler, context_tokens=8192):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return OllamaClient(http, "http://ollama.test", context_tokens=context_tokens)


def test_the_request_asks_for_json_without_thinking():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return ollama_reply(json.dumps(ANSWER))

    reply = client_for(handler).chat("m:1b", "system", "posting", {"type": "object"})
    body = seen[0]
    assert (body["model"], body["stream"], body["think"]) == ("m:1b", False, False)
    assert body["format"] == {"type": "object"}
    assert body["options"] == {"temperature": 0, "num_ctx": 8192}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert (reply.prompt_tokens, reply.output_tokens) == (500, 120)


def test_ollama_not_running():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaUnavailable, match="start the Ollama app"):
        client_for(handler).chat("m", "s", "u", {})


def test_a_model_that_is_not_downloaded():
    with pytest.raises(ModelMissing, match="ollama pull m:9b"):
        client_for(lambda r: httpx.Response(404, json={"error": "not found"})).chat(
            "m:9b", "s", "u", {})


def test_input_that_fills_the_context_is_too_long():
    with pytest.raises(TooLong):
        client_for(lambda r: ollama_reply("{}", prompt_tokens=8192)).chat("m", "s", "u", {})


# --- the run loop -----------------------------------------------------------------------


def passing_jobs(conn, *titles):
    ids = [db.upsert_job(conn, pipeline.enrich(make_job(str(n), title=t, description=POSTING)))
           .job_id for n, t in enumerate(titles, start=1)]
    conn.commit()
    pipeline.screen(conn, PROFILE)
    conn.commit()
    return ids


class FakeOllama:
    """Answers each request with the next item: a dict (JSON answer), a str (raw content),
    or an exception."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = 0

    def __call__(self, request):
        self.requests += 1
        item = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        return ollama_reply(item if isinstance(item, str) else json.dumps(item))


def test_extract_saves_checked_fields_and_resumes(conn):
    ids = passing_jobs(conn, "Junior Developer", "Junior Python Developer")
    fake = FakeOllama(ANSWER | {"clearance": "must_hold", "clearance_quote": "made up"})
    report = pipeline.extract(conn, client_for(fake), "m:1b")
    assert (report.jobs, report.saved, report.unverified) == (2, 2, 2)

    row = db.job_extraction(conn, ids[0], "m:1b")
    fields = json.loads(row["fields_json"])
    assert fields["years_with_degree"] == 0 and fields["clearance"] == "none"
    assert json.loads(row["unverified_json"]) == ["clearance"]

    again = pipeline.extract(conn, client_for(fake), "m:1b")
    assert again.jobs == 0 and fake.requests == 2  # nothing redone
    other_model = pipeline.extract(conn, client_for(fake), "m:9b")
    assert other_model.saved == 2  # each model keeps its own results


def test_a_new_prompt_version_redoes_jobs(conn, monkeypatch):
    passing_jobs(conn, "Junior Developer")
    fake = FakeOllama(ANSWER)
    pipeline.extract(conn, client_for(fake), "m")
    monkeypatch.setattr(pipeline, "PROMPT_VERSION", "next")
    assert pipeline.extract(conn, client_for(fake), "m").saved == 1


def test_a_malformed_answer_is_asked_again(conn):
    passing_jobs(conn, "Junior Developer")
    fake = FakeOllama("not json", ANSWER)
    report = pipeline.extract(conn, client_for(fake), "m")
    assert (report.saved, report.failed, fake.requests) == (1, [], 2)


def test_two_malformed_answers_fail_the_job_and_three_failures_stop(conn):
    passing_jobs(conn, "Junior Developer", "Junior Python Developer", "Junior Web Developer",
                 "Junior Java Developer")
    report = pipeline.extract(conn, client_for(FakeOllama("{}")), "m")
    assert report.saved == 0
    assert len(report.failed) == 3
    assert report.stopped_reason == "3 failed jobs in a row"


def test_a_posting_that_is_too_long_is_skipped(conn):
    ids = passing_jobs(conn, "Junior Developer", "Junior Python Developer")
    fake = FakeOllama(ollama_reply("{}", prompt_tokens=9000), ANSWER)
    report = pipeline.extract(conn, client_for(fake), "m")
    assert len(report.too_long) == 1 and report.too_long[0] in ids
    assert report.saved == 1


def test_ollama_down_stops_the_run(conn):
    passing_jobs(conn, "Junior Developer")
    with pytest.raises(OllamaUnavailable):
        pipeline.extract(conn, client_for(FakeOllama(httpx.ConnectError("refused"))), "m")


def test_only_passing_jobs_are_read(conn):
    passing_jobs(conn, "Junior Developer", "Senior Developer")  # the senior one is rejected
    report = pipeline.extract(conn, client_for(FakeOllama(ANSWER)), "m")
    assert report.jobs == 1


def test_command_explains_when_ollama_is_down(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    passing_jobs(conn, "Junior Developer")
    conn.close()
    monkeypatch.setenv("JM_DB_PATH", str(path))
    result = CliRunner().invoke(cli.app, ["extract", "--ollama-url", "http://127.0.0.1:9"])
    assert result.exit_code == 1
    assert "start the Ollama app" in result.output


def test_experience_replacing_a_degree_is_not_a_degree_replacing_years():
    # Real trial error: "or equivalent experience" read as a degree replacing the years.
    posting = ("1-3 years of professional software development experience. Bachelor's degree "
               "in Computer Science, Software Engineering, or equivalent experience.")
    reversed_claim = answer(
        years_required=1, years_with_degree=0,
        years_quote="1-3 years of professional software development experience",
        degree_substitutes_for_years=True,
        degree_substitute_quote="Bachelor's degree in Computer Science, Software "
                                "Engineering, or equivalent experience",
        work_authorization="none_stated", work_authorization_quote="")
    checked = check_quotes(reversed_claim, posting)
    assert checked.unverified == ["degree_substitutes_for_years"]
    assert checked.fields["years_with_degree"] == 1



@pytest.mark.parametrize(("with_degree", "expected"), [
    (5, 2),     # a degree never raises the years: capped at years_required
    (None, 0),  # a proven substitute with no number means the degree covers it all
    (1, 1),
])
def test_years_with_a_degree_only_go_down(with_degree, expected):
    checked = check_quotes(answer(years_with_degree=with_degree), POSTING)
    assert checked.fields["years_with_degree"] == expected


def test_every_field_is_checked_a_quote_or_listed_as_unchecked():
    # A new field must either get a check or be marked informational on purpose.
    checked_claims = {"years_required", "years_with_degree", "degree_substitutes_for_years",
                      "entry_level_band", "work_authorization", "clearance"}
    every = set(Extraction.model_fields)
    assert every == checked_claims | set(QUOTE_FIELDS) | set(UNCHECKED_FIELDS)


def test_the_raw_answer_is_saved_and_rechecked_without_the_model(conn, monkeypatch):
    ids = passing_jobs(conn, "Junior Developer")
    pipeline.extract(conn, client_for(FakeOllama(ANSWER)), "m")
    row = db.job_extraction(conn, ids[0], "m")
    assert json.loads(row["answer_json"])["years_with_degree"] == 0
    assert row["check_version"] == pipeline.CHECK_VERSION

    # The checks change (here: everything is dropped); saved answers are checked again.
    monkeypatch.setattr(pipeline, "CHECK_VERSION", "next")
    monkeypatch.setattr(pipeline, "check_quotes",
                        lambda extraction, posting: check_quotes(extraction, ""))
    never = FakeOllama(AssertionError("the model must not be asked"))
    report = pipeline.extract(conn, client_for(never), "m")
    assert (report.rechecked, report.jobs, never.requests) == (1, 0, 0)
    row = db.job_extraction(conn, ids[0], "m")
    assert json.loads(row["fields_json"])["years_required"] is None
    assert row["check_version"] == "next"
