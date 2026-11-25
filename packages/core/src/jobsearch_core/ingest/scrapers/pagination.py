"""Next-page traversal for employer careers pages.

An employer careers page is the source of truth for its own listings, and the
only thing standing between page 1 and page 40 is a next-page affordance. This
module finds that affordance and reports *why* a crawl stopped, because "the
crawler returned 12 jobs" and "the crawler stopped because the button was
disabled" are very different facts for a benchmark to score.

Two families are handled:

``link``
    A real hyperlink to the next document — ``rel="next"``, a "Next" /
    "Next page" / "»" anchor, or a pager widget. Crawled with plain fetches.

``load_more``
    An in-page button ("Load more", "Show more jobs") that appends listings to
    the same document via XHR. There is no second URL to fetch, so detection
    reports the signal and the crawl stops with ``load_more_unsupported``.
    Acting on it needs a click loop in the browser tier, which this unit does
    not touch.

Stopping is enforced by four independent conditions, so no input can loop
forever:

1. ``no_signal`` / ``signal_disabled`` / ``offsite`` — nothing to follow.
2. ``repeat`` — the next URL was already visited in this crawl.
3. ``no_new_jobs`` — the page fetched but contributed no unseen job URL.
4. ``page_cap`` — the hard page ceiling was reached.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

# Anchors whose whole purpose is navigation between pages, not a job.
_PAGE_WORDS = re.compile(
    r"^(next(\s*(page|»|>|\u203a))?|older(\s*(posts|entries|jobs))?|"
    r"more(\s+(jobs|positions|results|openings))?|"
    r"load\s+more(\s+(jobs|positions|results))?|"
    r"show\s+more(\s+(jobs|positions|results))?|"
    r"view\s+more(\s+(jobs|positions|results))?|"
    r"»|>|next\s+›)$",
    re.IGNORECASE,
)

_PREV_WORDS = re.compile(
    r"^(prev(ious)?(\s*(page|»|>|\u203a))?|earlier|back|newer|"
    r"«|<|prev\s+‹|first|last|\d+)$",
    re.IGNORECASE,
)

_LOAD_MORE_WORDS = re.compile(
    r"(load\s+more|show\s+more|view\s+more|see\s+more|more\s+(jobs|positions|results))",
    re.IGNORECASE,
)

_PAGER_HINT = re.compile(r"(pagination|pager|page-nav|paging|page-link)", re.IGNORECASE)

# A "See more" / "Show more" button that expands a *filter* panel rather than
# loading more results. Zalando's job list has one of these next to its real
# pager, and it is indistinguishable by label alone — only by where it sits.
_FACET_HINT = re.compile(
    r"(filter|facet|sidebar|refine|categor|job-category|location-filter)",
    re.IGNORECASE,
)

_DISABLED_HINT = re.compile(r"(disabled|is-disabled|aria-disabled)", re.IGNORECASE)

_NEXT_ATTRS = re.compile(r"(next|forward|older)", re.IGNORECASE)

# Query parameters that only ever mean "which slice of the list".
_PAGE_PARAMS = frozenset(
    {
        "page",
        "p",
        "pagenum",
        "pagenumber",
        "page-number",
        "pageindex",
        "offset",
        "start",
        "from",
        "s",
        "size",
        "per_page",
        "per-page",
        "results_per_page",
    }
)

_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "ref",
        "ref_src",
        "source",
        "sort",
        "order",
        "orderby",
    }
)

_MAX_PAGES_DEFAULT = 20

# Terminal states. Anything not in this set is a bug in a caller.
TERMINAL_REASONS = frozenset(
    {
        "no_signal",
        "signal_disabled",
        "offsite",
        "repeat",
        "no_new_jobs",
        "page_cap",
        "fetch_error",
        "load_more_unsupported",
    }
)


@dataclass(frozen=True)
class NextPageSignal:
    """A next-page affordance found in a document.

    ``url`` is ``None`` when there is no second document to fetch: either an
    in-page control (``kind == "load_more"``, needs a browser click loop) or a
    control that has been determined to be exhausted/disabled
    (``kind == "disabled"``).
    """

    kind: str  # "link" | "load_more" | "disabled"
    url: str | None
    text: str
    source: str  # which detector fired: rel / label / pager / text / control

    @property
    def fetchable(self) -> bool:
        return self.kind == "link" and bool(self.url)


@dataclass
class CrawlResult:
    """Everything a benchmark needs to score one careers-page crawl."""

    start_url: str
    jobs: list[ScrapedJob] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    signals: list[NextPageSignal] = field(default_factory=list)
    stop_reason: str = "no_signal"
    stop_detail: str = ""
    #: URLs refused because they left the employer's host or path.
    refused: list[str] = field(default_factory=list)

    @property
    def pages_crawled(self) -> int:
        return len(self.pages)

    @property
    def followed(self) -> bool:
        return len(self.pages) > 1

    def to_dict(self) -> dict[str, object]:
        return {
            "start_url": self.start_url,
            "pages_crawled": self.pages_crawled,
            "pages": list(self.pages),
            "jobs": len(self.jobs),
            "job_urls": sorted({job.url for job in self.jobs}),
            "signals": [
                {"kind": s.kind, "source": s.source, "text": s.text, "url": s.url}
                for s in self.signals
            ],
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
            "refused": list(self.refused),
        }


def normalize_url(url: str) -> str:
    """Canonical form for equality: no fragment, no tracking params, no case.

    Two spellings of the same page must compare equal, or the visited-set guard
    misses its own loop and the crawl runs to the page cap instead of stopping
    at the repeat.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    if host.endswith(":443") and scheme == "https":
        host = host[: -len(":443")]
    elif host.endswith(":80") and scheme == "http":
        host = host[: -len(":80")]

    kept = [
        (k, v)
        for k, v in _pairs(parsed.query)
        if k.lower() not in _TRACKING_PARAMS
    ]
    query = "&".join(f"{k}={v}" for k, v in kept)
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunparse((scheme, host, path, "", query, ""))


def _pairs(query: str) -> list[tuple[str, str]]:
    return [(p.split("=", 1)[0], p.split("=", 1)[1] if "=" in p else "") for p in query.split("&") if p]


def is_pagination_link(a_tag: Tag) -> bool:
    """True when this anchor is page navigation, not a job posting.

    The URL alone cannot decide this: ``/jobs?page=2`` matches every job-link
    pattern in :mod:`jobsearch_core.ingest.scrapers.careers_page`, which is how
    a "Next" button used to get scraped as a job titled "Next".
    """
    rel = a_tag.get("rel") or []
    rel_text = " ".join(rel) if isinstance(rel, list) else str(rel)
    aria = str(a_tag.get("aria-label") or "")
    own_classes = " ".join(a_tag.get("class") or []) + " " + str(a_tag.get("id") or "")
    # Pager markup usually marks the wrapper (<nav class="pagination">) and
    # leaves the anchors bare, so ancestors count as pager context too.
    ancestor_classes = " ".join(
        cls for parent in a_tag.parents for el in [parent] for cls in (el.get("class") or [])
    )
    classes = own_classes + " " + ancestor_classes
    text = a_tag.get_text(" ", strip=True)

    if _NEXT_ATTRS.search(rel_text):
        return True
    if _PREV_WORDS.match(text) and _PAGER_HINT.search(classes):
        # A bare page number inside a pager widget is navigation.
        return True
    if _PAGE_WORDS.match(text):
        return True
    if _NEXT_ATTRS.search(aria) and not _PREV_WORDS.match(text):
        return True
    if _PAGER_HINT.search(own_classes) and (
        _NEXT_ATTRS.search(own_classes) or _NEXT_ATTRS.search(rel_text)
    ):
        return True
    return False


def find_next_page(html: str, current_url: str) -> NextPageSignal | None:
    """Locate the next-page affordance in ``html``.

    Detectors are ordered by trustworthiness: an explicit ``rel="next"`` beats a
    pager class name, which beats guessing from link text. The first match
    wins, so a page that offers both a real link and a decorative arrow uses the
    link.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. rel="next" on <link> (head) or <a> (body) — machine-readable intent.
    for tag in soup.find_all(["link", "a"], href=True):
        rel = tag.get("rel") or []
        rel_text = " ".join(rel) if isinstance(rel, list) else str(rel)
        if not re.search(r"(^|\s)next(\s|$)", rel_text, re.IGNORECASE):
            continue
        return NextPageSignal(
            kind="link",
            url=urljoin(current_url, tag["href"]),
            text=tag.get_text(" ", strip=True) or "next",
            source="rel",
        )

    # 2. An explicit aria-label on a real anchor.
    for a_tag in soup.find_all("a", href=True):
        aria = str(a_tag.get("aria-label") or "")
        if re.search(r"(next|older|load more|show more)", aria, re.IGNORECASE) and not re.search(
            r"(prev|previous|first|last)", aria, re.IGNORECASE
        ):
            return NextPageSignal(
                kind="link",
                url=urljoin(current_url, a_tag["href"]),
                text=aria,
                source="label",
            )

    # 3. Pager widgets: an anchor whose class/id marks it as navigation.
    #    A bare page number is never a next signal — it can only ever be the
    #    page you are already on — so "2" while sitting on page 2 is not
    #    followed. Only class/id/rel-marked anchors qualify here.
    for a_tag in soup.find_all("a", href=True):
        classes = " ".join(a_tag.get("class") or []) + " " + str(a_tag.get("id") or "")
        if not _PAGER_HINT.search(classes):
            continue
        if _PREV_WORDS.match(a_tag.get_text(" ", strip=True)) and not _NEXT_ATTRS.search(classes):
            continue
        if not _NEXT_ATTRS.search(classes):
            continue
        if _is_current_page(a_tag, current_url):
            continue
        return NextPageSignal(
            kind="link",
            url=urljoin(current_url, a_tag["href"]),
            text=a_tag.get_text(" ", strip=True) or "page",
            source="pager",
        )

    # 3b. Button-based pager: no href anywhere, so steps 2/3/5 cannot see it.
    #     Real example: Zalando ?page=2 is a <nav> of <button aria-label="Next
    #     page button">. The label is machine-readable intent, so it ranks with
    #     the link detectors rather than with the generic control scan in 4.
    for tag in soup.find_all("button"):
        aria = str(tag.get("aria-label") or tag.get("title") or "")
        if not re.search(r"\bnext\b", aria, re.IGNORECASE) or re.search(
            r"(prev|previous|first|last)", aria, re.IGNORECASE
        ):
            continue
        # Require the label to name a *page*, not just "next". Zalando's pager
        # is a <section> of buttons inside <main> with no nav and no pager
        # class, so ancestry cannot vouch for it — "Next page button" is
        # explicit enough on its own. A carousel's "Next" still is not.
        if not re.search(r"\bpage\b", aria, re.IGNORECASE):
            continue
        # A button pager has no href to read, but its step is still a plain
        # ?page= sibling: Zalando's buttons are wired to exactly that. Derive
        # it from the current URL so the crawler can advance without a browser.
        return NextPageSignal(
            kind="link",
            url=paginated_variant(current_url, _current_page(current_url) + 1),
            text=aria or tag.get_text(" ", strip=True) or "next",
            source="button_pager",
        )

    # 4. Button / <a> with no href whose label says load more: in-page control.
    for tag in soup.find_all(["button", "input", "a", "div", "span"]):
        if tag.name == "a" and tag.get("href"):
            continue
        label = tag.get_text(" ", strip=True) or str(
            tag.get("value") or tag.get("aria-label") or ""
        )
        if not label or len(label) > 60:
            continue
        if _LOAD_MORE_WORDS.search(label) and not _PREV_WORDS.match(label):
            if _in_facet_region(tag):
                # Expands the filter list beside the results. Following it
                # would never add a job, so it is not a next signal.
                continue
            return NextPageSignal(kind="load_more", url=None, text=label, source="control")

    # 5. Link text only ("Next", "»", "Show more jobs") on a real href.
    for a_tag in soup.find_all("a", href=True):
        text = a_tag.get_text(" ", strip=True)
        if not text or len(text) > 40:
            continue
        if _PREV_WORDS.match(text):
            continue
        if _PAGE_WORDS.match(text):
            href = urljoin(current_url, a_tag["href"])
            if normalize_url(href) == normalize_url(current_url):
                # The "Next" control points at the page we are already on: the
                # pager is exhausted. A self-referential next link is the
                # truthful end-of-list signal, not a link worth following.
                return NextPageSignal(
                    kind="disabled", url=None, text=text, source="self_ref"
                )
            return NextPageSignal(
                kind="link",
                url=href,
                text=text,
                source="text",
            )

    return None


def _current_page(url: str) -> int:
    """The ``?page=`` value currently requested, defaulting to 1.

    Used to step a button pager forward. Returns 1 for a URL with no page
    parameter or an unparseable one, so page-1 -> page-2 always works.
    """
    parsed = urlparse(url)
    for key, value in _pairs(parsed.query):
        if key.lower() in {"page", "p", "pagenum", "pagenumber"}:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 1
    return 1


def _in_facet_region(tag: Tag) -> bool:
    """True when ``tag`` sits inside a filter/facet panel rather than the results.

    Filter panels and result lists both say "See more"; only the surrounding
    markup distinguishes them. The walk uses ``.parents`` because a control is
    frequently the sole child of the element it belongs to.
    """
    for anc in tag.parents:
        if not isinstance(anc, Tag):
            continue
        # Facet evidence wins over any container stop: a <section> inside an
        # <aside class="...Filter"> is still a filter panel.
        if anc.name in ("aside", "form"):
            return True
        if _FACET_HINT.search(" ".join(anc.get("class") or []) + " " + str(anc.get("id") or "")):
            return True
        # Stop at the first structural container that holds the results.
        if anc.name in ("main", "nav") or anc.get("role") in ("main", "list"):
            return False
    return False


def _is_current_page(a_tag: Tag, current_url: str) -> bool:
    """True when a pager link points back at the page already being viewed."""
    return normalize_url(urljoin(current_url, a_tag["href"])) == normalize_url(current_url)


def is_disabled(node: Tag) -> bool:
    """A visible-but-unusable next control. Stopping here is a clean crawl."""
    if node.has_attr("disabled"):
        return True
    if str(node.get("aria-disabled") or "").lower() == "true":
        return True
    if str(node.get("tabindex") or "") == "-1":
        return True
    classes = " ".join(node.get("class") or [])
    if _DISABLED_HINT.search(classes):
        return True
    parent = node.find_parent(class_=_DISABLED_HINT)
    if parent is not None:
        return True
    return False


def next_link_is_disabled(html: str, signal: NextPageSignal, current_url: str) -> bool:
    """Resolve the anchor behind ``signal`` and test whether it is disabled.

    ``current_url`` is the base for resolving hrefs: a pager's href is often
    relative to the page it sits on, not to the absolute next-page URL.
    """
    if signal.kind == "disabled":
        return True
    if signal.url is None:
        # A load-more control: not exhausted, just not crawlable by URL.
        return False
    target = normalize_url(signal.url)
    soup = BeautifulSoup(html, "html.parser")
    for a_tag in soup.find_all(["a", "button"], href=True):
        if normalize_url(urljoin(current_url, a_tag["href"])) != target:
            continue
        return is_disabled(a_tag)
    # No matching anchor: a <link rel=next> or pager control we already accepted.
    return False


def same_careers_scope(start_url: str, candidate: str) -> bool:
    """Refuse to leave the employer's careers section.

    Three guards. Same host and scheme stop "Next" from walking into an
    unrelated site or a downgraded http link. Same first path segment stops a
    careers page from following a pager that actually belongs to a sibling
    section — ``/careers/jobs`` must not crawl into ``/blog?page=2`` — while
    still allowing every page under ``/careers/...``, which is how employers
    spread one job list across ``/careers/jobs``, ``/careers/openings`` and
    ``/careers?page=3``.
    """
    start = urlparse(start_url)
    nxt = urlparse(candidate)
    if start.netloc.lower() != nxt.netloc.lower():
        return False
    if start.scheme != nxt.scheme:
        return False
    return _section(start.path) == _section(nxt.path)


def _section(path: str) -> str:
    """The top-level section a path belongs to: ``/careers/...`` -> ``careers``."""
    for segment in (path or "/").split("/"):
        if segment:
            return segment.lower()
    return ""


def paginated_variant(url: str, page: int) -> str:
    """Build the ``?page=N`` sibling of ``url``.

    Used only as a last-resort detector for sites whose pager is plain links
    that the HTML detectors already catch; kept here so the benchmark can build
    a synthetic page-2 probe without a second copy of the query logic.
    """
    parsed = urlparse(url)
    pairs = _pairs(parsed.query)
    out: list[str] = []
    replaced = False
    for key, value in pairs:
        if key.lower() in {"page", "p", "pagenum", "pagenumber"}:
            out.append(f"{key}={page}")
            replaced = True
        else:
            out.append(f"{key}={value}" if value != "" else key)
    if not replaced:
        out.append(f"page={page}")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "&".join(out), ""))


def crawl_careers_pages(
    start_url: str,
    scrape: Callable[[str, str], list[ScrapedJob]],
    *,
    client: PoliteClient | None = None,
    max_pages: int = _MAX_PAGES_DEFAULT,
    start_html: str | None = None,
) -> CrawlResult:
    """Crawl ``start_url`` and every next page it advertises.

    ``scrape(html, url)`` is injected so this stays independent of which scraper
    handled the page: the generic careers-page scraper, or an adapter that
    replaced it. The traversal rules are the same either way.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")

    result = CrawlResult(start_url=start_url)
    visited: set[str] = {normalize_url(start_url)}
    seen_jobs: set[str] = set()

    url = start_url
    html = start_html
    close_client = client is None
    if client is None:
        client = PoliteClient()

    try:
        while True:
            if html is None:
                try:
                    html = client.fetch(url)
                except RuntimeError as exc:
                    result.stop_reason = "fetch_error"
                    result.stop_detail = f"{url}: {exc}"
                    break

            result.pages.append(url)
            found = scrape(html, url)

            new_urls: set[str] = set()
            for job in found:
                key = normalize_url(job.url)
                if key in seen_jobs:
                    continue
                seen_jobs.add(key)
                new_urls.add(key)
                result.jobs.append(job)

            if result.pages_crawled >= max_pages:
                result.stop_reason = "page_cap"
                result.stop_detail = f"reached max_pages={max_pages}"
                break

            signal = find_next_page(html, url)
            if signal is None:
                result.stop_reason = "no_signal"
                break

            result.signals.append(signal)

            if next_link_is_disabled(html, signal, url):
                result.stop_reason = "signal_disabled"
                result.stop_detail = signal.text or signal.url or signal.source
                break

            if not signal.fetchable:
                result.stop_reason = "load_more_unsupported"
                result.stop_detail = signal.text
                break

            nxt = normalize_url(signal.url or "")
            if not same_careers_scope(start_url, nxt):
                result.stop_reason = "offsite"
                result.stop_detail = signal.url or ""
                result.refused.append(signal.url or "")
                break

            if nxt in visited:
                result.stop_reason = "repeat"
                result.stop_detail = signal.url or ""
                break

            if not new_urls:
                result.stop_reason = "no_new_jobs"
                result.stop_detail = f"{url} contributed no unseen jobs"
                break

            visited.add(nxt)
            url = signal.url or url
            html = None
    finally:
        if close_client:
            client.close()

    if result.stop_reason not in TERMINAL_REASONS:  # pragma: no cover - guard
        raise AssertionError(f"unknown stop reason: {result.stop_reason}")

    logger.info(
        "Crawled %d page(s) from %s, %d unique job(s), stopped: %s (%s)",
        result.pages_crawled,
        start_url,
        len(result.jobs),
        result.stop_reason,
        result.stop_detail,
    )
    return result
