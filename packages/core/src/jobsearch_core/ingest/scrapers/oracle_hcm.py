from __future__ import annotations

import json
import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

_BASE_API_RE = re.compile(r"data-apibaseurl=[\"']([^\"']+)", re.IGNORECASE)
_SITE_NUMBER_RE = re.compile(r"data-sitenumber=[\"']([^\"']+)", re.IGNORECASE)
_BASE_HREF_RE = re.compile(r"<base\b[^>]*\bhref=[\"']([^\"']+)", re.IGNORECASE)
_LIST_ENDPOINT = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


class OracleHcmScraper:
    """Extract jobs through Oracle HCM after an official page exposes its config."""

    requires_client = True

    def matches(self, url: str, html: str | None = None) -> bool:
        return bool(html and _BASE_API_RE.search(html) and _SITE_NUMBER_RE.search(html))

    def scrape_listings(self, html: str, base_url: str) -> list[ScrapedJob]:
        raise RuntimeError("Oracle HCM extraction requires a PoliteClient")

    def scrape_listings_with_client(
        self, html: str, base_url: str, client: PoliteClient
    ) -> list[ScrapedJob]:
        api_base = self._match_value(_BASE_API_RE, html)
        site_number = self._match_value(_SITE_NUMBER_RE, html)
        if not api_base or not site_number:
            return []

        job_base = urljoin(base_url, self._match_value(_BASE_HREF_RE, html) or "")
        jobs: list[ScrapedJob] = []
        offset = 0
        page_size = 100

        while True:
            params = {
                "onlyData": "true",
                "expand": "requisitionList.secondaryLocations",
                "finder": f"findReqs;siteNumber={site_number},limit={page_size},offset={offset}",
            }
            endpoint = f"{api_base.rstrip('/')}{_LIST_ENDPOINT}?{urlencode(params)}"
            try:
                payload = json.loads(client.fetch_json(endpoint))
            except RuntimeError as exc:
                logger.warning("Oracle HCM request failed for %s: %s", base_url, exc)
                break

            summary = (payload.get("items") or [{}])[0]
            requisitions = summary.get("requisitionList") or []
            jobs.extend(self._parse_requisitions(requisitions, job_base or base_url))
            if len(requisitions) < page_size:
                break
            offset += len(requisitions)

        logger.info("OracleHcmScraper found %d listings", len(jobs))
        return jobs

    def _parse_requisitions(self, requisitions: list[dict], job_base: str) -> list[ScrapedJob]:
        jobs: list[ScrapedJob] = []
        for item in requisitions:
            requisition_id = str(item.get("Id") or "").strip()
            title = str(item.get("Title") or "").strip()
            if not requisition_id or not title:
                continue

            description = _html_to_text(item.get("ShortDescriptionStr"))
            location = str(item.get("PrimaryLocation") or "").strip() or None
            jobs.append(
                ScrapedJob(
                    url=urljoin(job_base.rstrip("/") + "/", f"job/{requisition_id}"),
                    title=title,
                    company="",
                    location=location,
                    posted_date=item.get("PostedDate"),
                    description=description,
                    authority_url=job_base,
                    verified=True,
                )
            )
        return jobs

    @staticmethod
    def _match_value(pattern: re.Pattern[str], html: str) -> str | None:
        match = pattern.search(html)
        return match.group(1) if match else None


def _html_to_text(value: object) -> str | None:
    if not value:
        return None
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return text or None
