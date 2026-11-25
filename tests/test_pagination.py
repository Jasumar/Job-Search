"""Pagination traversal: detection, termination, and loop safety.

Properties under test, and what enforces each:

* A pager link is never scraped as a job. ``careers_page`` excludes every
  anchor ``is_pagination_link`` claims, so a ``/jobs?page=2`` "Next" no longer
  becomes a job titled "Next".
* A crawl cannot loop forever. ``repeat`` (visited set), ``no_new_jobs``, and
  ``page_cap`` are independent of each other, so disabling one still leaves two
  others. The tests below cut each guard individually and confirm termination.
* Following a next link stays on the employer's careers section. The host and
  path guards in ``same_careers_scope`` are the enforcement point.
* Termination is always attributable. ``stop_reason`` is always one of
  ``TERMINAL_REASONS``; nothing can end a crawl without a reason.
"""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from jobsearch_core.ingest.scrapers import pagination as pag
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.pagination import (
    TERMINAL_REASONS,
    CrawlResult,
    crawl_careers_pages,
    find_next_page,
    is_disabled,
    is_pagination_link,
    next_link_is_disabled,
)

JOB1 = "/careers/opening?id=1"
JOB2 = "/careers/opening?id=2"
JOB3 = "/careers/opening?id=3"


def _html(body: str) -> str:
    return f"<html><body>{body}</body></html>"


def _job_link(href: str, text: str) -> str:
    return f'<a href="{href}">{text}</a>'


def _listing(*job_hrefs: str, next_href: str | None = None, rel: str | None = "next") -> str:
    links = "".join(_job_link(h, f"Senior Engineer {h[-1]}") for h in job_hrefs)
    rel_attr = f' rel="{rel}"' if rel else ""
    nxt = f'<a href="{next_href}"{rel_attr}>Next</a>' if next_href else ""
    return _html(links + nxt)


def _anchors(html: str) -> list:
    return BeautifulSoup(html, "html.parser").find_all("a", href=True)


def _first_anchor(html: str):
    return _anchors(html)[0]


class _FakeClient:
    """Serves HTML by URL. Records every URL the crawl asked for."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def fetch(self, url: str, **_: object) -> str:
        self.requested.append(url)
        if url not in self.pages:
            raise RuntimeError(f"404 for {url}")
        return self.pages[url]

    def close(self) -> None:
        pass


def _scrape(html: str, url: str) -> list[ScrapedJob]:
    return CareersPageScraper().scrape_listings(html, url)


# --- detection -------------------------------------------------------------


def test_rel_next_is_the_strongest_signal():
    html = _html(
        '<link rel="next" href="/careers/jobs?page=2">'
        '<a href="/careers/jobs?page=2" class="pager-link">Next</a>'
    )
    signal = find_next_page(html, "https://acme.example/careers/jobs")
    assert signal is not None
    assert signal.kind == "link"
    assert signal.source == "rel"
    assert signal.url == "https://acme.example/careers/jobs?page=2"


def test_pager_widget_next_anchor_is_detected():
    html = _html('<a href="/careers/jobs?page=2" class="pagination-next">Next</a>')
    signal = find_next_page(html, "https://acme.example/careers/jobs")
    assert signal is not None
    assert signal.source == "pager"
    assert signal.url == "https://acme.example/careers/jobs?page=2"


def test_load_more_button_yields_a_non_fetchable_signal():
    signal = find_next_page(
        _html('<button class="btn">Load more jobs</button>'),
        "https://acme.example/careers/jobs",
    )
    assert signal is not None
    assert signal.kind == "load_more"
    assert signal.url is None
    assert signal.fetchable is False


def test_aria_labelled_next_is_detected():
    signal = find_next_page(
        _html('<a href="/careers?page=2" aria-label="Next page of results">…</a>'),
        "https://acme.example/careers",
    )
    assert signal is not None
    assert signal.source == "label"
    assert signal.url == "https://acme.example/careers?page=2"


def test_active_page_number_is_not_followed_as_next():
    """Real Django pager: on page 2 the "2" link is active and Next is disabled.

    A bare page number can only be the current page, so treating it as a next
    signal makes the crawl take a redundant step and misreport why it stopped.
    """
    html = _html(
        '<ul class="pagination menu">'
        '<li class="previous"><a href="?page=1">Prev</a></li>'
        '<li><a href="?page=1">1</a></li>'
        '<li><a class="active" href="?page=2">2</a></li>'
        '<li class="next"><a class="disabled" href="">Next</a></li>'
        "</ul>"
    )
    signal = find_next_page(html, "https://www.python.org/jobs/?page=2")
    # The active "2" is never the next signal. If a signal is found at all it
    # must be the real Next control, and that control is disabled.
    if signal is not None:
        assert signal.text == "Next"
        assert next_link_is_disabled(html, signal, "https://www.python.org/jobs/?page=2")


def _django_pager(*job_hrefs: str, active: int, has_next: bool) -> str:
    """The exact markup python.org serves (Django's pagination menu)."""
    links = "".join(_job_link(h, f"Engineer {h[-1]}") for h in job_hrefs)
    prev = (
        '<li class="previous"><a class="disabled" href="">Prev</a></li>'
        if active == 1
        else f'<li class="previous"><a href="?page={active - 1}">Prev</a></li>'
    )
    nxt = (
        f'<li class="next"><a href="?page={active + 1}">Next</a></li>'
        if has_next
        else '<li class="next"><a class="disabled" href="">Next</a></li>'
    )
    numbers = "".join(
        f'<li><a class="active" href="?page={n}">{n}</a></li>'
        if n == active
        else f'<li><a href="?page={n}">{n}</a></li>'
        for n in (1, 2)
    )
    return _html(links + f'<ul class="pagination menu">{prev}{numbers}{nxt}</ul>')


def test_django_pager_crawl_stops_with_signal_disabled_not_repeat():
    """The live python.org pager: page 1 -> 2, then a disabled Next stops it.

    Regression: the bare "2" page-number link used to be read as the next
    signal, so the crawl took a redundant step and reported ``repeat`` when the
    truthful answer was ``signal_disabled``.
    """
    pages = {
        "https://www.python.org/jobs/": _django_pager("/jobs/a", active=1, has_next=True),
        "https://www.python.org/jobs/?page=2": _django_pager("/jobs/b", active=2, has_next=False),
    }
    result = crawl_careers_pages(
        "https://www.python.org/jobs/", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason == "signal_disabled"
    assert result.pages_crawled == 2
    assert len(result.jobs) == 2


def test_pager_widget_next_is_followed_when_active_page_is_present():
    """Same pager on page 1: "1" is active, and Next must still be found."""
    html = _html(
        '<ul class="pagination menu">'
        '<li class="previous"><a class="disabled" href="">Prev</a></li>'
        '<li><a class="active" href="?page=1">1</a></li>'
        '<li><a href="?page=2">2</a></li>'
        '<li class="next"><a href="?page=2">Next</a></li>'
        "</ul>"
    )
    signal = find_next_page(html, "https://www.python.org/jobs/")
    assert signal is not None
    assert signal.url == "https://www.python.org/jobs/?page=2"


def test_no_affordance_returns_none():
    assert find_next_page(_html("<p>No jobs</p>"), "https://acme.example/careers") is None


def test_prev_link_is_not_a_next_signal():
    html = _html('<a href="/careers/jobs?page=1" class="pagination-prev">Previous</a>')
    signal = find_next_page(html, "https://acme.example/careers/jobs")
    assert signal is None or signal.url != "https://acme.example/careers/jobs?page=1"


# --- pager links must not become jobs --------------------------------------


def test_next_anchor_is_excluded_from_careers_page_jobs():
    html = _listing(JOB1, next_href="/careers/jobs?page=2")
    jobs = CareersPageScraper().scrape_listings(html, "https://acme.example/careers/jobs")
    titles = [job.title for job in jobs]
    assert "Next" not in titles
    assert titles == ["Senior Engineer 1"]


def test_pager_page_numbers_inside_widget_are_not_jobs():
    html = _html(
        _job_link("/careers/job/42", "Senior Backend Engineer")
        + '<nav class="pagination">'
        '<a href="/careers/jobs?page=1">1</a><a href="/careers/jobs?page=2">2</a></nav>'
    )
    jobs = CareersPageScraper().scrape_listings(html, "https://acme.example/careers/jobs")
    assert [job.url for job in jobs] == ["https://acme.example/careers/job/42"]


def test_load_more_button_is_not_a_job():
    html = _html('<button>Load more jobs</button>' + _job_link("/careers/job/1", "Engineer"))
    jobs = CareersPageScraper().scrape_listings(html, "https://acme.example/careers")
    assert [job.title for job in jobs] == ["Engineer"]


def test_is_pagination_link_flags_next_anchor():
    html = _listing(JOB1, next_href="/careers/jobs?page=2")
    assert is_pagination_link(_anchors(html)[-1]) is True


def test_is_pagination_link_leaves_real_job_links_alone():
    html = _html(_job_link("/careers/job/7", "Senior Data Engineer") + _job_link(JOB1, "Platform Engineer"))
    assert all(is_pagination_link(a) is False for a in _anchors(html))


# --- termination -----------------------------------------------------------


def test_crawl_follows_pagination_and_terminates():
    pages = {
        "https://acme.example/careers/jobs": _listing(JOB1, next_href="/careers/jobs?page=2"),
        "https://acme.example/careers/jobs?page=2": _listing(JOB2),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs", _scrape, client=_FakeClient(pages)
    )
    assert result.followed is True
    assert result.pages_crawled == 2
    assert {job.url for job in result.jobs} == {
        "https://acme.example/careers/opening?id=1",
        "https://acme.example/careers/opening?id=2",
    }
    assert result.stop_reason in TERMINAL_REASONS


def test_crawl_stops_when_next_link_is_disabled():
    pages = {
        "https://acme.example/careers/jobs": _listing(JOB1, next_href="/careers/jobs?page=2"),
        "https://acme.example/careers/jobs?page=2": _html(
            _job_link("/careers/opening?id=2", "Senior Engineer 2")
            + '<a href="/careers/jobs?page=3" class="disabled" aria-disabled="true">Next</a>'
        ),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason == "signal_disabled"
    assert result.pages_crawled == 2


def test_page_cap_is_the_backstop_for_an_endless_chain():
    pages = {
        f"https://acme.example/careers/jobs?page={i}": _listing(
            f"/careers/opening?id={i}", next_href=f"/careers/jobs?page={i + 1}"
        )
        for i in range(1, 40)
    }
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs?page=1",
        _scrape,
        client=_FakeClient(pages),
        max_pages=5,
    )
    assert result.stop_reason == "page_cap"
    assert result.pages_crawled == 5


def test_visited_set_stops_a_pager_that_points_backwards():
    """The start URL is page 1, so page 2's link back to it is a real repeat."""
    pages = {
        "https://acme.example/careers/jobs?page=1": _listing(JOB1, next_href="/careers/jobs?page=2"),
        "https://acme.example/careers/jobs?page=2": _listing(
            JOB2, next_href="/careers/jobs?page=1"
        ),
    }
    client = _FakeClient(pages)
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs?page=1", _scrape, client=client
    )
    assert result.stop_reason == "repeat"
    assert result.pages_crawled == 2
    # The repeated URL was detected before being fetched a second time.
    assert client.requested.count("https://acme.example/careers/jobs?page=1") == 1


def test_crawl_stops_when_a_page_adds_no_new_jobs():
    """Page 2 re-lists page 1's job but still advertises a page 3."""
    pages = {
        "https://acme.example/careers/jobs": _listing(JOB1, next_href="/careers/jobs?page=2"),
        "https://acme.example/careers/jobs?page=2": _listing(
            JOB1, next_href="/careers/jobs?page=3"
        ),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason == "no_new_jobs"
    assert len(result.jobs) == 1


def test_crawl_refuses_to_leave_the_employer_host():
    pages = {
        "https://acme.example/careers/jobs": _listing(
            JOB1, next_href="https://other.example/jobs?page=2"
        ),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason == "offsite"
    assert result.refused == ["https://other.example/jobs?page=2"]


def test_crawl_refuses_a_sibling_section_on_the_same_host():
    pages = {
        "https://acme.example/careers": _html(
            _job_link("/careers/job/1", "Engineer") + '<a href="/blog?page=2" rel="next">Next</a>'
        ),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason == "offsite"
    assert result.refused == ["https://acme.example/blog?page=2"]


def test_crawl_refuses_to_leave_the_careers_section_tree():
    """A careers page must not follow a pager into /blog, /press or /news."""
    pages = {
        "https://acme.example/careers": _html(
            _job_link("/careers/job/1", "Engineer") + '<a href="/news?page=2" rel="next">Next</a>'
        ),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason == "offsite"
    assert result.refused == ["https://acme.example/news?page=2"]


def test_crawl_follows_pagination_within_the_careers_tree():
    """Employers do split one list across /careers, /careers/jobs, ?page=N."""
    pages = {
        "https://acme.example/careers": _listing(JOB1, next_href="/careers/engineering?page=2"),
        "https://acme.example/careers/engineering?page=2": _listing(JOB2),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason in TERMINAL_REASONS
    assert result.pages_crawled == 2
    assert {job.url for job in result.jobs} == {
        "https://acme.example/careers/opening?id=1",
        "https://acme.example/careers/opening?id=2",
    }


def test_page_cap_of_one_prevents_any_following():
    pages = {
        "https://acme.example/careers/jobs": _listing(JOB1, next_href="/careers/jobs?page=2"),
        "https://acme.example/careers/jobs?page=2": _listing(JOB2),
    }
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs",
        _scrape,
        client=_FakeClient(pages),
        max_pages=1,
    )
    assert result.pages_crawled == 1
    assert result.stop_reason == "page_cap"
    assert result.jobs  # page 1 is still collected


def test_max_pages_zero_is_rejected():
    with pytest.raises(ValueError):
        crawl_careers_pages("https://acme.example/careers/jobs", _scrape, client=_FakeClient({}), max_pages=0)


def test_fetch_error_names_the_failing_url():
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs", _scrape, client=_FakeClient({})
    )
    assert result.stop_reason == "fetch_error"
    assert "acme.example" in result.stop_detail


def test_load_more_is_reported_rather_than_silently_ignored():
    pages = {"https://acme.example/careers/jobs": _html('<button>Load more jobs</button>')}
    result = crawl_careers_pages(
        "https://acme.example/careers/jobs", _scrape, client=_FakeClient(pages)
    )
    assert result.stop_reason == "load_more_unsupported"
    assert result.signals and result.signals[0].kind == "load_more"


# --- url normalization -----------------------------------------------------


def test_normalize_url_collapses_spelling_differences():
    assert pag.normalize_url("https://Acme.Example/careers/jobs/") == "https://acme.example/careers/jobs"
    assert (
        pag.normalize_url("https://acme.example/careers/jobs?utm_source=x#top")
        == "https://acme.example/careers/jobs"
    )
    assert pag.normalize_url("https://acme.example:443/careers/jobs") == "https://acme.example/careers/jobs"


def test_page_param_builder_round_trips():
    assert pag.paginated_variant("https://acme.example/jobs?page=1&q=py", 2) == (
        "https://acme.example/jobs?page=2&q=py"
    )
    assert pag.paginated_variant("https://acme.example/jobs", 3) == "https://acme.example/jobs?page=3"


def test_crawl_result_serializes_for_a_benchmark():
    payload = CrawlResult(start_url="https://acme.example/careers").to_dict()
    assert payload["pages_crawled"] == 0
    assert payload["stop_reason"] in TERMINAL_REASONS
    assert payload["job_urls"] == []


def test_is_disabled_detects_common_markers():
    assert is_disabled(_first_anchor(_html('<a href="/x" disabled>Next</a>'))) is True
    assert is_disabled(_first_anchor(_html('<a href="/x" aria-disabled="true">Next</a>'))) is True
    assert is_disabled(_first_anchor(_html('<a href="/x" class="next disabled">Next</a>'))) is True
    assert is_disabled(_first_anchor(_html('<a href="/x" class="next">Next</a>'))) is False


# --- Regression: facet "See more" and button pagers (real sites, 2026-10-02) ---


def test_facet_see_more_is_not_a_next_page_signal():
    """Zalando's filter panel has a "See more" that expands categories.

    Following it never adds a job, so it must not outrank the real pager.
    """
    html = _html(
        "<aside><section><h3>Job Category</h3>"
        "<label>Legal, Compliance &amp; Governance</label>"
        "<button>See more<img src='/i/plus.svg'/></button>"
        "</section></aside>"
        "<main><a href='/en/jobs/1'>Some Job</a></main>"
    )
    assert pag.find_next_page(html, "https://jobs.zalando.com/en/jobs") is None


def test_load_more_beside_a_facet_panel_still_counts():
    """A results-scoped "Show more" is a real signal even with filters on the page."""
    html = _html(
        "<aside><button>See more</button></aside>"
        "<main><a href='/en/jobs/1'>Some Job</a>"
        "<button>Show more jobs</button></main>"
    )
    signal = pag.find_next_page(html, "https://acme.example/careers")
    assert signal is not None
    assert signal.kind == "load_more"
    assert signal.source == "control"


def test_button_pager_yields_a_fetchable_next_page_url():
    """Zalando's pager is buttons, not links: no href for the crawler to read.

    The step is still ?page=N, so the signal must carry a URL or the crawl
    stops at page 1 with load_more_unsupported.
    """
    html = _html(
        "<main><a href='/en/jobs/1'>Some Job</a>"
        "<section><button aria-label='Previous page button'>prev</button>"
        "<button aria-label='Next page button'><img src='/i/pagination.svg'/></button>"
        "</section></main>"
    )
    signal = pag.find_next_page(html, "https://jobs.zalando.com/en/jobs?page=1")
    assert signal is not None
    assert signal.source == "button_pager"
    assert signal.fetchable is True
    assert signal.url == "https://jobs.zalando.com/en/jobs?page=2"


def test_button_pager_steps_forward_from_the_current_page():
    html = _html("<main><button aria-label='Next page button'>next</button></main>")
    signal = pag.find_next_page(html, "https://acme.example/careers?page=4")
    assert signal is not None
    assert signal.url == "https://acme.example/careers?page=5"


def test_carousel_next_is_not_treated_as_a_page_step():
    """A bare "Next" names no page, so it is not a pager step."""
    html = _html("<main><div class='carousel'><button aria-label='Next'>Next</button></div></main>")
    assert pag.find_next_page(html, "https://acme.example/careers") is None
