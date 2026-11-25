from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.detail import extract_detail_description, fetch_detail
from jobsearch_core.ingest.scrapers.oracle_hcm import OracleHcmScraper


class TestCareersPageScraper:
    def setup_method(self):
        self.scraper = CareersPageScraper()

    def test_matches_always_true(self):
        assert self.scraper.matches("https://example.com/careers") is True

    def test_scrape_job_links_with_job_path(self):
        html = """
        <html><body>
            <a href="/jobs/senior-backend-engineer">Senior Backend Engineer</a>
            <a href="/jobs/data-scientist">Data Scientist</a>
            <a href="/about">About Us</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert len(jobs) == 2
        assert jobs[0].title == "Senior Backend Engineer"
        assert jobs[0].url == "https://example.com/jobs/senior-backend-engineer"
        assert jobs[1].title == "Data Scientist"

    def test_apple_shaped_details_links_are_job_links(self):
        """``/details/<id>`` carries no job word in the path or the title (F4)."""
        html = """
        <html><body>
            <a href="/en-us/details/200688368/software-engineer-ml">Staff Silicon Lead</a>
            <a href="/en-us/details/200688369/retail-genius">Genius</a>
            <a href="/en-us/details/about-us">Our story</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://jobs.apple.com/en-us/search")
        assert [j.url for j in jobs] == [
            "https://jobs.apple.com/en-us/details/200688368/software-engineer-ml",
            "https://jobs.apple.com/en-us/details/200688369/retail-genius",
        ]

    def test_location_label_is_not_glued_to_the_value(self):
        html = """
        <html><body><div>
            <a href="/jobs/dev-1">Developer</a>
            <div class="job-location"><span>Location</span><span>Tampere, Finland</span></div>
        </div></body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert jobs[0].location == "Tampere, Finland"

    def test_scrape_job_links_with_posting_path(self):
        html = """
        <html><body>
            <a href="/postings/12345">Python Developer</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert len(jobs) == 1
        assert jobs[0].url == "https://example.com/postings/12345"

    def test_scrape_deduplicates_urls(self):
        html = """
        <html><body>
            <a href="/jobs/dev">Developer</a>
            <a href="/jobs/dev/">Developer (duplicate)</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert len(jobs) == 1

    def test_scrape_title_keyword_match(self):
        html = """
        <html><body>
            <a href="/careers/role-123">Junior Backend Engineer</a>
            <a href="/page/about">Company Blog</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert len(jobs) == 1
        assert jobs[0].title == "Junior Backend Engineer"

    def test_scrape_empty_page(self):
        html = "<html><body></body></html>"
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert jobs == []

    def test_scrape_relative_urls_resolved(self):
        html = """
        <html><body>
            <a href="jobs/dev">Developer</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://careers.example.com/page")
        assert jobs[0].url == "https://careers.example.com/jobs/dev"

    def test_scrape_location_extraction(self):
        html = """
        <html><body>
            <div>
                <a href="/jobs/123">Backend Engineer</a>
                <span class="location">Helsinki, Finland</span>
            </div>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert len(jobs) == 1
        assert jobs[0].location == "Helsinki, Finland"

    def test_scrape_title_from_attr(self):
        html = """
        <html><body>
            <a href="/jobs/123" title="Full Stack Developer">Click here</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert len(jobs) == 1
        assert jobs[0].title == "Full Stack Developer"

    def test_a_textless_link_takes_its_title_from_the_slug(self):
        """Google's card links carry no text at all; the slug is the last resort."""
        html = """
        <html><body>
            <a href="/careers/jobs/senior-backend-engineer-10422"></a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert len(jobs) == 1
        assert jobs[0].title == "Senior Backend Engineer"

    def test_a_generic_link_label_falls_through_to_the_slug(self):
        html = """
        <html><body>
            <a href="/jobs/people-operations-manager">Apply now</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert jobs[0].title == "People Operations Manager"

    def test_a_slug_of_one_word_or_an_id_stays_unknown(self):
        """A locale marker is not a role: "Prod" must never become a title."""
        html = """
        <html><body>
            <a href="/jobs/73697523738256070-prod"></a>
            <a href="/jobs/1"></a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert [job.title for job in jobs] == ["Unknown", "Unknown"]

    def test_a_heading_still_beats_the_slug(self):
        html = """
        <html><body>
            <a href="/jobs/senior-backend-engineer"><h3>Backend Engineer, Payments</h3></a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert jobs[0].title == "Backend Engineer, Payments"

    def test_bare_section_urls_are_not_jobs(self):
        """Regression (4.1d defect 3): a bare section root (/jobs, /careers/)
        is a listing surface, not a posting. The live DB holds
        builtin.com/jobs/remote titled "Remote" and employers.builtin.com/careers/
        titled "Careers" — both junk rows created by this hole."""
        html = """
        <html><body>
            <a href="/jobs/remote">Remote</a>
            <a href="https://employers.builtin.com/careers/">Careers</a>
            <a href="/jobs/senior-backend-engineer">Senior Backend Engineer</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert [j.url for j in jobs] == ["https://example.com/jobs/senior-backend-engineer"]

    def test_document_links_are_not_jobs(self):
        """Regression (4.1d defect 2/3): the DuckDuckGo PDF privacy policy was
        mined as a posting. A link to a document is never a posting URL."""
        html = """
        <html><body>
            <a href="https://duckduckgo.com/static-assets/files/pages/careers/policy.pdf">
                Recruiting Information Policy
            </a>
            <a href="/jobs/backend-engineer">Backend Engineer</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert [j.url for j in jobs] == ["https://example.com/jobs/backend-engineer"]

    def test_keyword_fallback_does_not_capture_single_generic_word_links(self):
        """The keyword fallback fires on link text only; a one-word nav label
        like 'Français' must not become a job because of it."""
        html = """
        <html><body>
            <a href="/fr">Français</a>
            <a href="/careers/data-scientist-helsinki">Data Scientist</a>
        </body></html>
        """
        jobs = self.scraper.scrape_listings(html, "https://example.com")
        assert [j.url for j in jobs] == ["https://example.com/careers/data-scientist-helsinki"]


class TestOracleHcmScraper:
    def setup_method(self):
        self.scraper = OracleHcmScraper()

    def test_matches_oracle_configured_official_page(self):
        html = '<base href="/en/sites/CX_1" data-apibaseurl="https://oracle.example" data-sitenumber="CX_1">'
        assert self.scraper.matches("https://jobs.example.com/careers", html) is True

    def test_does_not_match_plain_html(self):
        assert self.scraper.matches("https://example.com/careers", "<html></html>") is False

    def test_fetches_and_maps_requisitions(self):
        html = '<base href="/en/sites/CX_1" data-apibaseurl="https://oracle.example" data-sitenumber="CX_1">'
        response = {
            "items": [{
                "requisitionList": [{
                    "Id": "123",
                    "Title": "Backend Engineer",
                    "PostedDate": "2026-08-01",
                    "PrimaryLocation": "Helsinki, Finland",
                    "ShortDescriptionStr": "<p>Build Python services.</p>",
                }]
            }]
        }
        client = MagicMock()
        client.fetch_json.return_value = json.dumps(response)

        jobs = self.scraper.scrape_listings_with_client(
            html, "https://jobs.example.com/en/sites/CX_1/jobs?mode=location", client
        )

        assert len(jobs) == 1
        assert jobs[0].url == "https://jobs.example.com/en/sites/CX_1/job/123"
        assert jobs[0].title == "Backend Engineer"
        assert jobs[0].description == "Build Python services."
        assert jobs[0].verified is True
        assert "findReqs%3BsiteNumber%3DCX_1" in client.fetch_json.call_args[0][0]


class TestExtractDetailDescription:
    def test_extracts_from_main_tag(self):
        html = """
        <html><body>
            <nav>Navigation</nav>
            <main>
                <h1>Backend Engineer</h1>
                <p>We are looking for a skilled backend engineer to join our team.</p>
                <p>Requirements include Python, SQL, and Docker experience.</p>
            </main>
            <footer>Footer</footer>
        </body></html>
        """
        desc = extract_detail_description(html)
        assert desc is not None
        assert "Backend Engineer" in desc
        assert "Python" in desc
        assert "Navigation" not in desc
        assert "Footer" not in desc

    def test_extracts_from_article_tag(self):
        html = """
        <html><body>
            <article>
                <h2>Data Scientist</h2>
                <p>Join our data team. We work with large-scale datasets.</p>
            </article>
        </body></html>
        """
        desc = extract_detail_description(html)
        assert desc is not None
        assert "Data Scientist" in desc

    def test_extracts_from_job_description_class(self):
        html = """
        <html><body>
            <div class="job-description">
                <p>We need a Python developer with 2+ years experience.</p>
                <p>Knowledge of Django and REST APIs required.</p>
            </div>
        </body></html>
        """
        desc = extract_detail_description(html)
        assert desc is not None
        assert "Python developer" in desc

    def test_returns_none_for_short_content(self):
        html = """
        <html><body>
            <main><p>Hi</p></main>
        </body></html>
        """
        desc = extract_detail_description(html)
        assert desc is None

    def test_removes_script_and_style(self):
        html = """
        <html><body>
            <main>
                <script>var x = 1;</script>
                <style>.foo { color: red; }</style>
                <p>This is a real job description with enough content to pass the threshold.</p>
            </main>
        </body></html>
        """
        desc = extract_detail_description(html)
        assert desc is not None
        assert "var x" not in desc
        assert "color: red" not in desc
        assert "real job description" in desc

    def test_collapses_whitespace(self):
        html = """
        <html><body>
            <main>
                <p>Line one of the job description with sufficient content.</p>




                <p>Line two of the job description with more details about the role.</p>
            </main>
        </body></html>
        """
        desc = extract_detail_description(html)
        assert desc is not None
        assert "\n\n\n" not in desc

    def test_fallback_to_body(self):
        html = """
        <html><body>
            <p>This is a job description that is long enough to pass the minimum threshold for extraction.</p>
            <p>We are looking for candidates with strong technical skills and experience.</p>
        </body></html>
        """
        desc = extract_detail_description(html)
        assert desc is not None
        assert "job description" in desc


class TestFetchDetail:
    def test_fetch_detail_propagates_runtime_error(self):
        """4.1d defect 1: fetch_detail no longer swallows transport errors —
        'the network failed' must be distinguishable from 'the page had no
        posting' at the call site."""
        client = MagicMock()
        client.fetch.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            fetch_detail(client, "https://example.com/jobs/1")

    def test_fetch_detail_propagates_value_error(self):
        client = MagicMock()
        client.fetch.side_effect = ValueError("unknown url type: '/info@corp.example'")
        with pytest.raises(ValueError):
            fetch_detail(client, "/info@corp.example")

    def test_fetch_detail_returns_description(self):
        client = MagicMock()
        client.fetch.return_value = """
        <html><body><main>
            <p>This is a job description that is long enough to be extracted as detail content.</p>
            <p>Additional paragraph with more details about the role requirements.</p>
        </main></body></html>
        """
        desc = fetch_detail(client, "https://job.example/jobs/1")
        assert desc is not None
        assert "job description" in desc


class TestFillDescriptionsFailureIsVisible:
    """Regression (4.1d defect 1): a failed fetch must not silently look like a
    successful scrape that found no description. fetch_detail() catches
    (RuntimeError, ValueError) and returns None, collapsing "the network
    failed" into "the page had no posting" — the pipeline stored nothing and
    logged the same debug line for both. The distinguishing signal must
    survive into the caller."""

    def test_fill_descriptions_distinguishes_fetch_failure_from_no_content(self):
        from jobsearch_core.ingest.scrapers.pipeline import _fill_descriptions

        client = MagicMock()

        def fetch(url, **kwargs):
            if url.endswith("/ok"):
                return (
                    "<html><body><main><p>"
                    "This description is long enough to be extracted by the detail extractor."
                    "</p></main></body></html>"
                )
            if url.endswith("/empty"):
                return "<html><body><main><p>Short.</p></main></body></html>"
            raise RuntimeError("connection reset")

        client.fetch.side_effect = fetch
        jobs = [
            ScrapedJob(url="https://job.example/jobs/ok", title="Ok", company="C"),
            ScrapedJob(url="https://job.example/jobs/empty", title="Empty", company="C"),
            ScrapedJob(url="https://job.example/jobs/boom", title="Boom", company="C"),
        ]
        _fill_descriptions(jobs, client, "https://job.example/careers")
        assert jobs[0].description and "long enough" in jobs[0].description
        assert jobs[1].description is None  # fetched fine, nothing in the page
        assert jobs[2].description is None  # fetch failed — signal survives above


class TestLoginWallGuard:
    """4.1d defect 4, production side (user call 2026-10-04: minor): a login
    wall must never be stored as a description. The real DB rows 471/474 held
    763-char wall pages for exactly this reason — the length check blessed
    them."""

    def test_short_login_wall_is_refused(self):
        wall = (
            "<html><body><main><h1>Sign in to continue</h1>"
            "<p>Please log in or sign up to view this content.</p>"
            "<form><input name=\'user\'><input name=\'pass\' type=\'password\'></form>"
            "</main></body></html>"
        )
        assert extract_detail_description(wall) is None

    def test_long_page_with_incidental_login_link_kept(self):
        # Calibrated domain: real incidental-widget postings are 4.8-15KB
        # (textguards docstring); this fixture is ~4.6KB of body.
        body = "We are hiring a backend engineer. " * 140
        html = (
            "<html><body><main><p>" + body
            + "</p><a href='/login'>Please log in or sign up to report this job</a></main></body></html>"
        )
        assert extract_detail_description(html) is not None


class TestBinaryDocumentGuard:
    """Regression (4.1d defect 2): raw PDF bytes must never be stored as a
    description. The real DB row held 82KB of `%PDF-1.7` payload because it
    cleared every length check; the binary content must be refused instead."""

    def test_pdf_bytes_are_refused(self):
        pdf = "%PDF-1.7\r\n%\xff\xff\xff\xff\r\n1 0 obj\r\n<< /Length 1024 >>" + "x" * 500
        assert extract_detail_description(
            f"<html><body><main><pre>{pdf}</pre></main></body></html>"
        ) is None

    def test_plain_text_is_still_extracted(self):
        html = (
            "<html><body><main><p>"
            "We are looking for a backend engineer with strong Python skills."
            "</p></main></body></html>"
        )
        assert extract_detail_description(html) is not None
