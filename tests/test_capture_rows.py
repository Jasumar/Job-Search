"""Persistence of XHR capture evidence rows (Phase 4.3 S4 + R1, plan-scraping.md).

Rows are evidence only: they never set companies.site_class. One row per capture
the render made, one ``capture_batch`` per render (R1): a later render appends a
batch instead of replacing the previous set, so two runs can be compared.
"""

from __future__ import annotations

import json
from datetime import datetime

from jobsearch_core.db.models import Company, CompanyXhrCapture
from jobsearch_core.db.session import get_session, init_db
from jobsearch_core.ingest import site_class as sc
from jobsearch_core.ingest.xhr_capture import XhrCapture

from tests.test_site_class import (
    NVIDIA_API,
    SPA_SHELL,
    _no_plain_render,
    _nvidia_api_capture,
    _plain_api_body,
    _render_with_xhr,
)

CAPTURED_AT = datetime(2026, 10, 14, 12, 0, 0)


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
    rows = (
        session.query(CompanyXhrCapture)
        .filter(CompanyXhrCapture.company_id == company_id)
        .order_by(CompanyXhrCapture.id)
        .all()
    )
    session.close()
    return rows


def _other_capture(url: str, method: str = "GET", status: int = 200) -> XhrCapture:
    return XhrCapture(
        url=url,
        method=method,
        status=status,
        resource_type="xhr",
        content_type="application/json",
        request_headers=(),
        request_body=None,
        response_body="{}",
        response_bytes=2,
        body_truncated=False,
    )


def _classify(captures, fetch_plain=_plain_api_body):
    return sc.classify_with_render(
        "https://jobs.nvidia.com/careers",
        SPA_SHELL,
        fetch_rendered=_no_plain_render,
        fetch_rendered_with_xhr=_render_with_xhr(captures),
        fetch_plain=fetch_plain,
    )


def _fetch_by_url(url):
    """Serve the NVIDIA job list for its API URL only; anything else is not a job list."""
    if url == NVIDIA_API:
        return _plain_api_body(url)
    return "<html>not json</html>"


def test_replayable_capture_row_records_match_and_samples(db_url):
    company_id = _company(db_url)
    profile = _classify([_nvidia_api_capture()])

    assert sc.record_capture_rows(company_id, profile, captured_at=CAPTURED_AT) == 1

    (row,) = _rows(company_id)
    assert row.url == NVIDIA_API
    assert row.method == "GET"
    assert row.status == 200
    assert row.replay_matches is True
    assert row.replay_reason is None
    assert row.class_a_candidate is True
    assert json.loads(row.sample_titles)
    assert row.captured_at == CAPTURED_AT
    # R1 evidence: what the body was, identified without being stored.
    assert row.content_type == "application/json"
    assert row.body_sha256 == _nvidia_api_capture().body_sha256
    assert len(row.body_sha256) == 64
    assert row.body_truncated is False
    assert row.capture_batch == 1


def test_capture_rows_do_not_set_company_site_class(db_url):
    company_id = _company(db_url)
    profile = _classify([_nvidia_api_capture()])
    sc.record_capture_rows(company_id, profile, captured_at=CAPTURED_AT)

    session = get_session()
    company = session.get(Company, company_id)
    assert company.site_class is None
    session.close()


def test_post_capture_is_null_match_not_false(db_url):
    """A refused-before-request capture has no comparison: replay_matches is NULL."""
    company_id = _company(db_url)
    profile = _classify([_nvidia_api_capture(method="POST")])
    sc.record_capture_rows(company_id, profile, captured_at=CAPTURED_AT)

    (row,) = _rows(company_id)
    assert row.method == "POST"
    assert row.replay_matches is None
    assert row.class_a_candidate is False
    assert "needs a request body" in row.replay_reason
    assert row.sample_titles is None


def test_failed_request_is_false_not_null(db_url):
    """A replay that ran and failed is a verdict of False, not an unknown."""
    company_id = _company(db_url)

    def broken(url):
        raise RuntimeError("replay blocked")

    profile = _classify([_nvidia_api_capture()], fetch_plain=broken)
    sc.record_capture_rows(company_id, profile, captured_at=CAPTURED_AT)

    (row,) = _rows(company_id)
    assert row.replay_matches is False
    assert "replay request failed" in row.replay_reason


def test_one_row_per_capture_including_mixed_outcomes(db_url):
    company_id = _company(db_url)
    captures = [
        _nvidia_api_capture(),
        _other_capture("https://jobs.nvidia.com/analytics.json"),
        _nvidia_api_capture(method="POST"),
    ]
    profile = _classify(captures, fetch_plain=_fetch_by_url)
    written = sc.record_capture_rows(company_id, profile, captured_at=CAPTURED_AT)

    assert written == 3
    rows = _rows(company_id)
    assert [r.url for r in rows] == [c.url for c in captures]
    assert rows[0].replay_matches is True
    # Non-API analytics URL: the GET is replayed, the body is not a job list.
    assert rows[1].replay_matches is False
    assert rows[1].replay_reason
    # POST refused before any request: NULL, not False.
    assert rows[2].replay_matches is None


def test_second_render_appends_a_new_batch_and_keeps_history(db_url):
    """R1: capture history is a timeline, so batch 1 survives batch 2."""
    company_id = _company(db_url)
    first = _classify([_nvidia_api_capture(), _other_capture("https://jobs.nvidia.com/a.json")])
    sc.record_capture_rows(company_id, first, captured_at=CAPTURED_AT)
    assert len(_rows(company_id)) == 2

    second = _classify([_nvidia_api_capture()])
    sc.record_capture_rows(company_id, second, captured_at=datetime(2026, 10, 15))

    rows = _rows(company_id)
    assert len(rows) == 3
    assert [r.capture_batch for r in rows] == [1, 1, 2]
    assert rows[0].captured_at == CAPTURED_AT
    assert rows[2].captured_at == datetime(2026, 10, 15)
    # The old rows are untouched evidence, not overwritten verdicts.
    assert rows[1].url == "https://jobs.nvidia.com/a.json"


def test_batch_numbers_are_monotonic_across_renders(db_url):
    company_id = _company(db_url)
    for day in (14, 15, 16):
        sc.record_capture_rows(
            company_id, _classify([_nvidia_api_capture()]), captured_at=datetime(2026, 10, day)
        )

    assert sorted({r.capture_batch for r in _rows(company_id)}) == [1, 2, 3]


def test_render_without_captures_appends_nothing_and_keeps_history(db_url):
    company_id = _company(db_url)
    sc.record_capture_rows(company_id, _classify([_nvidia_api_capture()]), captured_at=CAPTURED_AT)
    assert len(_rows(company_id)) == 1

    assert sc.record_capture_rows(company_id, _classify([]), captured_at=datetime(2026, 10, 15)) == 0
    assert len(_rows(company_id)) == 1
    # No rows observed means no batch exists for that render, so the next
    # recorded batch is still 2 (the gap is documented on record_capture_rows).
    sc.record_capture_rows(company_id, _classify([_nvidia_api_capture()]), captured_at=datetime(2026, 10, 16))
    assert sorted({r.capture_batch for r in _rows(company_id)}) == [1, 2]


def test_static_profile_writes_nothing(db_url):
    """No render, no XHR observation: a batch would be evidence of a render."""
    company_id = _company(db_url)
    static = sc.classify_site(
        "https://jobs.example.fi/careers",
        "<html><body><a href='/job/1'>Engineer</a></body></html>",
    )
    assert static.rendered is False
    assert sc.record_capture_rows(company_id, static, captured_at=CAPTURED_AT) == 0
    assert _rows(company_id) == []


def test_missing_company_writes_nothing(db_url):
    init_db(db_url)
    profile = _classify([_nvidia_api_capture()])
    assert sc.record_capture_rows(999, profile, captured_at=CAPTURED_AT) == 0


def test_writer_never_inserts_for_missing_company(db_url):
    """The writer checks the company first; it never relies on the FK for this.

    SQLite does not enforce foreign keys in this project (no PRAGMA foreign_keys),
    so an orphan insert would succeed at the DB level. The guard is in the writer.
    """
    init_db(db_url)
    profile = _classify([_nvidia_api_capture()])
    assert sc.record_capture_rows(424242, profile, captured_at=CAPTURED_AT) == 0
    session = get_session()
    assert session.query(CompanyXhrCapture).count() == 0
    session.close()

