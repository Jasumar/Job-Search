"""Reading a sitemap: the XML layer, with no judgement about its contents.

A sitemap is a file of URLs, either a ``urlset`` (pages) or a ``sitemapindex``
(children sitemaps). This module answers one question — what URLs does this
file list, and how much of it did we actually read — and nothing else.
Whether those URLs are job postings, and whether the file is therefore a
listing in its own right, is decided in ``entry_resolution`` (hint vs.
listing) and used again at scrape time (``pipeline``).

Bounded by design (plan-scraping.md §Sitemap listings): one index level, at
most ``MAX_CHILDREN`` child sitemaps and ``MAX_LOCS`` URLs, and every cut is
reported as ``truncated`` so a short read never looks like a complete one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

from jobsearch_core.log import get_logger

logger = get_logger(__name__)

#: Child sitemaps read from one index. A chain of indexes would be a crawl.
MAX_CHILDREN = 5
#: URLs kept from one sitemap read (root plus children).
MAX_LOCS = 5000

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_INDEX = re.compile(r"<sitemapindex[\s>]", re.IGNORECASE)


@dataclass(frozen=True)
class SitemapDocument:
    """What one sitemap read produced.

    ``children`` counts the child sitemaps actually read (0 for a urlset);
    ``truncated`` is set when a cap cut the read short, so the caller can
    report the loss instead of presenting a slice as the whole file.
    """

    url: str
    locs: list[str]
    children: int = 0
    truncated: bool = False
    is_index: bool = False


def is_sitemap_document(body: str) -> bool:
    """True when *body* is a sitemap urlset or sitemapindex, not an HTML page.

    The check is content-based, not URL-based: an entry URL that happens to
    end in ``.xml`` may still answer with an HTML page, and XML must never
    reach an HTML extractor.
    """
    head = body[:4096].lower()
    return "<urlset" in head or bool(_INDEX.search(head))


def read_sitemap(client: object, url: str, *, body: str | None = None) -> SitemapDocument:
    """Read a sitemap, one level into an index, within the caps.

    ``body`` lets a caller that already fetched the root reuse that response
    instead of paying for a second request. Any child that fails is skipped —
    a sitemap is read for evidence, so a partially readable file is better
    than none.
    """
    if body is None:
        body = _fetch(client, url)
    if not body or not is_sitemap_document(body):
        return SitemapDocument(url=url, locs=[])

    locs = _locs(body)
    if not _INDEX.search(body):
        # Keep the shortest signal: a urlset without locs is unreadable, not empty.
        return SitemapDocument(
            url=url,
            locs=locs[:MAX_LOCS],
            truncated=len(locs) > MAX_LOCS,
            is_index=False,
        )

    child_urls = [loc for loc in locs if loc.lower().endswith(".xml")]
    truncated = len(child_urls) > MAX_CHILDREN
    children = 0
    collected: list[str] = []
    for child in child_urls[:MAX_CHILDREN]:
        child_body = _fetch(client, child)
        if not child_body:
            continue
        children += 1
        collected.extend(_locs(child_body))

    collected = _dedupe(collected)
    if len(collected) > MAX_LOCS:
        truncated = True
    return SitemapDocument(
        url=url,
        locs=collected[:MAX_LOCS],
        children=children,
        truncated=truncated,
        is_index=True,
    )


def _locs(body: str) -> list[str]:
    return [unescape(loc) for loc in _LOC.findall(body)]


def _dedupe(urls: list[str]) -> list[str]:
    """Document order, first occurrence kept (the read's natural order)."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _fetch(client: object, url: str) -> str | None:
    """Fetch *url*, or ``None`` when it is unreachable.

    A sitemap that cannot be read is a miss for the caller, never an
    exception: the shortcut and the listing classification are optimisations.
    """
    try:
        return client.fetch(url)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - any transport failure is a miss
        logger.info("Sitemap unreachable %s: %s", url, exc)
        return None
