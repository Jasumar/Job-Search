"""JSON-API adapters for the common ATS platforms.

Each platform exposes a stable public JSON listing endpoint that a
TLS-impersonated client can query directly — the same pattern proven for
Oracle HCM. These adapters are dispatched by URL before the generic HTML
scraper and paginate until short. On any fetch or parse failure they log a
warning and return whatever was collected so the generic scraper still gets a
chance. They never touch authority-URL semantics; ``scrape_company`` sets the
authority URL from the verified careers URL.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_MAX_PAGES = 20


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


def _iso_date(value: object) -> str | None:
    """Normalize ISO timestamps or epoch milliseconds to a ``YYYY-MM-DD`` date."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=UTC).date().isoformat()
    text = str(value)
    if _ISO_DATE_RE.match(text):
        return text[:10]
    return None


class JsonApiScraper:
    """Base for ATS platforms with a stable public JSON listing endpoint."""

    requires_client = True

    def matches(self, url: str, html: str | None = None) -> bool:
        return self._match_url(urlparse(url))

    def _match_url(self, parsed: urlparse.ParseResult) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def scrape_listings(self, html: str, base_url: str) -> list[ScrapedJob]:
        raise RuntimeError(f"{type(self).__name__} requires a PoliteClient")

    def scrape_listings_with_client(
        self, html: str, base_url: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        raise NotImplementedError


class GreenhouseScraper(JsonApiScraper):
    """``boards.greenhouse.io/{token}`` and ``job-boards.*.greenhouse.io/{token}``."""

    _HOST_PREFIXES = ("boards.", "job-boards.")

    def _match_url(self, parsed: urlparse.ParseResult) -> bool:
        host = (parsed.hostname or "").lower()
        if not host.endswith(".greenhouse.io") and host != "greenhouse.io":
            return False
        if not host.startswith(self._HOST_PREFIXES):
            return False
        return self._board_token(parsed) is not None

    def _board_token(self, parsed: urlparse.ParseResult) -> str | None:
        if parsed.path.rstrip("/") == "/embed/job_board":
            return _text(parse_qs(parsed.query or "").get("for", [""])[0])
        segments = [part for part in parsed.path.split("/") if part]
        return _text(segments[0]) if segments else None

    def scrape_listings_with_client(
        self, html: str, base_url: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        parsed = urlparse(base_url)
        token = self._board_token(parsed)
        if not token:
            return []
        jobs: list[ScrapedJob] = []
        total: int | None = None
        for page in range(1, _MAX_PAGES + 1):
            endpoint = (
                f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
                f"?content=true&paginate=true&page={page}"
            )
            try:
                payload = json.loads(client.fetch_json(endpoint))
            except RuntimeError as exc:
                logger.warning("Greenhouse request failed for %s: %s", base_url, exc)
                break
            page_jobs = payload.get("jobs") or []
            jobs.extend(self._parse_jobs(page_jobs))
            if total is None:
                total = (payload.get("meta") or {}).get("total")
            if not page_jobs or (total is not None and len(jobs) >= total):
                break
        logger.info("GreenhouseScraper found %d listings", len(jobs))
        return jobs

    def _parse_jobs(self, items: list[dict]) -> list[ScrapedJob]:
        jobs: list[ScrapedJob] = []
        for item in items:
            title = _text(item.get("title"))
            url = _text(item.get("absolute_url"))
            if not title or not url:
                continue
            location = _text((item.get("location") or {}).get("name"))
            jobs.append(
                ScrapedJob(
                    url=url,
                    title=title,
                    company="",
                    location=location,
                    posted_date=_iso_date(item.get("first_published") or item.get("updated_at")),
                    description=_html_to_text(item.get("content")),
                    verified=True,
                )
            )
        return jobs


class LeverScraper(JsonApiScraper):
    """``jobs.lever.co/{company}`` (also ``jobs.eu.lever.co``)."""

    _HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")

    def _match_url(self, parsed: urlparse.ParseResult) -> bool:
        return (parsed.hostname or "").lower() in self._HOSTS and self._company(parsed) is not None

    def _company(self, parsed: urlparse.ParseResult) -> str | None:
        segments = [part for part in parsed.path.split("/") if part]
        return _text(segments[0]) if segments else None

    def scrape_listings_with_client(
        self, html: str, base_url: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        parsed = urlparse(base_url)
        company = self._company(parsed)
        if not company:
            return []
        endpoint = f"https://api.lever.co/v0/postings/{company}?mode=json"
        try:
            payload = json.loads(client.fetch_json(endpoint))
        except RuntimeError as exc:
            logger.warning("Lever request failed for %s: %s", base_url, exc)
            return []
        if not isinstance(payload, list):
            logger.warning("Lever returned unexpected payload for %s", base_url)
            return []
        jobs = self._parse_jobs(payload)
        logger.info("LeverScraper found %d listings", len(jobs))
        return jobs

    def _parse_jobs(self, postings: list[dict]) -> list[ScrapedJob]:
        jobs: list[ScrapedJob] = []
        for item in postings:
            title = _text(item.get("text"))
            url = _text(item.get("hostedUrl"))
            if not title or not url:
                continue
            categories = item.get("categories") or {}
            description = _text(item.get("descriptionPlain")) or _html_to_text(
                item.get("description")
            )
            jobs.append(
                ScrapedJob(
                    url=url,
                    title=title,
                    company="",
                    location=_text(categories.get("location")),
                    posted_date=_iso_date(item.get("createdAt")),
                    description=description,
                    employment_type=_text(categories.get("commitment")),
                    verified=True,
                )
            )
        return jobs


class AshbyScraper(JsonApiScraper):
    """``jobs.ashbyhq.com/{org}`` via the public posting API."""

    def _match_url(self, parsed: urlparse.ParseResult) -> bool:
        host = (parsed.hostname or "").lower()
        if host != "jobs.ashbyhq.com" and not host.endswith(".jobs.ashbyhq.com"):
            return False
        segments = [part for part in parsed.path.split("/") if part]
        return bool(segments) and segments[0].lower() != "archive"

    def _org(self, parsed: urlparse.ParseResult) -> str | None:
        segments = [part for part in parsed.path.split("/") if part]
        return _text(segments[0]) if segments else None

    def scrape_listings_with_client(
        self, html: str, base_url: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        parsed = urlparse(base_url)
        org = self._org(parsed)
        if not org:
            return []
        endpoint = f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"
        try:
            payload = json.loads(client.fetch_json(endpoint))
        except RuntimeError as exc:
            logger.warning("Ashby request failed for %s: %s", base_url, exc)
            return []
        jobs = self._parse_jobs(payload.get("jobs") or [])
        logger.info("AshbyScraper found %d listings", len(jobs))
        return jobs

    def _parse_jobs(self, items: list[dict]) -> list[ScrapedJob]:
        jobs: list[ScrapedJob] = []
        for item in items:
            if item.get("isListed") is False:
                continue
            title = _text(item.get("title"))
            url = _text(item.get("jobUrl"))
            if not title or not url:
                continue
            compensation = item.get("compensation") or {}
            description = _text(item.get("descriptionPlain")) or _html_to_text(
                item.get("descriptionHtml")
            )
            jobs.append(
                ScrapedJob(
                    url=url,
                    title=title,
                    company="",
                    location=_text(item.get("location")),
                    posted_date=_iso_date(item.get("publishedAt")),
                    description=description,
                    employment_type=_text(item.get("employmentType")),
                    salary=_text(compensation.get("compensationTierSummary")),
                    verified=True,
                )
            )
        return jobs


class SmartRecruitersScraper(JsonApiScraper):
    """``careers.smartrecruiters.com/{company}`` via the public posting API.

    The listing payload has no description, so each posting's detail endpoint
    is fetched for the job ad sections (one request per job, matching the
    generic scraper's per-detail cost but with structured payloads).
    """

    _HOSTS = ("careers.smartrecruiters.com", "jobs.smartrecruiters.com")
    _PAGE_SIZE = 100

    def _match_url(self, parsed: urlparse.ParseResult) -> bool:
        return (parsed.hostname or "").lower() in self._HOSTS and self._company(parsed) is not None

    def _company(self, parsed: urlparse.ParseResult) -> str | None:
        segments = [part for part in parsed.path.split("/") if part]
        return _text(segments[0]) if segments else None

    def scrape_listings_with_client(
        self, html: str, base_url: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        parsed = urlparse(base_url)
        company = self._company(parsed)
        if not company:
            return []
        jobs: list[ScrapedJob] = []
        offset = 0
        total: int | None = None
        while True:
            endpoint = (
                f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
                f"?limit={self._PAGE_SIZE}&offset={offset}"
            )
            try:
                payload = json.loads(client.fetch_json(endpoint))
            except RuntimeError as exc:
                logger.warning("SmartRecruiters request failed for %s: %s", base_url, exc)
                break
            content = payload.get("content") or []
            if total is None:
                total = payload.get("totalFound")
            jobs.extend(self._parse_postings(content, company, client))
            offset += len(content)
            if not content or len(content) < self._PAGE_SIZE:
                break
            if total is not None and offset >= total:
                break
            if offset >= _MAX_PAGES * self._PAGE_SIZE:
                break
        logger.info("SmartRecruitersScraper found %d listings", len(jobs))
        return jobs

    def _parse_postings(
        self, postings: list[dict], company: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        jobs: list[ScrapedJob] = []
        for item in postings:
            posting_id = _text(item.get("id"))
            title = _text(item.get("name"))
            if not posting_id or not title:
                continue
            location = item.get("location") or {}
            employment = item.get("typeOfEmployment") or {}
            experience = item.get("experienceLevel") or {}
            jobs.append(
                ScrapedJob(
                    url=f"https://jobs.smartrecruiters.com/{company}/{posting_id}",
                    title=title,
                    company="",
                    location=_text(location.get("fullLocation"))
                    or _text(location.get("city")),
                    posted_date=_iso_date(item.get("releasedDate")),
                    description=self._fetch_description(company, posting_id, client),
                    employment_type=_text(employment.get("label")),
                    seniority=_text(experience.get("label")),
                    verified=True,
                )
            )
        return jobs

    def _fetch_description(self, company: str, posting_id: str, client: PoliteClient) -> str | None:
        endpoint = f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}"
        try:
            payload = json.loads(client.fetch_json(endpoint))
        except RuntimeError as exc:
            logger.debug("SmartRecruiters detail failed for %s/%s: %s", company, posting_id, exc)
            return None
        sections = (payload.get("jobAd") or {}).get("sections") or {}
        parts: list[str] = []
        for section in sections.values():
            if not isinstance(section, dict):
                continue
            text = _html_to_text(section.get("text"))
            if text:
                parts.append(text)
        return "\n\n".join(parts) or None


class TeamtailorScraper(JsonApiScraper):
    """``{sub}.teamtailor.com`` via its public JSON Feed at ``/jobs.json``.

    The feed is a JSON Feed document whose items carry the job title, URL,
    publication date, HTML body, and a schema.org ``_jobposting`` object.
    """

    _SUFFIX = ".teamtailor.com"

    def _match_url(self, parsed: urlparse.ParseResult) -> bool:
        host = (parsed.hostname or "").lower()
        return host.endswith(self._SUFFIX) and host != "teamtailor.com"

    def scrape_listings_with_client(
        self, html: str, base_url: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        parsed = urlparse(base_url)
        if not self._match_url(parsed):
            return []
        jobs: list[ScrapedJob] = []
        for page in range(1, _MAX_PAGES + 1):
            endpoint = f"{parsed.scheme or 'https'}://{parsed.hostname}/jobs.json?page={page}"
            try:
                payload = json.loads(client.fetch_json(endpoint))
            except RuntimeError as exc:
                logger.warning("Teamtailor request failed for %s: %s", base_url, exc)
                break
            items = payload.get("items") or []
            jobs.extend(self._parse_items(items))
            if not items:
                break
        logger.info("TeamtailorScraper found %d listings", len(jobs))
        return jobs

    def _parse_items(self, items: list[dict]) -> list[ScrapedJob]:
        jobs: list[ScrapedJob] = []
        for item in items:
            title = _text(item.get("title"))
            url = _text(item.get("url"))
            if not title or not url:
                continue
            posting = item.get("_jobposting") or {}
            description = _html_to_text(
                item.get("content_html") or posting.get("description")
            )
            jobs.append(
                ScrapedJob(
                    url=url,
                    title=title,
                    company="",
                    posted_date=_iso_date(item.get("date_published")),
                    description=description,
                    verified=True,
                )
            )
        return jobs
