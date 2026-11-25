from __future__ import annotations

from typing import Any

from jobsearch_core.ingest.scrapers.base import ScrapedJob, Scraper
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.detail import extract_detail_description
from jobsearch_core.ingest.scrapers.oracle_hcm import OracleHcmScraper

__all__ = [
    "CareersPageScraper",
    "ScrapedJob",
    "Scraper",
    "extract_detail_description",
    "ingest_scraped",
    "OracleHcmScraper",
    "scrape_company",
]

# ``pipeline`` imports the recipe stack, which imports this package; an eager
# re-export of its names here would close that cycle (R5a). Import on first use.
_LAZY = ("ingest_scraped", "scrape_company")


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from jobsearch_core.ingest.scrapers.pipeline import ingest_scraped, scrape_company

        return {"ingest_scraped": ingest_scraped, "scrape_company": scrape_company}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
