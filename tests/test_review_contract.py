"""M1 demo gate: the batch review endpoint and the review-screen query surface.

Properties pinned here (plan-source-runs.md §API, §Demo gate contract):

- a batch accept writes the status the screen asked for and nothing else — the
  resolved entry and ``careers_url`` are evidence, never inputs;
- accepting verifies exactly as the single-row review does, so the scrape gate
  only ever sees one kind of accepted row;
- ids that no longer exist come back as ``unknown_ids`` while the rest of the
  batch still applies, and repeated ids collapse;
- the request is bounded (500 ids) and status-limited before it reaches the
  database;
- ``has_entry`` and ``order`` are what the review screen reads with;
- a company response carries the entry evidence and the run that listed it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.db.models import Company, JobRun, RunSource
from jobsearch_core.db.session import get_session


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url)
    with TestClient(app) as client:
        yield client


def _pending(
    name: str,
    *,
    careers_url: str | None = None,
    entry: str | None = None,
    run_id: int | None = None,
) -> Company:
    return Company(
        name=name,
        status="pending",
        careers_url=careers_url,
        open_positions_url=entry,
        open_positions_landing_url="https://acme.example/tyopaikat" if entry else None,
        open_positions_link_text="Avoimet työpaikat" if entry else None,
        run_id=run_id,
    )


def _seed(*companies: Company) -> list[int]:
    session = get_session()
    session.add_all(companies)
    session.commit()
    ids = [company.id for company in companies]
    session.close()
    return ids


def test_a_batch_accept_sets_the_status_and_leaves_the_urls_alone(api_client):
    entry_only, with_both = (
        _pending("Entry Only", entry="https://entry.example/jobs"),
        _pending(
            "With Both",
            careers_url="https://both.example/careers",
            entry="https://both.example/tyot",
        ),
    )
    first, second = _seed(entry_only, with_both)

    response = api_client.post(
        "/companies/review-batch",
        json={"company_ids": [first, second], "status": "accepted"},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 2, "unknown_ids": []}

    rows = {row["id"]: row for row in api_client.get("/companies", params={"status": "accepted"}).json()}
    assert set(rows) == {first, second}

    accepted = rows[first]
    assert accepted["verification_status"] == "verified"
    assert accepted["verified_at"] is not None
    # The entry is what the scrape will use; the accept cannot rewrite it, and
    # it cannot invent a careers_url either.
    assert accepted["careers_url"] is None
    assert accepted["open_positions_url"] == "https://entry.example/jobs"
    assert accepted["open_positions_landing_url"] == "https://acme.example/tyopaikat"
    assert accepted["open_positions_link_text"] == "Avoimet työpaikat"

    untouched = rows[second]
    assert untouched["careers_url"] == "https://both.example/careers"
    assert untouched["open_positions_url"] == "https://both.example/tyot"


def test_a_batch_reject_does_not_claim_verification(api_client):
    (company_id,) = _seed(_pending("Reject Me", entry="https://reject.example/jobs"))

    response = api_client.post(
        "/companies/review-batch",
        json={"company_ids": [company_id], "status": "rejected"},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 1, "unknown_ids": []}

    row = api_client.get(f"/companies/{company_id}").json()
    assert row["status"] == "rejected"
    assert row["verification_status"] == "unverified"
    assert row["verified_at"] is None


def test_unknown_ids_are_reported_and_the_known_ones_still_apply(api_client):
    (company_id,) = _seed(_pending("Still Here", entry="https://here.example/jobs"))

    response = api_client.post(
        "/companies/review-batch",
        json={"company_ids": [company_id, 99999, company_id], "status": "accepted"},
    )
    assert response.status_code == 200
    assert response.json() == {"updated": 1, "unknown_ids": [99999]}

    assert api_client.get(f"/companies/{company_id}").json()["status"] == "accepted"


def test_the_batch_is_bounded_and_accepts_only_the_two_gate_statuses(api_client):
    def post(payload):
        return api_client.post("/companies/review-batch", json=payload).status_code

    assert post({"company_ids": [], "status": "accepted"}) == 422
    assert post({"company_ids": list(range(1, 502)), "status": "accepted"}) == 422
    assert post({"company_ids": [1], "status": "pending"}) == 422
    assert post({"company_ids": [1], "status": "approved"}) == 422
    assert post({"status": "accepted"}) == 422


def test_has_entry_selects_the_review_rows_and_order_reverses_them(api_client):
    both, no_entry, entry_only = (
        _pending("Both", careers_url="https://both.example/careers", entry="https://both.example/jobs"),
        _pending("No Entry", careers_url="https://none.example/careers"),
        _pending("Entry Only", entry="https://entry.example/jobs"),
    )
    both_id, no_entry_id, entry_only_id = _seed(both, no_entry, entry_only)

    with_entry = api_client.get("/companies", params={"has_entry": 1}).json()
    assert {row["id"] for row in with_entry} == {both_id, entry_only_id}

    without_entry = api_client.get("/companies", params={"has_entry": 0}).json()
    assert {row["id"] for row in without_entry} == {no_entry_id}

    newest_first = api_client.get("/companies", params={"order": "id_desc"}).json()
    assert [row["id"] for row in newest_first] == sorted(
        [both_id, no_entry_id, entry_only_id], reverse=True
    )

    # The review screen's actual request: pending rows with an entry, newest first.
    review_screen = api_client.get(
        "/companies", params={"status": "pending", "has_entry": 1, "order": "id_desc"}
    ).json()
    assert [row["id"] for row in review_screen] == [entry_only_id, both_id]

    assert api_client.get("/companies", params={"order": "name"}).status_code == 422


def _run(session, *, status="completed", run_type="careers_search") -> JobRun:
    run = JobRun(run_type=run_type, status=status, params="{}")
    session.add(run)
    session.commit()
    return run


def _manifest_row(session, run, company, *, status, identities=0, finished_at=None):
    row = RunSource(
        run_id=run.id,
        company_id=company.id,
        status=status,
        identities_found=identities,
        finished_at=finished_at,
    )
    session.add(row)
    session.commit()
    return row


def test_statuses_selects_the_resolved_entries_scope(api_client):
    session = get_session()
    session.add_all(
        [
            Company(name="Pending", status="pending", open_positions_url="https://a.example/jobs"),
            Company(name="Accepted", status="accepted", open_positions_url="https://b.example/jobs"),
            Company(name="Known", status="known", open_positions_url="https://c.example/jobs"),
            Company(name="Rejected", status="rejected", open_positions_url="https://d.example/jobs"),
        ]
    )
    session.commit()
    session.close()

    rows = api_client.get(
        "/companies", params={"statuses": ["accepted", "known"], "has_entry": 1}
    ).json()
    assert [row["name"] for row in rows] == ["Accepted", "Known"]

    # The single-value filter keeps working next to the repeatable one.
    assert [row["name"] for row in api_client.get("/companies", params={"status": "pending"}).json()] == [
        "Pending"
    ]


def test_entry_run_comes_from_the_newest_completed_resolving_manifest_row(api_client):
    session = get_session()
    old_run = _run(session)
    skip_run = _run(session)
    miss_run = _run(session)
    new_run = _run(session, status="running")
    resolved = Company(name="Resolved", status="accepted", open_positions_url="https://r.example/jobs")
    listing_only = Company(name="Never Resolved", status="accepted")
    session.add_all([resolved, listing_only])
    session.commit()

    _manifest_row(
        session,
        old_run,
        resolved,
        status="completed",
        identities=1,
        finished_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _manifest_row(session, skip_run, resolved, status="completed")  # careers_url skip
    _manifest_row(session, miss_run, resolved, status="zero_yield")
    _manifest_row(session, new_run, resolved, status="running")  # not terminal yet
    _manifest_row(
        session,
        new_run,
        resolved,
        status="completed",
        identities=1,
        finished_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    resolved_id, listing_only_id, new_run_id = resolved.id, listing_only.id, new_run.id
    session.close()

    rows = {
        row["id"]: row
        for row in api_client.get("/companies", params={"statuses": ["accepted"], "has_entry": 1}).json()
    }
    assert set(rows) == {resolved_id}
    assert rows[resolved_id]["entry_run_id"] == new_run_id
    assert rows[resolved_id]["entry_resolved_at"].startswith("2026-02-01")

    # A row with no manifest evidence reports no resolving run, not a guess.
    single = api_client.get(f"/companies/{listing_only_id}").json()
    assert single["entry_run_id"] is None
    assert single["entry_resolved_at"] is None


def test_a_company_response_names_the_entry_and_the_run_that_listed_it(api_client):
    session = get_session()
    run = JobRun(run_type="name_collect", status="completed", params="{}")
    session.add(run)
    session.commit()
    company = _pending("Listed Co", entry="https://listed.example/jobs", run_id=run.id)
    session.add(company)
    session.commit()
    company_id, run_id = company.id, run.id
    session.close()

    row = api_client.get(f"/companies/{company_id}").json()
    assert row["run_id"] == run_id
    assert row["open_positions_url"] == "https://listed.example/jobs"


def test_company_response_carries_the_newest_collect_scrape_and_job_count(api_client):
    from jobsearch_core.db.models import Job

    [scraped_id, never_id] = _seed(_pending("Scraped"), _pending("Never"))
    session = get_session()
    careers = JobRun(run_type="careers_search", status="completed")
    old = JobRun(run_type="collect", status="completed")
    new = JobRun(run_type="collect", status="completed")
    session.add_all([careers, old, new])
    session.flush()
    finished = datetime(2026, 11, 20, 10, 0)
    session.add_all(
        [
            RunSource(run_id=old.id, company_id=scraped_id, status="failed",
                      finished_at=datetime(2026, 11, 1)),
            RunSource(run_id=new.id, company_id=scraped_id, status="zero_yield",
                      finished_at=finished,
                      dropped='{"total": 22, "dropped": 22, "reasons": {"chrome": 22}, "examples": []}'),
            # A careers search row is not a scrape.
            RunSource(run_id=careers.id, company_id=never_id, status="completed",
                      finished_at=finished),
        ]
    )
    session.add(Job(url="https://x.example/1", raw_title="T", raw_company="Scraped",
                    company_id=scraped_id))
    session.commit()
    new_id = new.id
    session.close()

    rows = {r["name"]: r for r in api_client.get("/companies").json()}
    assert rows["Scraped"]["last_scrape_status"] == "zero_yield"
    assert rows["Scraped"]["last_scrape_run_id"] == new_id
    assert rows["Scraped"]["last_scraped_at"].startswith("2026-11-20")
    assert rows["Scraped"]["job_count"] == 1
    assert rows["Scraped"]["last_scrape_dropped"]["dropped"] == 22
    assert rows["Never"]["last_scrape_dropped"] is None
    assert rows["Never"]["last_scraped_at"] is None
    assert rows["Never"]["job_count"] == 0
