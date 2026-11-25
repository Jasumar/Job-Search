"""Replay verification for stored recipes (R3, plan-scraping.md §Recipe closure R3).

Three questions, three mechanisms, and only one of them may stop a run:

1. **Statistics** (fill rates, duplicate titles and URLs, page advance, detail
   success) are *routers*: each firing signal names where to look next in the
   recipe. A statistic never rejects a replay — the 2026-10-19 lesson is that
   every deterministic filter tried so far rejected correct work.
2. **DOM agreement** compares the replay's first page with what the rendered
   DOM actually shows. It answers open question 6 (an empty replay is an empty
   board or a did-not-navigate) and gives the report an independent reading of
   the same page.
3. **The posting-existence gate** asks a model one question — "is at least one
   of these items a job posting?" — over an adversarially selected sample whose
   worst-looking items come first. The model never sees the reference, and its
   analysis is written in terms of the items, never of what was expected.

Stop rule: a ``none`` verdict flags immediately; stopping additionally needs one
independent agreement — structural emptiness, or a repeat ``none`` for the same
recipe body (``stop_decision``). An unverifiable gate fails open.

Writes nothing. The caller stores a flag (``db/recipe_flags.py``) keyed by body
hash so a human never reads analysis against a re-authored recipe.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearch_core.ingest.llm_protocol import JsonCompleter
from jobsearch_core.ingest.recipe import ApiRecipe, DomRecipe
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper

# Fields a recipe may map that are not guaranteed by construction. Title and URL
# are not here: an emitted posting always has both, so their fill rate is 1.0.
_OPTIONAL_API_FIELDS = ("location", "posted_date", "employment_type", "seniority", "description")
_OPTIONAL_DOM_FIELDS = ("location",)

GATE_SAMPLE_LIMIT = 15
DOM_WINDOW_CHARS = 4000
_NONE = "none"
_POSTINGS = "postings"


def body_hash(body: dict | str) -> str:
    """Stable sha256 of a recipe body: key order and whitespace do not matter.

    The flag table is keyed by this hash, so two spellings of the same body must
    produce one key while any change to a path, selector, or template produces a
    different one.
    """
    data = json.loads(body) if isinstance(body, str) else body
    if not isinstance(data, dict):
        raise ValueError("a recipe body must be a JSON object")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- Statistics: routers, never judges -------------------------------------------


@dataclass(frozen=True)
class ReplayStatistics:
    """What one replay's output looks like, measured. Nothing here rejects it."""

    count: int
    location_filled: int = 0
    posted_date_filled: int = 0
    employment_type_filled: int = 0
    seniority_filled: int = 0
    description_filled: int = 0
    # Repeated occurrences beyond the first, so a clean list reads 0.
    duplicate_urls: int = 0
    duplicate_titles: int = 0
    page_counts: tuple[int, ...] = ()
    detail_attempted: int = 0
    detail_succeeded: int = 0

    @property
    def page_advance(self) -> bool:
        """True when a page after the first added a new posting."""
        return any(count > 0 for count in self.page_counts[1:])

    def filled(self, name: str) -> int:
        return int(getattr(self, f"{name}_filled"))

    def fill_rate(self, name: str) -> float | None:
        """Fraction of postings that filled ``name``; None when nothing was emitted."""
        if self.count == 0:
            return None
        return self.filled(name) / self.count


def collect_statistics(
    postings: Sequence[ScrapedJob],
    *,
    page_counts: Sequence[int] = (),
    detail_attempted: int = 0,
    detail_succeeded: int = 0,
) -> ReplayStatistics:
    """Measure a replay's output. Duplicate counts are occurrences beyond the first."""
    urls = [job.url.rstrip("/") for job in postings]
    titles = [" ".join(job.title.split()).casefold() for job in postings]
    return ReplayStatistics(
        count=len(postings),
        location_filled=sum(1 for job in postings if job.location),
        posted_date_filled=sum(1 for job in postings if job.posted_date),
        employment_type_filled=sum(1 for job in postings if job.employment_type),
        seniority_filled=sum(1 for job in postings if job.seniority),
        description_filled=sum(1 for job in postings if job.description),
        duplicate_urls=len(urls) - len(set(urls)),
        duplicate_titles=len(titles) - len(set(titles)),
        page_counts=tuple(page_counts),
        detail_attempted=detail_attempted,
        detail_succeeded=detail_succeeded,
    )


@dataclass(frozen=True)
class Signal:
    """One firing router: a measured value plus where in the recipe to look."""

    name: str
    value: str
    points_at: str


def _mapped_fields(recipe: ApiRecipe | DomRecipe) -> tuple[str, ...]:
    if isinstance(recipe, ApiRecipe):
        mapped = [name for name in _OPTIONAL_API_FIELDS if getattr(recipe.field_map, name) is not None]
        if recipe.detail is not None and "description" not in mapped:
            mapped.append("description")
        return tuple(mapped)
    return tuple(name for name in _OPTIONAL_DOM_FIELDS if getattr(recipe, name, None) is not None)


def signals(recipe: ApiRecipe | DomRecipe, stats: ReplayStatistics) -> tuple[Signal, ...]:
    """Every router this replay sets off, worst first. An empty tuple is a clean run.

    A statistic is a pointer, not a verdict: duplicate titles are also what a
    board with 24 postings all titled "Apply" looks like.
    """
    fired: list[Signal] = []
    if stats.duplicate_urls:
        fired.append(
            Signal(
                "duplicate_urls",
                str(stats.duplicate_urls),
                "container selector or list_path spans two lists: the same posting appears more than once",
            )
        )
    if stats.duplicate_titles:
        fired.append(
            Signal(
                "duplicate_titles",
                str(stats.duplicate_titles),
                "title selector/path may read a button or badge label; a board can also share one title",
            )
        )
    detail_failed = stats.detail_attempted > stats.detail_succeeded
    for name in _mapped_fields(recipe):
        if name == "description" and detail_failed:
            # The failure already names detail.url; "nothing filled description"
            # is the same fault and would send the reader to the wrong place.
            continue
        if stats.count and stats.filled(name) == 0:
            where = f"field_map.{name}" if isinstance(recipe, ApiRecipe) else f"{name} selector"
            fired.append(Signal(f"empty_{name}", "0", f"{where}: no posting filled this field"))
    if isinstance(recipe, ApiRecipe):
        pagination = recipe.pagination
        more_than_one_request = len(stats.page_counts) > 1
        if pagination.kind != "none" and more_than_one_request and not stats.page_advance:
            fired.append(
                Signal(
                    "pagination_no_advance",
                    str(list(stats.page_counts)),
                    f"pagination {pagination.kind}: param/step/cursor_path, or the site repeats page one",
                )
            )
        if stats.detail_attempted and stats.detail_succeeded < stats.detail_attempted:
            fired.append(
                Signal(
                    "detail_failures",
                    f"{stats.detail_succeeded}/{stats.detail_attempted}",
                    "detail.url template or description_paths",
                )
            )
    elif recipe.pagination.kind != "none":
        # A dom replay can page too (R5b's demo slice). Same router, different
        # knobs: the next anchor or the query parameter that was rewritten.
        if len(stats.page_counts) > 1 and not stats.page_advance:
            hint = (
                "selector (the next anchor) or param/step"
                if recipe.pagination.kind == "next"
                else "param/start/step"
            )
            fired.append(
                Signal(
                    "pagination_no_advance",
                    str(list(stats.page_counts)),
                    f"pagination {recipe.pagination.kind}: {hint}, or the site repeats page one",
                )
            )
    return tuple(fired)


# --- First-page-to-rendered-DOM agreement ---------------------------------------


@dataclass(frozen=True)
class DomAgreement:
    """The replay's first page against the rendered DOM's job links.

    ``missing`` are replayed URLs the DOM does not show (a replay reading
    something other than the page); ``extra`` are DOM links the replay did not
    produce (normal on a paginated page, so it is reported, never enforced).
    """

    replay_count: int
    rendered_count: int
    matched: int
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()

    @property
    def agrees(self) -> bool:
        """Subset rule, mirroring the class-A replay check: every replay URL is on the page."""
        return self.replay_count > 0 and not self.missing


def _norm_url(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def dom_agreement(
    postings: Sequence[ScrapedJob], rendered_html: str | None, page_url: str
) -> DomAgreement | None:
    """Compare a replay with the rendered page's job links. None when there is no render."""
    if rendered_html is None:
        return None
    rendered = CareersPageScraper().scrape_listings(rendered_html, page_url)
    replayed = {_norm_url(job.url) for job in postings}
    shown = {_norm_url(job.url) for job in rendered}
    return DomAgreement(
        replay_count=len(postings),
        rendered_count=len(rendered),
        matched=len(replayed & shown),
        missing=tuple(sorted(replayed - shown)),
        extra=tuple(sorted(shown - replayed)),
    )


EMPTY_BOARD = "empty_board"
NAVIGATION_FAILURE = "navigation_failure"
UNVERIFIABLE = "unverifiable"


def classify_empty_replay(agreement: DomAgreement | None) -> str:
    """Open question 6: an empty replay is an empty board or a did-not-navigate.

    The rendered DOM is the arbiter: it is the page after navigation, so job
    links there with nothing replayed means the recipe did not navigate or did
    not select. No render means no evidence, and the caller must not stop.
    """
    if agreement is None:
        return UNVERIFIABLE
    if agreement.rendered_count == 0:
        return EMPTY_BOARD
    return NAVIGATION_FAILURE


# --- Posting-existence gate -----------------------------------------------------


@dataclass(frozen=True)
class GateItem:
    """One item handed to the gate, with the reason it was selected."""

    url: str
    title: str
    posted_date: str | None
    why: str


def adversarial_selection(
    postings: Sequence[ScrapedJob], page_url: str, *, limit: int = GATE_SAMPLE_LIMIT
) -> tuple[GateItem, ...]:
    """Worst-first sample: the items most likely to be non-postings come first.

    Order is duplicated URL, duplicated title, the page itself, short title,
    empty date, then clean anchors up to ``limit``. Each item takes the first
    bucket it lands in, so a repeated URL that is also the page itself reads
    "duplicate url". The clean anchors keep the gate honest: a selection of only
    suspicious items invites a "none" verdict.
    """
    if limit < 1:
        raise ValueError("gate sample limit must be positive")
    urls = [_norm_url(job.url) for job in postings]
    titles = [" ".join(job.title.split()).casefold() for job in postings]
    url_counts = {url: urls.count(url) for url in set(urls)}
    title_counts = {title: titles.count(title) for title in set(titles)}
    self_url = _norm_url(page_url)

    buckets: list[tuple[str, list[int]]] = [
        ("duplicate url", [i for i, u in enumerate(urls) if url_counts[u] > 1]),
        ("duplicate title", [i for i, t in enumerate(titles) if title_counts[t] > 1]),
        ("page itself", [i for i, u in enumerate(urls) if u == self_url]),
        ("short title", [i for i, job in enumerate(postings) if len(job.title.strip()) < 6]),
        ("empty date", [i for i, job in enumerate(postings) if not job.posted_date]),
    ]
    selected: list[GateItem] = []
    taken: set[int] = set()
    for why, indexes in buckets:
        for index in indexes:
            if len(selected) >= limit:
                return tuple(selected)
            if index in taken:
                continue
            taken.add(index)
            job = postings[index]
            selected.append(GateItem(job.url, job.title, job.posted_date, why))
    for index, job in enumerate(postings):
        if len(selected) >= limit:
            break
        if index not in taken:
            taken.add(index)
            selected.append(GateItem(job.url, job.title, job.posted_date, "clean anchor"))
    return tuple(selected)


class GateError(Exception):
    """The gate's answer was unusable (malformed, or about items it was not given)."""


class _GateAnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class _GateAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["postings", "none"]
    items: list[_GateAnswerItem] = Field(default_factory=list)


@dataclass(frozen=True)
class GateAnalysisItem:
    url: str
    reason: str


@dataclass(frozen=True)
class GateVerdict:
    """The gate's answer. ``items`` explains a ``none``; it is empty for ``postings``."""

    verdict: Literal["postings", "none"]
    model_id: str
    items: tuple[GateAnalysisItem, ...] = ()

    @property
    def is_none(self) -> bool:
        return self.verdict == _NONE

    def analysis(self) -> dict:
        """The JSON a flag stores: the verdict and one sentence per judged item."""
        return {
            "verdict": self.verdict,
            "model_id": self.model_id,
            "items": [{"url": item.url, "reason": item.reason} for item in self.items],
        }


_SYSTEM = """You answer one question about a scraper's output: does it contain at \
least one real job posting?

You cannot see any reference list and you are not judging whether the scrape is
complete. Judge only the items and the page you are shown.

A job posting is a listing for one specific vacancy at this employer, normally a
role title with a link to its own page. Navigation, section, language, privacy
and search-filter links, pagination controls, login or apply buttons, and article
or blog cards are not postings. A shared title such as "Apply" does not
disqualify items that link to distinct vacancies, but one URL repeated is one
posting, not several.

Reply with JSON only, no prose:
{"verdict": "postings" or "none",
 "items": [{"url": "<the item url exactly as given>", "reason": "<one sentence>"}]}

If no item is a job posting, reply "none" and give every item you judged a
one-sentence reason saying what it is instead. If at least one item is a job
posting, reply "postings"; then items may be empty."""


def _shape(recipe: ApiRecipe | DomRecipe) -> str:
    if isinstance(recipe, ApiRecipe):
        fields = []
        for name in ("title", "url", *_OPTIONAL_API_FIELDS):
            spec = getattr(recipe.field_map, name, None)
            if spec is None:
                continue
            fields.append(f"{name}.path={spec.path}")
        return (
            f"api replay; list_path={recipe.list_path}; url_template={recipe.url_template!r}; "
            + ", ".join(fields)
        )
    return (
        f"dom replay; container={recipe.container!r}; "
        f"link.selector={recipe.link.selector if recipe.link else None!r}; "
        f"title.selector={recipe.title.selector!r}"
    )


def _dom_window(html: str, selector: str, *, max_chars: int = DOM_WINDOW_CHARS) -> str | None:
    """Outer HTML of the first containers the recipe selects, truncated."""
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select(selector)[:2]
    if not nodes:
        return None
    text = "\n".join(str(node) for node in nodes)
    return text[:max_chars]


def build_gate_prompt(
    recipe: ApiRecipe | DomRecipe,
    items: Sequence[GateItem],
    page_url: str,
    *,
    dom_html: str | None = None,
) -> tuple[str, str]:
    """The gate's system and user prompts. The reference is never included."""
    rows = [{"url": item.url, "title": item.title, "posted_date": item.posted_date} for item in items]
    parts = [
        f"Page: {page_url}",
        f"Recipe shape: {_shape(recipe)}",
        f"Items ({len(rows)}), selected worst-looking first:",
        json.dumps(rows, indent=2, ensure_ascii=False),
    ]
    window = None
    if dom_html is not None and isinstance(recipe, DomRecipe):
        window = _dom_window(dom_html, recipe.container)
    if window:
        parts.append("DOM around the containers the recipe selects:")
        parts.append(window)
    return _SYSTEM, "\n\n".join(parts)


def run_posting_gate(
    recipe: ApiRecipe | DomRecipe,
    postings: Sequence[ScrapedJob],
    page_url: str,
    llm: JsonCompleter,
    *,
    dom_html: str | None = None,
    limit: int = GATE_SAMPLE_LIMIT,
) -> GateVerdict:
    """Ask the model whether at least one emitted item is a real job posting.

    The sample is adversarial and worst-first. A ``none`` answer must name every
    judged item with a reason; a reason about a URL the gate was never shown is
    refused, so stored analysis can only be about real replay output.
    """
    if not postings:
        raise ValueError("the gate judges a non-empty replay; an empty one is classified, not gated")
    items = adversarial_selection(postings, page_url, limit=limit)
    system, user = build_gate_prompt(recipe, items, page_url, dom_html=dom_html)
    raw = llm.complete_json(system, user)
    try:
        answer = _GateAnswer.model_validate_json(raw)
    except ValidationError as exc:
        raise GateError(f"gate answer rejected: {exc}") from exc

    offered = {_norm_url(item.url) for item in items}
    judged: list[GateAnalysisItem] = []
    for row in answer.items:
        if _norm_url(row.url) not in offered:
            raise GateError(f"gate answer judges a URL it was not shown: {row.url!r}")
        judged.append(GateAnalysisItem(row.url, row.reason))
    if answer.verdict == _NONE and not judged:
        raise GateError("gate answered none without a per-item reason")
    return GateVerdict(answer.verdict, llm.model, tuple(judged))


# --- Cadence and the stop rule --------------------------------------------------


def should_run_gate(
    *, first_accepted: bool, signal_fired: bool, stop_cleared: bool
) -> bool:
    """The gate runs once on the first accepted replay, and again only on an event.

    Not every run: the cost of one model call per source per run buys nothing
    once a body has an answer that has not been invalidated.
    """
    return first_accepted or signal_fired or stop_cleared


def structural_empty(postings: Sequence[ScrapedJob]) -> bool:
    """True when no emitted item passes the cheapest fill checks (URL and title)."""
    return not any(job.url.strip() and job.title.strip() for job in postings)


def stop_decision(
    *, verdict: str, structural_empty: bool = False, prior_none: bool = False
) -> bool:
    """Whether a ``none`` verdict stops the run.

    Flagging is immediate; stopping needs an independent agreement. Structural
    emptiness is one; a repeat ``none`` for the same recipe body is the other.
    Anything that is not a ``none`` verdict never stops.
    """
    if verdict != _NONE:
        return False
    return structural_empty or prior_none


__all__ = [
    "DOM_WINDOW_CHARS",
    "EMPTY_BOARD",
    "GATE_SAMPLE_LIMIT",
    "NAVIGATION_FAILURE",
    "UNVERIFIABLE",
    "DomAgreement",
    "GateAnalysisItem",
    "GateError",
    "GateItem",
    "GateVerdict",
    "ReplayStatistics",
    "Signal",
    "adversarial_selection",
    "body_hash",
    "build_gate_prompt",
    "classify_empty_replay",
    "collect_statistics",
    "dom_agreement",
    "should_run_gate",
    "signals",
    "stop_decision",
    "structural_empty",
]
