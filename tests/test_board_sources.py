"""Reading company job boards (Ashby, Greenhouse), from trimmed real responses."""

import copy
import json
from pathlib import Path

import httpx
import pytest

from job_matcher.sources.boards import (
    AshbyReader,
    BoardClient,
    BoardError,
    BoardGone,
    GreenhouseReader,
    ashby_job,
    greenhouse_job,
    html_to_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def ashby_raw():
    return load("ashby_board.json")["jobs"]


@pytest.fixture
def greenhouse_raw():
    return load("greenhouse_board.json")["jobs"]


# --- Ashby ------------------------------------------------------------------------------


def test_ashby_remote_job(ashby_raw):
    job = ashby_job(ashby_raw[0], "examplecorp")
    assert job.source == "ashby"
    assert job.source_id == "examplecorp:00000000-0000-4000-8000-000000000001"
    assert job.title == "Financial Planning Manager"
    assert job.company == "examplecorp"
    assert job.url == "https://jobs.ashbyhq.com/examplecorp/00000000-0000-4000-8000-000000000001"
    assert (job.country, job.location_area) == ("us", ["US"])
    assert job.location_display == "United States | Remote"
    assert job.work_mode == "remote"
    assert job.contract_time == "full_time"
    assert (job.salary_min, job.salary_max, job.salary_currency) == (95000, 120000, "USD")
    assert job.salary_is_predicted is False
    assert job.posted_at == "2026-09-15T20:47:13Z"
    assert job.description.startswith("ABOUT EXAMPLE CORP")


def test_ashby_on_site_job_uses_the_structured_address(ashby_raw):
    job = ashby_job(ashby_raw[1], "examplecorp")
    assert (job.country, job.location_area) == ("us", ["US", "Virginia", "Norfolk"])
    assert job.location_display == "Norfolk, VA"
    assert job.work_mode == "onsite"


def test_ashby_job_abroad(ashby_raw):
    job = ashby_job(ashby_raw[2], "examplecorp")
    assert (job.country, job.location_area) == ("germany", ["Germany", "Berlin"])


def test_ashby_prefers_a_secondary_location_in_a_target_metro(ashby_raw):
    raw = copy.deepcopy(ashby_raw[1])
    raw["secondaryLocations"] = [{
        "location": "Orlando, FL",
        "address": {"postalAddress": {"addressRegion": "Florida", "addressCountry": "USA",
                                      "addressLocality": "Orlando"}},
    }]
    job = ashby_job(raw, "examplecorp")
    assert job.location_area == ["US", "Florida", "Orlando"]
    assert job.location_display == "Norfolk, VA | Orlando, FL"


def test_ashby_hourly_pay_is_not_a_salary(ashby_raw):
    raw = copy.deepcopy(ashby_raw[0])
    for part in raw["compensation"]["summaryComponents"]:
        part["interval"] = "1 HOUR"
    job = ashby_job(raw, "examplecorp")
    assert (job.salary_min, job.salary_max) == (None, None)


def test_ashby_job_without_compensation_or_address(ashby_raw):
    raw = copy.deepcopy(ashby_raw[0])
    del raw["compensation"], raw["address"]
    job = ashby_job(raw, "examplecorp")
    assert job.salary_min is None
    assert (job.country, job.location_area) == ("us", ["US"])  # from the location text


@pytest.mark.parametrize(("value", "time", "kind"), [
    ("PartTime", "part_time", None), ("Contract", None, "contract"),
    ("Temporary", None, "contract"), ("Intern", None, None), (None, None, None),
])
def test_ashby_employment_type(ashby_raw, value, time, kind):
    raw = dict(ashby_raw[0], employmentType=value)
    job = ashby_job(raw, "examplecorp")
    assert (job.contract_time, job.contract_type) == (time, kind)


# --- Greenhouse -------------------------------------------------------------------------


def test_greenhouse_listing_picks_the_most_relevant_of_several_places(greenhouse_raw):
    job = greenhouse_job(greenhouse_raw[0], "examplelabs")
    assert (job.source, job.source_id) == ("greenhouse", "examplelabs:1000001")
    assert job.company == "Example Labs"
    assert job.location_display == (
        "London, UK; Ontario, CAN; Remote-Friendly, United States; San Francisco, CA")
    assert (job.country, job.location_area) == ("us", ["US"])  # the remote US place
    assert job.work_mode == "remote"
    assert job.posted_at == "2026-04-10T00:28:23Z"  # converted to UTC
    assert job.description == ""  # the listing has none; complete() fetches it


def test_greenhouse_location_type_is_not_trusted(greenhouse_raw):
    # Companies define "Location Type" themselves: here London is "Remote".
    london = greenhouse_job(greenhouse_raw[2], "examplelabs")
    assert london.work_mode is None
    on_site = greenhouse_job(greenhouse_raw[1], "examplelabs")
    assert (on_site.country, on_site.location_area) == ("us", ["US", "California", "San Francisco"])
    assert on_site.work_mode is None  # left for the description to tell


def test_greenhouse_description_comes_from_the_job_page():
    job = greenhouse_job(load("greenhouse_job.json"), "examplelabs")
    assert job.description.startswith("About Example Labs")
    assert "<" not in job.description and "&lt;" not in job.description
    assert "mission" in job.description


def test_html_to_text_keeps_line_breaks_between_blocks():
    assert html_to_text("<h2>Title</h2><p>One &amp; two</p><ul><li>a</li><li>b</li></ul>") == (
        "Title\nOne & two\na\nb")


# --- requests ---------------------------------------------------------------------------


def make_client(handler):
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return BoardClient(http, sleep=sleeps.append, delay=1.0, backoff=10.0), sleeps


def test_a_missing_board_is_gone():
    client, _ = make_client(lambda request: httpx.Response(404, text="Not Found"))
    with pytest.raises(BoardGone):
        client.get_json("https://api.example/x")


def test_temporary_errors_are_retried():
    responses = iter([httpx.Response(503), httpx.Response(200, json={"jobs": []})])
    client, sleeps = make_client(lambda request: next(responses))
    assert client.get_json("https://api.example/x") == {"jobs": []}
    assert sleeps == [1.0, 10.0, 1.0]
    assert client.requests_made == 2


def test_errors_that_never_clear_give_up():
    client, _ = make_client(lambda request: httpx.Response(429))
    with pytest.raises(BoardError, match="gave up after 4 attempts: HTTP 429"):
        client.get_json("https://api.example/x")


def test_an_unreadable_answer_is_an_error():
    client, _ = make_client(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(BoardError, match="not JSON"):
        client.get_json("https://api.example/x")


def test_ashby_reader_asks_for_the_board_with_pay():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=load("ashby_board.json"))

    client, _ = make_client(handler)
    jobs = AshbyReader(client).list_jobs("examplecorp")
    assert len(jobs) == 4
    assert seen[0].path == "/posting-api/job-board/examplecorp"
    assert seen[0].params["includeCompensation"] == "true"
    assert AshbyReader(client).complete(jobs[0]) is jobs[0]  # nothing more to fetch


def test_greenhouse_reader_lists_without_descriptions_then_fetches_one():
    seen = []

    def handler(request):
        seen.append(request.url)
        if request.url.path.endswith("/jobs"):
            return httpx.Response(200, json=load("greenhouse_board.json"))
        return httpx.Response(200, json=load("greenhouse_job.json"))

    client, _ = make_client(handler)
    reader = GreenhouseReader(client)
    jobs = reader.list_jobs("examplelabs")
    assert [j.source_id for j in jobs][:1] == ["examplelabs:1000001"]
    assert seen[0].host == "boards-api.greenhouse.io"
    assert seen[0].path == "/v1/boards/examplelabs/jobs"
    assert "content" not in seen[0].params

    full = reader.complete(jobs[0])
    assert seen[1].path == "/v1/boards/examplelabs/jobs/1000001"
    assert full.description.startswith("About Example Labs")
    assert full.location_display == jobs[0].location_display
