from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from sqlalchemy import and_, func

from jobsearch_core.company_source import has_source_clause
from jobsearch_core.config import SearchScope, load_scope
from jobsearch_core.db.models import Company, Job, JobRun, RunSource
from jobsearch_core.db.session import get_session
from jobsearch_core.ingest.scrapers.pipeline import _classify_ats_url
from jobsearch_core.llm.cache import load_cached, save_cached
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.llm.decisions import DecisionsClient
from jobsearch_core.log import get_logger
from jobsearch_core.search.chunking import names_signal, select_chunks, strip_gated_content
from jobsearch_core.search.client import SearchClient, SearchError, SearchResult
from jobsearch_core.search.resolver import resolve_url
from jobsearch_core.search.templates import (
    COUNTRIES,
    KNOWN_AGGREGATOR_HOSTS,
    KNOWN_AGGREGATORS,
    KNOWN_RECRUITER_HOSTS,
    NOISY_PATH_PARTS,
    RECRUITER_PATH_PARTS,
    build_company_segment_queries_for_selection,
    build_queries,
    is_known_board_host,
    select_baseline_countries,
)

logger = get_logger(__name__)

DISCOVERY_TRIAGE_SYSTEM = (
    "Triage web-search results for discovering credible technology employers hiring. "
    "Keep a result only when it plausibly represents an employer careers page or a connected ATS hiring source. "
    "Reject aggregators, recruiters, marketplaces, content pages, and unrelated results. "
    "Use uncertain when the supplied title, URL, and snippet are insufficient. "
    "Do not infer current job status, official employer relationship, location eligibility, or candidate fit. "
    "Return JSON with one results array. Each result must contain id, decision (keep, reject, or uncertain), "
    "source_type (employer_careers, ats, aggregator, recruiter, content, unrelated, or unknown), "
    "confidence (0.0-1.0), evidence (array of short strings), and reason (short string)."
)

def _company_identity_system(employer_scope: str) -> str:
    """Company-identity extraction prompt, scoped by the employer domain prose."""
    return (
        "Extract a probable employer identity and whether it belongs to "
        f"{employer_scope} from web-search evidence. "
        "The input contains only a result title, URL, and snippet. "
        "Return unknown when those fields do not support a company name. Do not infer "
        "officiality, current job status, location eligibility, or candidate fit. "
        "tech_employer is yes when the evidence clearly shows the company fits the "
        "in-scope employer description above, no when it clearly does not, and unclear "
        "otherwise. "
        "Return JSON with one results array. Each result must contain id, company_name "
        "(or unknown), tech_employer (yes, no, or unclear), confidence (0.0-1.0), "
        "evidence (array of short quoted or closely paraphrased facts), and reason (short string)."
    )


def _query_expansion_system(employer_scope: str) -> str:
    """Query-expansion prompt, scoped by the employer domain prose."""
    return (
        f"You generate deeper web-search query variants for discovering {employer_scope} "
        "that are hiring. Given one baseline search query, a target country, a role family, and "
        "location preferences, produce a small set of similar-but-deeper query variants. "
        "Vary the phrasing across company segments: domestic tech and software companies, "
        "startups and scale-ups, international companies with local presence, and tech-enabled "
        "non-tech companies (industrial, retail, finance with in-house software teams). Use "
        "office-location and local-presence phrasing with the target country's own tech-hub cities. "
        "Variants may also be written in the target country's local language, translating the "
        "baseline company-segment terms so that domestic companies with non-English hiring "
        "pages can surface; mix local-language and English variants. "
        "Stay within the requested country and role family. Never broaden the target market or "
        "mention a country other than the target country. "
        "Return JSON with exactly one 'queries' array of short query strings."
    )

_ALL_COUNTRY_NAMES: tuple[str, ...] = tuple(
    sorted({name.casefold() for name in COUNTRIES.values()})
)
# MF6: whole names only (the full ISO list holds "Oman", "Mali", "India": a
# substring test would find them in "woman", "Somalia", "Indiana"). Longest
# first, so "Papua New Guinea" wins over "Guinea".
_COUNTRY_NAME_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(name) for name in sorted(_ALL_COUNTRY_NAMES, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)

# Phase 3.5k: deterministic listicle/content detection for opt-in multi-company
# mining. A Stage A signal may name dozens of companies in one page (e.g. "top
# companies in X"); the identifier marks those pages as many-per-page candidates.
LISTICLE_PATH_PARTS: set[str] = {
    "article",
    "articles",
    "best-of",
    "blog",
    "blogs",
    "list",
    "lists",
    "listicle",
    "news",
    "ranking",
    "rankings",
    "top",
    "top-10",
    "top-100",
}
_LIST_STYLE_TITLE_RE = re.compile(
    r"\b(top|biggest|largest|best|greatest|leading|most|ranked|ranking|list)\b",
    re.IGNORECASE,
)

# Bound the analysis-model prompt size for mined pages: listicles naming 30-40
# employers are typically a few thousand characters, so 20k covers them cheaply.
_MINING_PAGE_TEXT_CAP = 20000
# Gated mining may join several content chunks; cap the joined prompt text
# higher than the ungated head-truncation cap but still bounded.
_GATED_MINING_TEXT_CAP = 120000
# Per-item field ceilings. A mined ranking returns ~100 items in one response;
# these keep one verbose row from bloating the payload (and trim a model that
# ignores the prompt's length instruction) without ever rejecting an item.
_MAX_NAME_CHARS = 200
_MAX_TEXT_CHARS = 300
_MAX_EVIDENCE_CHARS = 200
_MAX_EVIDENCE_ENTRIES = 3

def _mined_company_system(employer_scope: str) -> str:
    """Listicle multi-company extraction prompt, scoped by the employer domain prose."""
    return (
        "Extract the credible "
        f"{employer_scope} named in the given page content. "
        "The page may be a listicle, article, blog, news, or ranking that names many "
        "companies. List every distinct in-scope employer the page clearly names. "
        "Return unknown as company_name for content that does not name a company. "
        "tech_employer is yes when the page clearly shows the company fits the in-scope "
        "employer description above, no when it clearly does not, and unclear otherwise. "
        "Do not infer officiality, current job status, location eligibility, or candidate fit. "
        "Return JSON with exactly one 'companies' array holding one item per named company. "
        "Each item must contain company_name, tech_employer (yes, no, or unclear), "
        "confidence (0.0-1.0), evidence (at most one short quoted phrase, max 8 words), "
        "and reason (max 6 words). Keep every item compact: verbose evidence is what "
        "makes a long ranking response unparsable."
    )


def _signal_routing_system(employer_scope: str) -> str:
    """Stage A yield-discernment prompt: route each result as hub/single/noise."""
    return (
        "Classify web-search results while looking for "
        f"{employer_scope} that are hiring. "
        "For each result, decide from its title, URL, and snippet alone whether it is: "
        "hub: a page that lists or names many companies in one place (listicle, ranking, "
        "article, blog overview, directory, or encyclopedia entry) worth mining for several "
        "company names; "
        "single: a page plausibly centered on one specific employer (careers page, ATS board, "
        "or an article about a single company); "
        "noise: unusable for employer discovery (no probable employer identity). "
        "Base the decision only on the supplied evidence. Do not infer officiality, current "
        "job status, location eligibility, or candidate fit. "
        "Return JSON with one results array. Each result must contain id, route "
        "(hub, single, or noise), confidence (0.0-1.0), and reason (short string)."
    )


class SignalRouteItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    route: str = "single"
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""


class SignalRouteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    results: list[SignalRouteItem] = Field(default_factory=list)


class DiscoveryTriageItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    decision: str = "uncertain"
    source_type: str = "unknown"
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""


class DiscoveryTriageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    results: list[DiscoveryTriageItem] = Field(default_factory=list)


class CompanyIdentityItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    company_name: str = "unknown"
    tech_employer: str = "unclear"
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""


class CompanyIdentityResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    results: list[CompanyIdentityItem] = Field(default_factory=list)


class QueryExpansionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    queries: list[str] = Field(default_factory=list)


class MinedCompanyItem(BaseModel):
    """One company named by a mined listicle page.

    Every field is tolerant by design: a mined ranking names up to ~100
    companies in one response, and one oddly-shaped value must cost at most
    that single item — never the page. Scores are clamped instead of rejected
    and ``evidence`` accepts either a single string or a list, trimmed to
    short quotes so a long list stays small enough to parse.
    """

    model_config = ConfigDict(extra="ignore")
    company_name: str = "unknown"
    tech_employer: str = "unclear"
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: object) -> float:
        """Clamp rather than reject: a stray score is not a bad company."""
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return min(max(number, 0.0), 1.0)

    @field_validator("evidence", mode="before")
    @classmethod
    def _normalize_evidence(cls, value: object) -> list[str]:
        """Accept a bare string; cap entries — evidence is informational only."""
        if isinstance(value, str):
            entries: list[object] = [value]
        elif isinstance(value, (list, tuple)):
            entries = list(value)
        else:
            return []
        cleaned = [
            str(entry).strip()[:_MAX_EVIDENCE_CHARS]
            for entry in entries
            if str(entry).strip()
        ]
        return cleaned[:_MAX_EVIDENCE_ENTRIES]

    @field_validator("company_name", "tech_employer", "reason", mode="before")
    @classmethod
    def _plain_text(cls, value: object, info: ValidationInfo) -> str:
        """Coerce to trimmed text; never reject, only shorten."""
        limit = _MAX_NAME_CHARS if info.field_name == "company_name" else _MAX_TEXT_CHARS
        return str(value if value is not None else "").strip()[:limit]


class MinedCompanyResponse(BaseModel):
    """Many-companies-per-page extraction, separate from the 1:1 result path.

    The 1:1 ``CompanyIdentityItem`` is keyed by search-result index because only
    title/URL/snippet reach the model. A mined listicle page supplies its own
    body, so items carry no ``id`` and list every company the page names.
    """

    model_config = ConfigDict(extra="ignore")
    companies: list[MinedCompanyItem] = Field(default_factory=list)


@dataclass(frozen=True)
class CompanyIdentity:
    """Normalized identity evidence used by identity-first discovery.

    ``source_url`` and ``source_query`` carry the provenance for both the 1:1
    result path (the search result) and the listicle-mining path (the fetched
    page and the query that surfaced it), so both flow through the same
    follow-up, source-selection, and persistence logic.
    """

    company_name: str
    tech_employer: str
    confidence: float
    evidence: list[str]
    reason: str
    source_url: str
    source_query: str


@dataclass(frozen=True)
class DiscoveryBudget:
    """Explicit per-run request budget for identity-first discovery.

    ``max_baseline_queries`` is the ceiling on baseline queries (Phase 4.3:
    computed from segments x countries and capped by this value — the run
    emits fewer when the selection is smaller, never more).
    """

    max_baseline_queries: int = 15
    max_identity_signals: int = 30
    max_companies: int = 10
    max_followup_queries_per_company: int = 3
    max_variants_per_baseline: int = 2
    max_listicles: int = 3
    max_companies_per_listicle: int = 15
    num_results: int = 10

    @property
    def total_search_requests(self) -> int:
        baseline_requests = self.max_baseline_queries * (1 + self.max_variants_per_baseline)
        followup_requests = self.max_companies * self.max_followup_queries_per_company
        return baseline_requests + followup_requests


@dataclass(frozen=True)
class DiscoveryRunSummary:
    """Run-level counters for an identity-first discovery pass."""

    baseline_queries: int
    search_requests_used: int
    identity_signals: int
    known_companies: int
    known_alternate_urls: int
    new_companies: int
    followed_companies: int
    sources_selected: int
    candidates_persisted: int
    dry_run: bool
    listicle_fetches: int = 0
    listicle_companies: int = 0
    listicle_renders: int = 0
    hub_routed: int = 0
    noise_dropped: int = 0
    #: Queries that produced no answer at all (master_plan P4). A sweep with a
    #: high count found fewer companies because the engine was blocked, not
    #: because the countries have none.
    search_failures: int = 0


def _parse_items(
    raw: str,
    item_model: type[DiscoveryTriageItem] | type[CompanyIdentityItem] | type[SignalRouteItem],
    response_model: type[DiscoveryTriageResponse]
    | type[CompanyIdentityResponse]
    | type[SignalRouteResponse],
) -> list:
    """Parse a named ``results`` array or a bare array, degrading to ``[]``.

    The analysis model occasionally returns the items as a plain JSON array
    instead of the ``{"results": [...]}`` wrapper. Treat a top-level list as
    the results array and tolerate an invalid payload by returning no items —
    model output is a recommendation, never final truth, and a single bad
    batch must not abort the run.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(data, list):
        try:
            return [item_model.model_validate(item) for item in data]
        except (TypeError, ValueError):
            return []
    try:
        return response_model.model_validate(data).results
    except (TypeError, ValueError) as exc:
        logger.warning("Invalid %s response: %s", item_model.__name__, exc)
        return []


_ITEM_ANCHOR_WINDOW = 400
_MAX_ITEM_BYTES = 20000


def _matching_brace(text: str, start: int) -> int | None:
    """Index of the ``}`` closing the ``{`` at ``start``, or ``None`` if unbalanced.

    String-aware so braces inside values do not miscount. A malformed element
    desynchronises the scan (an unbalanced quote swallows the rest), which
    makes this return ``None`` for that element only — the caller then simply
    moves on to the next anchor instead of losing the rest of the response.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(len(text), start + _MAX_ITEM_BYTES)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _iter_json_objects(text: str) -> Iterator[str]:
    """Yield the ``{...}`` blobs that carry a ``company_name`` key.

    Anchoring on each object's own opening brace (rather than tracking global
    nesting) keeps a malformed element from desynchronising the scan: the bad
    blob is dropped on its own and every later company is still recovered.
    """
    for start, char in enumerate(text):
        if char != "{":
            continue
        if '"company_name"' not in text[start : start + _ITEM_ANCHOR_WINDOW]:
            continue
        end = _matching_brace(text, start)
        if end is not None:
            yield text[start : end + 1]


def _validate_mined_items(elements: list[object]) -> tuple[list[MinedCompanyItem], int]:
    """Validate each candidate element on its own, counting the unusable ones."""
    items: list[MinedCompanyItem] = []
    dropped = 0
    for element in elements:
        try:
            items.append(MinedCompanyItem.model_validate(element))
        except (TypeError, ValueError):
            dropped += 1
    return items, dropped


def _salvage_mined_items(raw: str) -> list[MinedCompanyItem]:
    """Recover the complete company objects from a payload that is not valid JSON."""
    items: list[MinedCompanyItem] = []
    unreadable = 0
    for blob in _iter_json_objects(raw):
        if '"company_name"' not in blob:
            continue
        try:
            element = json.loads(blob)
        except ValueError:
            unreadable += 1
            continue
        # Skip the enclosing ``{"companies": [...]}`` wrapper: it spans the
        # malformed element too, so it never parses, but a stray valid wrapper
        # would otherwise validate as one nameless item.
        if not isinstance(element, dict) or "companies" in element:
            continue
        items.append(MinedCompanyItem.model_validate(element))
    if items or unreadable:
        logger.warning(
            "Invalid listicle-mining payload: salvaged %d companies, %d objects unreadable",
            len(items),
            unreadable,
        )
    return items


def _parse_mined_companies(raw: str) -> list[MinedCompanyItem]:
    """Parse a mining response item by item, salvaging what is still valid.

    A mined page returns one item per named company, so a 100-company ranking
    is a single ~19 KB JSON string from a model that slips on syntax once in a
    while: a real run emitted ``"confidence=1.0`` inside one of 100 items,
    which made the whole payload invalid JSON and discarded all 100 correct
    companies (class-3 zero recall). Parse each element on its own; when the
    payload itself does not parse, fall back to the balanced ``{...}`` objects
    that are still readable inside it. Only the genuinely broken elements are
    lost, and document order is preserved.
    """
    elements: list[object] = []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        companies = data.get("companies")
        elements = companies if isinstance(companies, list) else []
    elif isinstance(data, list):
        elements = data

    if elements:
        items, dropped = _validate_mined_items(elements)
        if dropped:
            logger.warning(
                "Listicle-mining response: kept %d of %d companies (%d invalid)",
                len(items),
                len(elements),
                dropped,
            )
        return items
    return _salvage_mined_items(raw)


def _triage_results(
    results: list[SearchResult], client: LLMClient, cache_dir: Path, batch_size: int = 10
) -> dict[int, DiscoveryTriageItem]:
    """Triage search metadata in batches without fetching untrusted result pages."""
    triage: dict[int, DiscoveryTriageItem] = {}
    for start in range(0, len(results), batch_size):
        batch = results[start : start + batch_size]
        payload = [
            {"id": index, "url": result.url, "title": result.title, "snippet": result.snippet}
            for index, result in enumerate(batch, start=start)
        ]
        user_prompt = "Search results to triage:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        cached = load_cached(cache_dir, DISCOVERY_TRIAGE_SYSTEM, user_prompt)
        raw = cached if cached is not None else client.complete_json(DISCOVERY_TRIAGE_SYSTEM, user_prompt)
        if cached is None:
            save_cached(cache_dir, DISCOVERY_TRIAGE_SYSTEM, user_prompt, raw)
        response_items = _parse_items(raw, DiscoveryTriageItem, DiscoveryTriageResponse)
        for item in response_items:
            if item.id in range(start, start + len(batch)):
                item.decision = item.decision.lower()
                if item.decision not in {"keep", "reject", "uncertain"}:
                    item.decision = "uncertain"
                triage[item.id] = item
    return triage


def _extract_company_identities(
    results: list[SearchResult],
    client: LLMClient,
    cache_dir: Path,
    batch_size: int = 10,
    employer_scope: str | None = None,
) -> dict[int, CompanyIdentityItem]:
    employer_scope = employer_scope or load_scope().employer_scope
    system_prompt = _company_identity_system(employer_scope)
    identities: dict[int, CompanyIdentityItem] = {}
    for start in range(0, len(results), batch_size):
        batch = results[start : start + batch_size]
        payload = [
            {"id": index, "url": result.url, "title": result.title, "snippet": result.snippet}
            for index, result in enumerate(batch, start=start)
        ]
        user_prompt = "Search-result evidence for company identity extraction:\n" + json.dumps(
            payload, ensure_ascii=False, indent=2
        )
        cached = load_cached(cache_dir, system_prompt, user_prompt)
        raw = cached if cached is not None else client.complete_json(system_prompt, user_prompt)
        if cached is None:
            save_cached(cache_dir, system_prompt, user_prompt, raw)
        valid_ids = set(range(start, start + len(batch)))
        for item in _parse_items(raw, CompanyIdentityItem, CompanyIdentityResponse):
            if item.id in valid_ids:
                item.company_name = item.company_name.strip()
                if not item.company_name or item.company_name.casefold() == "unknown":
                    item.company_name = "unknown"
                item.tech_employer = item.tech_employer.strip().casefold()
                if item.tech_employer not in {"yes", "no", "unclear"}:
                    item.tech_employer = "unclear"
                identities[item.id] = item
    return identities


def _route_stage_a_signals(
    results: list[SearchResult],
    client: LLMClient,
    cache_dir: Path,
    batch_size: int = 10,
    employer_scope: str | None = None,
) -> dict[int, str]:
    """Classify each Stage A signal as ``hub``, ``single``, or ``noise``.

    Phase 3.6c yield discernment: the routing recommendation is derived from
    title, URL, and snippet only — no page fetch and no search requests.
    ``hub`` pages are mined for many companies, ``single`` results stay on the
    1:1 identity path, and ``noise`` results are dropped before identity
    extraction. Degradation is fail-open: a missing, invalid, or
    out-of-vocabulary classification means ``single``, so only a positive
    ``noise`` verdict removes a signal; the deterministic filters have already
    run by this point.
    """
    routes: dict[int, str] = {}
    if not results:
        return routes
    employer_scope = employer_scope or load_scope().employer_scope
    system_prompt = _signal_routing_system(employer_scope)
    for start in range(0, len(results), batch_size):
        batch = results[start : start + batch_size]
        payload = [
            {"id": index, "url": result.url, "title": result.title, "snippet": result.snippet}
            for index, result in enumerate(batch, start=start)
        ]
        user_prompt = "Search results to classify:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        cached = load_cached(cache_dir, system_prompt, user_prompt)
        raw = cached if cached is not None else client.complete_json(system_prompt, user_prompt)
        if cached is None:
            save_cached(cache_dir, system_prompt, user_prompt, raw)
        valid_ids = set(range(start, start + len(batch)))
        for item in _parse_items(raw, SignalRouteItem, SignalRouteResponse):
            if item.id not in valid_ids:
                continue
            route = item.route.strip().casefold()
            routes[item.id] = route if route in {"hub", "single", "noise"} else "single"
    return routes


def _is_likely_hiring_source(result: SearchResult) -> bool:
    """Reject obvious content and marketplace results before resolving URLs.

    Phase 4.1e: ATS domains are no longer accepted as hiring sources. A search
    must surface the employer's own careers pages; ATS boards are only reached
    later, through links on a verified employer careers page (§Employer Pages
    Over ATS Boards).
    """
    parsed = urlparse(result.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    host = parsed.netloc.lower().split(":", 1)[0].removeprefix("www.")
    if any(host == domain or host.endswith(f".{domain}") for domain in KNOWN_AGGREGATOR_HOSTS):
        return False

    # Phase 4.1e: an ATS board result is not an employer hiring source — the
    # board must be reached later via the employer's own careers page.
    if _classify_ats_url(result.url):
        return False

    # P3: brand/TLD variants and tenant boards (glassdoor.co.uk,
    # acme.eightfold.ai) are third parties too, never an employer hiring source.
    if is_known_board_host(result.url):
        return False

    path_parts = {part for part in parsed.path.lower().split("/") if part}
    if path_parts & NOISY_PATH_PARTS:
        return False
    hiring_parts = {
        "career", "careers", "hiring", "job", "jobs", "join-us",
        "join-our-team", "openings", "opportunities", "vacancies", "work-with-us",
    }
    return bool(path_parts & hiring_parts)


def _is_recruiter_or_marketplace(result: SearchResult) -> bool:
    """Reject recruiter, staffing-agency, and job-marketplace sources."""
    parsed = urlparse(result.url)
    host = parsed.netloc.lower().split(":", 1)[0].removeprefix("www.")
    if any(host == domain or host.endswith(f".{domain}") for domain in KNOWN_RECRUITER_HOSTS):
        return True
    path_parts = {part for part in parsed.path.lower().split("/") if part}
    return bool(path_parts & RECRUITER_PATH_PARTS)


def _is_acceptable_identity_signal(result: SearchResult) -> bool:
    """Stage A filter: keep anything that may name a credible employer.

    Unlike the strict hiring-source filter, content paths (article, blog, news)
    remain valid company-name identity signals. Only unusable sources are dropped.
    """
    parsed = urlparse(result.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = parsed.netloc.lower().split(":", 1)[0].removeprefix("www.")
    if any(host == domain or host.endswith(f".{domain}") for domain in KNOWN_AGGREGATOR_HOSTS):
        return False
    if is_known_board_host(result.url):
        return False
    if _is_recruiter_or_marketplace(result):
        return False
    return True


def _is_likely_listicle(result: SearchResult) -> bool:
    """Determine if a Stage A result is a listicle/content page worth mining.

    Deterministic detection only (Phase 3.5k): a content-style URL path
    (article/blog/news/list/ranking) or a list-style title (top, biggest,
    largest, best, ...) marks a page that may name many companies in one place.
    Since Phase 3.6c this detection is a hard hub override in the
    yield-discernment routing: a regex-detected listicle is always routed to the
    mining path (when mining is enabled) regardless of the LLM route
    classification, and mining is additionally bounded by ``max_listicles``.
    """
    parsed = urlparse(result.url)
    path_parts = {part for part in parsed.path.casefold().strip("/").split("/") if part}
    has_content_path = bool(path_parts & LISTICLE_PATH_PARTS)
    has_list_title = bool(_LIST_STYLE_TITLE_RE.search(result.title or ""))
    return has_content_path or has_list_title


def _fetch_listicle_page(http_client, url: str) -> str | None:
    """Fetch a listicle page body through the polite client.

    Page fetches are counted separately from the search-request budget. A fetch
    failure degrades to ``None`` (a single bad page must not abort the run).
    """
    try:
        return http_client.fetch(url)
    except RuntimeError as exc:
        logger.warning("Could not fetch listicle page for mining: %s", exc)
        return None


def mine_page_text(
    page_url: str,
    page_text: str,
    client: LLMClient,
    cache_dir: Path,
    max_companies: int = 15,
    employer_scope: str | None = None,
    gate: DecisionsClient | None = None,
) -> tuple[list[MinedCompanyItem], bool | None]:
    """Extract many companies from a fetched listicle page body.

    The raw page is converted to text, chunked, and (when a Jev gate client
    is supplied) Noul-gated so only content-bearing chunks reach the model —
    list content often sits far beyond the first ``_MINING_PAGE_TEXT_CAP``
    characters of raw HTML. Without a gate (or when every gate call fails)
    the pre-gating truncation behavior is kept as the fallback. The result is
    capped at ``max_companies`` per page, cached like other model outputs,
    and treated as a recommendation: only ``unknown`` / empty names are
    dropped, the ``tech_employer`` classification is normalized and retained
    as an informational signal, and human review remains the gate
    (Phase 3.6a).

    Returns ``(mined, names_present)``. ``names_present`` is the gate's verdict
    on whether any chunk actually contained company names (``None`` when no
    gate was supplied, the gate could not answer, or the page was a single
    chunk that was mined whole). Callers use it to decide whether one extra
    client-rendered fetch is worth trying — see
    :func:`mine_listicle_with_render`.
    """
    employer_scope = employer_scope or load_scope().employer_scope
    system_prompt = _mined_company_system(employer_scope)

    # The gated-content guard applies on both paths: it is a politeness guard,
    # not a gate feature, and must not be bypassed by losing the gate.
    scores: list[float | None] | None = None
    if gate is not None:
        try:
            kept_chunks, scores = select_chunks(page_text, gate)
        except Exception as exc:  # noqa: BLE001 — gate must never crash mining
            logger.warning(
                "Chunk gating unavailable for %s; falling back to truncated head: %s",
                page_url, exc,
            )
            kept_chunks = [strip_gated_content(page_text)[:_MINING_PAGE_TEXT_CAP]]
        # Join the kept chunks; the per-prompt cap still bounds prompt size.
        mining_text = "".join(kept_chunks)[:_GATED_MINING_TEXT_CAP]
    else:
        mining_text = strip_gated_content(page_text)[:_MINING_PAGE_TEXT_CAP]

    payload = {
        "url": page_url,
        "page_text": mining_text,
    }
    user_prompt = "Page content for multi-company extraction:\n" + json.dumps(
        payload, ensure_ascii=False, indent=2
    )
    cached = load_cached(cache_dir, system_prompt, user_prompt)
    raw = cached if cached is not None else client.complete_json(system_prompt, user_prompt)
    if cached is None:
        save_cached(cache_dir, system_prompt, user_prompt, raw)
    parsed = _parse_mined_companies(raw)
    if not parsed:
        logger.warning("No usable listicle-mining response for %s", page_url)
        return [], names_signal(scores or [])

    mined: list[MinedCompanyItem] = []
    for item in parsed:
        item.company_name = item.company_name.strip()
        if not item.company_name or item.company_name.casefold() == "unknown":
            continue
        item.tech_employer = item.tech_employer.strip().casefold()
        if item.tech_employer not in {"yes", "no", "unclear"}:
            item.tech_employer = "unclear"
        mined.append(item)
        if len(mined) >= max_companies:
            break
    return mined, names_signal(scores or [])


def mine_listicle_with_render(
    page_url: str,
    page_text: str,
    client: LLMClient,
    cache_dir: Path,
    http_client: Any,
    max_companies: int = 15,
    employer_scope: str | None = None,
    gate: DecisionsClient | None = None,
) -> tuple[list[MinedCompanyItem], bool]:
    """Mine a hub page, rendering once if the names are not in the HTML.

    Class-3 (client-rendered) list sources answer HTTP 200 with perfectly
    usable HTML that simply does not contain the list: the rows arrive later
    from a JSON/AJAX call. ``PoliteClient.fetch`` cannot detect that — it only
    escalates when a response *looks* broken. The gate can: when it has seen
    every chunk and none carried company names, the list is not in this
    document, so the page is re-fetched through the browser tier and mined
    again.

    Properties this relies on:
      * At most one extra fetch per page, and never without positive evidence
        that the names are missing (``names_present is False``). A gate that
        failed, or a page with no gate, takes the plain path — a broken gate
        must not cost a render.
      * A render failure or an empty second result degrades to the plain-HTML
        result; a class-3 site that renders to nothing must not lose the page.
      * Returns ``(mined, rendered)`` so callers can report which pages needed
        a browser.
    """
    mined, names_present = mine_page_text(
        page_url, page_text, client, cache_dir, max_companies, employer_scope, gate
    )
    if names_present is not False:
        return mined, False
    logger.info("No company names in the HTML of %s; retrying through a browser render", page_url)
    rendered_html: str | None = None
    try:
        rendered_html = http_client.fetch_rendered(page_url)
    except Exception as exc:  # noqa: BLE001 — a failed render must not lose the page
        logger.warning("Render retry failed for %s: %s", page_url, exc)
    if not rendered_html:
        return mined, False
    rerendered, rendered_present = mine_page_text(
        page_url, rendered_html, client, cache_dir, max_companies, employer_scope, gate
    )
    if not rerendered:
        return mined, True
    if rendered_present is False and mined:
        # The render produced no better evidence than the plain page; keep the
        # original so a worse render can never lose good extractions.
        return mined, True
    return rerendered, True


def _extract_company_from_ats_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return None
    slug = parts[0]
    slug_lower = slug.lower()
    if "lever" in host and slug_lower not in ("jobs", "login", "admin"):
        return slug.replace("-", " ").title()
    if "greenhouse" in host and slug_lower not in ("boards", "login", "admin", "embed"):
        return slug.replace("-", " ").title()
    if "ashbyhq" in host:
        if slug_lower == "jobs" and len(parts) > 1:
            return parts[1].replace("-", " ").title()
        if slug_lower not in ("login", "admin"):
            return slug.replace("-", " ").title()
    if "workable" in host:
        if len(parts) >= 3 and parts[0] == "view" and slug_lower not in ("login", "admin"):
            from urllib.parse import unquote
            raw = unquote(parts[-1])
            company_part = raw.rsplit("-at-", 1)
            if len(company_part) == 2:
                name = company_part[1].replace("-", " ").strip().title()
                if name and len(name) > 2:
                    return name
    return None


def _extract_company_name(result: SearchResult) -> str:
    ats_company = _extract_company_from_ats_url(result.url)
    if ats_company:
        return ats_company

    title = result.title
    title = re.sub(r"\s*\[jobs\]\s*$", "", title, flags=re.IGNORECASE).strip()

    for sep in (" - ", " | "):
        if sep in title:
            candidate = title.rsplit(sep, 1)[-1].strip()
            if not candidate:
                continue
            multi_word = " " in candidate
            if not multi_word and candidate.lower() in {
                "careers", "jobs", "hiring", "work", "remote",
                "openings", "vacancies", "positions",
            }:
                continue
            return candidate

    if result.url:
        host = urlparse(result.url).netloc
        return host.replace("www.", "").split(".")[0].title()
    return "Unknown"


def _company_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()


def _known_company_sources() -> tuple[set[str], set[str], set[str]]:
    """Return known company names, source URLs, and source hosts.

    Known sources come only from the ``companies`` table with status
    ``pending``, ``accepted``, or ``known`` (the curated YAML file was removed
    in Phase 3.6). Pending candidates are suppressed by default so an
    unreviewed company is not re-discovered and re-searched on the next pass; a
    repeated observation still counts as identity evidence elsewhere and
    nothing is auto-accepted. ``rejected`` companies are not suppressed.
    """
    names: set[str] = set()
    urls: set[str] = set()
    hosts: set[str] = set()

    try:
        session = get_session()
    except RuntimeError:
        return names, urls, hosts

    try:
        records = (
            session.query(Company)
            .filter(Company.status.in_(["pending", "accepted", "known"]))
            .all()
        )
        for company in records:
            names.add(_company_key(company.name))
            if company.careers_url:
                normalized_url = company.careers_url.rstrip("/").casefold()
                urls.add(normalized_url)
                host = urlparse(company.careers_url).netloc.casefold().removeprefix("www.")
                if host:
                    hosts.add(host)
    except Exception as exc:
        logger.warning("Could not load company records: %s", exc)
    finally:
        session.close()

    return names, urls, hosts


def _classify_company(
    name: str,
    source_url: str | None,
    known_names: set[str],
    known_urls: set[str],
    known_hosts: set[str],
) -> str:
    """Classify a candidate as ``known``, ``known_alternate_url``, or ``new``.

    A candidate is ``known`` when its normalized identity matches a curated or
    accepted company. It is ``known_alternate_url`` when the identity is new but
    the source URL or host already belongs to a known company. Anything else is
    ``new``.
    """
    if _company_key(name) in known_names:
        return "known"
    if not source_url:
        return "new"
    normalized_url = source_url.rstrip("/").casefold()
    if normalized_url in known_urls:
        return "known_alternate_url"
    host = urlparse(source_url).netloc.casefold().removeprefix("www.")
    return "known_alternate_url" if host and host in known_hosts else "new"


def _mentioned_countries(text: str) -> set[str]:
    """Return the set of known target-country names mentioned in ``text``."""
    return {match.group(0).casefold() for match in _COUNTRY_NAME_RE.finditer(text)}


def _expand_baseline_queries(
    baseline: list[str],
    country_code: str | None,
    role_family: str | None,
    client: LLMClient,
    cache_dir: Path,
    max_variants: int,
    preferences: dict | None = None,
    employer_scope: str | None = None,
) -> list[str]:
    """Generate a bounded set of deeper variants per baseline query.

    The baseline query is always retained. Generated variants are capped at
    ``max_variants`` each and filtered so that none mention a country outside
    the baseline query's own scope. Variants count toward the run request
    budget because they are later executed as real searches.
    """
    employer_scope = employer_scope or load_scope().employer_scope
    system_prompt = _query_expansion_system(employer_scope)
    expanded: list[str] = []
    for query in baseline:
        expanded.append(query)
        country_scope = _mentioned_countries(query)
        if not country_scope and country_code and country_code in COUNTRIES:
            country_scope = {COUNTRIES[country_code].casefold()}
        if not country_scope:
            country_scope = set(_ALL_COUNTRY_NAMES)

        context = {
            "baseline_query": query,
            "country_code": country_code,
            "role_family": role_family,
            "preferences": preferences or {},
        }
        user_prompt = (
            "Baseline query and search context for query expansion:\n"
            f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
            "Produce deeper variant queries."
        )
        cached = load_cached(cache_dir, system_prompt, user_prompt)
        raw = cached if cached is not None else client.complete_json(system_prompt, user_prompt)
        if cached is None:
            save_cached(cache_dir, system_prompt, user_prompt, raw)
        try:
            response = QueryExpansionResponse.model_validate_json(raw)
        except (TypeError, ValueError):
            response = QueryExpansionResponse()

        kept = 0
        for variant in response.queries:
            variant = variant.strip()
            if not variant or variant.casefold() == query.casefold():
                continue
            mentioned = _mentioned_countries(variant)
            # Drop a variant only when it names a country outside the baseline's
            # scope without naming the baseline's own country. City-only variants
            # carry no country name and therefore always pass.
            if mentioned and mentioned.isdisjoint(country_scope):
                logger.info("Dropping out-of-scope generated query: %s", variant)
                continue
            expanded.append(variant)
            kept += 1
            if kept >= max_variants:
                break
    return expanded


def _company_followup_queries(name: str) -> list[str]:
    """Plain, role- and country-agnostic Stage B follow-up queries.

    The original Stage A country and role context already shaped company
    discovery, so an employer's own hiring page is not suppressed for companies
    whose careers page is not scoped to the target country or role.
    """
    return [
        f'"{name}" careers',
        f'"{name}" jobs',
        f'"{name}" hiring',
    ]


def _source_score(result: SearchResult, company_name: str) -> int:
    parsed = urlparse(result.url)
    path = parsed.path.casefold()
    title = result.title.casefold()
    score = 0
    if any(term in path for term in ("career", "job", "hiring", "join", "opening", "opportunit")):
        score += 3
    if any(term in title for term in ("career", "job", "hiring", "join", "opening", "opportunit")):
        score += 2
    company_terms = _company_key(company_name).split()
    if company_terms and any(term in parsed.netloc.casefold() for term in company_terms if len(term) > 2):
        score += 3
    return score


def refused_source_reason(result: SearchResult) -> str | None:
    """Why *result* can never be the employer's own hiring page, or ``None``.

    P3 (master_plan §Demo polish): ``careers_search`` refuses a third-party
    host by name *before* fetching it, because a board is never a job source
    (4.1e) and a fetched aggregator page is a guaranteed miss. The reason is
    what the run row records, so a miss caused by Glassdoor does not read
    like a company with no careers page.
    """
    parsed = urlparse(result.url)
    host = (parsed.hostname or "").lower()
    platform = _classify_ats_url(result.url)
    if platform:
        return f"ATS board host refused: {host} ({platform})"
    if is_known_board_host(result.url):
        return f"aggregator host refused: {host}"
    if _is_recruiter_or_marketplace(result):
        return f"recruiter or marketplace host refused: {host}"
    return None


def is_employer_host(url: str, company_name: str) -> bool:
    """True when *url*'s host carries a distinctive token of the company name.

    The employer-domain test the candidate rule already used for ranking, made
    public: ``careers_search`` also accepts an employer start page whose path is
    not jobs-shaped (a site root or a brand page whose nav leads to the
    listing). The entry descent, not the path, then decides whether the page
    lists postings.
    """
    host = (urlparse(url).hostname or "").casefold()
    terms = [term for term in _company_key(company_name).split() if len(term) > 2]
    return bool(host) and bool(terms) and any(term in host for term in terms)


def _is_employer_start_page(result: SearchResult, company_name: str) -> bool:
    """An employer-host result that is not a content page.

    Relaxed candidate tier for ``careers_search``: the URL need not carry a
    hiring token, because the descent reads the page's nav. Blog/news/wiki
    paths on the employer's own domain are still refused — those are articles
    about the company, not a hiring surface.
    """
    parsed = urlparse(result.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    path_parts = {part for part in parsed.path.lower().split("/") if part}
    if path_parts & NOISY_PATH_PARTS:
        return False
    return is_employer_host(result.url, company_name)


def rank_company_sources_with_reason(
    results: list[SearchResult],
    company_name: str,
    *,
    allow_employer_root: bool = False,
) -> tuple[list[SearchResult], list[str]]:
    """Every viable candidate page for one company, best first.

    Returns the ranked candidates and the names of the hosts that were refused.
    Ranking is employer-host first, then ``_source_score``; ties keep result
    order. ``allow_employer_root`` adds the relaxed tier (see
    ``_is_employer_start_page``) and is off for discovery, whose behaviour is
    unchanged; ``careers_search`` turns it on because it already fetches the
    page and lets the extractor judge.
    """
    refusals: list[str] = []
    candidates: list[SearchResult] = []
    for result in results:
        reason = refused_source_reason(result)
        if reason is not None:
            if reason not in refusals:
                refusals.append(reason)
            continue
        if _is_likely_hiring_source(result) or (
            allow_employer_root and _is_employer_start_page(result, company_name)
        ):
            candidates.append(result)

    candidates.sort(
        key=lambda result: (
            is_employer_host(result.url, company_name),
            _source_score(result, company_name),
        ),
        reverse=True,
    )
    return candidates, refusals


def select_company_source_with_reason(
    results: list[SearchResult], company_name: str
) -> tuple[SearchResult | None, str | None]:
    """The candidate-page rule, plus the names of the hosts it refused.

    A board, aggregator or recruiter result is classified (never fetched) and
    its host is reported; the second value is a one-line reason for a miss
    that was caused by those hosts, and ``None`` when nothing was refused.
    """
    candidates, refusals = rank_company_sources_with_reason(results, company_name)
    if candidates:
        return candidates[0], None
    if refusals:
        # Three names is enough to tell the owner what the search surfaced.
        return None, "; ".join(refusals[:3])
    return None, None


def select_company_source(results: list[SearchResult], company_name: str) -> SearchResult | None:
    """The one candidate-page rule: an employer page that looks like hiring.

    Public because discovery and ``careers_search`` (source_runs.py) pick a
    page with the same rule: no ATS board, no aggregator or
    recruiter/marketplace, employer domain preferred, best careers-ish path
    wins. The run records the refusal reason through
    ``select_company_source_with_reason``.
    """
    return select_company_source_with_reason(results, company_name)[0]


def search_once(
    search_client: SearchClient, query: str, num_results: int
) -> list[SearchResult] | None:
    """One query, or ``None`` when the search itself failed (master_plan P4).

    Discovery spends a request budget on queries; a failed query must not abort
    the sweep, and it must not look like a query that found no eligible page
    either. The caller counts these; the client already warned with the query.
    """
    try:
        return search_client.search(query, num_results=num_results)
    except SearchError:
        return None


def _discover_identity_first(
    search_client: SearchClient,
    country_code: str | None,
    role_family: str | None,
    dry_run: bool,
    num_results: int,
    resolve_links: bool,
    http_client,
    llm_client: LLMClient,
    cache_dir: Path,
    max_baseline_queries: int,
    max_identity_signals: int,
    max_companies: int,
    max_followup_queries_per_company: int,
    expand_queries: bool = False,
    max_variants_per_baseline: int = 2,
    preferences: dict | None = None,
    segments: list[str] | None = None,
    mine_listicles: bool = False,
    max_listicles: int = 3,
    max_companies_per_listicle: int = 15,
    scope: SearchScope | None = None,
    gate_client: DecisionsClient | None = None,
) -> tuple[list[dict], DiscoveryRunSummary]:
    scope = scope or load_scope()
    if mine_listicles and not http_client:
        raise ValueError("mine_listicles discovery requires an http client to fetch listicle pages")
    budget = DiscoveryBudget(
        max_baseline_queries=max_baseline_queries,
        max_identity_signals=max_identity_signals,
        max_companies=max_companies,
        max_followup_queries_per_company=max_followup_queries_per_company,
        max_variants_per_baseline=max_variants_per_baseline,
        max_listicles=max_listicles,
        max_companies_per_listicle=max_companies_per_listicle,
        num_results=num_results,
    )
    logger.info(
        "Identity-first discovery budget: %d baseline queries (%s), up to %d companies "
        "followed up, total <= %d search requests, %s listicle mining (%d pages x %d companies)",
        budget.max_baseline_queries,
        "query expansion on" if expand_queries else "query expansion off",
        budget.max_companies,
        budget.total_search_requests,
        "on" if mine_listicles else "off",
        budget.max_listicles,
        budget.max_companies_per_listicle,
    )
    search_requests = 0

    # Phase 4.3: the baseline query set is computed from the run's selection —
    # segments (scope templates) x countries — and capped by the budget
    # ceiling. Selection precedence: explicit ``country_code`` (single-country
    # run) > ``preferences.target_countries`` (evidence.yaml fallback until the
    # 3.8f wizard writes a DB profile) > all scope countries. Narrow templates
    # carry no OR chains across segments (master_plan.md "Discovery Query
    # Precision Direction").
    selected_countries = select_baseline_countries(
        country_code,
        (preferences or {}).get("target_countries"),
    )
    baseline_queries = build_company_segment_queries_for_selection(
        selected_countries, scope, max_queries=budget.max_baseline_queries, segments=segments
    )
    if expand_queries and llm_client and cache_dir:
        baseline_queries = _expand_baseline_queries(
            baseline_queries,
            country_code,
            role_family,
            llm_client,
            cache_dir,
            budget.max_variants_per_baseline,
            preferences,
            scope.employer_scope,
        )
        logger.info("Expanded %d baseline queries with generated variants", len(baseline_queries))
    queries = baseline_queries
    known_names, known_urls, known_hosts = _known_company_sources()
    initial: list[tuple[SearchResult, str]] = []
    seen_urls: set[str] = set()
    search_failures = 0
    for query in queries:
        if search_requests >= budget.total_search_requests:
            break
        search_requests += 1
        results = search_once(search_client, query, budget.num_results)
        if results is None:
            search_failures += 1
            continue
        for result in results:
            if not result.url or not _is_acceptable_identity_signal(result):
                continue
            key = result.url.rstrip("/").casefold()
            if key not in seen_urls:
                seen_urls.add(key)
                initial.append((result, query))
            if len(initial) >= budget.max_identity_signals:
                break
        if len(initial) >= budget.max_identity_signals:
            break

    # Phase 3.6c: LLM-guided yield discernment. Each surviving Stage A signal is
    # routed as hub (many-companies page), single (1:1 identity), or noise
    # (dropped). Routing uses only title/URL/snippet and consumes no search
    # requests; the deterministic listicle detector is a hard hub override the
    # model cannot downgrade.
    routes = _route_stage_a_signals(
        [result for result, _ in initial], llm_client, cache_dir, employer_scope=scope.employer_scope
    )
    single_pool: list[tuple[SearchResult, str]] = []
    hub_pool: list[tuple[SearchResult, str]] = []
    noise_dropped = 0
    for index, (result, query) in enumerate(initial):
        is_listicle = _is_likely_listicle(result)
        route = routes.get(index, "single")
        if route == "noise" and not is_listicle:
            noise_dropped += 1
            logger.debug("Stage A routing dropped noise signal: %s", result.url)
            continue
        if is_listicle or route == "hub":
            hub_pool.append((result, query))
        else:
            single_pool.append((result, query))
    hub_routed = len(hub_pool)

    grouped: dict[str, CompanyIdentity] = {}
    listicle_fetches = 0
    listicle_companies = 0
    listicle_renders = 0
    for result, query in hub_pool:
        # Mining disabled, the per-run fetch cap reached, or a failed fetch: the
        # hub page falls back to the inclusive 1:1 identity path instead of
        # being dropped — only clear noise is removed.
        if not mine_listicles or listicle_fetches >= budget.max_listicles:
            single_pool.append((result, query))
            continue
        page = _fetch_listicle_page(http_client, result.url)
        if page is None:
            single_pool.append((result, query))
            continue
        listicle_fetches += 1
        page_items, rendered = mine_listicle_with_render(
            result.url,
            page,
            llm_client,
            cache_dir,
            http_client,
            budget.max_companies_per_listicle,
            scope.employer_scope,
            gate_client,
        )
        listicle_renders += int(rendered)
        for item in page_items:
            # Phase 3.6a: inclusive sieve — no ``tech_employer == "no"`` drop.
            # Only duplicate names are skipped; the classification is retained
            # as an informational signal for human review.
            key = _company_key(item.company_name)
            if not key or key in grouped:
                continue
            listicle_companies += 1
            grouped[key] = CompanyIdentity(
                company_name=item.company_name,
                tech_employer=item.tech_employer,
                confidence=item.confidence,
                evidence=item.evidence,
                reason=item.reason,
                source_url=result.url,
                source_query=query,
            )

    identities = _extract_company_identities(
        [result for result, _ in single_pool], llm_client, cache_dir, employer_scope=scope.employer_scope
    )
    for index, (result, query) in enumerate(single_pool):
        identity = identities.get(index)
        # Phase 3.6a: the company sieve is inclusive. Only unknown identities and
        # non-positive confidence are dropped; a ``tech_employer == "no"``
        # classification is retained as an informational signal and left to human
        # review, rather than removed before Stage B.
        if not identity or identity.company_name == "unknown" or identity.confidence <= 0:
            continue
        key = _company_key(identity.company_name)
        if key and key not in grouped:
            grouped[key] = CompanyIdentity(
                company_name=identity.company_name,
                tech_employer=identity.tech_employer,
                confidence=identity.confidence,
                evidence=identity.evidence,
                reason=identity.reason,
                source_url=result.url,
                source_query=query,
            )
    if hub_routed or noise_dropped:
        logger.info(
            "Stage A routing: %d hub signal(s) routed to mining, %d noise signal(s) dropped",
            hub_routed,
            noise_dropped,
        )

    candidates: list[dict] = []
    known_companies = 0
    for company_identity in grouped.values():
        if _classify_company(
            company_identity.company_name,
            None,
            known_names,
            known_urls,
            known_hosts,
        ) == "known":
            known_companies += 1

    known_alternate_urls = 0
    followed_companies = 0
    for company_identity in grouped.values():
        identity = company_identity
        if _classify_company(
            identity.company_name,
            None,
            known_names,
            known_urls,
            known_hosts,
        ) == "known":
            logger.info("Skipping known company during identity-first discovery: %s", identity.company_name)
            continue
        if followed_companies >= budget.max_companies:
            break
        followed_companies += 1
        followup_queries = _company_followup_queries(identity.company_name)
        followup_results: list[tuple[SearchResult, str]] = []
        for query in followup_queries[:budget.max_followup_queries_per_company]:
            if search_requests >= budget.total_search_requests:
                break
            search_requests += 1
            results = search_once(search_client, query, budget.num_results)
            if results is None:
                search_failures += 1
                continue
            for result in results:
                followup_results.append((result, query))
        selected = select_company_source([result for result, _ in followup_results], identity.company_name)
        if selected is None:
            continue
        source_result = selected
        followup_query = next(query for result, query in followup_results if result is source_result)
        source_url = identity.source_url
        careers_url = source_result.url
        if _classify_company(
            identity.company_name,
            careers_url,
            known_names,
            known_urls,
            known_hosts,
        ) == "known_alternate_url":
            logger.info(
                "Alternate hiring URL for known company during identity-first discovery: %s",
                identity.company_name,
            )
            known_alternate_urls += 1
            continue
        resolution_status = "unresolved"
        resolution_page_type = "unknown"
        if resolve_links and http_client:
            resolution = resolve_url(careers_url, http_client, llm_client, cache_dir)
            careers_url = resolution.resolved_url
            resolution_status = resolution.status
            resolution_page_type = resolution.page_type
        candidates.append(
            {
                "name": identity.company_name,
                "country": country_code.upper() if country_code else None,
                "careers_url": careers_url,
                "source_url": source_url,
                "resolution_status": resolution_status,
                "resolution_page_type": resolution_page_type,
                # Phase 4.1e: ats_platform is scrape-time provenance only. It
                # is filled when scraping the careers page reveals an ATS
                # board behind it, never from the discovery source URL.
                "ats_platform": None,
                "source_query": identity.source_query,
                "identity_name": identity.company_name,
                "identity_confidence": identity.confidence,
                "identity_evidence": json.dumps(identity.evidence, ensure_ascii=False),
                "identity_reason": identity.reason,
                "employer_domain": identity.tech_employer,
                "followup_query": followup_query,
            }
        )

    persisted = 0
    if not dry_run:
        persisted = _persist_candidates(candidates)

    summary = DiscoveryRunSummary(
        baseline_queries=budget.max_baseline_queries,
        search_requests_used=search_requests,
        identity_signals=len(initial),
        known_companies=known_companies,
        known_alternate_urls=known_alternate_urls,
        new_companies=len(candidates),
        followed_companies=followed_companies,
        sources_selected=len(candidates),
        candidates_persisted=persisted,
        dry_run=dry_run,
        listicle_fetches=listicle_fetches,
        listicle_companies=listicle_companies,
        listicle_renders=listicle_renders,
        hub_routed=hub_routed,
        noise_dropped=noise_dropped,
        search_failures=search_failures,
    )
    if search_failures:
        logger.warning(
            "Discovery: %d of %d search request(s) produced no answer (blocked or throttled "
            "engine); fewer candidates than the query set implies is not a country with no "
            "careers pages",
            search_failures,
            search_requests,
        )
    return candidates, summary


def _persist_candidates(candidates: list[dict]) -> int:
    session = get_session()
    inserted = 0
    try:
        for candidate in candidates:
            existing = session.query(Company).filter(
                Company.careers_url == candidate["careers_url"]
            ).first()
            if not existing:
                session.add(Company(**candidate))
                inserted += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    logger.info("Inserted %d new identity-first candidates", inserted)
    return inserted


def discover(
    search_client: SearchClient,
    country_code: str | None = None,
    role_family: str | None = None,
    dry_run: bool = False,
    num_results: int = 10,
    resolve_links: bool = False,
    http_client=None,
    llm_client=None,
    cache_dir=None,
    triage: bool = False,
    triage_batch_size: int = 10,
    identity_first: bool = False,
    max_baseline_queries: int = 8,
    max_identity_signals: int = 30,
    max_companies: int = 10,
    max_followup_queries_per_company: int = 3,
    expand_queries: bool = False,
    max_variants_per_baseline: int = 2,
    preferences: dict | None = None,
    segments: list[str] | None = None,
    mine_listicles: bool = False,
    max_listicles: int = 3,
    max_companies_per_listicle: int = 15,
    scope: SearchScope | None = None,
) -> list[dict]:
    """Run a discovery pass and return the candidate records.

    The identity-first variant returns only ``new`` candidates; known companies
    and alternate URLs for known companies are suppressed. Use
    :func:`discover_with_summary` when the run-level counters are needed.
    """
    candidates, _ = discover_with_summary(
        search_client,
        country_code=country_code,
        role_family=role_family,
        dry_run=dry_run,
        num_results=num_results,
        resolve_links=resolve_links,
        http_client=http_client,
        llm_client=llm_client,
        cache_dir=cache_dir,
        triage=triage,
        triage_batch_size=triage_batch_size,
        identity_first=identity_first,
        max_baseline_queries=max_baseline_queries,
        max_identity_signals=max_identity_signals,
        max_companies=max_companies,
        max_followup_queries_per_company=max_followup_queries_per_company,
        expand_queries=expand_queries,
        max_variants_per_baseline=max_variants_per_baseline,
        preferences=preferences,
        segments=segments,
        mine_listicles=mine_listicles,
        max_listicles=max_listicles,
        max_companies_per_listicle=max_companies_per_listicle,
        scope=scope,
    )
    return candidates


def discover_with_summary(
    search_client: SearchClient,
    country_code: str | None = None,
    role_family: str | None = None,
    dry_run: bool = False,
    num_results: int = 10,
    resolve_links: bool = False,
    http_client=None,
    llm_client=None,
    cache_dir=None,
    triage: bool = False,
    triage_batch_size: int = 10,
    identity_first: bool = False,
    max_baseline_queries: int = 8,
    max_identity_signals: int = 30,
    max_companies: int = 10,
    max_followup_queries_per_company: int = 3,
    expand_queries: bool = False,
    max_variants_per_baseline: int = 2,
    preferences: dict | None = None,
    segments: list[str] | None = None,
    mine_listicles: bool = False,
    max_listicles: int = 3,
    max_companies_per_listicle: int = 15,
    scope: SearchScope | None = None,
    gate_client: DecisionsClient | None = None,
) -> tuple[list[dict], DiscoveryRunSummary]:
    """Run a discovery pass and return candidates plus a run-level summary.

    The summary distinguishes known companies, alternate hiring URLs for known
    companies, and genuinely new candidates, and reports request usage.
    """
    scope = scope or load_scope()
    if identity_first:
        if not llm_client or not cache_dir:
            raise ValueError("identity_first discovery requires an analysis LLM client and cache directory")
        return _discover_identity_first(
            search_client,
            country_code,
            role_family,
            dry_run,
            num_results,
            resolve_links,
            http_client,
            llm_client,
            cache_dir,
            max_baseline_queries,
            max_identity_signals,
            max_companies,
            max_followup_queries_per_company,
            expand_queries,
            max_variants_per_baseline,
            preferences,
            segments,
            mine_listicles,
            max_listicles,
            max_companies_per_listicle,
            scope,
            gate_client,
        )
    queries = build_queries(country_code, role_family, scope)
    logger.info("Running %d search queries", len(queries))

    results_by_url: dict[str, tuple[SearchResult, str]] = {}
    search_failures = 0

    for query in queries:
        logger.debug("Query: %s", query)
        results = search_once(search_client, query, num_results)
        if results is None:
            search_failures += 1
            continue
        logger.debug("Got %d results", len(results))

        for result in results:
            if not result.url:
                continue
            if not _is_likely_hiring_source(result):
                logger.debug("Skipping non-hiring search result: %s", result.url)
                continue

            source_url = result.url
            dedup_key = source_url.rstrip("/").lower()
            if dedup_key in results_by_url:
                continue
            results_by_url[dedup_key] = (result, query)

    search_results = list(results_by_url.values())
    triage_by_id = (
        _triage_results([result for result, _ in search_results], llm_client, cache_dir, triage_batch_size)
        if triage and llm_client and cache_dir
        else {}
    )
    candidates: list[dict] = []
    seen: set[str] = set()

    for result_index, (result, query) in enumerate(search_results):
        triage_item = triage_by_id.get(result_index)
        if triage_item and triage_item.decision == "reject":
            logger.debug("Triage rejected search result: %s", result.url)
            continue

        source_url = result.url
        careers_url = source_url
        resolution_status = "unresolved"
        resolution_page_type = "unknown"
        if resolve_links and http_client:
            resolution = resolve_url(source_url, http_client, llm_client, cache_dir)
            careers_url = resolution.resolved_url
            resolution_status = resolution.status
            resolution_page_type = resolution.page_type
        dedup_key = careers_url.rstrip("/").lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Phase 4.1e: no ATS detection at discovery time — ats_platform is
        # filled at scrape time from the careers page itself.
        name = _extract_company_name(result)

        if name.lower() in KNOWN_AGGREGATORS:
            logger.debug("Skipping aggregator: %s (%s)", name, result.url)
            continue

        candidate = {
            "name": name,
            "country": country_code.upper() if country_code else None,
            "careers_url": careers_url,
            "source_url": source_url,
            "resolution_status": resolution_status,
            "resolution_page_type": resolution_page_type,
            "ats_platform": None,
            "source_query": query,
            "triage_decision": triage_item.decision if triage_item else None,
            "triage_source_type": triage_item.source_type if triage_item else None,
            "triage_confidence": triage_item.confidence if triage_item else None,
            "triage_evidence": json.dumps(triage_item.evidence, ensure_ascii=False) if triage_item else None,
            "triage_reason": triage_item.reason if triage_item else None,
        }
        candidates.append(candidate)

    logger.info("Found %d unique candidates", len(candidates))

    if not dry_run:
        session = get_session()
        inserted = 0
        try:
            for c in candidates:
                existing = (
                    session.query(Company)
                    .filter(Company.careers_url == c["careers_url"])
                    .first()
                )
                if not existing:
                    session.add(Company(**c))
                    inserted += 1
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        logger.info("Inserted %d new candidates (%d skipped as duplicates)", inserted, len(candidates) - inserted)

    summary = DiscoveryRunSummary(
        baseline_queries=len(queries),
        search_requests_used=len(queries),
        identity_signals=len(candidates),
        known_companies=0,
        known_alternate_urls=0,
        new_companies=len(candidates),
        followed_companies=0,
        sources_selected=len(candidates),
        candidates_persisted=len(candidates) if not dry_run else 0,
        dry_run=dry_run,
        search_failures=search_failures,
    )
    return candidates, summary


def review_discoveries(
    status_filter: str = "pending",
) -> list[Company]:
    session = get_session()
    try:
        if status_filter:
            results = (
                session.query(Company)
                .filter(Company.status == status_filter)
                .all()
            )
        else:
            results = session.query(Company).all()
        return list(results)
    finally:
        session.close()


def update_discovery_status(ids: list[int], status: str) -> int:
    session = get_session()
    try:
        values = {Company.status: status}
        if status == "accepted":
            values[Company.verification_status] = "verified"
            values[Company.verified_at] = datetime.now(UTC)
        count = (
            session.query(Company)
            .filter(Company.id.in_(ids))
            .update(values, synchronize_session=False)
        )
        session.commit()
        return count
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_CLEARABLE_STATUSES: tuple[str, ...] = ("pending", "accepted", "rejected")

# The demo review screen's bulk gate. ``pending`` is deliberately absent: it
# only ever means "put this back in the queue" on the single-row path.
_BATCH_STATUSES: tuple[str, ...] = ("accepted", "rejected")

# One request is one bounded transaction (plan-source-runs §API).
_BATCH_CAP = 500


def review_companies_batch(company_ids: list[int], status: str) -> tuple[int, list[int]]:
    """Bulk accept/reject for the review screen; returns (updated, unknown_ids).

    Status only — never a ``careers_url``. The entry a scraping run uses is
    evidence the reviewer sees, not something this endpoint can write, or the
    gate would be rubber-stamping a URL it never showed anyone.

    Repeated ids collapse (first-seen order kept). Ids that no longer exist are
    returned rather than dropped, so a stale selection refreshes instead of
    silently applying to fewer rows than the screen showed. Accepting verifies
    exactly as ``review_discovery`` does: ``accepted`` is the only scrape gate,
    and the two paths must produce the same row.
    """
    if status not in _BATCH_STATUSES:
        raise ValueError(f"batch review accepts {_BATCH_STATUSES}, not {status!r}")
    ordered = list(dict.fromkeys(company_ids))
    if len(ordered) > _BATCH_CAP:
        raise ValueError(f"at most {_BATCH_CAP} companies per batch review")
    if not ordered:
        return 0, []
    session = get_session()
    try:
        existing = [
            row[0]
            for row in session.query(Company.id).filter(Company.id.in_(ordered)).all()
        ]
        found = set(existing)
        unknown = [company_id for company_id in ordered if company_id not in found]
        values: dict = {Company.status: status}
        if status == "accepted":
            values[Company.verification_status] = "verified"
            values[Company.verified_at] = datetime.now(UTC)
        updated = 0
        if existing:
            updated = (
                session.query(Company)
                .filter(Company.id.in_(existing))
                .update(values, synchronize_session=False)
            )
        session.commit()
        return updated, unknown
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def clear_discovery_queue(status: str = "pending") -> int:
    """Delete removable ``companies`` rows, protecting ``known`` companies.

    ``known`` rows are durable, scraped sources and are never removed by
    ``clear``. ``all`` deletes only ``pending``, ``accepted``, and ``rejected``
    rows; a request to clear ``known`` is rejected.
    """
    if status == "known":
        raise ValueError("known companies are protected and cannot be cleared")
    session = get_session()
    try:
        query = session.query(Company)
        if status == "all":
            query = query.filter(Company.status.in_(_CLEARABLE_STATUSES))
        else:
            query = query.filter(Company.status == status)
        count = query.delete(synchronize_session=False)
        session.commit()
        return count
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def review_discovery(
    discovery_id: int,
    status: str,
    careers_url: str | None = None,
    open_positions_url: str | None = None,
) -> int:
    """Accept or reject one company, optionally writing its URLs.

    The owner may paste the careers page, the open-positions page, or both.
    When only one URL is given, the same page is accepted for both fields:
    the pasted page is the page that lists postings, so there is no second
    link to review. Passing neither leaves the stored URLs untouched.
    """
    session = get_session()
    try:
        discovery = session.query(Company).filter(Company.id == discovery_id).first()
        if not discovery:
            return 0
        if careers_url or open_positions_url:
            accepted_url = careers_url or open_positions_url
            discovery.careers_url = accepted_url
            discovery.open_positions_url = open_positions_url or accepted_url
        discovery.status = status
        if status == "accepted":
            discovery.verification_status = "verified"
            discovery.verified_at = datetime.now(UTC)
        session.commit()
        return 1
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_COMPANY_ORDERS: tuple[str, ...] = ("id", "id_desc")


def list_companies(
    *,
    status: str | None = None,
    statuses: Sequence[str] | None = None,
    country: str | None = None,
    no_careers: bool = False,
    has_entry: bool | None = None,
    order: str = "id",
    limit: int | None = None,
) -> list[Company]:
    """List ``companies`` rows, optionally filtered by status, country,
    whether a careers page is missing, and whether an entry was resolved.

    ``no_careers`` is the M1 selection list (plan-source-runs §Selection): rows
    with no ``careers_url``. An empty string counts as missing, because the
    column is nullable and older writers left it blank rather than NULL.
    ``has_entry`` is the demo review screen's view of the opposite question —
    whether 4.0d resolved an entry to scrape — and an empty string likewise
    counts as absent. ``order`` is ``id`` (insertion order) or ``id_desc``
    (newest first, which is the review screen's default request).

    ``statuses`` is the review screen's multi-status scope (``accepted`` +
    ``known`` for resolved entries); ``status`` keeps its single-value callers.

    Every returned row also carries two derived, non-column attributes read
    from its newest completed ``run_sources`` row (``company_id`` is set only on
    ``careers_search`` rows): ``entry_run_id`` and ``entry_resolved_at``. They
    are provenance for the review screen, never inputs — the entry itself is
    written only by ``record_open_positions_entry``.
    """
    if order not in _COMPANY_ORDERS:
        raise ValueError(f"order must be one of {_COMPANY_ORDERS}, not {order!r}")
    session = get_session()
    try:
        query = session.query(Company)
        if status:
            query = query.filter(Company.status == status)
        if statuses:
            query = query.filter(Company.status.in_(list(statuses)))
        if country:
            query = query.filter(Company.country == country.upper())
        if no_careers:
            # The selection list this flag drives is "companies that still need
            # a hiring source", not literally "no careers_url": a row the gate
            # accepted over a resolved entry is already scrapable.
            query = query.filter(~has_source_clause())
        if has_entry is not None:
            entry = and_(
                Company.open_positions_url.isnot(None),
                Company.open_positions_url != "",
            )
            query = query.filter(entry if has_entry else ~entry)
        query = query.order_by(Company.id.desc() if order == "id_desc" else Company.id)
        if limit is not None:
            query = query.limit(limit)
        companies = list(query.all())
        _attach_entry_runs(session, companies)
        _attach_scrape_state(session, companies)
        return companies
    finally:
        session.close()


def _attach_scrape_state(session, companies: Sequence[Company]) -> None:
    """Set ``last_scraped_at``/``last_scrape_status``/``last_scrape_run_id`` and
    ``job_count`` on each row. Derived, never inputs.

    The scrape fields come from the company's newest finished ``run_sources``
    row of a ``collect`` run (``careers_search`` rows share the table and must
    not count as a scrape). A company scraped before run evidence existed has
    none: ``last_scraped_at`` stays NULL, and ``job_count`` is the only trace.
    """
    if not companies:
        return
    ids = [company.id for company in companies]
    rows = (
        session.query(RunSource)
        .join(JobRun, JobRun.id == RunSource.run_id)
        .filter(
            RunSource.company_id.in_(ids),
            JobRun.run_type == "collect",
            RunSource.finished_at.isnot(None),
        )
        .order_by(RunSource.id.desc())
        .all()
    )
    newest: dict[int, RunSource] = {}
    for row in rows:
        newest.setdefault(row.company_id, row)
    counts = dict(
        session.query(Job.company_id, func.count(Job.id))
        .filter(Job.company_id.in_(ids))
        .group_by(Job.company_id)
        .all()
    )
    for company in companies:
        run_source = newest.get(company.id)
        company.last_scraped_at = run_source.finished_at if run_source else None
        company.last_scrape_status = run_source.status if run_source else None
        company.last_scrape_run_id = run_source.run_id if run_source else None
        company.last_scrape_dropped = _loads_dict(run_source.dropped) if run_source else None
        company.job_count = counts.get(company.id, 0)


def _loads_dict(text: str | None) -> dict | None:
    """A stored JSON object, or ``None`` for NULL or anything unreadable."""
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _attach_entry_runs(session, companies: Sequence[Company]) -> None:
    """Set ``entry_run_id``/``entry_resolved_at`` on each row from its newest
    completed ``run_sources`` row that actually resolved an entry.

    ``identities_found > 0`` is the resolving-run condition: a ``careers_search``
    row over a company that gained a ``careers_url`` mid-run also closes as
    ``completed``, with the skip reason in ``error`` and no entry written.
    """
    if not companies:
        return
    rows = (
        session.query(RunSource)
        .filter(
            RunSource.company_id.in_([company.id for company in companies]),
            RunSource.status == "completed",
            RunSource.identities_found > 0,
        )
        .order_by(RunSource.id.desc())
        .all()
    )
    newest: dict[int, RunSource] = {}
    for row in rows:
        newest.setdefault(row.company_id, row)
    for company in companies:
        run_source = newest.get(company.id)
        company.entry_run_id = run_source.run_id if run_source else None
        company.entry_resolved_at = run_source.finished_at if run_source else None


def count_companies() -> dict[str, dict[str, int]]:
    """Return company counts per status and per country."""
    session = get_session()
    try:
        by_status: dict[str, int] = {}
        for row in session.query(Company.status, func.count(Company.id)).group_by(Company.status):
            by_status[row[0]] = row[1]
        by_country: dict[str, int] = {}
        for row in session.query(Company.country, func.count(Company.id)).group_by(Company.country):
            key = row[0] or "unknown"
            by_country[key] = row[1]
        return {"by_status": by_status, "by_country": by_country}
    finally:
        session.close()


def get_company(company_id: int) -> Company | None:
    """Fetch a single ``companies`` row by id."""
    session = get_session()
    try:
        return session.query(Company).filter(Company.id == company_id).first()
    finally:
        session.close()


def add_company(
    name: str,
    careers_url: str,
    *,
    notes: str = "",
    country: str | None = None,
    ats_platform: str | None = None,
) -> Company:
    """Insert a manually curated company as a durable ``known`` row.

    A manual entry is a human-approved hiring source: it is recorded as
    ``known`` (not ``pending``) so it is scraped as part of the normal recurring
    set. It is protected from ``discover clear``.

    Phase 4.1e origin check: an ATS board URL is not a careers URL — a manual
    entry pointing at a board would be silently marked verified while skipping
    the employer-page delegation rule, so it is refused with guidance.
    """
    board = _classify_ats_url(careers_url)
    if board:
        raise ValueError(
            f"ATS board URL rejected as careers_url ({board}: {careers_url}) — "
            "use the employer's own careers page; a delegated board is discovered "
            "by scraping it, not stored as the source"
        )
    session = get_session()
    try:
        company = Company(
            name=name.strip(),
            careers_url=careers_url.strip(),
            notes=notes.strip(),
            country=country.upper() if country else None,
            ats_platform=ats_platform,
            status="known",
            verification_status="verified",
            verified_at=datetime.now(UTC),
        )
        session.add(company)
        session.commit()
        session.refresh(company)
        return company
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
