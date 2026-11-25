"""Paid Brave search backend (owner decision 2026-11-09).

The contract under test is the one the callers rely on: url/title/snippet out,
the query sent verbatim (operator syntax intact), an empty 200 read as an empty
answer, and a key/quota problem raised rather than returned as ``[]``.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from jobsearch_core.search.client import (
    BRAVE_SEARCH_ENDPOINT,
    BraveSearchClient,
    DdgsSearchClient,
    SearchError,
    SearchRefusedError,
    SearchResult,
    build_search_client,
)


def _payload(*results: dict) -> dict:
    return {"type": "search", "web": {"type": "search", "results": list(results)}}


def _client(**kwargs) -> BraveSearchClient:
    return BraveSearchClient(api_key="test-key", **kwargs)


def test_maps_brave_results_onto_the_search_result_contract():
    client = _client()
    payload = _payload(
        {
            "title": "Careers at Acme",
            "url": "https://acme.example/careers",
            "description": "Open positions in Espoo.",
            "page_age": "2026-09-01T00:00:00",
            "profile": {"long_name": "acme.example"},
        },
        {"title": "No url", "description": "dropped because url is missing"},
    )

    with respx.mock as mock:
        route = mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        results = client.search("Acme careers open positions", num_results=10)

    assert results == [
        SearchResult(
            url="https://acme.example/careers",
            title="Careers at Acme",
            snippet="Open positions in Espoo.",
        )
    ]
    # Beautiful-soup free: the description is used as delivered, no page fields.
    assert "page_age" not in results[0].__dict__
    assert route.called


def test_sends_the_query_verbatim_so_operators_survive():
    """The query builders emit site:, quotes, OR and -term; Brave must see them."""
    client = _client()
    query = '("careers" OR "join our team") "Software Engineer" "Finland" site:.fi -blog -news'

    with respx.mock as mock:
        route = mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))
        client.search(query, num_results=10)

    sent = route.calls[0].request
    assert sent.url.params["q"] == query
    assert sent.url.params["text_decorations"] == "false"
    assert sent.headers["X-Subscription-Token"] == "test-key"


def test_count_is_capped_at_the_api_maximum():
    client = _client()

    with respx.mock as mock:
        route = mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))
        client.search("x", num_results=50)

    assert route.calls[0].request.url.params["count"] == "20"


def test_parse_truncates_to_num_results():
    payload = _payload(*[{"title": f"t{i}", "url": f"https://e.example/{i}"} for i in range(20)])
    assert len(BraveSearchClient._parse(payload, 5)) == 5


def test_empty_web_results_are_an_answer_not_a_failure():
    client = _client()

    with respx.mock as mock:
        mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(200, json=_payload()))
        assert client.search("nothing here") == []


@pytest.mark.parametrize("status", [401, 402, 403])
def test_refused_key_or_spent_quota_raises_without_retrying(status):
    client = _client(max_retries=1)

    with respx.mock as mock:
        route = mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(status, text="nope"))
        with pytest.raises(RuntimeError, match="BRAVE_SEARCH_KEY"):
            client.search("q")

    assert route.call_count == 1


def test_http_422_surfaces_the_body_instead_of_a_bare_httpx_error():
    """Brave answers a bad token *and* a malformed query with 422."""
    client = _client()
    body = {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}, "type": "ErrorResponse"}

    with respx.mock as mock:
        mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(422, json=body))
        with pytest.raises(RuntimeError, match="422"):
            client.search("q")


def test_transient_429_is_retried_once_then_succeeds(monkeypatch):
    monkeypatch.setattr("jobsearch_core.search.client.time.sleep", lambda _s: None)
    client = _client(max_retries=1)
    payload = _payload({"title": "t", "url": "https://e.example/1"})

    with respx.mock as mock:
        route = mock.get(BRAVE_SEARCH_ENDPOINT).mock(
            side_effect=[httpx.Response(429, text="slow down"), httpx.Response(200, json=payload)]
        )
        results = client.search("q")

    assert [r.url for r in results] == ["https://e.example/1"]
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("header", "expected"),
    [("3", 3.0), ("", 1.0), ("not-a-number", 1.0), ("999", 30.0)],
)
def test_retry_pause_obeys_retry_after_within_a_cap(monkeypatch, header, expected):
    sleeps: list[float] = []
    monkeypatch.setattr("jobsearch_core.search.client.time.sleep", sleeps.append)
    client = _client(max_retries=1)
    payload = _payload({"title": "t", "url": "https://e.example/1"})
    headers = {"Retry-After": header} if header else {}

    with respx.mock as mock:
        mock.get(BRAVE_SEARCH_ENDPOINT).mock(
            side_effect=[
                httpx.Response(429, headers=headers, text="slow down"),
                httpx.Response(200, json=payload),
            ]
        )
        client.search("q")

    assert sleeps == [expected]


def test_persistent_5xx_raises_after_the_attempt_budget(monkeypatch):
    monkeypatch.setattr("jobsearch_core.search.client.time.sleep", lambda _s: None)
    client = _client(max_retries=1)

    with respx.mock as mock:
        route = mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(503, text="down"))
        with pytest.raises(RuntimeError, match="after 2 attempt"):
            client.search("q")

    assert route.call_count == 2


def test_refusal_and_retry_exhaustion_are_different_errors(monkeypatch):
    """RF4: a refusal pauses a run; exhausted retries count toward a streak."""
    monkeypatch.setattr("jobsearch_core.search.client.time.sleep", lambda _s: None)
    client = _client(max_retries=1)

    with respx.mock as mock:
        mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(402, text="quota"))
        with pytest.raises(SearchRefusedError):
            client.search("q")
    with respx.mock as mock:
        mock.get(BRAVE_SEARCH_ENDPOINT).mock(return_value=httpx.Response(503, text="down"))
        with pytest.raises(SearchError) as caught:
            client.search("the query")
    assert not isinstance(caught.value, SearchRefusedError)
    assert caught.value.query == "the query"


def test_missing_key_refuses_to_construct(monkeypatch):
    monkeypatch.setenv("BRAVE_SEARCH_KEY", "")
    with pytest.raises(RuntimeError, match="BRAVE_SEARCH_KEY"):
        BraveSearchClient()


def test_factory_builds_each_known_backend_and_rejects_the_rest():
    assert isinstance(build_search_client("brave", api_key="k"), BraveSearchClient)
    assert isinstance(build_search_client("ddgs"), DdgsSearchClient)

    with pytest.raises(ValueError, match="Unknown search backend"):
        build_search_client("altavista")
