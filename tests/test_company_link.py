"""Gap 12: jobs.company_id attribution.

The invariant under test: a job is linked to a company only when the link is
provable, and every unprovable case leaves company_id NULL rather than
guessing. A wrong company_id is worse than none, because company-scoped
filters silently hide jobs the user expects to see.
"""

from __future__ import annotations

from jobsearch_core.db.company_link import (
    CompanyLinkResolver,
    canonical_name,
    canonical_url,
)
from jobsearch_core.db.models import Company, Job
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pipeline import ingest_scraped


def _company(session, name, **kwargs) -> Company:
    row = Company(name=name, status=kwargs.pop("status", "accepted"), **kwargs)
    session.add(row)
    # Commit, not just flush: ingest_scraped opens its own session, and SQLite
    # will not let a second connection write while this one holds the lock.
    session.commit()
    return row


# --- canonicalization ------------------------------------------------------


class TestCanonicalUrl:
    def test_scheme_www_and_trailing_slash_are_insignificant(self):
        variants = [
            "https://www.reaktor.fi/careers",
            "http://reaktor.fi/careers",
            "https://reaktor.fi/careers/",
            "https://reaktor.fi/careers?utm_source=x",
            "reaktor.fi/careers",
        ]
        assert len({canonical_url(v) for v in variants}) == 1

    def test_path_case_is_preserved(self):
        # ATS paths are frequently case-sensitive; folding them would merge
        # two genuinely different endpoints.
        assert canonical_url("https://x.com/Careers") != canonical_url("https://x.com/careers")

    def test_empty_and_none_are_unusable(self):
        assert canonical_url(None) is None
        assert canonical_url("") is None
        assert canonical_url("   ") is None


class TestCanonicalName:
    def test_case_and_whitespace_are_insignificant(self):
        assert canonical_name("Reaktor  Oy") == canonical_name("reaktor oy")

    def test_none_is_empty(self):
        assert canonical_name(None) == ""


# --- resolution ------------------------------------------------------------


class TestResolve:
    def test_resolves_by_careers_url(self, session):
        co = _company(session, "Reaktor", careers_url="https://reaktor.fi/careers")
        assert CompanyLinkResolver(session).resolve(
            careers_url="https://www.reaktor.fi/careers/", company_name="Whatever"
        ) == co.id

    def test_resolves_by_name_when_no_url_matches(self, session):
        co = _company(session, "Reaktor", careers_url="https://reaktor.fi/careers")
        assert CompanyLinkResolver(session).resolve(
            careers_url="https://jobs.example.com/listing/1", company_name="Reaktor"
        ) == co.id

    def test_unknown_company_resolves_to_none(self, session):
        assert CompanyLinkResolver(session).resolve(
            careers_url="https://nobody.example/careers", company_name="Nobody Ltd"
        ) is None

    def test_ambiguous_name_does_not_link(self, session):
        """Two company rows that normalize to the same name must not be guessed between.

        "Acme OY" and "Acme Oy." are the same string after normalization, so
        a name-only lookup would have to pick one arbitrarily.
        """
        _company(session, "Acme OY")
        _company(session, "Acme Oy.")
        # Neither is returned; the pair only exists to create the ambiguity.
        assert CompanyLinkResolver(session).resolve(
            careers_url=None, company_name="Acme OY"
        ) is None

    def test_ambiguous_careers_url_does_not_link(self, session):
        _company(session, "Dup A", careers_url="https://dup.example/careers")
        _company(session, "Dup B", careers_url="https://dup.example/careers")
        assert CompanyLinkResolver(session).resolve(
            careers_url="https://dup.example/careers", company_name=None
        ) is None

    def test_pending_company_still_links(self, session):
        """Provenance is correct even for a company not yet accepted."""
        co = _company(session, "Pending Co", status="pending")
        assert CompanyLinkResolver(session).resolve(
            careers_url=None, company_name="Pending Co"
        ) == co.id


# --- ingest wiring ---------------------------------------------------------


class TestIngestAttribution:
    def test_explicit_company_id_is_used(self, session):
        co = _company(session, "Acme", careers_url="https://acme.example/careers")
        stats = ingest_scraped(
            [
                ScrapedJob(
                    url="https://acme.example/jobs/1",
                    title="Backend Engineer",
                    company="Totally Different Name",
                    description="Python and Django experience required.",
                )
            ],
            source_url=co.careers_url,
            company_id=co.id,
        )
        assert stats["added"] == 1
        assert stats["linked"] == 1
        job = session.query(Job).one()
        assert job.company_id == co.id

    def test_resolved_from_name_when_no_company_id_given(self, session):
        co = _company(session, "Acme", careers_url="https://acme.example/careers")
        stats = ingest_scraped(
            [
                ScrapedJob(
                    url="https://other.example/jobs/1",
                    title="Backend Engineer",
                    company="Acme",
                    description="Python and Django experience required.",
                )
            ],
            source_url="https://other.example/careers",
        )
        assert stats["linked"] == 1
        assert session.query(Job).one().company_id == co.id

    def test_unattributable_job_stays_null_and_is_counted(self, session):
        stats = ingest_scraped(
            [
                ScrapedJob(
                    url="https://ghost.example/jobs/1",
                    title="Backend Engineer",
                    company="Ghost Holdings",
                    description="Python and Django experience required.",
                )
            ],
            source_url="https://ghost.example/careers",
        )
        assert stats["added"] == 1
        assert stats["linked"] == 0
        assert session.query(Job).one().company_id is None
