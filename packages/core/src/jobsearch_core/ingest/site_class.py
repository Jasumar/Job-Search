"""Website-type classifier (Phase 4.3): how should a careers/listing page be scraped?

Deterministic-first. ``classify_site`` probes the page for evidence of each
class and returns all classes ranked most-likely first:

  A  known backend JSON API (ATS board URL, ATS links, Oracle HCM markers)
  B  embedded structured data (schema.org/JobPosting in JSON-LD or microdata)
  C  server-rendered HTML job links
  D  JS/SPA shell that needs a rendered DOM
  E  blocked or unusable page (dropped edge case)

The ranking is also the trial order for scraping. Only an ambiguous or
unknown profile is sent to Jev (``resolve_with_jev``); a clear deterministic
result is never overridden. Jev is never a hard dependency: on failure the
deterministic profile is returned unchanged.

Invariants (docs/plan-scraping.md): the classifier recommends and does not
decide; it never promotes an ATS board to a discovery source or
``careers_url``; a class is persisted only once resolved, never guessed.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from sqlalchemy import func, select

from jobsearch_core.db.models import Company, CompanyXhrCapture
from jobsearch_core.db.session import get_session
from jobsearch_core.ingest.html_text import html_to_text
from jobsearch_core.ingest.replay import PlainFetcher, ReplayResult, replay_capture
from jobsearch_core.ingest.scrapers.ats_links import _classify_ats_url, _find_ats_board_links
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.oracle_hcm import OracleHcmScraper
from jobsearch_core.ingest.xhr_capture import XhrCapture
from jobsearch_core.log import get_logger

logger = get_logger(__name__)


class JevGate(Protocol):
    """Anything exposing Jev's ``ask(state, questions) -> answers``.

    ``DecisionsClient`` satisfies this; tests substitute a fake.
    """

    def ask(self, state: dict | str, questions: dict) -> dict: ...

CLASSES: tuple[str, ...] = ("A", "B", "C", "D", "E")
# Chain order A→B→C→D→E: the deterministic tie-break for equal scores.
_CHAIN_INDEX = {c: i for i, c in enumerate(CLASSES)}

UNKNOWN_FLOOR = 0.3  # best class below this = no usable evidence
AMBIGUITY_MARGIN = 0.15  # top two closer than this = tie, Jev decides
JEV_ACCEPT = 0.5  # Jev score a class needs to be resolved
MIN_JOB_LINKS = 3
SPA_MAX_TEXT = 400
JEV_EXCERPT_CHARS = 6000

_JSON_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_MICRODATA_JOB_RE = re.compile(r"itemtype=[\"']https?://schema\.org/JobPosting[\"']", re.IGNORECASE)
_SPA_MARKER_RE = re.compile(
    r"id=[\"'](root|app|__next|__nuxt)[\"']|__NEXT_DATA__|__NUXT__|ng-version=|"
    r"data-reactroot|window\.__INITIAL_STATE__",
    re.IGNORECASE,
)
_BLOCK_MARKER_RE = re.compile(
    r"cf-chl|just a moment|verify you are human|enable javascript and cookies",
    re.IGNORECASE,
)
# A bot-check page is a short page whose title/heading/short text says so. A marker
# inside a long text block (a job description, an embedded config blob) is not one.
_BLOCK_TEXT_MAX_CHARS = 300

_CLASS_DESCRIPTIONS = {
    "A": "a page that lists jobs from a known applicant-tracking system backend or links to one",
    "B": "a page with embedded structured job postings (schema.org JobPosting data)",
    "C": "a server-rendered page that lists individual job openings as HTML links",
    "D": "a JavaScript app shell whose job listings only appear after the page renders",
    "E": "a login wall, bot-check, PDF-only or otherwise unusable page with no job listings",
}


@dataclass(frozen=True)
class ClassScore:
    site_class: str
    score: float
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaptureVerdict:
    capture: XhrCapture
    result: ReplayResult


@dataclass(frozen=True)
class SiteProfile:
    url: str
    ranked: tuple[ClassScore, ...]  # every class, most likely first
    ambiguous: bool
    unknown: bool
    jev_consulted: bool = False
    jev_scores: dict[str, float] | None = field(default=None, compare=False)
    # True when the profile was re-probed on a browser-rendered DOM (Class D gate).
    rendered: bool = False
    # Why no XHR capture replayed to A (S3); empty when A was decided or no captures were tried.
    replay_reasons: tuple[str, ...] = ()
    # Every capture the render made, with its replay verdict (S4 evidence rows).
    capture_results: tuple[CaptureVerdict, ...] = ()
    # The browser-rendered DOM the profile was probed on (Class D render only).
    # The scrape path uses it in place of the static HTML (S3 wiring, 2026-10-14),
    # so the verdict and the scrapers see the same page.
    rendered_html: str | None = field(default=None, compare=False, repr=False)

    @property
    def resolved_class(self) -> str | None:
        """The class to persist, or None while the profile is ambiguous or unknown."""
        if self.ambiguous or self.unknown:
            return None
        return self.ranked[0].site_class

    def as_evidence(self) -> dict:
        return {
            "url": self.url,
            "ranked": [
                {"class": cs.site_class, "score": cs.score, "signals": list(cs.signals)}
                for cs in self.ranked
            ],
            "ambiguous": self.ambiguous,
            "unknown": self.unknown,
            "jev_consulted": self.jev_consulted,
            "jev_scores": self.jev_scores,
            "replay_reasons": list(self.replay_reasons),
        }


def _json_ld_job_postings(html: str) -> int:
    """Count JobPosting nodes inside JSON-LD blocks (handles @graph and lists)."""

    def walk(node: object) -> int:
        if isinstance(node, list):
            return sum(walk(item) for item in node)
        if not isinstance(node, dict):
            return 0
        found = 0
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "JobPosting" in types:
            found += 1
        for value in node.values():
            if isinstance(value, (dict, list)):
                found += walk(value)
        return found

    total = 0
    for block in _JSON_LD_RE.findall(html):
        try:
            total += walk(json.loads(block))
        except ValueError:
            continue
    return total


def _job_link_count(html: str, url: str) -> int:
    return len(CareersPageScraper().scrape_listings(html, url))


def _score_a(url: str, html: str) -> ClassScore:
    candidates: list[tuple[float, str]] = []
    platform = _classify_ats_url(url)
    if platform:
        candidates.append((1.0, f"ats_url:{platform}"))
    board_links = _find_ats_board_links(html, url)
    if board_links:
        candidates.append((0.9, f"ats_board_links:{len(board_links)}"))
    if OracleHcmScraper().matches(url, html):
        candidates.append((0.9, "oracle_hcm_markers"))
    if not candidates:
        return ClassScore("A", 0.0)
    best = max(score for score, _ in candidates)
    return ClassScore("A", best, tuple(sig for _, sig in candidates))


def _score_b(html: str) -> ClassScore:
    postings = _json_ld_job_postings(html)
    if postings:
        return ClassScore("B", 0.9, (f"json_ld_jobposting:{postings}",))
    if _MICRODATA_JOB_RE.search(html):
        return ClassScore("B", 0.7, ("microdata_jobposting",))
    return ClassScore("B", 0.0)


def _score_c(job_links: int) -> ClassScore:
    if job_links >= MIN_JOB_LINKS:
        return ClassScore("C", min(1.0, 0.5 + job_links / 20), (f"job_links:{job_links}",))
    return ClassScore("C", 0.0)


def _score_d(html: str, text_len: int, job_links: int) -> ClassScore:
    if not _SPA_MARKER_RE.search(html) or text_len >= SPA_MAX_TEXT:
        return ClassScore("D", 0.0)
    if job_links < MIN_JOB_LINKS:
        return ClassScore("D", 0.8, ("spa_marker", f"text_chars:{text_len}"))
    # Listings already present in the shell: a render may still help, weakly.
    return ClassScore("D", 0.3, ("spa_marker", f"job_links:{job_links}"))


def _block_marker_text(html: str) -> str:
    """Text a bot-check marker may legitimately appear in.

    Title, h1 headings, and visible text blocks under _BLOCK_TEXT_MAX_CHARS.
    Script/style/noscript bodies are dropped first. A marker inside a long
    block (NVIDIA's careers shell carries "recaptchaEnabled" in an embedded
    config blob) is not a bot check (live probe, 2026-10-11).
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string)
    parts.extend(h.get_text(" ", strip=True) for h in soup.find_all("h1"))
    for block in soup.find_all(string=True):
        text = " ".join(block.split())
        if text and len(text) <= _BLOCK_TEXT_MAX_CHARS:
            parts.append(text)
    return " ".join(parts)


def _score_e(html: str, text_len: int) -> ClassScore:
    if _BLOCK_MARKER_RE.search(_block_marker_text(html)):
        return ClassScore("E", 0.9, ("block_marker",))

    if text_len < 50 and not _SPA_MARKER_RE.search(html):
        return ClassScore("E", 0.6, (f"empty_page:{text_len}",))
    return ClassScore("E", 0.0)


def _rank(scores: list[ClassScore]) -> tuple[tuple[ClassScore, ...], bool, bool]:
    ranked = tuple(sorted(scores, key=lambda cs: (-cs.score, _CHAIN_INDEX[cs.site_class])))
    top = ranked[0].score
    second = ranked[1].score if len(ranked) > 1 else 0.0
    unknown = top < UNKNOWN_FLOOR
    ambiguous = (not unknown) and second >= UNKNOWN_FLOOR and (top - second) < AMBIGUITY_MARGIN
    return ranked, ambiguous, unknown


def classify_site(url: str, html: str) -> SiteProfile:
    """Probe ``html`` for each class and return all classes ranked. No network I/O."""
    text_len = len(html_to_text(html))
    job_links = _job_link_count(html, url)
    scores = [
        _score_a(url, html),
        _score_b(html),
        _score_c(job_links),
        _score_d(html, text_len, job_links),
        _score_e(html, text_len),
    ]
    ranked, ambiguous, unknown = _rank(scores)
    return SiteProfile(url=url, ranked=ranked, ambiguous=ambiguous, unknown=unknown)


def _replay_verdict(
    captures: list[XhrCapture],
    rendered_postings: list[ScrapedJob],
    company_host: str,
    fetch_plain: Callable[[str], str],
) -> tuple[ReplayResult | None, tuple[str, ...], tuple[CaptureVerdict, ...]]:
    """Replay every capture; return the first passing replay, the failure reasons, and all verdicts.

    Every capture is replayed (owner decision 2026-10-14) so each one gets its own
    evidence row. The first replay that matches the rendered postings is the A
    evidence; when none matches, the reasons explain the non-A verdict.
    """
    verdicts: list[CaptureVerdict] = []
    first_pass: ReplayResult | None = None
    reasons: list[str] = []
    for capture in captures:
        result = replay_capture(
            capture,
            dom_postings=rendered_postings,
            company_host=company_host,
            fetch_plain=fetch_plain,
        )
        verdicts.append(CaptureVerdict(capture, result))
        if result.class_a_candidate and first_pass is None:
            first_pass = result
        elif not result.class_a_candidate:
            reasons.append(f"{capture.url}: {result.reason}")
    if not captures:
        reasons.append("no xhr captures on the rendered page")
    return first_pass, tuple(reasons), tuple(verdicts)


def _with_capture_verdict(profile: SiteProfile, replay: ReplayResult) -> SiteProfile:
    """Rank A first on a verified replay; the replay is a decided verdict, not a score tie.

    The rendered listings also score C, which would otherwise make A and C tie
    and leave the profile ambiguous. The replay already proves the listed jobs
    came from a backend API, so A is decided here.
    """
    signals = (f"xhr_replay:{replay.matched_count}/{replay.replayed_count}",)
    scores = [cs for cs in profile.ranked if cs.site_class != "A"]
    scores.append(ClassScore("A", 1.0, signals))
    ranked, _, _ = _rank(scores)
    return dataclasses.replace(profile, ranked=ranked, ambiguous=False, unknown=False)


def classify_with_render(
    url: str,
    html: str,
    fetch_rendered: Callable[[str], str],
    *,
    fetch_rendered_with_xhr: Callable[[str], tuple[str, list[XhrCapture]]] | None = None,
    fetch_plain: Callable[[str], str] | None = None,
) -> SiteProfile:
    """Class D gate: when the static page needs a render, re-probe the rendered DOM.

    Runs only when the static profile ranks D first or is unknown (a plain page
    never costs a browser start). The rendered DOM is probed with the same
    deterministic chain, so a page whose listings appear after render is
    classified by what it now shows (typically C, or A when the rendered page
    links an ATS board). A render failure returns the static profile unchanged.

    When ``fetch_rendered_with_xhr`` is given, the render also returns the page's
    JSON/XHR captures. A capture whose plain replay matches the rendered postings
    (``replay_capture``) ranks A; without one the verdict stays as above and the
    replay reasons are recorded on the profile. The capture never decides A alone.
    """
    profile = classify_site(url, html)
    if not (profile.unknown or profile.ranked[0].site_class == "D"):
        return profile
    captures: list[XhrCapture] = []
    try:
        if fetch_rendered_with_xhr is not None:
            rendered, captures = fetch_rendered_with_xhr(url)
        else:
            rendered = fetch_rendered(url)
    except Exception as exc:  # noqa: BLE001 — a failed render must not break classification
        logger.warning("Browser render failed for %s; keeping static profile: %s", url, exc)
        return profile
    if not rendered:
        return profile
    rendered_profile = dataclasses.replace(
        classify_site(url, rendered), rendered=True, rendered_html=rendered
    )
    if fetch_rendered_with_xhr is None:
        return rendered_profile

    host = urlparse(url).hostname or ""
    rendered_postings = CareersPageScraper().scrape_listings(rendered, url)
    if fetch_plain is None:
        with PlainFetcher() as fetcher:
            replay, reasons, verdicts = _replay_verdict(captures, rendered_postings, host, fetcher)
    else:
        replay, reasons, verdicts = _replay_verdict(captures, rendered_postings, host, fetch_plain)
    rendered_profile = dataclasses.replace(rendered_profile, capture_results=verdicts)
    if replay is not None:
        return _with_capture_verdict(rendered_profile, replay)
    return dataclasses.replace(rendered_profile, replay_reasons=reasons)


def _jev_questions() -> dict:
    # One noul-typed question per class: the only question type the existing
    # Jev callers use (search/chunking.py), so the request shape is proven.
    return {
        f"is_class_{cls.lower()}": {
            "type": "noul",
            "instructions": (
                "Does `page_text` show that the page is "
                f"{description}? Answer with a score from 0 to 1."
            ),
        }
        for cls, description in _CLASS_DESCRIPTIONS.items()
    }


def resolve_with_jev(profile: SiteProfile, html: str, gate: JevGate) -> SiteProfile:
    """Let Jev break a tie or classify an unknown page. Clear profiles pass through.

    On any Jev failure the deterministic profile is returned unchanged, with
    ``jev_consulted`` False, so the caller never depends on Jev being up.
    """
    if not (profile.ambiguous or profile.unknown):
        return profile
    state = {
        "page_text": html_to_text(html)[:JEV_EXCERPT_CHARS],
        "url": profile.url,
    }
    try:
        answers = gate.ask(state, _jev_questions())
        jev_scores = {
            cls: float(answers[f"is_class_{cls.lower()}"]["noul"]) for cls in CLASSES
        }
    except Exception as exc:  # noqa: BLE001 — Jev must never break classification
        logger.warning("Jev site classification failed for %s: %s", profile.url, exc)
        return profile

    scores = [ClassScore(cls, jev_scores[cls], ("jev",)) for cls in CLASSES]
    ranked, ambiguous, unknown = _rank(scores)
    top = ranked[0]
    second = ranked[1].score
    resolved = top.score >= JEV_ACCEPT and (top.score - second) >= AMBIGUITY_MARGIN
    return SiteProfile(
        url=profile.url,
        ranked=ranked,
        ambiguous=not resolved,
        unknown=top.score < JEV_ACCEPT,
        jev_consulted=True,
        jev_scores=jev_scores,
    )


def record_capture_rows(company_id: int, profile: SiteProfile, *, captured_at: datetime) -> int:
    """Append this render's capture verdicts as a new batch (R1).

    One row per capture the render made, one ``capture_batch`` per render. The
    previous batches are kept: the table is a timeline, so run N can be compared
    to run N-1 (plan-scraping.md §Recipe closure R1). ``capture_batch`` is
    ``max(existing) + 1`` for the company, assigned in the same transaction as
    the insert, so a batch is never half-numbered.

    Nothing is written when no render ran (``profile.rendered`` is False): the
    static-HTML path observes no XHR, and a batch would be evidence of a render
    that never happened. A render that observed no job-list XHR also writes no
    rows — that gap is not representable in this table without a per-render
    marker row, which R1 does not add (reported as an open item).

    Evidence only; this never touches ``companies.site_class``. Returns the
    number of rows written.
    """
    if not profile.rendered:
        return 0
    session = get_session()
    try:
        company = session.get(Company, company_id)
        if company is None:
            return 0
        last_batch = session.scalar(
            select(func.max(CompanyXhrCapture.capture_batch)).where(
                CompanyXhrCapture.company_id == company_id
            )
        )
        batch = (last_batch or 0) + 1
        for verdict in profile.capture_results:
            session.add(
                CompanyXhrCapture(
                    company_id=company_id,
                    captured_at=captured_at,
                    url=verdict.capture.url,
                    method=verdict.capture.method,
                    status=verdict.capture.status,
                    response_bytes=verdict.capture.response_bytes,
                    sample_titles=json.dumps(list(verdict.result.sample_titles), ensure_ascii=False)
                    if verdict.result.sample_titles
                    else None,
                    replay_matches=verdict.result.replay_matches,
                    replay_reason=verdict.result.reason,
                    class_a_candidate=verdict.result.class_a_candidate,
                    content_type=verdict.capture.content_type or None,
                    body_sha256=verdict.capture.body_sha256 or None,
                    body_truncated=verdict.capture.body_truncated,
                    capture_batch=batch,
                )
            )
        session.commit()
        return len(profile.capture_results)
    finally:
        session.close()


def record_site_class(company_id: int, profile: SiteProfile) -> bool:
    """Persist a resolved class and its evidence on the company row.

    Writes nothing for an ambiguous or unknown profile: a guessed class is
    never stored. Returns True when a row was written.
    """

    resolved = profile.resolved_class
    if resolved is None:
        return False
    session = get_session()
    try:
        company = session.get(Company, company_id)
        if company is None:
            return False
        company.site_class = resolved
        company.site_class_evidence = json.dumps(profile.as_evidence(), ensure_ascii=False)
        session.commit()
        return True
    finally:
        session.close()
