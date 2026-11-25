from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ScrapedJob:
    url: str
    title: str
    company: str
    location: str | None = None
    posted_date: str | None = None
    closing_date: str | None = None
    remote_policy: str | None = None
    employment_type: str | None = None
    seniority: str | None = None
    languages_required: list[str] | None = None
    work_authorization: str | None = None
    sponsorship: str | None = None
    salary: str | None = None
    extracted_requirements: dict | None = None
    description: str | None = None
    # Phase 4.1e: scrape-time provenance — set only when jobs came from an ATS
    # board linked from a verified employer careers page. Never search-derived.
    ats_platform: str | None = None
    authority_url: str | None = None
    # Programmatic callers must provide the verified authority URL; scrapers override this.
    verified: bool = True


class Scraper(Protocol):
    def matches(self, url: str, html: str | None = None) -> bool:
        ...

    def scrape_listings(self, html: str, base_url: str) -> list[ScrapedJob]:
        ...
