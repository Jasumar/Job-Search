from __future__ import annotations

import json
import subprocess
from importlib import import_module
from unittest.mock import MagicMock, patch

import pytest
from jobsearch_core.db.models import Company
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.llm.decisions import DecisionsClient
from jobsearch_core.search.client import (
    DDGS_TEXT_BACKENDS,
    DDGS_TIMEOUT_SECONDS,
    SEARCH_FAILURE_NO_ENGINE,
    SEARCH_FAILURE_NOTHING_MATCHED,
    DdgsSearchClient,
    SearchError,
    SearchResult,
    SearXNGSearchClient,
    _ensure_searxng,
)
from jobsearch_core.search.discover import (
    CompanyIdentityItem,
    CompanyIdentityResponse,
    DiscoveryBudget,
    MinedCompanyItem,
    MinedCompanyResponse,
    _classify_company,
    _company_followup_queries,
    _expand_baseline_queries,
    _is_acceptable_identity_signal,
    _is_likely_hiring_source,
    _is_likely_listicle,
    _is_recruiter_or_marketplace,
    _mentioned_countries,
    _parse_items,
    _route_stage_a_signals,
    discover,
    discover_with_summary,
    mine_listicle_with_render,
    mine_page_text,
    rank_company_sources_with_reason,
    refused_source_reason,
    select_company_source,
    select_company_source_with_reason,
)
from jobsearch_core.search.templates import (
    build_company_segment_queries,
    build_queries,
    is_known_board_host,
)

discover_module = import_module("jobsearch_core.search.discover")
client_module = import_module("jobsearch_core.search.client")

class TestParseItems:
    def test_parse_items_accepts_wrapped_results_array(self):
        raw = json.dumps({"results": [{"id": 0, "company_name": "Acme"}]})
        items = _parse_items(raw, CompanyIdentityItem, CompanyIdentityResponse)
        assert isinstance(items, list)
        assert items[0].company_name == "Acme"

    def test_parse_items_accepts_bare_array(self):
        raw = json.dumps([{"id": 0, "company_name": "Acme"}])
        items = _parse_items(raw, CompanyIdentityItem, CompanyIdentityResponse)
        assert len(items) == 1
        assert items[0].company_name == "Acme"

    def test_parse_items_degrades_on_invalid_payload(self):
        assert _parse_items("not json", CompanyIdentityItem, CompanyIdentityResponse) == []
        assert _parse_items(json.dumps({"bogus": True}), CompanyIdentityItem, CompanyIdentityResponse) == []

    def test_parse_items_tolerates_partial_bare_array_elements(self):
        raw = json.dumps([{"id": 0}])
        items = _parse_items(raw, CompanyIdentityItem, CompanyIdentityResponse)
        assert len(items) == 1
        assert items[0].company_name == "unknown"

class TestSearXNGHealth:
    def test_health_ok(self):
        client = SearXNGSearchClient()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "OK"
        with patch.object(client._client, "get", return_value=resp):
            assert client.health() is True

    def test_health_non_ok_status(self):
        client = SearXNGSearchClient()
        resp = MagicMock()
        resp.status_code = 503
        resp.text = "Service Unavailable"
        with patch.object(client._client, "get", return_value=resp):
            assert client.health() is False

    def test_health_connection_error(self):
        import httpx

        client = SearXNGSearchClient()
        with patch.object(
            client._client, "get", side_effect=httpx.ConnectError("refused")
        ):
            assert client.health() is False

    def test_health_partial_ok_text(self):
        client = SearXNGSearchClient()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = "OK\n"
        with patch.object(client._client, "get", return_value=resp):
            assert client.health() is True


def test_discovery_queries_do_not_exclude_seniority():
    queries = build_queries("fi", "backend")
    assert queries
    assert all(
        term not in query
        for query in queries
        for term in ("-senior", "-staff", "-principal")
    )


def test_ddgs_search_uses_declared_package(monkeypatch):
    fake_ddgs = MagicMock()
    fake_ddgs.text.return_value = [
        {"href": "https://example.com/jobs", "title": "Jobs", "body": "Careers"}
    ]
    factory = MagicMock(return_value=fake_ddgs)
    monkeypatch.setattr(client_module, "DDGS", factory)

    results = DdgsSearchClient().search("example jobs", num_results=1)

    assert results[0].url == "https://example.com/jobs"
    # The library default of 5 s discards a slow engine's results (P4).
    factory.assert_called_once_with(timeout=DDGS_TIMEOUT_SECONDS)
    fake_ddgs.text.assert_called_once_with(
        "example jobs", max_results=1, backend=DDGS_TEXT_BACKENDS
    )


def test_ddgs_search_throttles_inter_query_requests(monkeypatch):
    """Phase 4.3 politeness: consecutive searches wait at least the interval."""
    from jobsearch_core.search.client import DdgsSearchClient as Client

    fake_ddgs = MagicMock()
    fake_ddgs.text.return_value = [
        {"href": "https://example.com/jobs", "title": "Jobs", "body": "Careers"}
    ]
    monkeypatch.setattr(client_module, "DDGS", MagicMock(return_value=fake_ddgs))
    sleeps: list[float] = []
    monkeypatch.setattr(Client, "min_interval_seconds", 2.0)
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr("time.monotonic", lambda: 0.0)

    client = Client()
    client.search("query one")
    client.search("query two")
    client.search("query three")

    # First request never waits; each later one waits for the full interval
    # (monotonic is frozen at 0.0, so elapsed is always 0).
    assert sleeps == [2.0, 2.0]


def test_ddgs_search_skips_wait_when_interval_already_elapsed(monkeypatch):
    from jobsearch_core.search.client import DdgsSearchClient as Client

    fake_ddgs = MagicMock()
    fake_ddgs.text.return_value = [
        {"href": "https://example.com/jobs", "title": "Jobs", "body": "Careers"}
    ]
    monkeypatch.setattr(client_module, "DDGS", MagicMock(return_value=fake_ddgs))
    sleeps: list[float] = []
    monkeypatch.setattr(Client, "min_interval_seconds", 2.0)
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    # Two calls per search (now, then the timestamp stored for the next wait).
    ticks = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))

    client = Client()
    client.search("query one")
    client.search("query two")

    assert sleeps == []


def test_ddgs_search_retries_a_blocked_query_once(monkeypatch):
    """P4: a soft-error page clears within seconds, so one retry is spent on it."""
    fake_ddgs = MagicMock()
    fake_ddgs.text.side_effect = [
        RuntimeError("Received 429 from yahoo"),
        [{"href": "https://example.com/jobs", "title": "Jobs", "body": "Careers"}],
    ]
    monkeypatch.setattr(client_module, "DDGS", MagicMock(return_value=fake_ddgs))
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))

    client = DdgsSearchClient(min_interval_seconds=0.0, retry_pause_seconds=3.0)
    results = client.search("example jobs")

    assert [result.url for result in results] == ["https://example.com/jobs"]
    assert fake_ddgs.text.call_count == 2
    assert sleeps == [3.0]


def test_ddgs_empty_aggregate_raises_a_search_failure_naming_the_query(monkeypatch):
    """P4: ddgs collapses a block and a real empty into one answer; neither is a miss."""
    fake_ddgs = MagicMock()
    fake_ddgs.text.return_value = []
    monkeypatch.setattr(client_module, "DDGS", MagicMock(return_value=fake_ddgs))
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    client = DdgsSearchClient(min_interval_seconds=0.0)
    with pytest.raises(SearchError) as caught:
        client.search("Acme Oy careers open positions")

    assert caught.value.query == "Acme Oy careers open positions"
    assert caught.value.reason == SEARCH_FAILURE_NOTHING_MATCHED
    assert "Acme Oy careers open positions" in str(caught.value)
    assert fake_ddgs.text.call_count == 2


def test_ddgs_blocked_engines_are_reported_as_no_engine_answered(monkeypatch):
    """A raised engine error is a block, not a company without a careers page."""
    fake_ddgs = MagicMock()
    fake_ddgs.text.side_effect = RuntimeError("Received 403 from mojeek")
    monkeypatch.setattr(client_module, "DDGS", MagicMock(return_value=fake_ddgs))
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    with pytest.raises(SearchError) as caught:
        DdgsSearchClient(min_interval_seconds=0.0).search("example jobs")

    assert caught.value.reason == SEARCH_FAILURE_NO_ENGINE
    assert "403" in caught.value.detail


def test_ddgs_own_no_results_message_is_nothing_matched(monkeypatch):
    fake_ddgs = MagicMock()
    fake_ddgs.text.side_effect = RuntimeError("No results found.")
    monkeypatch.setattr(client_module, "DDGS", MagicMock(return_value=fake_ddgs))
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    with pytest.raises(SearchError) as caught:
        DdgsSearchClient(min_interval_seconds=0.0).search("example jobs")

    assert caught.value.reason == SEARCH_FAILURE_NOTHING_MATCHED


def test_ddgs_timeout_is_above_the_library_default():
    assert DDGS_TIMEOUT_SECONDS > 5.0
    assert DdgsSearchClient().timeout == DDGS_TIMEOUT_SECONDS


def test_searxng_unresponsive_engines_raise_a_search_failure():
    """The same rule as ddgs: an empty aggregate over dead engines is no answer."""
    client = SearXNGSearchClient()
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "results": [],
        "unresponsive_engines": [["duckduckgo", "timeout"], ["yahoo", "HTTP 429"]],
    }
    with patch.object(client._client, "get", return_value=resp):
        with pytest.raises(SearchError) as caught:
            client.search("example jobs")

    assert caught.value.reason == SEARCH_FAILURE_NO_ENGINE
    assert "duckduckgo" in caught.value.detail


def test_searxng_empty_answer_with_every_engine_answering_is_empty():
    client = SearXNGSearchClient()
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"results": [], "unresponsive_engines": []}
    with patch.object(client._client, "get", return_value=resp):
        assert client.search("example jobs") == []


def test_searxng_request_error_raises_a_search_failure():
    import httpx

    client = SearXNGSearchClient()
    with patch.object(client._client, "get", side_effect=httpx.ConnectError("refused")):
        with pytest.raises(SearchError, match="no engine answered"):
            client.search("example jobs")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/blog/backend-python", False),
        ("https://www.freelancer.com/job-search/python", False),
        ("https://englishjobs.fi/jobs/backend-developer", False),
        ("https://example.com/careers/backend-engineer", True),
        # Phase 4.1e: ATS boards are never hiring sources from search.
        ("https://jobs.lever.co/example/123", False),
    ],
)
def test_likely_hiring_source_filters_noise(url, expected):
    assert _is_likely_hiring_source(SearchResult(url=url, title="Backend Python")) is expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/careers", True),
        ("https://jobs.lever.co/example/123", True),
        ("https://example.com/blog/backend-python", True),
        ("https://example.com/news/company-x-hiring", True),
        ("https://example.com/article/tech-roundup", True),
        ("https://englishjobs.fi/jobs/backend-developer", False),
        ("https://www.linkedin.com/jobs/view/123", False),
        ("https://talent.com/backend-developer", False),
        ("https://example.com/staffing", False),
    ],
)
def test_acceptable_identity_signal_keeps_content_signals(url, expected):
    signal = SearchResult(url=url, title="Some tech company")
    assert _is_acceptable_identity_signal(signal) is expected


def test_recruiter_and_marketplace_detection():
    assert _is_recruiter_or_marketplace(SearchResult(url="https://randstad.com/jobs", title="Randstad"))
    assert _is_recruiter_or_marketplace(SearchResult(url="https://example.com/staffing", title="Staffing"))
    assert not _is_recruiter_or_marketplace(SearchResult(url="https://acme.example/careers", title="Acme careers"))


def testselect_company_source_prefers_employer_domain():
    results = [
        SearchResult(url="https://jobs.lever.co/acme/123", title="Acme", snippet="careers"),
        SearchResult(url="https://acme.example/careers", title="Acme careers", snippet="join us"),
        SearchResult(url="https://randstad.com/acme-jobs", title="Acme via Randstad", snippet="recruiter"),
    ]
    selected = select_company_source(results, "Acme")
    assert selected is not None
    assert selected.url == "https://acme.example/careers"


def testselect_company_source_rejects_recruiter_only():
    results = [
        SearchResult(url="https://randstad.com/acme-jobs", title="Acme via Randstad", snippet="recruiter"),
    ]
    assert select_company_source(results, "Acme") is None


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.glassdoor.com/Jobs/Acme-careers", True),
        ("https://www.glassdoor.co.uk/Jobs/Acme-careers", True),
        ("https://jobcrawls.com/company/acme/jobs", True),
        ("https://eightfold.ai/careers/acme", True),
        ("https://acme.eightfold.ai/careers", True),
        ("https://alion.io/companies/acme/jobs", True),
        # A real employer whose name merely starts with a board brand's letters.
        ("https://alion.com/careers", False),
        ("https://acme.example/careers", False),
    ],
)
def test_known_board_hosts(url, expected):
    assert is_known_board_host(url) is expected


def test_a_board_or_aggregator_result_is_refused_by_host_before_selection():
    results = [
        SearchResult(url="https://www.glassdoor.com/Jobs/Acme", title="Acme Jobs", snippet=""),
        SearchResult(url="https://acme.eightfold.ai/careers", title="Acme", snippet=""),
        SearchResult(url="https://jobs.lever.co/acme", title="Acme", snippet=""),
        SearchResult(url="https://randstad.com/acme-jobs", title="Acme via Randstad", snippet=""),
    ]
    reasons = [refused_source_reason(result) for result in results]
    assert reasons == [
        "aggregator host refused: www.glassdoor.com",
        "aggregator host refused: acme.eightfold.ai",
        "ATS board host refused: jobs.lever.co (lever)",
        "recruiter or marketplace host refused: randstad.com",
    ]


def test_a_selection_miss_names_the_refused_hosts():
    results = [
        SearchResult(url="https://www.glassdoor.fi/Jobs/Acme", title="Acme", snippet=""),
        SearchResult(url="https://jobcrawls.com/acme", title="Acme", snippet=""),
        SearchResult(url="https://alion.io/acme", title="Acme", snippet=""),
    ]
    selected, reason = select_company_source_with_reason(results, "Acme")
    assert selected is None
    assert reason == (
        "aggregator host refused: www.glassdoor.fi; "
        "aggregator host refused: jobcrawls.com; "
        "aggregator host refused: alion.io"
    )


def test_a_refused_host_never_wins_over_an_employer_page():
    results = [
        SearchResult(url="https://www.glassdoor.com/Jobs/Acme-careers", title="Acme careers", snippet=""),
        SearchResult(url="https://acme.example/careers", title="Acme careers", snippet=""),
    ]
    selected, reason = select_company_source_with_reason(results, "Acme")
    assert selected is not None and selected.url == "https://acme.example/careers"
    assert reason is None


def test_ranking_returns_every_candidate_page_best_first():
    results = [
        SearchResult(url="https://acme.example/careers/ura", title="Acme", snippet=""),
        SearchResult(url="https://jobs.lever.co/acme", title="Acme", snippet=""),
        SearchResult(url="https://acme.example/careers", title="Acme careers", snippet=""),
    ]
    candidates, refusals = rank_company_sources_with_reason(results, "Acme")
    assert [candidate.url for candidate in candidates] == [
        "https://acme.example/careers",
        "https://acme.example/careers/ura",
    ]
    assert refusals == ["ATS board host refused: jobs.lever.co (lever)"]


def test_ranking_takes_an_employer_root_page_only_when_allowed():
    """A brand page on the employer's own host is a start page the descent can read."""
    results = [
        SearchResult(url="https://acme.example/", title="Acme — home", snippet=""),
        SearchResult(url="https://acme.example/en/company", title="Acme company", snippet=""),
    ]
    assert rank_company_sources_with_reason(results, "Acme")[0] == []
    candidates, _ = rank_company_sources_with_reason(
        results, "Acme", allow_employer_root=True
    )
    assert [candidate.url for candidate in candidates] == [
        "https://acme.example/",
        "https://acme.example/en/company",
    ]


def test_an_employer_content_page_is_never_a_start_page():
    results = [
        SearchResult(url="https://acme.example/blog/acme-hiring", title="Acme", snippet=""),
        SearchResult(url="https://acme.example/news/2026", title="Acme", snippet=""),
    ]
    candidates, _ = rank_company_sources_with_reason(
        results, "Acme", allow_employer_root=True
    )
    assert candidates == []


def test_an_employer_root_page_is_not_enough_for_a_third_party_company():
    """The relaxed tier needs the company's own host, not just a hiring-shaped path."""
    results = [SearchResult(url="https://other.example/acme", title="About Acme", snippet="")]
    candidates, _ = rank_company_sources_with_reason(
        results, "Acme", allow_employer_root=True
    )
    assert candidates == []


def test_classify_company_distinguishes_known_alternate_url_and_new():
    names = {"acme corp"}
    urls = {"https://acme.example/careers"}
    hosts = {"jobs.acme.example"}

    assert _classify_company("Acme Corp", "https://first.example/x", names, urls, hosts) == "known"
    assert (
        _classify_company("Brand New", "https://jobs.acme.example/roles", names, urls, hosts)
        == "known_alternate_url"
    )
    assert _classify_company("Brand New", "https://acme.example/careers", names, urls, hosts) == "known_alternate_url"
    assert _classify_company("Brand New", "https://fresh.example/careers", names, urls, hosts) == "new"
    assert _classify_company("Brand New", None, names, urls, hosts) == "new"


def test_identity_first_summary_counts_known_alternate_and_new(tmp_path, monkeypatch):
    search_client = MagicMock()
    baseline_queries = [[
        SearchResult("https://blog.example/acme-x", "Acme article", "Acme Corp"),
        SearchResult("https://blog.example/new-x", "NewOrg article", "NewOrg"),
    ], [
        SearchResult("https://blog.example/alt-x", "AltBrand article", "AltBrand"),
    ]]
    call_count = [0]

    def search(query, num_results=10):
        index = call_count[0]
        call_count[0] += 1
        if query.startswith('"Acme Corp"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join us")]
        if query.startswith('"NewOrg"'):
            return [SearchResult("https://new.example/careers", "NewOrg careers", "Join us")]
        if query.startswith('"AltBrand"'):
            return [SearchResult("https://jobs.acme.example/careers", "AltBrand careers", "Roles")]
        if index < len(baseline_queries):
            return baseline_queries[index]
        return []

    search_client.search.side_effect = search
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": ['
        '{"id": 0, "company_name": "Acme Corp", "confidence": 0.9, "evidence": [], "reason": "x"},'
        '{"id": 1, "company_name": "NewOrg", "confidence": 0.9, "evidence": [], "reason": "x"},'
        '{"id": 2, "company_name": "AltBrand", "confidence": 0.9, "evidence": [], "reason": "x"}'
        "]}"
    )
    # Ids map 0/1/2 to the three baseline results above.
    monkeypatch.setitem(
        discover.__globals__,
        "_known_company_sources",
        lambda: ({"acme corp"}, {"https://acme.example/careers"}, {"acme.example", "jobs.acme.example"}),
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        num_results=1,
        identity_first=True,
        max_baseline_queries=3,
        max_identity_signals=3,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    # Acme Corp known, AltBrand alternate URL (host matches jobs.acme.example), NewOrg new.
    assert summary.known_companies == 1
    assert summary.known_alternate_urls == 1
    assert summary.new_companies == 1
    assert [c["name"] for c in candidates] == ["NewOrg"]
    assert summary.candidates_persisted == 0  # dry run
    assert summary.dry_run is True


def test_identity_first_accepts_content_signal_and_uses_budget(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join our team")]
        return [SearchResult("https://blog.example/tech/acme-open-source", "Building with Acme - Blog", "Acme listed")]

    search_client.search.side_effect = search
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Acme", "confidence": 0.8, '
        '"evidence": ["Acme in blog"], "reason": "Name is explicit"}]}'
    )

    candidates = discover(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        max_followup_queries_per_company=2,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert len(candidates) == 1
    assert candidates[0]["name"] == "Acme"
    assert candidates[0]["source_url"] == "https://blog.example/tech/acme-open-source"
    assert candidates[0]["careers_url"] == "https://acme.example/careers"
    budget = DiscoveryBudget(max_baseline_queries=1, max_companies=1, max_followup_queries_per_company=2)
    assert search_client.search.call_count <= budget.total_search_requests


def test_a_failed_baseline_query_is_counted_and_the_sweep_continues(tmp_path):
    """P4: one blocked query is counted and skipped, not read as an empty sweep."""
    search_client = MagicMock()
    calls = {"baseline": 0}

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join our team")]
        calls["baseline"] += 1
        if calls["baseline"] == 1:
            raise SearchError(query, SEARCH_FAILURE_NO_ENGINE, "RuntimeError: Received 429")
        return [SearchResult("https://blog.example/tech/acme-open-source", "Building with Acme", "Acme listed")]

    search_client.search.side_effect = search
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Acme", "confidence": 0.8, '
        '"evidence": ["Acme in blog"], "reason": "Name is explicit"}]}'
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=2,
        max_companies=1,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert summary.search_failures == 1
    assert [candidate["name"] for candidate in candidates] == ["Acme"]


def test_discovery_budget_total_aggregates_caps():
    budget = DiscoveryBudget(
        max_baseline_queries=8,
        max_companies=10,
        max_followup_queries_per_company=3,
    )
    # Baseline requests include generated variants: 8 x (1 + 2) + 10 x 3.
    assert budget.total_search_requests == 54

    no_variants = DiscoveryBudget(
        max_baseline_queries=8,
        max_companies=10,
        max_followup_queries_per_company=3,
        max_variants_per_baseline=0,
    )
    assert no_variants.total_search_requests == 8 + 30


def test_discovery_budget_default_ceiling_is_15():
    """Phase 4.3: the baseline-query ceiling defaults to 15 (segments x countries)."""
    assert DiscoveryBudget().max_baseline_queries == 15


def test_identity_first_selects_baseline_countries_from_preferences(tmp_path, monkeypatch):
    """Phase 4.3: baseline countries come from preferences, not all countries."""
    search_client = MagicMock()
    executed_queries: list[str] = []

    def search(query, num_results=10):
        executed_queries.append(query)
        return []

    search_client.search.side_effect = search
    llm = MagicMock()

    discover_with_summary(
        search_client,
        country_code=None,
        role_family=None,
        dry_run=True,
        num_results=1,
        identity_first=True,
        max_baseline_queries=15,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        preferences={"target_countries": ["Finland", "Sweden"]},
    )

    # 11 narrow templates x 2 selected countries = 22 requested, capped at 15.
    assert len(executed_queries) == 15
    assert any("Finland" in q for q in executed_queries)
    assert any("Sweden" in q for q in executed_queries)
    assert not any("Germany" in q or "Netherlands" in q for q in executed_queries)


def test_identity_first_ignores_preferences_when_country_code_given(tmp_path):
    search_client = MagicMock()
    executed_queries: list[str] = []

    def search(query, num_results=10):
        executed_queries.append(query)
        return []

    search_client.search.side_effect = search
    llm = MagicMock()

    discover_with_summary(
        search_client,
        country_code="fi",
        role_family=None,
        dry_run=True,
        num_results=1,
        identity_first=True,
        max_baseline_queries=15,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        preferences={"target_countries": ["Germany"]},
    )

    assert executed_queries
    assert all("Finland" in q for q in executed_queries)
    assert not any("Germany" in q for q in executed_queries)


def test_identity_first_without_preferences_falls_back_to_all_countries(tmp_path):
    search_client = MagicMock()
    executed_queries: list[str] = []

    def search(query, num_results=10):
        executed_queries.append(query)
        return []

    search_client.search.side_effect = search
    llm = MagicMock()

    discover_with_summary(
        search_client,
        country_code=None,
        role_family=None,
        dry_run=True,
        num_results=1,
        identity_first=True,
        max_baseline_queries=15,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        preferences=None,
    )

    # Capped at the ceiling: 11 Finland templates + the first 4 Sweden templates
    # (countries are ordered, later countries wait for a later run).
    assert len(executed_queries) == 15
    assert sum(1 for q in executed_queries if "Finland" in q) == 11
    assert sum(1 for q in executed_queries if "Sweden" in q) == 4


def test_discovery_triage_rejects_before_resolution(tmp_path):
    search_client = MagicMock()
    search_client.search.return_value = [
        SearchResult("https://example.com/careers", "Example careers", "Open positions"),
        SearchResult("https://other.com/jobs", "Other jobs", "A recruiter listing"),
    ]
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": ['
        '{"id": 0, "decision": "keep", "source_type": "employer_careers", '
        '"confidence": 0.9, "evidence": ["careers"], "reason": "Hiring page"},'
        '{"id": 1, "decision": "reject", "source_type": "recruiter", '
        '"confidence": 0.95, "evidence": ["recruiter"], "reason": "Not authoritative"}'
        ']}'
    )

    candidates = discover(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        triage=True,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert [candidate["careers_url"] for candidate in candidates] == ["https://example.com/careers"]
    assert candidates[0]["triage_decision"] == "keep"
    assert candidates[0]["triage_evidence"] == '["careers"]'
    assert llm.complete_json.call_count == 1


def test_identity_first_searches_company_hiring_sources_with_bounded_budget(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join our team")]
        return [SearchResult("https://signal.example/jobs/python", "Python role - Acme", "Acme is hiring")]

    search_client.search.side_effect = search
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Acme", "confidence": 0.92, '
        '"evidence": ["Acme in snippet"], "reason": "Name is explicit"}]}'
    )

    candidates = discover(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        num_results=2,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert len(candidates) == 1
    assert candidates[0]["name"] == "Acme"
    assert candidates[0]["source_url"] == "https://signal.example/jobs/python"
    assert candidates[0]["careers_url"] == "https://acme.example/careers"
    assert candidates[0]["identity_confidence"] == 0.92
    assert search_client.search.call_count == 4


def test_identity_first_skips_curated_company_before_followup(tmp_path, monkeypatch):
    search_client = MagicMock()
    search_client.search.return_value = [
        SearchResult("https://nokia.example/jobs/python", "Python role - Nokia", "Nokia is hiring")
    ]
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Nokia", "confidence": 0.95, '
        '"evidence": ["Nokia in title"], "reason": "Name is explicit"}]}'
    )

    monkeypatch.setitem(discover.__globals__, "_known_company_sources", lambda: ({"nokia"}, set(), set()))

    candidates = discover(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert candidates == []
    assert search_client.search.call_count == 1


def test_identity_first_skips_previously_accepted_company(tmp_path, monkeypatch, db_url):
    init_db(db_url)
    session = get_session()
    session.add(
        Company(
            name="Acme",
            careers_url="https://acme.example/careers",
            status="accepted",
        )
    )
    session.commit()
    session.close()

    monkeypatch.setitem(discover.__globals__, "_known_company_sources", lambda: ({"acme"}, set(), set()))
    search_client = MagicMock()
    search_client.search.return_value = [
        SearchResult("https://signal.example/jobs/python", "Python role - Acme", "Acme is hiring")
    ]
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Acme", "confidence": 0.95, '
        '"evidence": ["Acme in snippet"], "reason": "Name is explicit"}]}'
    )

    candidates = discover(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert candidates == []
    assert search_client.search.call_count == 1


def test_identity_first_suppresses_pending_unreviewed_company(tmp_path, monkeypatch, db_url):
    init_db(db_url)
    session = get_session()
    session.add(
        Company(
            name="Acme",
            careers_url="https://acme.example/careers",
            status="pending",
        )
    )
    session.commit()
    session.close()

    monkeypatch.setitem(discover.__globals__, "_known_company_sources", lambda: ({"acme"}, set(), set()))
    search_client = MagicMock()
    search_client.search.return_value = [
        SearchResult("https://signal.example/jobs/python", "Python role - Acme", "Acme is hiring")
    ]
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Acme", "confidence": 0.95, '
        '"evidence": ["Acme in snippet"], "reason": "Name is explicit"}]}'
    )

    candidates = discover(
        search_client,
        country_code="fi",
        role_family="backend",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    # The pending (unreviewed) company is suppressed from Stage B follow-up by
    # default: no per-company careers/jobs/hiring search, and no new candidate.
    assert candidates == []
    assert search_client.search.call_count == 1


def test_mentioned_countries_detects_known_country_names():
    assert _mentioned_countries("Backend jobs in Finland") == {"finland"}
    assert _mentioned_countries("Python developer Sweden") == {"sweden"}
    assert _mentioned_countries("No country here") == set()


def test_expand_baseline_queries_keeps_baseline_caps_and_drops_out_of_scope(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        {
            "queries": [
                "Python backend Helsinki",
                "Python backend Germany",
                "Python backend Espoo OR Oulu",
                "Python backend",
            ]
        }
    )
    baseline = ['("careers" OR "open positions") ("backend") "Finland" site:.fi -blog']
    expanded = _expand_baseline_queries(
        baseline,
        country_code="fi",
        role_family="backend",
        client=llm,
        cache_dir=tmp_path / "cache",
        max_variants=2,
    )

    # Baseline always retained.
    assert expanded[0] == baseline[0]
    # Max 2 variants kept. Helsinki (in scope) kept; Germany (an explicit
    # out-of-scope country name) dropped; Espoo OR Oulu kept as the second variant.
    assert "Python backend Helsinki" in expanded
    assert "Python backend Germany" not in expanded
    assert "Python backend Espoo OR Oulu" in expanded
    assert len(expanded) == 3


def test_expand_baseline_queries_is_cached(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps({"queries": ["variant one"]})
    baseline = ['("careers") "Finland" site:.fi']

    _expand_baseline_queries(
        baseline, "fi", "backend", llm, tmp_path / "cache", max_variants=1
    )
    _expand_baseline_queries(
        baseline, "fi", "backend", llm, tmp_path / "cache", max_variants=1
    )

    assert llm.complete_json.call_count == 1


def test_segment_queries_are_role_agnostic_country_scoped_and_unrestricted():
    queries = build_company_segment_queries("fi")
    assert len(queries) == 11  # Phase 4.3: one narrow template per segment
    role_terms = {"backend", "python", "data scientist", "llm", "devops", "automation"}
    for query in queries:
        assert "Finland" in query
        assert "site:" not in query
        assert "-blog" not in query
        assert "-freelance" in query
        assert not any(term in query.casefold() for term in role_terms)


def test_segment_templates_have_no_or_chains_across_segments():
    """Phase 4.3: narrow per-segment templates — no segment-crossing OR chains."""
    from jobsearch_core.config import load_scope

    templates = load_scope().company_segment_templates
    assert len(templates) >= 10
    for template in templates:
        # A single-purpose template may still OR two synonyms of one segment,
        # but never chains three-plus alternatives (the old mega-queries).
        assert template.count(" OR ") <= 1, template


def test_segment_queries_cover_the_default_countries_by_default():
    from jobsearch_core.search.templates import DEFAULT_COUNTRIES

    queries = build_company_segment_queries()
    template_count = len(build_company_segment_queries("fi"))
    assert len(queries) == template_count * len(DEFAULT_COUNTRIES)
    for name in DEFAULT_COUNTRIES.values():
        assert sum(1 for query in queries if name in query) == template_count


def test_select_baseline_countries_precedence():
    from jobsearch_core.search.templates import DEFAULT_COUNTRIES, select_baseline_countries

    # Explicit country code wins over the preference list.
    assert select_baseline_countries("fi", ["Germany"]) == ["Finland"]
    # Preference list is validated, deduped, case-insensitive.
    assert select_baseline_countries(None, ["finland", " Germany ", "bogus", "Finland"]) == [
        "Finland",
        "Germany",
    ]
    # MF6: any ISO country, through its aliases.
    assert select_baseline_countries(None, ["Suomi", "USA", "Norway"]) == [
        "Finland",
        "United States",
        "Norway",
    ]
    # Empty/unknown preferences fall back to the discovery default.
    assert select_baseline_countries(None, ["Atlantis"]) == list(DEFAULT_COUNTRIES.values())
    assert select_baseline_countries(None, None) == list(DEFAULT_COUNTRIES.values())


def test_segment_queries_for_selection_computes_segments_times_countries():
    from jobsearch_core.search.templates import (
        build_company_segment_queries_for_selection,
    )

    queries = build_company_segment_queries_for_selection(["Finland", "Sweden"])
    per_country = len(build_company_segment_queries("fi"))
    assert len(queries) == per_country * 2
    # Country-grouped: all Finland queries precede all Sweden queries.
    finland_count = sum(1 for q in queries if "Finland" in q)
    assert queries[:finland_count] == [q for q in queries if "Finland" in q]
    # No cap: every emitted query is a full narrow template instantiation.
    for query in queries:
        assert "-freelance" in query


def test_segment_queries_for_selection_respects_max_queries_cap():
    from jobsearch_core.search.templates import (
        build_company_segment_queries_for_selection,
    )

    queries = build_company_segment_queries_for_selection(
        ["Finland", "Sweden"], max_queries=15
    )
    assert len(queries) == 15  # 11 Finland templates + first 4 Sweden templates
    assert sum(1 for q in queries if "Finland" in q) == 11
    assert sum(1 for q in queries if "Sweden" in q) == 4


def test_build_queries_is_scope_driven():
    from jobsearch_core.config import SearchScope

    scope = SearchScope(
        name="custom",
        employer_scope="finance employers",
        role_families={"banking": ["quant analyst", "risk"]},
        role_titles=[],
        company_segment_templates=['"{country}" finance employer careers'],
    )
    queries = build_queries("fi", "banking", scope)
    assert queries
    assert all("quant analyst" in q or "risk" in q for q in queries)
    assert all("backend" not in q for q in queries)
    segment = build_company_segment_queries("fi", scope)
    assert segment == ['"Finland" finance employer careers -freelance -job-search -tutorial -course']


def test_identity_and_mining_and_expansion_prompts_interpolate_employer_scope():
    from jobsearch_core.search.discover import (
        _company_identity_system,
        _mined_company_system,
        _query_expansion_system,
    )

    scope = "finance employers that operate banking, payments, or lending software"
    assert scope in _company_identity_system(scope)
    assert scope in _mined_company_system(scope)
    assert scope in _query_expansion_system(scope)


def test_identity_first_uses_scope_employer_scope_in_prompt(tmp_path):
    from jobsearch_core.config import SearchScope

    custom = SearchScope(
        name="custom",
        employer_scope="insurance employers",
        role_families={},
        role_titles=[],
        company_segment_templates=['"{country}" insurance employer hiring'],
    )
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"InsureCo"'):
            return [SearchResult("https://insure.example/careers", "InsureCo careers", "Join us")]
        return [SearchResult("https://blog.example/insureco", "InsureCo", "InsureCo article")]

    search_client.search.side_effect = search
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "InsureCo", "tech_employer": "yes", '
        '"confidence": 0.9, "evidence": [], "reason": "x"}]}'
    )

    candidates = discover(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        scope=custom,
    )

    assert [c["name"] for c in candidates] == ["InsureCo"]
    # The identity extraction system prompt is scoped by the custom employer_scope.
    assert "insurance employers" in llm.complete_json.call_args[0][0]


def test_company_followup_queries_are_plain():
    assert _company_followup_queries("Supercell") == [
        '"Supercell" careers',
        '"Supercell" jobs',
        '"Supercell" hiring',
    ]


def test_expand_baseline_queries_drops_only_foreign_country_variants(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        {
            "queries": [
                "Finland startup Helsinki",
                "Germany Helsinki startup",
                "Espoo OR Oulu",
                "Finland and Germany office",
            ]
        }
    )
    baseline = ['"Finland" startup OR scaleup hiring -freelance']
    expanded = _expand_baseline_queries(
        baseline,
        country_code="fi",
        role_family=None,
        client=llm,
        cache_dir=tmp_path / "cache",
        max_variants=3,
    )

    # Baseline always retained.
    assert expanded[0] == baseline[0]
    # A variant that names only a foreign country is dropped even though it also
    # names a hub city.
    assert "Germany Helsinki startup" not in expanded
    # A city-only variant passes because cities are not country names.
    assert "Espoo OR Oulu" in expanded
    # A variant naming the target country (even alongside a foreign one) is kept.
    assert "Finland startup Helsinki" in expanded
    assert "Finland and Germany office" in expanded
    assert len(expanded) == 4


def test_expand_baseline_queries_allows_local_language_variants(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        {
            "queries": [
                "Suomi startup yritykset avoimet tyopaikat",
                "Finland startup Helsinki",
                "startups Germany Berlin",
            ]
        }
    )
    baseline = ['"Finland" startup OR scaleup hiring -freelance']
    expanded = _expand_baseline_queries(
        baseline,
        country_code="fi",
        role_family=None,
        client=llm,
        cache_dir=tmp_path / "cache",
        max_variants=3,
    )

    # Baseline always retained.
    assert expanded[0] == baseline[0]
    # A local-language variant naming the target country passes: the country-scope
    # guard stays unchanged (English-only detection), so local-language country
    # names are not treated as out-of-scope evidence.
    assert "Suomi startup yritykset avoimet tyopaikat" in expanded
    # An English variant naming the target country is kept.
    assert "Finland startup Helsinki" in expanded
    # An English variant naming a foreign country is still dropped.
    assert "startups Germany Berlin" not in expanded
    assert len(expanded) == 3


def test_identity_first_retains_non_tech_identity_for_review(tmp_path):
    # Phase 3.6a: the company sieve is inclusive — a `tech_employer == "no"`
    # identity is no longer dropped before Stage B and human review. It is
    # retained as an informational `employer_domain` signal.
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join us")]
        if query.startswith('"Beta Corp"'):
            return [SearchResult("https://beta.example/careers", "Beta Corp careers", "Join us")]
        return [
            SearchResult("https://blog.example/acme-tech", "Acme article", "Acme"),
            SearchResult("https://blog.example/beta-tech", "Beta article", "Beta Corp"),
        ]

    search_client.search.side_effect = search
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": ['
        '{"id": 0, "company_name": "Acme", "tech_employer": "no", "confidence": 0.9, '
        '"evidence": [], "reason": "clearly non-tech"},'
        '{"id": 1, "company_name": "Beta Corp", "tech_employer": "unclear", '
        '"confidence": 0.9, "evidence": [], "reason": "ambiguous"}'
        "]}"
    )

    candidates = discover(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=2,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert {c["name"] for c in candidates} == {"Acme", "Beta Corp"}
    by_name = {c["name"]: c for c in candidates}
    assert by_name["Acme"]["employer_domain"] == "no"
    assert by_name["Beta Corp"]["employer_domain"] == "unclear"


def test_identity_first_uses_segment_queries_by_default(tmp_path, monkeypatch):
    sentinel = "SENTINEL SEGMENT QUERY"
    monkeypatch.setitem(
        discover.__globals__,
        "build_company_segment_queries_for_selection",
        lambda country_names, scope, max_queries=None, segments=None: [sentinel],
    )
    search_client = MagicMock()
    search_client.search.return_value = [
        SearchResult("https://blog.example/acme", "Acme article", "Acme")
    ]
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Acme", "tech_employer": "yes", '
        '"confidence": 0.9, "evidence": [], "reason": "x"}]}'
    )

    discover(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    first_query = search_client.search.call_args_list[0].args[0]
    assert first_query == sentinel


def test_known_company_sources_include_accepted_discoveries(monkeypatch, db_url):
    init_db(db_url)
    session = get_session()
    session.add(
        Company(
            name="Accepted Co",
            careers_url="https://accepted.example/careers",
            status="accepted",
        )
    )
    session.commit()
    session.close()

    names, urls, hosts = discover_module._known_company_sources()

    assert "accepted co" in names
    assert "https://accepted.example/careers" in urls
    assert "accepted.example" in hosts


def test_known_company_sources_include_pending_unreviewed_discoveries(monkeypatch, db_url):
    init_db(db_url)
    session = get_session()
    session.add(
        Company(
            name="Pending Co",
            careers_url="https://pending.example/careers",
            status="pending",
        )
    )
    session.commit()
    session.close()

    names, urls, hosts = discover_module._known_company_sources()

    assert "pending co" in names
    assert "https://pending.example/careers" in urls
    assert "pending.example" in hosts


def test_known_company_sources_include_known_companies(monkeypatch, db_url):
    init_db(db_url)
    session = get_session()
    session.add(
        Company(
            name="Known Co",
            careers_url="https://known.example/careers",
            status="known",
        )
    )
    session.add(
        Company(
            name="Rejected Co",
            careers_url="https://rejected.example/careers",
            status="rejected",
        )
    )
    session.commit()
    session.close()

    names, urls, hosts = discover_module._known_company_sources()

    assert "known co" in names
    assert "https://known.example/careers" in urls
    assert "rejected co" not in names
    assert "https://rejected.example/careers" not in urls


@pytest.mark.parametrize(
    "url, title, expected",
    [
        ("https://example.com/blog/top-fintech-companies", "Top 10 Finnish companies", True),
        ("https://example.com/article/biggest-employers-finland", "Biggest employers in Finland", True),
        ("https://example.com/news/largest-employers", "The largest tech employers in 2026", True),
        ("https://example.com/rankings/best-employers", "Best employers in the Nordics", True),
        ("https://example.com/careers", "Careers", False),
        ("https://example.com/jobs/backend-engineer", "Backend engineer at Acme", False),
        ("https://example.com/blog/why-we-use-python", "Why we use Python", True),
    ],
)
def test_likely_listicle_detection(url, title, expected):
    assert _is_likely_listicle(SearchResult(url=url, title=title)) is expected


def test_route_stage_a_signals_maps_routes(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        {
            "results": [
                {"id": 0, "route": "hub", "confidence": 0.9, "reason": "list page"},
                {"id": 1, "route": "single", "confidence": 0.8, "reason": "careers"},
                {"id": 2, "route": "noise", "confidence": 0.7, "reason": "unrelated"},
            ]
        }
    )
    results = [
        SearchResult("https://wiki.example/list", "Companies of Finland", "many companies"),
        SearchResult("https://acme.example/careers", "Acme careers", "join us"),
        SearchResult("https://noise.example/recipe", "Pancake recipe", "flour"),
    ]

    routes = _route_stage_a_signals(results, llm, tmp_path / "cache")

    assert routes == {0: "hub", 1: "single", 2: "noise"}


def test_route_stage_a_signals_degrades_fail_open(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = "not json"

    results = [SearchResult("https://acme.example/careers", "Acme careers", "join us")]

    # An invalid routing payload degrades to no routes; the caller treats every
    # unrouted signal as single — only a positive noise verdict removes a signal.
    assert _route_stage_a_signals(results, llm, tmp_path / "cache") == {}


def test_route_stage_a_signals_normalizes_unknown_route_to_single(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        {
            "results": [
                {"id": 0, "route": "HUB", "confidence": 0.9, "reason": "uppercase"},
                {"id": 1, "route": "weird", "confidence": 0.9, "reason": "out of vocabulary"},
            ]
        }
    )
    results = [
        SearchResult("https://wiki.example/list", "Companies of Finland", "many"),
        SearchResult("https://acme.example/careers", "Acme careers", "join us"),
    ]

    routes = _route_stage_a_signals(results, llm, tmp_path / "cache")

    assert routes == {0: "hub", 1: "single"}


def test_route_stage_a_signals_is_cached(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps({"results": [{"id": 0, "route": "single"}]})
    results = [SearchResult("https://acme.example/careers", "Acme careers", "join us")]

    _route_stage_a_signals(results, llm, tmp_path / "cache")
    _route_stage_a_signals(results, llm, tmp_path / "cache")

    assert llm.complete_json.call_count == 1


def test_route_stage_a_signals_empty_input_makes_no_calls(tmp_path):
    llm = MagicMock()
    assert _route_stage_a_signals([], llm, tmp_path / "cache") == {}
    llm.complete_json.assert_not_called()


def test_mine_listicle_companies_normalizes_and_caps(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        {
            "companies": [
                {"company_name": "  Alpha Co  ", "tech_employer": "YES", "confidence": 0.9,
                 "evidence": ["x"], "reason": "r"},
                {"company_name": "unknown", "tech_employer": "yes", "confidence": 0.5},
                {"company_name": "", "tech_employer": "yes", "confidence": 0.5},
                {"company_name": "Beta", "tech_employer": "maybe", "confidence": 0.6},
                {"company_name": "Cappa", "tech_employer": "no", "confidence": 0.7},
            ]
        }
    )

    mined, _names_present = mine_page_text(
        "https://example.com/list", "page body", llm, tmp_path / "cache", max_companies=2
    )

    # Trimmed, unknown/empty dropped, out-of-vocabulary tech_employer normalized,
    # and the per-page max cap applied.
    assert [m.company_name for m in mined] == ["Alpha Co", "Beta"]
    assert mined[0].tech_employer == "yes"
    assert mined[1].tech_employer == "unclear"


def test_mine_listicle_companies_degrades_on_invalid_payload(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = "not json"

    mined, _names_present = mine_page_text(
        "https://example.com/list", "body", llm, tmp_path / "cache"
    )
    assert mined == []


def _mining_llm(names):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        {"companies": [{"company_name": n, "tech_employer": "yes", "confidence": 0.9}
                       for n in names]}
    )
    return llm


def _gate_for(noul_values):
    """A DecisionsClient mock answering the Noul gate with canned scores."""
    gate = MagicMock(spec=DecisionsClient)
    calls = {"n": 0}

    def ask(state, questions):
        i = calls["n"]
        calls["n"] += 1
        return {"has_company_names": {"type": "noul", "noul": noul_values[i]}}

    gate.ask.side_effect = ask
    return gate


def _http_client(rendered=None, raises=False):
    client = MagicMock()
    if raises:
        client.fetch_rendered.side_effect = RuntimeError("browser tier unavailable")
    else:
        client.fetch_rendered.return_value = rendered
    return client


class TestRenderRetry:
    """Class-3 sources: HTTP 200 with HTML that does not contain the list.

    A render is the most expensive operation in discovery, so it must fire
    only on positive evidence that the names are absent, at most once per
    page, and must never lose an extraction that already succeeded.
    """

    def test_no_render_when_the_gate_never_ran(self, tmp_path):
        # No gate means no evidence; a render here would be a guess.
        llm = _mining_llm(["Alpha Co"])
        http = _http_client(rendered="<p>rendered</p>")
        mined, rendered = mine_listicle_with_render(
            "https://example.com/list", "page body", llm, tmp_path / "cache", http
        )
        assert rendered is False
        http.fetch_rendered.assert_not_called()
        assert [m.company_name for m in mined] == ["Alpha Co"]

    def test_render_when_every_chunk_lacks_names(self, tmp_path):
        # Two chunks, gate says no names in either: the list is not in this
        # document, so the browser tier is worth one fetch.
        body = ("<p>a</p>" + "x" * 20000 + "<p>b</p>" + "y" * 20000)
        gate = _gate_for([0.02, 0.03])
        llm = _mining_llm(["Rendered Co"])
        http = _http_client(rendered="<p>Rendered Co</p>")
        mined, rendered = mine_listicle_with_render(
            "https://example.com/list", body, llm, tmp_path / "cache", http, gate=gate
        )
        assert rendered is True
        http.fetch_rendered.assert_called_once_with("https://example.com/list")
        assert [m.company_name for m in mined] == ["Rendered Co"]

    def test_no_render_when_names_are_present(self, tmp_path):
        body = ("<p>a</p>" + "x" * 20000 + "<p>b</p>" + "y" * 20000)
        gate = _gate_for([0.95, 0.03])
        llm = _mining_llm(["Alpha Co"])
        http = _http_client(rendered="<p>Rendered Co</p>")
        mined, rendered = mine_listicle_with_render(
            "https://example.com/list", body, llm, tmp_path / "cache", http, gate=gate
        )
        assert rendered is False
        http.fetch_rendered.assert_not_called()
        assert [m.company_name for m in mined] == ["Alpha Co"]

    def test_render_failure_keeps_the_plain_result(self, tmp_path):
        body = ("<p>a</p>" + "x" * 20000 + "<p>b</p>" + "y" * 20000)
        gate = _gate_for([0.02, 0.03])
        llm = _mining_llm(["Alpha Co"])
        http = _http_client(raises=True)
        mined, rendered = mine_listicle_with_render(
            "https://example.com/list", body, llm, tmp_path / "cache", http, gate=gate
        )
        assert rendered is False
        assert [m.company_name for m in mined] == ["Alpha Co"]

    def test_render_that_finds_nothing_keeps_the_plain_result(self, tmp_path):
        # The plain page had names (the gate only under-called per chunk) but
        # the render produced nothing: the original must survive.
        body = ("<p>a</p>" + "x" * 20000 + "<p>b</p>" + "y" * 20000)
        gate = _gate_for([0.02, 0.03])
        llm = _mining_llm(["Alpha Co"])
        http = _http_client(rendered="<p>nothing useful</p>")
        mined, rendered = mine_listicle_with_render(
            "https://example.com/list", body, llm, tmp_path / "cache", http, gate=gate
        )
        assert rendered is True
        assert [m.company_name for m in mined] == ["Alpha Co"]

    def test_at_most_one_render_per_page(self, tmp_path):
        body = ("<p>a</p>" + "x" * 20000 + "<p>b</p>" + "y" * 20000)
        gate = _gate_for([0.02, 0.03, 0.02, 0.03])
        llm = _mining_llm(["Rendered Co"])
        http = _http_client(rendered="<p>Rendered Co</p>")
        mine_listicle_with_render(
            "https://example.com/list", body, llm, tmp_path / "cache", http, gate=gate
        )
        assert http.fetch_rendered.call_count == 1


def test_mine_listicle_companies_is_cached(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps({"companies": [{"company_name": "Alpha Co"}]})
    first, _ = mine_page_text("https://example.com/list", "same body", llm, tmp_path / "cache")
    second, _ = mine_page_text("https://example.com/list", "same body", llm, tmp_path / "cache")

    assert llm.complete_json.call_count == 1


def test_mined_company_response_parses_many_items_without_ids():
    raw = json.dumps(
        {
            "companies": [
                {"company_name": "Alpha Co", "confidence": 0.9},
                {"company_name": "Beta Oy", "confidence": 0.8},
            ]
        }
    )
    response = MinedCompanyResponse.model_validate_json(raw)
    assert [item.company_name for item in response.companies] == ["Alpha Co", "Beta Oy"]
    assert all(isinstance(item, MinedCompanyItem) for item in response.companies)
    # Unlike the 1:1 CompanyIdentityItem, mined items carry no result id.
    assert not hasattr(response.companies[0], "id")


def test_mine_listicle_companies_salvages_valid_items_from_invalid_payload(tmp_path):
    """One malformed item must not discard the whole page's companies.

    Shape of a real class-3 failure: a 100-company response with a syntax slip
    (``"confidence=1.0``) in one item. The payload is invalid JSON, so strict
    parsing returned zero companies and the page scored 0% recall.
    """
    rows = [
        json.dumps(
            {
                "company_name": f"Company {i}",
                "tech_employer": "no",
                "confidence": 0.9,
                "evidence": ["ranked by revenue"],
                "reason": "list row",
            }
        )
        for i in range(1, 101)
    ]
    rows[31] = (
        '{"company_name": "Broken Corp", "tech_employer": "no", '
        '"confidence=1.0, "evidence": ["ranked by revenue"], "reason": "list row"}'
    )
    raw = '{"companies": [' + ", ".join(rows) + "]}"
    with pytest.raises(ValueError):
        json.loads(raw)
    llm = MagicMock()
    llm.complete_json.return_value = raw

    mined, _names_present = mine_page_text(
        "https://example.com/ranking", "body", llm, tmp_path / "cache", max_companies=1000
    )

    names = [item.company_name for item in mined]
    assert len(names) == 99
    assert "Company 1" in names and "Company 100" in names
    assert "Broken Corp" not in names


def test_mine_listicle_companies_salvages_truncated_response(tmp_path):
    """A response cut off mid-array still yields its complete leading items."""
    rows = [json.dumps({"company_name": f"Company {i}"}) for i in range(1, 11)]
    raw = '{"companies": [' + ", ".join(rows)
    llm = MagicMock()
    llm.complete_json.return_value = raw

    mined, _names_present = mine_page_text(
        "https://example.com/ranking", "body", llm, tmp_path / "cache", max_companies=1000
    )

    assert [item.company_name for item in mined] == [f"Company {i}" for i in range(1, 11)]


def test_mine_listicle_companies_accepts_bare_array_payload(tmp_path):
    llm = MagicMock()
    llm.complete_json.return_value = json.dumps(
        [{"company_name": "Alpha Co"}, {"company_name": "Beta Oy"}]
    )

    mined, _ = mine_page_text("https://example.com/list", "body", llm, tmp_path / "cache")

    assert [item.company_name for item in mined] == ["Alpha Co", "Beta Oy"]


def test_mined_company_item_tolerates_stray_field_shapes():
    """Odd values are normalized, never rejected: one row must not cost a page."""
    item = MinedCompanyItem.model_validate(
        {
            "company_name": "  Alpha Co  ",
            "tech_employer": "YES",
            "confidence": 7,
            "evidence": "ranked first by revenue",
        }
    )
    assert item.company_name == "Alpha Co"
    assert item.confidence == 1.0
    assert item.evidence == ["ranked first by revenue"]

    bad_score = MinedCompanyItem.model_validate({"company_name": "Beta", "confidence": "n/a"})
    assert bad_score.confidence == 0.0

    capped = MinedCompanyItem.model_validate(
        {"company_name": "Gamma", "evidence": ["a", "b", "c", "d"]}
    )
    assert len(capped.evidence) == 3

    long_name = MinedCompanyItem.model_validate({"company_name": "x" * 5000})
    assert len(long_name.company_name) == 200

    empty = MinedCompanyItem.model_validate({"company_name": "Delta", "evidence": None})
    assert empty.evidence == []


def test_mined_company_system_prompt_asks_for_compact_items():
    """Verbosity is the size risk: keep the per-item contract short."""
    prompt = discover_module._mined_company_system("technology companies")
    assert "max 8 words" in prompt
    assert "max 6 words" in prompt


def test_identity_first_mining_requires_http_client(tmp_path):
    with pytest.raises(ValueError, match="http client"):
        discover(
            MagicMock(),
            country_code="fi",
            dry_run=True,
            identity_first=True,
            mine_listicles=True,
            llm_client=MagicMock(),
            cache_dir=tmp_path / "cache",
        )


def _listicle_llm(companies_payload):
    llm = MagicMock()

    def complete_json(system_prompt, user_prompt):
        if "Extract the credible" in system_prompt:
            return json.dumps({"companies": companies_payload})
        if "Extract a probable employer identity" in system_prompt:
            return (
                '{"results": [{"id": 0, "company_name": "unknown", "confidence": 0.0, '
                '"evidence": [], "reason": ""}]}'
            )
        return '{"queries": []}'

    llm.complete_json.side_effect = complete_json
    return llm


def test_identity_first_mines_listicle_companies_with_provenance(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Alpha Co"'):
            return [SearchResult("https://alpha.example/careers", "Alpha Co careers", "Join us")]
        if query.startswith('"Beta Oy"'):
            return [SearchResult("https://beta.example/careers", "Beta Oy careers", "Open roles")]
        return [
            SearchResult(
                "https://blog.example/top-fintech-companies",
                "Top fintech companies in Finland 2026",
                "Alpha Co, Beta Oy and Gamma Corp lead the market",
            )
        ]

    search_client.search.side_effect = search
    http_client = MagicMock()
    http_client.fetch.return_value = (
        "<h1>Top fintech companies in Finland</h1>"
        "<p>Alpha Co builds payments. Beta Oy does lending. Gamma Corp is a retailer.</p>"
    )
    llm = _listicle_llm(
        [
            {"company_name": "Alpha Co", "tech_employer": "yes", "confidence": 0.9,
             "evidence": ["named"], "reason": "listed"},
            {"company_name": "Beta Oy", "tech_employer": "yes", "confidence": 0.9,
             "evidence": ["named"], "reason": "listed"},
            {"company_name": "Gamma Corp", "tech_employer": "no", "confidence": 0.9,
             "evidence": ["retailer"], "reason": "non-tech"},
        ]
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
        mine_listicles=True,
        max_listicles=2,
        max_companies_per_listicle=10,
    )

    assert sorted(c["name"] for c in candidates) == ["Alpha Co", "Beta Oy"]
    # Provenance: source_url is the listicle page, the follow-up is a real hire path.
    assert all(c["source_url"] == "https://blog.example/top-fintech-companies" for c in candidates)
    assert all(c["careers_url"].endswith("/careers") for c in candidates)
    assert all(c["employer_domain"] == "yes" for c in candidates)
    assert summary.listicle_fetches == 1
    # Gamma (a non-tech retailer) is retained as an identity under the inclusive
    # sieve but yields no Stage B hiring source, so it is not a candidate.
    assert summary.listicle_companies == 3
    assert summary.new_companies == 2
    # Page fetches are counted separately from the search-request budget.
    http_client.fetch.assert_called_once_with("https://blog.example/top-fintech-companies")
    # One baseline + three Stage B follow-ups: Gamma (non-tech) is now followed
    # up under the inclusive sieve even though it yields no hiring source.
    assert summary.search_requests_used == 1 + 3


def test_identity_first_mined_companies_count_toward_max_companies(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Alpha Co"'):
            return [SearchResult("https://alpha.example/careers", "Alpha Co careers", "Join us")]
        if query.startswith('"Beta Oy"'):
            return [SearchResult("https://beta.example/careers", "Beta Oy careers", "Open roles")]
        return [SearchResult("https://blog.example/top-companies", "Top companies", "Alpha Co, Beta Oy")]

    search_client.search.side_effect = search
    http_client = MagicMock()
    http_client.fetch.return_value = "<p>Alpha Co and Beta Oy</p>"
    # Mined companies share the global --max-companies follow-up cap.
    llm = _listicle_llm(
        [
            {"company_name": "Alpha Co", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "listed"},
            {"company_name": "Beta Oy", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "listed"},
        ]
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=1,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
        mine_listicles=True,
        max_listicles=2,
        max_companies_per_listicle=10,
    )

    assert [c["name"] for c in candidates] == ["Alpha Co"]
    assert summary.followed_companies == 1
    # One baseline + one follow-up: Beta was capped out before any extra search.
    assert summary.search_requests_used == 2


def test_identity_first_mining_suppresses_known_company(tmp_path, monkeypatch):
    monkeypatch.setitem(
        discover.__globals__, "_known_company_sources", lambda: ({"alpha co"}, set(), set())
    )
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Alpha Co"'):
            return [SearchResult("https://alpha.example/careers", "Alpha Co careers", "Join us")]
        if query.startswith('"Beta Oy"'):
            return [SearchResult("https://beta.example/careers", "Beta Oy careers", "Open roles")]
        return [SearchResult("https://blog.example/top-companies", "Top companies", "Alpha Co, Beta Oy")]

    search_client.search.side_effect = search
    http_client = MagicMock()
    http_client.fetch.return_value = "<p>Alpha Co and Beta Oy</p>"
    llm = _listicle_llm(
        [
            {"company_name": "Alpha Co", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "listed"},
            {"company_name": "Beta Oy", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "listed"},
        ]
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
        mine_listicles=True,
        max_companies_per_listicle=10,
    )

    # The known company is suppressed from Stage B follow-up; Beta is mined fresh.
    assert [c["name"] for c in candidates] == ["Beta Oy"]
    assert summary.known_companies == 1


def test_identity_first_mining_is_off_by_default(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join us")]
        return [SearchResult("https://blog.example/top-companies", "Top companies", "Acme")]

    search_client.search.side_effect = search
    http_client = MagicMock()
    llm = MagicMock()
    llm.complete_json.return_value = (
        '{"results": [{"id": 0, "company_name": "Acme", "confidence": 0.9, '
        '"evidence": [], "reason": "x"}]}'
    )

    candidates = discover(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_companies=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
    )

    assert [c["name"] for c in candidates] == ["Acme"]
    # Without --mine-listicles a content signal stays a normal 1:1 identity
    # signal and is never fetched for mining.
    http_client.fetch.assert_not_called()


def _routing_llm(routes=None, identities=None, mined=None):
    """LLM mock distinguishing the routing, identity, and mining prompts."""
    llm = MagicMock()

    def complete_json(system_prompt, user_prompt):
        if "decide from its title, URL, and snippet" in system_prompt:
            return json.dumps({"results": routes or []})
        if "Extract the credible" in system_prompt:
            return json.dumps({"companies": mined or []})
        if "Extract a probable employer identity" in system_prompt:
            return json.dumps({"results": identities or []})
        return '{"queries": []}'

    llm.complete_json.side_effect = complete_json
    return llm


def test_identity_first_drops_noise_signals(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join us")]
        return [
            SearchResult("https://noise.example/recipe", "Pancake recipe", "flour and milk"),
            SearchResult("https://signal.example/acme", "Acme opens new office", "Acme"),
        ]

    search_client.search.side_effect = search
    llm = _routing_llm(
        routes=[
            {"id": 0, "route": "noise", "confidence": 0.9, "reason": "no employer"},
            {"id": 1, "route": "single", "confidence": 0.9, "reason": "one company"},
        ],
        # Identity extraction re-indexes the surviving single pool, so the
        # Acme signal is id 0 there.
        identities=[
            {"id": 0, "company_name": "Acme", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "x"},
        ],
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
    )

    assert [c["name"] for c in candidates] == ["Acme"]
    assert summary.noise_dropped == 1
    assert summary.hub_routed == 0
    # The noise result never reaches identity extraction: only the single pool
    # (re-indexed) is sent to the identity prompt.
    identity_calls = [
        c for c in llm.complete_json.call_args_list
        if "Extract a probable employer identity" in c[0][0]
    ]
    assert len(identity_calls) == 1
    assert "Acme opens new office" in identity_calls[0][0][1]
    assert "Pancake recipe" not in identity_calls[0][0][1]


def test_identity_first_routes_llm_hub_to_mining(tmp_path):
    # A non-regex-listicle page (no content path, no list-style title) is mined
    # because the LLM routing classifies it as a hub.
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Alpha Co"'):
            return [SearchResult("https://alpha.example/careers", "Alpha Co careers", "Join us")]
        if query.startswith('"Beta Oy"'):
            return [SearchResult("https://beta.example/careers", "Beta Oy careers", "Open roles")]
        return [
            SearchResult(
                "https://wiki.example/Companies_of_Finland",
                "Companies of Finland",
                "Alpha Co and Beta Oy",
            )
        ]

    search_client.search.side_effect = search
    http_client = MagicMock()
    http_client.fetch.return_value = "<p>Alpha Co builds payments. Beta Oy does lending.</p>"
    llm = _routing_llm(
        routes=[{"id": 0, "route": "hub", "confidence": 0.9, "reason": "encyclopedia list"}],
        mined=[
            {"company_name": "Alpha Co", "tech_employer": "yes", "confidence": 0.9,
             "evidence": ["named"], "reason": "listed"},
            {"company_name": "Beta Oy", "tech_employer": "yes", "confidence": 0.9,
             "evidence": ["named"], "reason": "listed"},
        ],
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
        mine_listicles=True,
    )

    assert sorted(c["name"] for c in candidates) == ["Alpha Co", "Beta Oy"]
    assert all(c["source_url"] == "https://wiki.example/Companies_of_Finland" for c in candidates)
    assert summary.hub_routed == 1
    assert summary.noise_dropped == 0
    assert summary.listicle_fetches == 1
    assert summary.listicle_companies == 2
    http_client.fetch.assert_called_once_with("https://wiki.example/Companies_of_Finland")
    # Routing adds no search requests: one baseline + two Stage B follow-ups.
    assert summary.search_requests_used == 3


def test_identity_first_listicle_override_beats_noise_route(tmp_path):
    # The deterministic listicle detector is a hard hub override: a regex hit is
    # fetched and mined even when the LLM routing misclassifies it as noise.
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Alpha Co"'):
            return [SearchResult("https://alpha.example/careers", "Alpha Co careers", "Join us")]
        return [SearchResult("https://blog.example/top-companies", "Top companies", "Alpha Co")]

    search_client.search.side_effect = search
    http_client = MagicMock()
    http_client.fetch.return_value = "<p>Alpha Co</p>"
    llm = _routing_llm(
        routes=[{"id": 0, "route": "noise", "confidence": 0.9, "reason": "mistaken"}],
        mined=[
            {"company_name": "Alpha Co", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "listed"},
        ],
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
        mine_listicles=True,
    )

    assert [c["name"] for c in candidates] == ["Alpha Co"]
    assert summary.hub_routed == 1
    assert summary.noise_dropped == 0
    assert summary.listicle_fetches == 1
    http_client.fetch.assert_called_once()


def test_identity_first_hub_beyond_cap_falls_back_to_identity_path(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join us")]
        return [
            SearchResult("https://wiki.example/Companies_of_Finland", "Companies of Finland", "Acme"),
            SearchResult("https://directory.example/employers", "Employers of Finland", "Acme"),
        ]

    search_client.search.side_effect = search
    http_client = MagicMock()
    http_client.fetch.return_value = "<p>Acme</p>"
    llm = _routing_llm(
        routes=[
            {"id": 0, "route": "hub", "confidence": 0.9, "reason": "list"},
            {"id": 1, "route": "hub", "confidence": 0.9, "reason": "list"},
        ],
        identities=[
            {"id": 0, "company_name": "Acme", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "x"},
        ],
        mined=[
            {"company_name": "Acme", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "mined"},
        ],
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
        mine_listicles=True,
        max_listicles=1,
    )

    # The first hub is mined; the second exceeds max_listicles and falls back to
    # the inclusive 1:1 identity path instead of being dropped. The mined
    # identity (richer page-body evidence) wins the duplicate-name merge.
    assert [c["name"] for c in candidates] == ["Acme"]
    assert summary.hub_routed == 2
    assert summary.listicle_fetches == 1
    assert summary.listicle_companies == 1
    http_client.fetch.assert_called_once_with("https://wiki.example/Companies_of_Finland")


def test_identity_first_llm_hub_falls_back_when_mining_disabled(tmp_path):
    search_client = MagicMock()

    def search(query, num_results=10):
        if query.startswith('"Acme"'):
            return [SearchResult("https://acme.example/careers", "Acme careers", "Join us")]
        return [SearchResult("https://wiki.example/Companies_of_Finland", "Companies of Finland", "Acme")]

    search_client.search.side_effect = search
    http_client = MagicMock()
    llm = _routing_llm(
        routes=[{"id": 0, "route": "hub", "confidence": 0.9, "reason": "list"}],
        identities=[
            {"id": 0, "company_name": "Acme", "tech_employer": "yes", "confidence": 0.9,
             "evidence": [], "reason": "x"},
        ],
    )

    candidates, summary = discover_with_summary(
        search_client,
        country_code="fi",
        dry_run=True,
        identity_first=True,
        max_baseline_queries=1,
        max_identity_signals=5,
        max_companies=5,
        max_followup_queries_per_company=1,
        llm_client=llm,
        cache_dir=tmp_path / "cache",
        http_client=http_client,
        mine_listicles=False,
    )

    # Mining disabled: the hub-routed signal falls back to the 1:1 identity
    # path and is never fetched.
    assert [c["name"] for c in candidates] == ["Acme"]
    assert summary.hub_routed == 1
    assert summary.listicle_fetches == 0
    http_client.fetch.assert_not_called()


class TestEnsureSearxng:
    def test_already_healthy_no_docker(self):
        client = MagicMock()
        client.health.return_value = True
        with patch("jobsearch_core.search.client.subprocess") as mock_sub:
            _ensure_searxng(client)
            mock_sub.run.assert_not_called()

    @patch("jobsearch_core.search.client.subprocess")
    @patch("jobsearch_core.search.client.time")
    def test_docker_starts_successfully(self, mock_time, mock_sub):
        client = MagicMock()
        # First call: not healthy (triggers docker), second: healthy
        client.health.side_effect = [False, True]
        mock_time.sleep = MagicMock()
        _ensure_searxng(client)
        mock_sub.run.assert_called_once()
        cmd = mock_sub.run.call_args[0][0]
        assert "docker" in cmd
        assert "compose" in cmd

    @patch("jobsearch_core.search.client.subprocess")
    def test_docker_fails_to_start(self, mock_sub):
        client = MagicMock()
        client.health.return_value = False
        mock_sub.run.side_effect = subprocess.CalledProcessError(
            1, "docker", stderr="image not found"
        )
        with pytest.raises(RuntimeError, match="Could not start SearXNG"):
            _ensure_searxng(client)

    @patch("jobsearch_core.search.client.subprocess")
    @patch("jobsearch_core.search.client.time")
    def test_docker_starts_but_health_timeout(self, mock_time, mock_sub):
        client = MagicMock()
        client.health.return_value = False
        mock_time.sleep = MagicMock()
        with pytest.raises(RuntimeError, match="health check did not pass"):
            _ensure_searxng(client, max_wait=2.0, poll_interval=1.0)

    def test_docker_not_installed(self):
        client = MagicMock()
        client.health.return_value = False
        with patch(
            "jobsearch_core.search.client.subprocess.run",
            side_effect=FileNotFoundError("docker"),
        ):
            with pytest.raises(RuntimeError, match="Is Docker installed"):
                _ensure_searxng(client)
