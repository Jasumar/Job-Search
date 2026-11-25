from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.scrapers.pipeline import _classify_ats_url
from jobsearch_core.llm.cache import load_cached, save_cached
from jobsearch_core.llm.client import LLMClient
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

RESOLVE_SYSTEM = (
    "Classify fetched web pages for job-search discovery. Choose the best URL only from the supplied URLs. "
    "A careers page lists jobs or explains hiring; a job page describes one specific vacancy; "
    "dead_or_unrelated means the page is unavailable or not a hiring page. Return JSON with keys: "
    "selected_url (string or null), page_type (careers, job, dead_or_unrelated), confidence (0.0-1.0)."
)


class ResolveResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    selected_url: str | None = None
    page_type: str = "dead_or_unrelated"
    confidence: float = Field(default=0, ge=0, le=1)


@dataclass
class ResolvedUrl:
    source_url: str
    resolved_url: str
    status: str
    page_type: str


def _parent_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    # Phase 4.1e: no ATS-root canonicalization. Search and resolution never
    # target ATS boards; a job-level URL simply degrades to its path parents.
    candidates = [url.rstrip("/")]
    for length in range(len(parts) - 1, 0, -1):
        candidates.append(urlunparse((parsed.scheme, parsed.netloc, "/" + "/".join(parts[:length]), "", "", "")))
    return list(dict.fromkeys(candidates))


def _page_score(url: str, html: str) -> tuple[int, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    headings = " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all(["h1", "h2", "h3"]))
    links = " ".join(tag.get_text(" ", strip=True) for tag in soup.find_all("a"))
    text = f"{title} {headings} {links} {soup.get_text(' ', strip=True)}".lower()
    path = urlparse(url).path.lower()
    score = 0
    if any(term in path for term in ("career", "jobs", "hiring", "vacancies")):
        score += 4
    if any(term in text for term in ("careers", "open positions", "job openings", "join our team", "vacancies")):
        score += 3
    if re.search(r"\b(job|position|role)\b", text):
        score += 1
    if len(text) < 120:
        score -= 3
    page_type = "careers" if score >= 4 else "job" if score >= 1 else "dead_or_unrelated"
    return score, page_type


def _llm_resolve(
    pages: dict[str, str],
    client: LLMClient,
    cache_dir: Path,
) -> tuple[str | None, str]:
    page_text = []
    for url, html in pages.items():
        soup = BeautifulSoup(html, "html.parser")
        excerpt = soup.get_text(" ", strip=True)[:2000]
        page_text.append(f"URL: {url}\nCONTENT: {excerpt}")
    user_prompt = "Fetched candidate pages:\n\n" + "\n\n".join(page_text)
    cached = load_cached(cache_dir, RESOLVE_SYSTEM, user_prompt)
    raw = cached if cached is not None else client.complete_json(RESOLVE_SYSTEM, user_prompt)
    if cached is None:
        save_cached(cache_dir, RESOLVE_SYSTEM, user_prompt, raw)
    result = ResolveResponse.model_validate_json(raw)
    selected = result.selected_url
    if selected not in pages:
        selected = None
    return selected, result.page_type


def resolve_url(
    url: str,
    http_client: PoliteClient,
    llm_client: LLMClient | None = None,
    cache_dir: Path | None = None,
) -> ResolvedUrl:
    pages: dict[str, str] = {}
    scores: dict[str, tuple[int, str]] = {}
    for candidate in _parent_candidates(url):
        # Phase 4.1e: an ATS board URL is never a resolvable target — search
        # and resolution never end on a board (§Employer Pages Over ATS
        # Boards). Skip it instead of scoring it, so it cannot win selection
        # or feed the LLM chooser.
        if _classify_ats_url(candidate):
            logger.debug("Resolver skips ATS board candidate %s", candidate)
            continue
        try:
            html = http_client.fetch(candidate)
        except Exception as exc:
            logger.debug("Could not fetch resolver candidate %s: %s", candidate, exc)
            continue
        pages[candidate] = html
        scores[candidate] = _page_score(candidate, html)

    if not pages:
        return ResolvedUrl(url, url, "unresolved", "dead_or_unrelated")

    ranked = sorted(scores, key=lambda candidate: scores[candidate][0], reverse=True)
    best = ranked[0]
    best_score, best_type = scores[best]
    tied = len(ranked) > 1 and scores[ranked[1]][0] == best_score
    if llm_client and cache_dir and (best_type == "dead_or_unrelated" or tied):
        try:
            selected, page_type = _llm_resolve(pages, llm_client, cache_dir)
            if selected:
                return ResolvedUrl(url, selected, "resolved", page_type)
        except Exception:
            logger.exception("LLM URL resolution failed for %s", url)

    return ResolvedUrl(url, best, "resolved" if best_type != "dead_or_unrelated" else "unresolved", best_type)
