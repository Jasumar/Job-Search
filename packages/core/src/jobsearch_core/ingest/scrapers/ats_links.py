"""ATS board link recognition, shared by scrapers, classifier, and discovery.

Leaf module: imports nothing from the scrape pipeline, so both
``scrapers/pipeline.py`` and ``ingest/site_class.py`` can use it without a cycle.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Phase 4.1e: ATS boards are reached only through links on a verified employer
# careers page — never through search. This map classifies an outgoing link on
# a careers page so scraping can dispatch to the platform's JSON adapter and
# record ``ats_platform`` as scrape-time provenance on the company row.
_ATS_HOST_EXACT = {
    "jobs.lever.co": "lever",
    "jobs.eu.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "careers.smartrecruiters.com": "smartrecruiters",
    "jobs.smartrecruiters.com": "smartrecruiters",
}
_ATS_HOST_SUFFIX = (".teamtailor.com",)


def _classify_ats_url(url: str) -> str | None:
    """Return the ATS platform name when *url* is a known ATS board URL."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    host = (parsed.hostname or "").lower()
    if not parsed.path.rstrip("/"):
        return None
    if host.endswith(".greenhouse.io") or host == "greenhouse.io":
        if host.startswith(("boards.", "job-boards.")):
            return "greenhouse"
        return None
    platform = _ATS_HOST_EXACT.get(host)
    if platform:
        return platform
    if any(host.endswith(suffix) and host != suffix.lstrip(".") for suffix in _ATS_HOST_SUFFIX):
        return "teamtailor"
    return None


def is_ats_board_url(url: str) -> bool:
    """True when *url* is a known ATS board (never a valid M1 source, 4.1e)."""
    return _classify_ats_url(url) is not None


def _find_ats_board_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Extract (platform, absolute_url) pairs for ATS board links in *html*.

    Scans anchors and meta-refresh redirects on an employer careers page.
    Returns de-duplicated pairs in document order.
    """
    soup = BeautifulSoup(html, "html.parser")
    hrefs: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if href:
            hrefs.append(href)
    for meta in soup.find_all("meta", attrs={"http-equiv": "refresh"}):
        content = meta.get("content", "")
        match = re.search(r"url=(.+)$", content, re.IGNORECASE)
        if match:
            hrefs.append(match.group(1).strip().strip("'\""))

    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for href in hrefs:
        absolute = urljoin(base_url, href)
        platform = _classify_ats_url(absolute)
        if not platform:
            continue
        key = absolute.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        links.append((platform, absolute))
    return links
