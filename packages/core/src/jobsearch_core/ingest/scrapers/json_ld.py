"""Class B: schema.org/JobPosting embedded as JSON-LD in the static HTML.

Deterministic and offline: reads the page's own ``<script type="application/ld+json">``
blocks, no network calls. A posting is kept only when it has a title and an
absolute URL (plan-scraping.md: the job URL is the identity, never a relative
or synthesized one). Nodes that are not JobPosting are ignored, including
nested ones under ``@graph`` or list values.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

_JSON_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _html_to_text(value: object) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return text or None


def _iter_nodes(node: object):
    """Yield every dict in a JSON-LD value (handles lists and nested @graph)."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_nodes(item)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _iter_nodes(value)


def _is_job_posting(node: dict[str, Any]) -> bool:
    node_type = node.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    return "JobPosting" in types


def _location(value: object) -> str | None:
    """Best-effort readable location from a JobPosting ``jobLocation`` value."""
    if isinstance(value, list):
        names = [name for name in (_location(item) for item in value) if name]
        return ", ".join(dict.fromkeys(names)) or None
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict):
        address = value.get("address")
        if isinstance(address, dict):
            parts = [
                _text(address.get(key))
                for key in ("addressLocality", "addressRegion", "addressCountry")
            ]
            joined = ", ".join(part for part in parts if part)
            if joined:
                return joined
        return _text(value.get("name"))
    return None


class JsonLdJobPostingScraper:
    """Scrapes class-B pages. Ranked before the generic careers-page scraper."""

    def matches(self, url: str, html: str | None = None) -> bool:
        if not html:
            return False
        return any(
            _is_job_posting(node)
            for block in _JSON_LD_RE.findall(html)
            for node in _iter_nodes(_parse_block(block))
        )

    def scrape_listings(self, html: str, base_url: str) -> list[ScrapedJob]:
        jobs: list[ScrapedJob] = []
        seen: set[str] = set()
        for block in _JSON_LD_RE.findall(html):
            for node in _iter_nodes(_parse_block(block)):
                if not _is_job_posting(node):
                    continue
                job = self._to_job(node, base_url)
                if job is None or job.url in seen:
                    continue
                seen.add(job.url)
                jobs.append(job)
        logger.info("JsonLdJobPostingScraper found %d listings", len(jobs))
        return jobs

    def _to_job(self, node: dict[str, Any], base_url: str) -> ScrapedJob | None:
        title = _text(node.get("title")) or _text(node.get("name"))
        raw_url = _text(node.get("url")) or _text(node.get("@id"))
        if not title or not raw_url:
            return None
        url = urljoin(base_url, raw_url)
        if not url.startswith(("http://", "https://")):
            return None
        return ScrapedJob(
            url=url,
            title=title,
            company="",
            location=_location(node.get("jobLocation")),
            posted_date=_text(node.get("datePosted")),
            closing_date=_text(node.get("validThrough")),
            employment_type=_text(node.get("employmentType")),
            description=_html_to_text(node.get("description")),
            verified=True,
        )


def _parse_block(block: str) -> object:
    try:
        return json.loads(block)
    except ValueError:
        return None
