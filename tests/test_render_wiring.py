"""Phase 4.3 S3/S4 wiring: Class D render and capture persistence in the scrape path.

Uses a fake client (no browser). The render gate is exercised through
``scrape_company``; S4 rows are checked against the database.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from jobsearch_core.db.models import Company, CompanyXhrCapture
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.ingest.scrapers import pipeline

from tests.test_site_class import SPA_SHELL, _nvidia_api_capture, _nvidia_rendered_html

CAREERS = "https://jobs.nvidia.com/careers"


def _company(db_url: str) -> int:
    init_db(db_url)
    session = get_session()
    company = Company(name="NVIDIA", country="US", status="known", verification_status="verified")
    session.add(company)
    session.commit()
    company_id = company.id
    session.close()
    return company_id


def _rows(company_id: int) -> list[CompanyXhrCapture]:
    session = get_session()
    try:
        return list(session.query(CompanyXhrCapture).filter_by(company_id=company_id).all())
    finally:
        session.close()


def _client(static_html: str, rendered_html: str, captures) -> MagicMock:
    client = MagicMock()
    client.fetch.return_value = static_html
    client.fetch_rendered.return_value = rendered_html
    client.fetch_rendered_with_xhr.return_value = (rendered_html, captures)
    return client


def test_class_d_page_renders_and_writes_rows(db_url, monkeypatch):
    company_id = _company(db_url)
    client = _client(SPA_SHELL, _nvidia_rendered_html(), [_nvidia_api_capture()])
    seen = {}

    def fake_select(url, html, _client):
        seen["html"] = html
        return []

    monkeypatch.setattr(pipeline, "_select_and_scrape", fake_select)
    monkeypatch.setattr(pipeline, "_scrape_via_ats_links", lambda *a, **k: [])
    pipeline.scrape_company("NVIDIA", CAREERS, client=client, verified=True, company_id=company_id)

    assert seen["html"] != SPA_SHELL, "rendered DOM must replace the static HTML"
    client.fetch_rendered_with_xhr.assert_called_once_with(CAREERS)
    assert len(_rows(company_id)) == 1


def test_static_page_never_renders_or_writes_rows(db_url, monkeypatch):
    company_id = _company(db_url)
    static = "<html><body><a href='/job/1'>Engineer</a></body></html>"
    client = _client(static, "", [])
    monkeypatch.setattr(pipeline, "_select_and_scrape", lambda url, html, c: [])
    monkeypatch.setattr(pipeline, "_scrape_via_ats_links", lambda *a, **k: [])
    pipeline.scrape_company("NVIDIA", CAREERS, client=client, verified=True, company_id=company_id)

    client.fetch_rendered.assert_not_called()
    client.fetch_rendered_with_xhr.assert_not_called()
    assert _rows(company_id) == []


def test_second_render_appends_a_batch(db_url, monkeypatch):
    """R1: each render writes its own batch; earlier batches are kept."""
    company_id = _company(db_url)
    monkeypatch.setattr(pipeline, "_select_and_scrape", lambda url, html, c: [])
    monkeypatch.setattr(pipeline, "_scrape_via_ats_links", lambda *a, **k: [])
    two = [_nvidia_api_capture(), _nvidia_api_capture()]
    client = _client(SPA_SHELL, _nvidia_rendered_html(), two)
    pipeline.scrape_company("NVIDIA", CAREERS, client=client, verified=True, company_id=company_id)
    assert len(_rows(company_id)) == 2

    client = _client(SPA_SHELL, _nvidia_rendered_html(), [_nvidia_api_capture()])
    pipeline.scrape_company("NVIDIA", CAREERS, client=client, verified=True, company_id=company_id)
    rows = _rows(company_id)
    assert len(rows) == 3
    assert sorted(row.capture_batch for row in rows) == [1, 1, 2]


def test_no_company_id_persists_nothing(db_url, monkeypatch):
    company_id = _company(db_url)
    client = _client(SPA_SHELL, _nvidia_rendered_html(), [_nvidia_api_capture()])
    monkeypatch.setattr(pipeline, "_select_and_scrape", lambda url, html, c: [])
    monkeypatch.setattr(pipeline, "_scrape_via_ats_links", lambda *a, **k: [])
    pipeline.scrape_company("NVIDIA", CAREERS, client=client, verified=True)
    assert _rows(company_id) == []


def test_render_failure_keeps_static_html(db_url, monkeypatch):
    company_id = _company(db_url)
    client = _client(SPA_SHELL, "", [])
    client.fetch_rendered_with_xhr.side_effect = RuntimeError("chromium missing")
    seen = {}

    def fake_select(url, html, _client):
        seen["html"] = html
        return []

    monkeypatch.setattr(pipeline, "_select_and_scrape", fake_select)
    monkeypatch.setattr(pipeline, "_scrape_via_ats_links", lambda *a, **k: [])
    pipeline.scrape_company("NVIDIA", CAREERS, client=client, verified=True, company_id=company_id)
    assert seen["html"] == SPA_SHELL
    assert _rows(company_id) == []

