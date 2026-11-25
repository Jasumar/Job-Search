"""Resolve a scraped job to its owning ``companies`` row (gap 12).

``jobs.company_id`` was a known gap: the only link from a job to the company it
came from was ``jobs.source_url`` vs ``companies.careers_url`` plus a
``normalized_company`` text match performed by hand. That made every
company-scoped question (per-company filtering, "which accepted company has no
fresh jobs", "drop everything from a rejected company") a fuzzy join.

This module provides the single resolution used at ingest time and by the
backfill. It is deliberately conservative: it only ever links a job to a
company it can prove, by exact URL or by exact normalized name. Ambiguity
yields ``None`` rather than a guess, because a wrong ``company_id`` silently
hides jobs from a filter the user trusts.

Resolution order (first match wins):

1. Exact careers-URL match, ignoring scheme, ``www.``, query string, and
   trailing slash. A job scraped from a company page resolves to that company.
2. Exact normalized-name match against ``companies.name``, then
   ``companies.identity_name`` (the Phase 4.0a canonical name, preferred when
   both rows exist and differ).
3. ``None``.

Ambiguity guards:

- More than one company matching the same normalized name -> no link, and the
  ambiguity is logged. Two genuinely different firms can normalize alike
  ("Acme OY" vs "Acme Oy Ab"); guessing would attribute a job to the wrong one.
- A name that matches only ``pending`` companies still links. Whether a
  ``pending`` company is scraped at all is enforced by the scrape path, not
  here; recording the provenance is still correct.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobsearch_core.db.models import Company
from jobsearch_core.normalize import _normalize_text

logger = logging.getLogger(__name__)


def canonical_url(url: str | None) -> str | None:
    """Reduce a careers URL to a comparable form, or ``None`` if unusable.

    Lowercases the host, drops ``www.``, the query string, the fragment, and a
    trailing slash, and keeps only ``https``. Path case is preserved because
    ATS paths are frequently case-sensitive.
    """
    if not url:
        return None
    text = url.strip()
    if not text:
        return None
    parts = urlsplit(text if "://" in text else f"https://{text}")
    if not parts.netloc:
        return None
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return urlunsplit(("https", host, path, "", ""))


def canonical_name(name: str | None) -> str:
    """Normalize a company name for exact comparison.

    Uses the same text normalization as job normalization (lowercase, collapsed
    whitespace, trailing punctuation stripped) so that the ingest-side value and
    the stored value agree.
    """
    if not name:
        return ""
    return _normalize_text(name)


class CompanyLinkResolver:
    """Per-session lookup cache over :func:`resolve_company_id`.

    A single scrape ingests dozens of jobs from one careers URL and resolves the
    same company for each; caching the candidate rows keeps that to one query
    per company instead of one per job.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._by_url: dict[str, int | None] = {}
        self._by_name: dict[str, int | None] = {}

    def resolve(self, *, careers_url: str | None, company_name: str | None) -> int | None:
        """Return the ``companies.id`` owning this job, or ``None``.

        ``None`` is a normal outcome, not an error: a job scraped from a raw URL
        with no company record behind it has no owner to point at.
        """
        url = canonical_url(careers_url)
        if url is not None and url not in self._by_url:
            self._by_url[url] = self._lookup_url(url)
        if url is not None:
            company_id = self._by_url[url]
            if company_id is not None:
                return company_id

        name = canonical_name(company_name)
        if not name:
            return None
        if name not in self._by_name:
            self._by_name[name] = self._lookup_name(name)
        return self._by_name[name]

    def _lookup_url(self, canonical: str) -> int | None:
        rows = self._session.execute(
            select(Company.id, Company.careers_url).where(
                Company.careers_url.is_not(None)
            )
        ).all()
        matches = [row_id for row_id, url in rows if canonical_url(url) == canonical]
        if not matches:
            return None
        if len(matches) > 1:
            # Same careers URL on two company rows: the review gate merged or
            # duplicated a source. Do not pick one.
            logger.warning(
                "Ambiguous company URL match for %s (companies %s) — not linking",
                canonical,
                matches,
            )
            return None
        return matches[0]

    def _lookup_name(self, canonical: str) -> int | None:
        rows = self._session.execute(
            select(Company.id, Company.name, Company.identity_name).where(
                (Company.name.is_not(None)) | (Company.identity_name.is_not(None))
            )
        ).all()
        direct = [row_id for row_id, name, _ in rows if canonical_name(name) == canonical]
        identity = [
            row_id for row_id, _, ident in rows if canonical_name(ident) == canonical
        ]
        # identity_name is the Phase 4.0a canonical form; prefer it, and fall
        # back to name only when it points somewhere else unambiguously.
        for candidates in (identity, direct):
            unique = set(candidates)
            if len(unique) == 1:
                return unique.pop()
            if len(unique) > 1:
                logger.warning(
                    "Ambiguous company name match for %r (companies %s) — not linking",
                    canonical,
                    sorted(unique),
                )
                return None
        return None
