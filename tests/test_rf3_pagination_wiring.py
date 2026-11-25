"""RF3: the generic scrape follows next pages."""

from __future__ import annotations

from jobsearch_core.ingest.scrapers import pipeline

BASE = "https://acme.example/careers"


def _page(n: int, nxt: str | None) -> str:
    link = f'<a rel="next" href="{nxt}">Next</a>' if nxt else ""
    return f"<html><body>page {n} {link}</body></html>"


class _Client:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        return self.pages[url]

    def close(self) -> None:
        pass


def _fake_select(url: str, html: str, client):  # noqa: ANN001
    n = url.rsplit("=", 1)[-1] if "=" in url else "1"
    return [pipeline.ScrapedJob(title=f"Job {n}", company="Acme", url=f"https://acme.example/j/{n}")]


def test_next_pages_are_followed(monkeypatch):
    monkeypatch.setattr(pipeline, "_select_and_scrape", _fake_select)
    client = _Client(
        {
            f"{BASE}?page=2": _page(2, f"{BASE}?page=3"),
            f"{BASE}?page=3": _page(3, None),
        }
    )
    jobs = pipeline._scrape_all_pages(BASE, _page(1, f"{BASE}?page=2"), client)
    assert [j.title for j in jobs] == ["Job 1", "Job 2", "Job 3"]
    assert client.fetched == [f"{BASE}?page=2", f"{BASE}?page=3"]


def test_single_page_fetches_nothing_more(monkeypatch):
    monkeypatch.setattr(pipeline, "_select_and_scrape", _fake_select)
    client = _Client({})
    jobs = pipeline._scrape_all_pages(BASE, _page(1, None), client)
    assert len(jobs) == 1
    assert client.fetched == []
