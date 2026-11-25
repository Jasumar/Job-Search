from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.textguards import is_login_wall
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

_NOISE_TAGS = {"script", "style", "nav", "header", "footer", "aside", "iframe", "noscript"}
# Binary payloads that must never be stored as a description. A %PDF body was
# once stored whole (82KB of bytes) because it cleared every length check; the
# check has to precede the length check, not follow it.
_BINARY_PREFIXES = ("%PDF", "PK\x03\x04", "\x89PNG", "GIF8", "\xff\xd8\xff")
_CONTENT_SELECTORS = [
    "main",
    "article",
    '[role="main"]',
    ".job-description",
    ".job-details",
    ".job-content",
    ".posting-description",
    ".content",
    "#content",
    "#job-description",
]


def extract_detail_description(html: str) -> str | None:
    # A binary payload delivered where a page was expected must be refused
    # before any length logic can bless it (4.1d defect 2: a privacy-PDF body
    # was stored whole as a job description).
    if html.startswith(_BINARY_PREFIXES):
        return None
    soup = BeautifulSoup(html, "html.parser")
    container = _find_content_container(soup)
    if container is None:
        container = soup.body or soup
    _remove_noise(container)
    text = container.get_text(separator="\n", strip=True)
    text = _collapse_whitespace(text)
    # The binary payload may arrive wrapped in an HTML page (a <pre> viewer,
    # a content-type mismatch), so the sniff runs on the extracted text too.
    if text.startswith(_BINARY_PREFIXES):
        return None
    # A login wall replaces the posting; storing it as a description is how
    # rows 471/474 got 763-char wall pages in the jobs table (4.1d defect 4,
    # production side). A long page with an incidental login link is kept.
    if is_login_wall(text):
        return None
    if len(text) < 50:
        return None
    return text


def extract_posting_title(html: str) -> str | None:
    """The posting's title from its own page: ``<title>``, og:title, then ``h1``.

    A sitemap lists URLs, never titles, so every expanded posting pays for the
    title here. The order is the page's own opinion first (``<title>`` usually
    carries the role), then the social card, then the on-page heading. A binary
    payload is refused like everywhere else: it is not a posting page.
    """
    if html.startswith(_BINARY_PREFIXES):
        return None
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.title.get_text(" ", strip=True) if soup.title else ""
    candidates = (
        title_tag,
        _meta_content(soup, "og:title"),
        soup.h1.get_text(" ", strip=True) if soup.h1 else "",
    )
    for value in candidates:
        cleaned = " ".join((value or "").split())
        if cleaned:
            return cleaned
    return None


def fetch_detail(client: PoliteClient, url: str) -> str | None:
    """Fetch a detail page and extract its description.

    Transport failures (RuntimeError from the HTTP client, ValueError from URL
    parsing) propagate to the caller — 4.1d defect 1: collapsing "the fetch
    failed" into "no description extracted" erased the only signal that
    distinguishes an unreachable posting from an uninformative one.
    ``extract_detail_description`` returning None remains the genuine
    no-content outcome.
    """
    # RF6: a detail page tries the plain tier even on a host whose listing
    # needed a render. Test doubles that are not a PoliteClient use fetch().
    if isinstance(client, PoliteClient):
        html = client.fetch_static_first(url)
    else:
        html = client.fetch(url)
    return extract_detail_description(html)


def _meta_content(soup: BeautifulSoup, name: str) -> str:
    """Content of a ``<meta property=...>`` or ``<meta name=...>`` tag, or ""."""
    tag = soup.find("meta", attrs={"property": name})
    if tag is None:
        tag = soup.find("meta", attrs={"name": name})
    if tag is None:
        return ""
    return str(tag.get("content") or "").strip()


def _find_content_container(soup: BeautifulSoup) -> Tag | None:
    for selector in _CONTENT_SELECTORS:
        el = soup.select_one(selector)
        if el and len(el.get_text(strip=True)) >= 50:
            return el
    return None


def _remove_noise(tag: Tag) -> None:
    for noise in tag.find_all(_NOISE_TAGS):
        noise.decompose()


def _collapse_whitespace(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
