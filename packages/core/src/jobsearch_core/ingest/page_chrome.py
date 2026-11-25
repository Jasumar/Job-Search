"""Page chrome: links that belong to the site frame, not to the page's content.

ES2 (master_plan §ES). A landing page's header, nav, footer and mega-menu link
to dozens of same-site pages. Counted as postings they outvote the real
listing. This helper is the one place that decides what chrome is; the entry
resolver uses it now and pasted-page extraction (PX2) reuses it.

Pure: reads HTML, writes nothing, never fetches.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobsearch_core.ingest.scrapers.pagination import normalize_url

#: Elements whose links are site furniture. ``aside`` carries "related" and
#: promo rails; the other three are the page frame.
CHROME_TAGS = ("header", "nav", "footer", "aside")
_NON_PAGE_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "#")


def _anchors(html: str, page_url: str):
    """(normalized url, text, in_chrome) for every real link on the page."""
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.lower().startswith(_NON_PAGE_SCHEMES):
            continue
        url = urljoin(page_url, href)
        in_chrome = anchor.find_parent(CHROME_TAGS) is not None
        yield url, " ".join(anchor.get_text(" ").split()), in_chrome


def page_urls(html: str, page_url: str) -> set[str]:
    """Normalized URL of every link on the page, chrome included.

    A link a candidate shares with the landing page is part of the site frame
    even when it sits outside a ``nav`` element, so the resolver subtracts this
    set from a candidate's postings.
    """
    return {normalize_url(url) for url, _, _ in _anchors(html, page_url)}


def chrome_urls(html: str, page_url: str) -> set[str]:
    """Normalized URL of every link inside header, nav, footer or aside."""
    return {normalize_url(url) for url, _, in_chrome in _anchors(html, page_url) if in_chrome}


def content_links(
    html: str,
    page_url: str,
    *,
    exclude: frozenset[str] | set[str] = frozenset(),
    drop_menu_duplicates: bool = True,
) -> list[tuple[str, str]]:
    """(url, text) for each distinct link outside the chrome, in document order.

    ``exclude`` holds normalized URLs to drop as well (links shared with
    another page). A URL that also appears inside chrome is dropped: the same
    link in the menu and in the body is still a menu link. A caller that lists
    names (PX2) sets ``drop_menu_duplicates=False``: a company linked in the
    body and again in a footer is still a listed company.
    """
    chrome = chrome_urls(html, page_url)
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url, text, in_chrome in _anchors(html, page_url):
        key = normalize_url(url)
        if in_chrome or (drop_menu_duplicates and key in chrome) or key in exclude or key in seen:
            continue
        seen.add(key)
        links.append((url, text))
    return links
