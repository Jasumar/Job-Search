"""Entry resolution (Phase 4.0d): landing page -> open-positions page.

The fixtures under ``tests/fixtures/entry_resolution/`` are the labelled
landing pages (FI/SV/DE/EN); the unit tests pin the ranking rules, the posting
shape, the hop and fetch bounds, the rendered retry, the sitemap shortcut and
the evidence a report carries.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin

import pytest
from jobsearch_core.config import EntryVocabulary, load_entry_vocabulary
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.entry_resolution import (
    MAX_FETCHES_PER_COMPANY,
    MAX_FETCHES_PER_HOP,
    MAX_TRACE_CANDIDATES,
    SITEMAP_LISTING,
    SITEMAP_NOT_JOB_POSTINGS,
    _descent_key,
    classify_sitemap,
    clears_fill_checks,
    fold,
    rank_entry_links,
    resolve_entry,
    sitemap_entry_candidates,
)
from jobsearch_core.ingest.scrapers.sitemap import MAX_LOCS, read_sitemap

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "entry_resolution"
VOCAB = load_entry_vocabulary(REPO_ROOT / "config" / "entry_vocabulary.yaml")
CASES = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
CASE_IDS = [case["name"] for case in CASES]


class FakeClient:
    """Serves a fixed URL->HTML map; records every fetch, in order.

    ``rendered`` is the browser tier: the same map by default, so a page the
    static tier served can be made to differ only where a test says so.
    """

    def __init__(
        self,
        pages: dict[str, str],
        dead: set[str] | None = None,
        rendered: dict[str, str] | None = None,
    ):
        self.pages = pages
        self.dead = dead or set()
        self.rendered_pages = pages if rendered is None else rendered
        self.fetched: list[str] = []
        self.rendered: list[str] = []

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        if url in self.dead or url not in self.pages:
            raise RuntimeError(f"HTTP 404 for {url}")
        return self.pages[url]

    def fetch_rendered(self, url: str) -> str:
        self.rendered.append(url)
        if url in self.dead or url not in self.rendered_pages:
            raise RuntimeError(f"render failed for {url}")
        return self.rendered_pages[url]

    def close(self) -> None:
        pass


def _job(url: str, title: str = "Senior Python Engineer") -> ScrapedJob:
    return ScrapedJob(url=url, title=title, company="Acme", authority_url=url)


def _extractor(postings: dict[str, int]):
    """Extractor stub: the page with N postings yields N jobs, others none."""

    def scrape(url: str, html: str) -> list[ScrapedJob]:
        return [_job(f"{url.rstrip('/')}/{1000 + i}") for i in range(1, postings.get(url, 0) + 1)]

    return scrape


def _page(*links: tuple[str, str]) -> str:
    anchors = "".join(f'<a href="{href}">{text}</a>' for href, text in links)
    return f"<html><body><h1>Landing</h1><nav>{anchors}</nav></body></html>"


def _sitemap(*urls: str) -> str:
    locs = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return f'<?xml version="1.0"?><urlset>{locs}</urlset>'


# --- fixtures ---------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_labelled_landing_page_resolves_to_its_jobs_page(case):
    base = case["base_url"]
    entry = urljoin(base, case["entry_path"])
    html = (FIXTURES / case["file"]).read_text(encoding="utf-8")
    client = FakeClient({base: html, entry: "<html></html>"})

    report = resolve_entry(base, client=client, scrape=_extractor({entry: 3}), vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == entry
    assert report.resolution.landing_url == base
    assert report.resolution.hops == 1
    assert report.resolution.postings == 3
    assert report.resolution.link_text.startswith(case["label_prefix"])
    # Every followable candidate is probed, not just the winner: the off-site,
    # mailto:, PDF and login links are structurally excluded, never fetched.
    assert client.fetched[0] == base
    assert entry in client.fetched
    assert all(f.startswith(("http://www.", "https://www.")) for f in client.fetched)
    assert not any(f.endswith(".pdf") for f in client.fetched)
    assert report.tried[0].url == entry and report.tried[0].outcome == "postings"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_fixture_landing_page_yields_no_postings(case):
    """The hole this lane exists for: a landing page is not a listing."""
    html = (FIXTURES / case["file"]).read_text(encoding="utf-8")
    assert CareersPageScraper().scrape_listings(html, case["base_url"]) == []


# --- ranking ----------------------------------------------------------------


def test_only_listing_shaped_same_site_links_are_candidates():
    html = _page(
        ("/jobs", "Jobs"),
        ("/jobs/senior-python-engineer", "Senior Python Engineer"),
        ("https://boards.greenhouse.io/acme", "Jobs at Greenhouse"),
        ("https://careers.acme.fi/jobs", "Open positions"),
        ("mailto:jobs@acme.fi", "Email us"),
        ("/privacy.pdf", "Privacy policy"),
        ("https://accounts.acme.fi/login", "Sign in"),
        ("/about", "About us"),
    )
    candidates = rank_entry_links(html, "https://www.acme.fi/", VOCAB)

    assert [c.url for c in candidates] == [
        "https://careers.acme.fi/jobs",
        "https://www.acme.fi/jobs",
    ]


def test_section_modifier_ending_a_listing_path_is_still_a_listing():
    """/avoimet-tyopaikat/kaikki is the "all openings" view, not a posting."""
    html = _page(("/avoimet-tyopaikat/kaikki", "Avoimet työpaikat"))
    candidates = rank_entry_links(html, "https://www.acme.fi/", VOCAB)
    assert [c.url for c in candidates] == ["https://www.acme.fi/avoimet-tyopaikat/kaikki"]


def test_id_shaped_last_segment_is_a_posting_never_an_entry():
    html = _page(
        ("/jobs/893398197391", "Open positions"),
        ("/careers/senior-engineer-123456", "Careers"),
        ("/careers/jobs", "Jobs"),
    )
    candidates = rank_entry_links(html, "https://www.acme.fi/", VOCAB)
    assert [c.url for c in candidates] == ["https://www.acme.fi/careers/jobs"]


def test_self_link_is_not_a_candidate():
    html = _page(("/careers", "Careers"), ("/careers/jobs", "Jobs"))
    candidates = rank_entry_links(html, "https://www.acme.fi/careers", VOCAB)
    assert [c.url for c in candidates] == ["https://www.acme.fi/careers/jobs"]


def test_ties_keep_document_order():
    html = _page(("/lediga-jobb", "Lediga jobb"), ("/careers/jobs", "Jobs"))
    candidates = rank_entry_links(html, "https://www.acme.se/", VOCAB)
    assert [c.url for c in candidates] == [
        "https://www.acme.se/lediga-jobb",
        "https://www.acme.se/careers/jobs",
    ]


def test_nav_label_that_does_not_name_a_jobs_path_is_rejected():
    # "Join us" is vocabulary, but /join-us is a culture page: the path rule
    # decides, not the label.
    html = _page(("/join-us", "Join us"))
    assert rank_entry_links(html, "https://www.acme.com/", VOCAB) == []


def test_vocabulary_matching_folds_case_and_accents():
    vocabulary = EntryVocabulary(link_terms={"fi": ["Työpaikat"]}, path_terms=["tyopaikat"])
    html = _page(("/TYÖPAIKAT", "TYÖPAIKAT"))
    candidates = rank_entry_links(html, "https://www.acme.fi/", vocabulary)
    assert [c.url for c in candidates] == ["https://www.acme.fi/TYÖPAIKAT"]


# --- the landing page is judged first (hop 0) -------------------------------


def test_a_landing_page_that_lists_more_than_its_candidates_is_the_entry():
    """JSON-LD / ATS / plain listing: the page itself stays the entry.

    It still competes — the candidate is fetched — but a page only replaces the
    entry on a strictly larger posting count, so a landing page that already
    lists (say) six postings is not lost to a thin section page.
    """
    base = "https://www.acme.fi/careers"
    html = _page(("/careers/jobs", "Jobs"))
    client = FakeClient({base: html})

    report = resolve_entry(base, client=client, scrape=_extractor({base: 6}), vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == base
    assert report.resolution.hops == 0
    assert report.resolution.postings == 6
    assert report.landing_postings == 6
    # Probed, not skipped: the candidate was fetched and yielded nothing.
    assert client.fetched == [base, urljoin(base, "/careers/jobs")]
    assert report.reason == "the landing page out-lists every page tried"


# --- the landing page competes; the listing must win ------------------------


def test_a_featured_jobs_block_loses_to_the_listing_behind_it():
    """The false flag this rule exists for.

    A marketing careers page that carries one or two featured postings clears
    the fill checks, but it is not the listing: the descent runs anyway and the
    page with more postings becomes the entry.
    """
    base = "https://www.acme.fi/careers"
    listing = "https://www.acme.fi/careers/open-positions"
    client = FakeClient(
        {base: _page(("/careers/open-positions", "Open positions")), listing: ""}
    )

    report = resolve_entry(
        base, client=client, scrape=_extractor({base: 1, listing: 9}), vocabulary=VOCAB
    )

    assert report.resolution is not None
    assert report.resolution.url == listing
    assert report.resolution.hops == 1
    assert report.resolution.postings == 9
    assert report.landing_postings == 1
    assert listing in client.fetched


def test_a_tie_never_moves_the_entry():
    """No gain, no move: the landing page keeps a tied candidate's place."""
    base = "https://www.acme.fi/careers"
    listing = "https://www.acme.fi/careers/open-positions"
    client = FakeClient(
        {base: _page(("/careers/open-positions", "Open positions")), listing: ""}
    )

    report = resolve_entry(
        base, client=client, scrape=_extractor({base: 4, listing: 4}), vocabulary=VOCAB
    )

    assert report.resolution is not None
    assert report.resolution.url == base
    assert report.resolution.postings == 4


def test_a_second_hop_can_still_beat_a_thin_first_hop():
    """Deeper is better only when it is bigger: /jobs/all wins on count."""
    base = "https://www.acme.fi/"
    jobs = urljoin(base, "/jobs")
    all_jobs = urljoin(base, "/jobs/all")
    client = FakeClient(
        {
            base: _page(("/jobs", "Jobs")),
            jobs: _page(("/jobs/all", "All jobs")),
            all_jobs: "",
        }
    )

    report = resolve_entry(
        base,
        client=client,
        scrape=_extractor({base: 3, jobs: 2, all_jobs: 11}),
        vocabulary=VOCAB,
    )

    assert report.resolution is not None
    assert report.resolution.url == all_jobs
    assert report.resolution.hops == 2
    assert report.resolution.postings == 11


def test_a_subset_page_never_displaces_a_page_that_lists_postings():
    """/jobs/job_categories lists a slice; it is not the board."""
    base = "https://www.acme.fi/careers"
    categories = "https://www.acme.fi/careers/job_categories"
    client = FakeClient(
        {base: _page(("/careers/job_categories", "Job categories")), categories: ""}
    )

    report = resolve_entry(
        base, client=client, scrape=_extractor({base: 2, categories: 9}), vocabulary=VOCAB
    )

    assert report.resolution is not None
    assert report.resolution.url == base
    assert report.resolution.postings == 2
    assert report.tried[0].url == categories and report.tried[0].outcome == "postings"


def test_a_subset_page_can_still_rescue_an_otherwise_empty_descent():
    """Nothing else lists anything: a slice beats nothing at all."""
    base = "https://www.acme.fi/careers"
    students = "https://www.acme.fi/careers/students-jobs"
    client = FakeClient(
        {base: _page(("/careers/students-jobs", "Student jobs")), students: ""}
    )

    report = resolve_entry(
        base, client=client, scrape=_extractor({students: 4}), vocabulary=VOCAB
    )

    assert report.resolution is not None
    assert report.resolution.url == students
    assert report.resolution.postings == 4


# --- the extractor, not the label, decides ----------------------------------


def test_best_labelled_link_loses_to_the_page_that_has_postings():
    base = "https://www.acme.fi/"
    listing = urljoin(base, "/avoimet-tyopaikat")
    decoy = urljoin(base, "/ura")
    html = _page(("/avoimet-tyopaikat", "Avoimet työpaikat"), ("/ura", "Uramme"))
    client = FakeClient({base: html, listing: "", decoy: ""})

    report = resolve_entry(base, client=client, scrape=_extractor({decoy: 2}), vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == decoy
    # Both candidates are probed; the labelled-but-empty one is not the entry,
    # and the sitemap is read only after the hop leaves nothing to rank.
    assert client.fetched == [base, listing, decoy, "https://www.acme.fi/sitemap.xml"]


def test_second_hop_when_the_first_hop_page_is_a_culture_page():
    base = "https://www.acme.fi/"
    culture = urljoin(base, "/ura")
    listing = urljoin(base, "/ura/tyopaikat")
    client = FakeClient(
        {
            base: _page(("/ura", "Uramme")),
            culture: _page(("/ura/tyopaikat", "Avoimet työpaikat")),
            listing: "",
        }
    )

    report = resolve_entry(base, client=client, scrape=_extractor({listing: 4}), vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == listing
    assert report.resolution.landing_url == culture
    assert report.resolution.hops == 2
    assert report.resolution.postings == 4


def test_two_hops_is_a_hard_cap():
    base = "https://www.acme.fi/"
    first = urljoin(base, "/jobs")
    second = urljoin(base, "/careers/jobs")
    third = urljoin(base, "/tyopaikat")
    client = FakeClient(
        {
            base: _page(("/jobs", "Jobs")),
            first: _page(("/careers/jobs", "Open positions")),
            second: _page(("/tyopaikat", "Avoimet työpaikat")),
            third: "",
        }
    )

    report = resolve_entry(base, client=client, scrape=_extractor({third: 1}), vocabulary=VOCAB)

    assert report.resolution is None
    assert client.fetched == [base, first, second]


def test_fetch_budget_is_per_hop_and_counts_failures():
    base = "https://www.acme.fi/"
    dead_one = urljoin(base, "/careers/jobs")
    dead_two = urljoin(base, "/lediga-jobb")
    third = urljoin(base, "/offene-stellen")
    html = _page(
        ("/careers/jobs", "Jobs"),
        ("/lediga-jobb", "Lediga jobb"),
        ("/offene-stellen", "Offene Stellen"),
        ("/vacancies", "Vacancies"),
    )
    client = FakeClient({base: html}, dead={dead_one, dead_two, third})

    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=VOCAB)

    assert report.resolution is None
    assert MAX_FETCHES_PER_HOP == 3
    # Three attempts, the dead ones included, and the fourth link never paid
    # for: the bound is on fetches, not on successes.
    assert client.fetched == [base, dead_one, dead_two, third]
    assert [tried.outcome for tried in report.tried] == ["unreachable"] * 3


def test_the_company_cap_bounds_two_rounds_of_three():
    base = "https://www.acme.fi/"
    first = urljoin(base, "/jobs")
    html = _page(
        ("/jobs", "Jobs"),
        ("/careers", "Careers"),
        ("/vacancies", "Vacancies"),
    )
    inner = _page(
        ("/jobs/all", "All jobs"),
        ("/jobs/search", "Search jobs"),
        ("/jobs/list", "List jobs"),
    )
    client = FakeClient(
        {base: html, first: inner},
        dead={urljoin(base, "/careers"), urljoin(base, "/vacancies")},
    )
    client.pages["https://www.acme.fi/sitemap.xml"] = ""

    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=VOCAB)

    assert report.resolution is None
    assert MAX_FETCHES_PER_COMPANY == 5
    # base + 2 dead in round 1, then 2 of round 2's three: the cap lands
    # mid-round instead of letting the second round start over.
    assert len(client.fetched) == 1 + MAX_FETCHES_PER_COMPANY


def test_landing_page_without_jobs_links_returns_a_miss_report():
    base = "https://www.acme.fi/"
    client = FakeClient(
        {base: _page(("/meista", "Meistä"), ("/yhteystiedot", "Ota yhteyttä"))}
    )
    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=VOCAB)

    assert report.resolution is None
    assert report.ok is False
    assert "no page under" in report.reason
    assert report.candidates == []
    # The landing page, then the one sitemap read the shortcut is allowed.
    assert client.fetched == [base, "https://www.acme.fi/sitemap.xml"]
    assert report.sitemap is not None


def test_start_html_is_used_instead_of_refetching_the_landing_page():
    base = "https://www.acme.fi/"
    listing = urljoin(base, "/avoimet-tyopaikat")
    client = FakeClient({listing: ""})
    html = _page(("/avoimet-tyopaikat", "Avoimet työpaikat"))

    report = resolve_entry(
        base,
        client=client,
        scrape=_extractor({listing: 1}),
        vocabulary=VOCAB,
        start_html=html,
    )

    assert report.resolution is not None and report.resolution.url == listing
    assert client.fetched[0] == listing
    assert base not in client.fetched


def test_empty_vocabulary_never_descends():
    base = "https://www.acme.fi/"
    html = _page(("/avoimet-tyopaikat", "Avoimet työpaikat"))
    client = FakeClient({base: html})
    empty = EntryVocabulary(link_terms={}, path_terms=[])

    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=empty)

    assert report.resolution is None
    assert "vocabulary" in report.reason
    assert client.fetched == [base]
    assert client.rendered == []


# --- the evidence a miss carries -------------------------------------------


def test_a_miss_reports_the_shortlist_and_every_fetch():
    base = "https://www.acme.fi/"
    jobs = urljoin(base, "/jobs")
    careers = urljoin(base, "/careers")
    ura = urljoin(base, "/ura")
    tyopaikat = urljoin(base, "/tyopaikat")
    vacancies = urljoin(base, "/vacancies")
    offene = urljoin(base, "/offene-stellen")
    html = _page(
        ("/jobs", "Jobs"),
        ("/careers", "Careers"),
        ("/ura", "Uramme"),
        ("/tyopaikat", "Työpaikat"),
        ("/vacancies", "Vacancies"),
        ("/offene-stellen", "Offene Stellen"),
    )
    client = FakeClient(
        {base: html, jobs: "", careers: "", ura: "", tyopaikat: "", vacancies: "", offene: ""},
        dead={urljoin(base, "/sitemap.xml")},
    )

    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=VOCAB)

    assert report.resolution is None
    assert len(report.tried) == MAX_FETCHES_PER_HOP
    assert all(tried.outcome == "no_postings" for tried in report.tried)
    # The shortlist is the top of the ranking, capped, and it outlives the
    # fetches: it is what makes a miss reviewable.
    assert len(report.candidates) == MAX_TRACE_CANDIDATES
    assert report.candidates[0].score >= report.candidates[-1].score
    trace = report.as_trace()
    assert len(trace["tried"]) == MAX_FETCHES_PER_HOP
    assert len(trace["candidates"]) == MAX_TRACE_CANDIDATES
    assert trace["entry"] is None
    json.dumps(trace)  # the run source stores this as text


# --- rendered retry (item 3) ------------------------------------------------


def test_a_client_rendered_nav_is_retried_once_on_the_rendered_dom():
    base = "https://www.acme.fi/"
    listing = urljoin(base, "/avoimet-tyopaikat")
    static = "<html><body><div id=root></div></body></html>"
    client = FakeClient(
        {base: static, listing: ""},
        rendered={base: _page(("/avoimet-tyopaikat", "Avoimet työpaikat"))},
    )

    report = resolve_entry(
        base,
        client=client,
        scrape=_extractor({listing: 2}),
        vocabulary=VOCAB,
        render=client.fetch_rendered,
    )

    assert report.resolution is not None and report.resolution.url == listing
    assert report.rendered is True
    assert client.rendered == [base]
    # The sitemap is a plain GET and is tried first; the browser is the last
    # resort, not the first.
    assert client.fetched == [base, "https://www.acme.fi/sitemap.xml", listing]


def test_the_rendered_retry_happens_at_most_once_per_descent():
    base = "https://www.acme.fi/"
    culture = urljoin(base, "/ura")
    client = FakeClient(
        {
            base: _page(("/ura", "Uramme")),
            culture: "<html><body><div id=root></div></body></html>",
        },
        rendered={base: "<html><body></body></html>", culture: "<html><body></body></html>"},
    )

    report = resolve_entry(
        base,
        client=client,
        scrape=_extractor({}),
        vocabulary=VOCAB,
        render=client.fetch_rendered,
    )

    assert report.resolution is None
    # The retry happens on the page being harvested — the frontier reached by
    # the first hop — and only once for the whole descent.
    assert client.rendered == [culture]
    assert report.rendered is True


def test_no_renderer_means_no_retry():
    base = "https://www.acme.fi/"
    client = FakeClient({base: "<html><body><div id=root></div></body></html>"})

    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=VOCAB)

    assert report.resolution is None
    assert client.rendered == []


# --- sitemap shortcut (item 4) ---------------------------------------------


def test_a_sitemap_of_posting_urls_names_the_listing_directory():
    base = "https://www.acme.fi/"
    listing = urljoin(base, "/jobs/")
    client = FakeClient(
        {
            base: _page(("/meista", "Meistä")),
            "https://www.acme.fi/sitemap.xml": _sitemap(
                "https://www.acme.fi/jobs/893398197391",
                "https://www.acme.fi/jobs/893398197033",
                "https://www.acme.fi/jobs/senior-engineer-123456",
                "https://www.acme.fi/about",
            ),
            urljoin(base, "/jobs/"): "",
        }
    )

    sitemap, derived = sitemap_entry_candidates(client, base)
    assert sitemap == "https://www.acme.fi/sitemap.xml"
    assert derived == [listing]

    report = resolve_entry(base, client=client, scrape=_extractor({listing: 5}), vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == listing
    assert report.resolution.postings == 5
    assert report.sitemap == "https://www.acme.fi/sitemap.xml"


def test_a_sitemap_index_reads_its_first_child_once():
    base = "https://www.acme.fi/"
    client = FakeClient(
        {
            "https://www.acme.fi/sitemap.xml": (
                "<sitemapindex><sitemap><loc>https://www.acme.fi/page-sitemap.xml</loc>"
                "</sitemap></sitemapindex>"
            ),
            "https://www.acme.fi/page-sitemap.xml": _sitemap(
                "https://www.acme.fi/jobs/893398197391",
                "https://www.acme.fi/jobs/893398197392",
                "https://www.acme.fi/jobs/893398197393",
            ),
        }
    )

    sitemap, derived = sitemap_entry_candidates(client, base)

    assert sitemap == "https://www.acme.fi/sitemap.xml"
    assert derived == ["https://www.acme.fi/jobs/"]
    assert client.fetched == [
        "https://www.acme.fi/sitemap.xml",
        "https://www.acme.fi/page-sitemap.xml",
    ]


def test_two_posting_urls_are_not_a_listing():
    base = "https://www.acme.fi/"
    client = FakeClient(
        {
            "https://www.acme.fi/sitemap.xml": _sitemap(
                "https://www.acme.fi/jobs/893398197391",
                "https://www.acme.fi/news/893398197392",
            )
        }
    )
    assert sitemap_entry_candidates(client, base) == ("https://www.acme.fi/sitemap.xml", [])


def test_a_sitemap_directory_that_is_the_site_root_is_refused():
    base = "https://www.acme.fi/about/"
    client = FakeClient(
        {
            "https://www.acme.fi/sitemap.xml": _sitemap(
                "https://www.acme.fi/893398197391",
                "https://www.acme.fi/893398197392",
                "https://www.acme.fi/893398197393",
            )
        }
    )
    sitemap_url, derived = sitemap_entry_candidates(client, base)
    assert sitemap_url == "https://www.acme.fi/sitemap.xml"
    assert derived == []


# --- fill checks ------------------------------------------------------------


@pytest.mark.parametrize(
    ("jobs", "page_url", "expected"),
    [
        ([], "https://acme.fi/jobs", False),
        ([_job("https://acme.fi/jobs/1", "AB")], "https://acme.fi/jobs", False),
        ([_job("/jobs/1", "Senior Python Engineer")], "https://acme.fi/jobs", False),
        ([_job("https://acme.fi/jobs", "Senior Python Engineer")], "https://acme.fi/jobs", False),
        ([_job("https://acme.fi/jobs/1", "Senior Python Engineer")], "https://acme.fi/jobs", True),
    ],
)
def test_clears_fill_checks(jobs, page_url, expected):
    assert clears_fill_checks(jobs, page_url) is expected


# --- the shipped vocabulary -------------------------------------------------


def test_the_shipped_vocabulary_covers_every_market_language_and_modifiers():
    """Item 5 guard: growth is a config edit, so the config is what is pinned."""
    assert {"fi", "sv", "de", "en", "lv", "lt", "et", "pl"} <= set(VOCAB.link_terms)
    assert {"all", "kaikki", "search", "list", "open"} <= {
        fold(term) for term in VOCAB.section_modifiers
    }
    assert "toopakkumised" in {fold(term) for term in VOCAB.link_terms["et"]}
    assert "oferty pracy" in {fold(term) for term in VOCAB.link_terms["pl"]}


# --- sitemap listings (P2) ---------------------------------------------------

def _belimo_sitemap(count: int = 6) -> str:
    """The run #17 shape: a urlset of /job/<slug>/<id>/ postings."""
    return _sitemap(
        *(
            f"https://jobsredirect.belimo.com/job/role-{index}-{1000 + index}/"
            for index in range(count)
        )
    )


def test_a_sitemap_of_postings_is_the_listing_when_no_page_lists_them():
    base = "https://jobsredirect.belimo.com/job/"
    sitemap = "https://jobsredirect.belimo.com/sitemap.xml"
    client = FakeClient(
        {base: _page(("/about", "About")), sitemap: _belimo_sitemap()}
    )

    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.kind == "sitemap"
    assert report.resolution.url == sitemap
    assert report.resolution.postings == 6
    assert report.reason == "the sitemap is the listing"
    assert report.sitemap_status == SITEMAP_LISTING
    # The derived directory equals the start URL, so it is never re-fetched:
    # the file is the answer, not a shortcut back to the page we started on.
    assert client.fetched == [base, sitemap]
    trace = report.as_trace()
    assert trace["entry"]["kind"] == "sitemap"
    assert trace["sitemap_status"] == SITEMAP_LISTING


def test_a_real_listing_page_always_beats_a_sitemap_listing():
    base = "https://www.acme.fi/careers"
    listing = "https://www.acme.fi/careers/jobs"
    client = FakeClient(
        {
            base: _page(("/careers/jobs", "Avoimet työpaikat")),
            listing: "",
            "https://www.acme.fi/sitemap.xml": _sitemap(
                *(f"https://www.acme.fi/jobs/post-{n}-{2000 + n}" for n in range(10))
            ),
        }
    )

    report = resolve_entry(
        base, client=client, scrape=_extractor({listing: 3}), vocabulary=VOCAB
    )

    assert report.resolution is not None
    assert report.resolution.kind == "page"
    assert report.resolution.url == listing


def test_a_product_catalogue_sitemap_is_refused_by_name():
    base = "https://www.acme.fi/"
    client = FakeClient(
        {
            base: _page(("/about", "Meistä")),
            "https://www.acme.fi/sitemap.xml": _sitemap(
                *(f"https://www.acme.fi/products/widget-{3000 + n}" for n in range(8))
            ),
        }
    )

    report = resolve_entry(base, client=client, scrape=_extractor({}), vocabulary=VOCAB)

    assert report.resolution is None
    assert report.sitemap_status == SITEMAP_NOT_JOB_POSTINGS
    assert SITEMAP_NOT_JOB_POSTINGS in report.reason
    assert report.as_trace()["sitemap_status"] == SITEMAP_NOT_JOB_POSTINGS


def test_a_site_wide_sitemap_contributes_only_its_job_subtree():
    start = "https://www.acme.fi/"
    doc = read_sitemap(
        FakeClient(
            {
                "https://www.acme.fi/sitemap.xml": _sitemap(
                    *(f"https://www.acme.fi/en/about-us-{4000 + n}" for n in range(3)),
                    *(f"https://www.acme.fi/careers/engineer-{5000 + n}" for n in range(6)),
                )
            }
        ),
        "https://www.acme.fi/sitemap.xml",
    )

    finding = classify_sitemap(start, doc)

    assert finding.status == SITEMAP_LISTING
    assert finding.is_listing
    assert len(finding.locs) == 6
    assert all("/careers/" in loc for loc in finding.locs)


def test_a_sitemap_index_reads_every_child_within_the_cap():
    client = FakeClient(
        {
            "https://www.acme.fi/sitemap.xml": (
                "<sitemapindex>"
                "<sitemap><loc>https://www.acme.fi/jobs-1.xml</loc></sitemap>"
                "<sitemap><loc>https://www.acme.fi/jobs-2.xml</loc></sitemap>"
                "</sitemapindex>"
            ),
            "https://www.acme.fi/jobs-1.xml": _sitemap(
                *(f"https://www.acme.fi/jobs/a-{6000 + n}" for n in range(3))
            ),
            "https://www.acme.fi/jobs-2.xml": _sitemap(
                *(f"https://www.acme.fi/jobs/b-{7000 + n}" for n in range(3))
            ),
        }
    )

    doc = read_sitemap(client, "https://www.acme.fi/sitemap.xml")

    assert doc.children == 2
    assert len(doc.locs) == 6
    assert doc.truncated is False


def test_a_sitemap_past_the_loc_cap_is_marked_truncated():
    client = FakeClient(
        {
            "https://www.acme.fi/sitemap.xml": _sitemap(
                *(f"https://www.acme.fi/jobs/x-{n}" for n in range(MAX_LOCS + 1))
            )
        }
    )

    doc = read_sitemap(client, "https://www.acme.fi/sitemap.xml")

    assert len(doc.locs) == MAX_LOCS
    assert doc.truncated is True


# --- host gate (RF11) -------------------------------------------------------


def test_a_jobs_named_host_is_a_listing_surface_without_a_jobs_path():
    """RF11: ``careers.<site>`` names the jobs section; the path says nothing."""
    html = _page(
        ("https://careers.evermade.fi/", "Careers"),
        ("/careers/join-us/", "Join us"),
    )
    urls = [c.url for c in rank_entry_links(html, "https://evermade.fi/", VOCAB)]

    assert "https://careers.evermade.fi/" in urls
    assert "https://evermade.fi/careers/join-us/" in urls

    google = _page(("/about/careers/applications/jobs/results", "Open positions"))
    google_urls = [
        c.url
        for c in rank_entry_links(google, "https://www.google.com/about/careers/", VOCAB)
    ]
    assert google_urls == ["https://www.google.com/about/careers/applications/jobs/results"]


def test_a_jobs_subdomain_on_a_different_name_is_a_listing_surface():
    html = _page(("https://tyopaikat.netum.fi/en", "Avoimet työpaikat"))
    urls = [c.url for c in rank_entry_links(html, "https://netum.fi/", VOCAB)]

    assert urls == ["https://tyopaikat.netum.fi/en"]


def test_same_host_link_gains_nothing_from_its_host():
    """A link inside careers.acme.fi to /about is not a listing, host or not."""
    html = _page(("https://careers.acme.fi/about", "About"))
    assert rank_entry_links(html, "https://careers.acme.fi/", VOCAB) == []


def test_a_posting_on_a_jobs_host_is_never_a_candidate():
    html = _page(("https://careers.evermade.fi/jobs/123456", "Senior engineer"))
    assert rank_entry_links(html, "https://evermade.fi/", VOCAB) == []


# --- locale-insensitive descent key (RF12) ----------------------------------


def test_descent_key_drops_locale_segment_and_locale_params():
    assert (
        _descent_key("https://www.acme.fi/en/job-categories")
        == _descent_key("https://www.acme.fi/fi/job-categories")
        == _descent_key("https://www.acme.fi/job-categories?lang=de")
    )
    assert _descent_key("https://www.acme.fi/en/search") != _descent_key(
        "https://www.acme.fi/en/job-categories"
    )


def test_locale_variants_of_one_page_cost_one_fetch():
    base = "https://www.acme.fi/"
    en = urljoin(base, "/en/job-categories")
    search = urljoin(base, "/en/search")
    html = _page(
        ("/en/job-categories", "Job categories"),
        ("/fi/job-categories", "Job categories"),
        ("/de/job-categories", "Job categories"),
        ("/en/search", "Search jobs"),
    )
    client = FakeClient({base: html, en: "", search: ""})

    report = resolve_entry(
        base, client=client, scrape=_extractor({search: 3}), vocabulary=VOCAB
    )

    assert report.resolution is not None
    assert report.resolution.url == search
    variants = [url for url in client.fetched if "job-categories" in url]
    assert variants == [en]
    assert search in client.fetched


# --- rendered retry on a candidate (RF13) -----------------------------------

_SHELL = "<html><body><div id=root></div></body></html>"
_RENDERED = "<html><body><div data-rendered>Senior Python Engineer</div></body></html>"


def _jobs_only_when_rendered(url: str, html: str) -> list[ScrapedJob]:
    """Extractor stub: the JS shell yields nothing, the rendered DOM yields 3."""
    if "data-rendered" not in html:
        return []
    return [_job(f"{url.rstrip('/')}/{1000 + i}") for i in range(1, 4)]


def test_a_js_shell_candidate_is_rendered_and_resolves():
    base = "https://www.acme.fi/"
    listing = urljoin(base, "/careers/open-positions")
    client = FakeClient(
        {base: _page(("/careers/open-positions", "Open positions")), listing: _SHELL},
        rendered={listing: _RENDERED},
    )

    report = resolve_entry(
        base,
        client=client,
        scrape=_jobs_only_when_rendered,
        vocabulary=VOCAB,
        render=client.fetch_rendered,
    )

    assert report.resolution is not None
    assert report.resolution.url == listing
    assert report.resolution.postings == 3
    assert report.rendered is True
    assert [t.outcome for t in report.tried if t.url == listing] == ["postings"]
    assert client.rendered == [listing]


def test_js_shell_candidate_is_a_miss_without_a_renderer():
    base = "https://www.acme.fi/"
    listing = urljoin(base, "/careers/open-positions")
    client = FakeClient(
        {base: _page(("/careers/open-positions", "Open positions")), listing: _SHELL},
        rendered={listing: _RENDERED},
    )

    report = resolve_entry(
        base, client=client, scrape=_jobs_only_when_rendered, vocabulary=VOCAB, render=None
    )

    assert report.resolution is None
    assert report.rendered is False
    assert client.rendered == []


def test_no_rendered_retry_once_the_fetch_budget_is_spent():
    """The candidate links onward, so the descent wants a hop-2 fetch the cap refuses."""
    base = "https://www.acme.fi/"
    listing = urljoin(base, "/careers/open-positions")
    static = (
        '<html><body><div id=root></div><nav>'
        '<a href="/careers/open-roles">Open roles</a></nav></body></html>'
    )
    client = FakeClient(
        {base: _page(("/careers/open-positions", "Open positions")), listing: static},
        rendered={listing: _RENDERED},
    )

    report = resolve_entry(
        base,
        client=client,
        scrape=_jobs_only_when_rendered,
        vocabulary=VOCAB,
        render=client.fetch_rendered,
        max_fetches=1,
    )

    assert report.resolution is None
    assert report.rendered is False
    assert client.rendered == []


# --- ES1-ES3: pages compete on real postings, not links ---------------------


def _html_links(*hrefs: tuple[str, str], wrap: str | None = None) -> str:
    body = "".join(f'<a href="{href}">{text}</a>' for href, text in hrefs)
    return f"<html><body><{wrap}>{body}</{wrap}></body></html>" if wrap else f"<html><body>{body}</body></html>"


def _link_extractor(url: str, html: str) -> list[ScrapedJob]:
    """Extractor stub that, like the generic one, emits every anchor as a job."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    return [
        _job(urljoin(url, a["href"]), a.get_text(strip=True) or "Senior Python Engineer")
        for a in soup.find_all("a", href=True)
    ]


def test_nav_links_do_not_outvote_the_real_listing():
    """The NVIDIA shape: the landing page's mega-menu out-counts the listing."""
    base = "https://www.acme.com/careers"
    listing = "https://www.acme.com/careers/open-positions"
    menu = [(f"/jobs/{1000 + i}", "Senior Python Engineer") for i in range(10)]
    landing = (
        "<html><body><nav>"
        + "".join(f'<a href="{h}">{t}</a>' for h, t in menu)
        + '</nav><a href="/careers/open-positions">Open positions</a></body></html>'
    )
    listing_html = _html_links(
        *[(f"/careers/open-positions/{2000 + i}", "Senior Python Engineer") for i in range(3)]
    )
    client = FakeClient({base: landing, listing: listing_html})

    report = resolve_entry(base, client=client, scrape=_link_extractor, vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == listing
    assert report.resolution.postings == 3
    assert report.landing_raw == 11
    tried = next(t for t in report.tried if t.url == listing)
    assert (tried.raw, tried.real) == (3, 3)
    assert report.as_trace()["landing_raw"] == report.landing_raw


def test_the_landing_page_still_wins_when_it_is_the_listing():
    base = "https://www.acme.com/careers"
    rows = [(f"/careers/{3000 + i}", "Senior Python Engineer") for i in range(4)]
    landing = _html_links(*rows) + ""
    landing = landing.replace("</body>", '<a href="/careers/open-positions">Open positions</a></body>')
    candidate = "https://www.acme.com/careers/open-positions"
    client = FakeClient({base: landing, candidate: _html_links(*rows[:2])})

    report = resolve_entry(base, client=client, scrape=_link_extractor, vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == base
    assert report.resolution.postings == 4


def test_links_shared_with_the_landing_page_are_frame_not_postings():
    base = "https://www.acme.com/careers"
    candidate = "https://www.acme.com/careers/open-positions"
    shared = [(f"/jobs/{4000 + i}", "Senior Python Engineer") for i in range(5)]
    landing = _html_links(*shared, ("/careers/open-positions", "Open positions"))
    # The candidate repeats the landing links and adds one posting of its own.
    own = ("/careers/open-positions/5001", "Senior Python Engineer")
    client = FakeClient({base: landing, candidate: _html_links(*shared, own)})

    report = resolve_entry(base, client=client, scrape=_link_extractor, vocabulary=VOCAB)

    tried = next(t for t in report.tried if t.url == candidate)
    assert (tried.raw, tried.real) == (6, 1)


def test_a_posting_shaped_cluster_counts_without_role_words():
    from jobsearch_core.ingest.scrapers.entry_resolution import real_postings

    base = "https://www.acme.com/careers/open-positions"
    names = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]
    rows = [(f"/careers/open-positions/{name}", f"Opening {name}") for name in names]
    jobs = [_job(urljoin(base, h), t) for h, t in rows]

    # No numeric id, no role words: ES1 alone rejects every one of them.
    from jobsearch_core.ingest.scrapers.posting_check import posting_obstacle

    assert all(posting_obstacle(job, base) == "no_posting_signal" for job in jobs)
    # Nine same-shaped sibling links are a cluster (ES3), so they count.
    assert len(real_postings(jobs, base, _html_links(*rows))) == 9
    # Two links form no cluster and still fail the posting check.
    assert real_postings(jobs[:2], base, _html_links(*rows[:2])) == []


def test_page_chrome_helpers():
    from jobsearch_core.ingest.page_chrome import chrome_urls, content_links, page_urls

    html = (
        '<header><a href="/a">A</a></header><a href="/b">B</a>'
        '<footer><a href="/c">C</a></footer><a href="/c">C again</a><a href="mailto:x@y.z">m</a>'
    )
    base = "https://acme.com/x"
    assert chrome_urls(html, base) == {"https://acme.com/a", "https://acme.com/c"}
    assert content_links(html, base) == [("https://acme.com/b", "B")]
    assert content_links(html, base, exclude={"https://acme.com/b"}) == []
    assert "https://acme.com/c" in page_urls(html, base)


def test_posting_check_shape_rules():
    from jobsearch_core.ingest.scrapers.posting_check import posting_obstacle

    listing = "https://jobs.acme.com/search"
    assert posting_obstacle(_job("https://jobs.acme.com/details/200688368/x"), listing) is None
    assert posting_obstacle(_job("https://jobs.acme.com/x", "Sign in"), listing) == "nav_link"
    assert posting_obstacle(_job("https://other.org/jobs/1234"), listing) == "off_domain"
    assert posting_obstacle(_job(listing), listing) == "self_link"
    assert posting_obstacle(_job("https://jobs.acme.com/benefits", "Benefits"), listing) == "no_posting_signal"


def test_when_no_page_has_a_real_posting_the_raw_count_still_decides():
    """Fail-open: nothing real anywhere resolves as before, never as a miss."""
    base = "https://www.acme.com/careers"
    landing = _html_links(*[(f"/about/{n}", f"Section {n}x") for n in "abcdef"])
    client = FakeClient({base: landing})

    report = resolve_entry(base, client=client, scrape=_link_extractor, vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == base
    assert report.landing_raw == 6


def test_one_real_posting_beats_a_larger_raw_only_page():
    base = "https://www.acme.com/careers"
    candidate = "https://www.acme.com/careers/open-positions"
    landing = _html_links(
        *[(f"/about/{n}", f"Section {n}x") for n in "abcde"],
        ("/careers/open-positions", "Open positions"),
    )
    listing = _html_links(("/careers/open-positions/9001", "Senior Python Engineer"))
    client = FakeClient({base: landing, candidate: listing})

    report = resolve_entry(base, client=client, scrape=_link_extractor, vocabulary=VOCAB)

    assert report.resolution is not None
    assert report.resolution.url == candidate
