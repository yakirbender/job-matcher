"""Reading the index, against a fake server: no real network and no real waiting."""

import httpx
import pytest

from job_matcher.discovery import CdxClient, CdxError, latest_indexes

API = "https://index.example/CC-MAIN-2026-43-index"
PATTERN = "boards.greenhouse.io/*"


def make_client(handler, **kwargs) -> tuple[CdxClient, list[float]]:
    """A CdxClient talking to `handler` instead of the internet, recording its pauses."""
    sleeps: list[float] = []
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CdxClient(http, sleep=sleeps.append, delay=2.0, backoff=30.0, **kwargs), sleeps


def test_latest_indexes_takes_the_newest_first():
    collinfo = [
        {"id": "CC-MAIN-2026-43", "cdx-api": "https://x/43-index"},
        {"id": "CC-MAIN-2026-39", "cdx-api": "https://x/39-index"},
        {"id": "CC-MAIN-2026-34", "cdx-api": "https://x/34-index"},
    ]
    assert latest_indexes(collinfo, 2) == ["https://x/43-index", "https://x/39-index"]
    assert latest_indexes(collinfo, 10) == [c["cdx-api"] for c in collinfo]  # fewer than asked
    assert latest_indexes([], 2) == []


# --- requests --------------------------------------------------------------------------


def test_get_pauses_before_the_request_and_returns_the_response():
    client, sleeps = make_client(lambda request: httpx.Response(200, text="hello"))
    response = client._get(API, {"url": PATTERN})
    assert response.text == "hello"
    assert sleeps == [2.0]


def test_get_sends_the_params():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200)

    client, _ = make_client(handler)
    client._get(API, {"url": PATTERN, "output": "json"})
    assert seen[0].params["url"] == PATTERN
    assert seen[0].params["output"] == "json"


def test_get_backs_off_on_503_then_succeeds():
    responses = iter([httpx.Response(503), httpx.Response(503), httpx.Response(200, text="ok")])
    client, sleeps = make_client(lambda request: next(responses))
    assert client._get(API, {}).text == "ok"
    # delay, 503, backoff, delay, 503, backoff, delay, 200
    assert sleeps == [2.0, 30.0, 2.0, 30.0, 2.0]


def test_get_gives_up_after_max_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    client, _ = make_client(handler, max_retries=2)
    with pytest.raises(CdxError):
        client._get(API, {})
    assert len(calls) == 3  # the first try, plus 2 retries


@pytest.mark.parametrize("status", [400, 404, 500])
def test_get_does_not_retry_other_errors(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    client, _ = make_client(handler)
    with pytest.raises(CdxError):
        client._get(API, {})
    assert len(calls) == 1


def test_get_retries_a_dropped_connection_like_a_503():
    answers = iter([httpx.ConnectError("dropped"), httpx.Response(200, text="ok")])

    def handler(request):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client, sleeps = make_client(handler)
    assert client._get(API, {}).text == "ok"
    assert sleeps == [2.0, 30.0, 2.0]


def test_get_gives_up_when_the_connection_keeps_failing():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("too slow")

    client, _ = make_client(handler, max_retries=2)
    with pytest.raises(CdxError, match="ReadTimeout"):
        client._get(API, {})
    assert len(calls) == 3


# --- page count ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"blocks": 1}),   # no "pages"
        httpx.Response(200, json=["pages"]),       # JSON, but not an object
        httpx.Response(200, json={"pages": None}),
    ],
)
def test_num_pages_reports_an_unreadable_answer(response):
    client, _ = make_client(lambda request: response)
    with pytest.raises(CdxError, match="page count"):
        client.num_pages(API, PATTERN)


def test_num_pages_asks_for_the_count():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"pages": 85, "pageSize": 5, "blocks": 423})

    client, _ = make_client(handler)
    assert client.num_pages(API, PATTERN) == 85
    params = seen[0].params
    assert (params["url"], params["output"], params["showNumPages"]) == (PATTERN, "json", "true")


# --- paging ----------------------------------------------------------------------------


def paged_server(total_pages: int):
    """A fake index: answers the page count, then each page with two lines."""
    requested: list[str] = []

    def handler(request):
        params = request.url.params
        if params.get("showNumPages") == "true":
            return httpx.Response(200, json={"pages": total_pages})
        requested.append(params["page"])
        return httpx.Response(200, text=f'{{"page": {params["page"]}}}\n{{"second": true}}\n')

    return handler, requested


def test_pages_yields_every_page_as_lines():
    handler, requested = paged_server(3)
    client, _ = make_client(handler)
    pages = list(client.pages(API, PATTERN))
    assert requested == ["0", "1", "2"]
    assert pages[1] == ['{"page": 1}', '{"second": true}']
    assert len(pages) == 3


def test_pages_with_no_results_yields_nothing():
    handler, requested = paged_server(0)
    client, _ = make_client(handler)
    assert list(client.pages(API, PATTERN)) == []
    assert requested == []


def test_pages_stops_requesting_when_the_caller_stops():
    handler, requested = paged_server(5)
    client, _ = make_client(handler)
    for _page in client.pages(API, PATTERN):
        break  # take only the first page
    assert requested == ["0"]
