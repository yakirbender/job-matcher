import httpx
import pytest

from job_matcher.pipeline import enrich
from job_matcher.sources.adzuna import AdzunaSource, RateLimiter, clean_text
from job_matcher.sources.base import SourceAuthError, SourceError, SourceQuotaError

from .conftest import make_spec

APP_ID, APP_KEY = "test-id-123", "test-key-456"


def make_source(handler, **kwargs) -> tuple[AdzunaSource, list[float]]:
    sleeps: list[float] = []
    source = AdzunaSource(
        APP_ID,
        APP_KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
        clock=lambda: 0.0,
        **kwargs,
    )
    return source, sleeps


def test_clean_text_strips_highlight_tags_and_entities():
    assert clean_text("Junior <strong>Dev</strong> &amp; QA ") == "Junior Dev & QA"


def test_build_params_for_city_and_remote_searches():
    city = AdzunaSource.build_params(make_spec(where="Orlando", distance_km=40, max_days_old=7))
    assert city["where"] == "Orlando"
    assert city["distance"] == 40
    assert city["max_days_old"] == 7
    assert city["sort_by"] == "date"
    assert "what_or" not in city
    assert "app_key" not in city

    remote = AdzunaSource.build_params(
        make_spec(where=None, remote=True, remote_keywords=("remote", "télétravail"))
    )
    assert remote["what_or"] == "remote télétravail"
    assert "where" not in remote


def test_search_normalizes_results(adzuna_page):
    requests: list[httpx.Request] = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=adzuna_page)

    source, _ = make_source(handler)
    pages = list(source.search(make_spec(max_pages=3)))

    # Seven results is less than a full page of 50, so there is no second request.
    assert len(pages) == 1
    assert source.requests_made == 1
    assert requests[0].url.path == "/v1/api/jobs/us/search/1"
    assert requests[0].url.params["app_key"] == APP_KEY

    jobs = pages[0]
    assert len(jobs) == 7
    first = jobs[0]
    assert first.title == "SAP Backend Developer"
    assert first.company == "Example Staffing"
    assert first.location_area == ["US", "Florida", "Orange County", "Alafaya"]
    assert (first.salary_min, first.salary_max) == (100000, 150000)
    assert first.salary_currency == "USD"
    assert first.salary_is_predicted is False
    assert first.contract_time == "full_time"
    assert first.contract_type is None
    assert first.category == "it-jobs"
    assert first.posted_at == "2026-11-01T00:16:32Z"

    assert jobs[1].salary_is_predicted is True
    french = jobs[3]
    assert french.title.startswith("Développeur Angular Équipe")
    assert french.location_area == ["Canada"]
    assert french.salary_min is None
    # Adzuna cuts every description to a ~500-character snippet ending in an ellipsis.
    # (Anonymized names shift the fixture's lengths a little, hence the loose bound.)
    assert all(len(j.description) <= 530 and j.description.endswith("…") for j in jobs)


def test_sample_results_get_the_right_metro_and_work_mode(adzuna_page):
    """Derived fields on sample results."""
    countries = {"900004": "ca", "900005": "ca"}
    expected = {
        "900001": ("orlando", "remote"),       # "SAP Backend Developer – Remote", Alafaya
        "900002": ("orlando", None),           # clearance role, snippet doesn't say
        "900003": ("orlando", "hybrid"),       # Celebration, "(Hybrid: 1–2 days onsite)"
        "900004": (None, "remote"),            # area is just ["Canada"], "100% télétravail"
        "900005": ("montreal", None),          # inclusive French title, Montréal
        "900006": ("florida_other", None),     # Brevard County (Space Coast)
        "900007": (None, "remote"),            # area is just ["US"], "|| Remote" in title
    }
    source, _ = make_source(lambda r: httpx.Response(200, json={}))
    for raw in adzuna_page["results"]:
        spec = make_spec(country=countries.get(raw["id"], "us"))
        job = enrich(source.normalize(raw, spec))
        assert (job.metro, job.work_mode) == expected[raw["id"]], raw["id"]


def test_search_pages_until_a_short_page(adzuna_page):
    full = adzuna_page | {"results": adzuna_page["results"] * 13}  # 52 results > 50

    def handler(request):
        page = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json=full if page == 1 else adzuna_page)

    source, _ = make_source(handler)
    pages = list(source.search(make_spec(max_pages=5)))
    assert len(pages) == 2
    assert source.requests_made == 2


def test_auth_failure_is_fatal_and_hides_the_key():
    source, _ = make_source(lambda r: httpx.Response(401, text=f"bad key {APP_KEY}"))
    with pytest.raises(SourceAuthError) as info:
        list(source.search(make_spec()))
    assert APP_KEY not in str(info.value)
    assert source.requests_made == 1


def test_error_body_is_redacted():
    source, _ = make_source(lambda r: httpx.Response(400, text=f"app_id={APP_ID} invalid"))
    with pytest.raises(SourceError) as info:
        list(source.search(make_spec()))
    assert APP_ID not in str(info.value)
    assert "***" in str(info.value)


def test_retries_rate_limits_then_succeeds(adzuna_page):
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(503),
        httpx.Response(200, json=adzuna_page),
    ])
    source, sleeps = make_source(lambda r: next(responses))
    pages = list(source.search(make_spec()))
    assert len(pages[0]) == 7
    assert source.requests_made == 3
    assert 7.0 in sleeps


def test_gives_up_after_max_attempts():
    source, _ = make_source(lambda r: httpx.Response(500), max_attempts=2)
    with pytest.raises(SourceError, match="gave up after 2 attempts"):
        list(source.search(make_spec()))


def test_network_errors_do_not_leak_the_url():
    def handler(request):
        raise httpx.ConnectError(f"failed {request.url}")

    source, _ = make_source(handler, max_attempts=1)
    with pytest.raises(SourceError) as info:
        list(source.search(make_spec()))
    assert APP_KEY not in str(info.value)


def test_request_budget_is_enforced(adzuna_page):
    source, _ = make_source(lambda r: httpx.Response(200, json=adzuna_page))
    source.max_requests = 0
    with pytest.raises(SourceQuotaError):
        list(source.search(make_spec()))
    assert source.requests_made == 0


def test_missing_credentials_fail_fast():
    with pytest.raises(SourceAuthError):
        AdzunaSource(None, "key")


def test_repr_hides_credentials():
    source, _ = make_source(lambda r: httpx.Response(200, json={}))
    assert APP_KEY not in repr(source)
    assert APP_ID not in repr(source)


def test_rate_limiter_spaces_requests():
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(30, clock=lambda: now[0], sleep=sleep)  # one request per 2 s
    for _ in range(3):
        limiter.wait()
    assert sleeps == [2.0, 2.0]
