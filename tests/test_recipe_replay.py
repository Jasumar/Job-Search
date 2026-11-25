"""R5a: production replay of a stored recipe (no network, no real LLM).

Properties under test:
- A verified recipe is replayed and its posting URL set replaces the run
  source's previous set through ``record_verified_urls``.
- A replay that raises writes no evidence: a failed fetch must never look like
  every posting disappeared.
- A ``dom`` replay retries once with the rendered page when the static HTML has
  no names, and the rendered page is the DOM-agreement arbiter.
- The posting-existence gate runs on its cadence (first accepted replay or a
  fired signal), records its verdict on the recipe body, and the stop rule
  needs an independent agreement.
- A recipe that no longer replays is retired (``stale``), never retried.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from jobsearch_core.db.models import Company, CompanyRecipe, JobRun, RecipeFlag, RunSource
from jobsearch_core.db.recipe_flags import record_flag
from jobsearch_core.ingest.recipe_replay import (
    REPLAY_ERROR,
    REPLAY_OK,
    load_verified_recipe,
    mark_stale,
    replay_stored_recipe,
)
from jobsearch_core.ingest.recipe_verify import body_hash

PAGE = "https://acme.example/careers"
API_PAGE = "https://api.acme.example/jobs"


def _verification(**overrides) -> dict:
    base = {
        "expected_count": 2,
        "sample_titles": ["Backend Engineer"],
        "url_pattern": r"^https://acme\.example/jobs/\d+$",
    }
    base.update(overrides)
    return base


def _api_body() -> dict:
    return {
        "kind": "api",
        "allowed_hosts": ["api.acme.example"],
        "verification": _verification(),
        "url_template": API_PAGE,
        "list_path": ["jobs"],
        "field_map": {
            "url": {"path": ["url"]},
            "title": {"path": ["title"]},
        },
    }


def _dom_body() -> dict:
    return {
        "kind": "dom",
        "allowed_hosts": ["acme.example"],
        "verification": _verification(url_pattern=r"/jobs/\d+$"),
        "container": "li.job",
        "link": {"selector": "a", "attr": "href"},
        "title": {"selector": "h3"},
    }


LISTING_HTML = """
<ul>
  <li class="job"><a href="/jobs/1"><h3>Backend Engineer</h3></a></li>
  <li class="job"><a href="/jobs/2"><h3>Data Engineer</h3></a></li>
</ul>
"""


class FakeClient:
    """The three reads a replay needs, scripted per test."""

    def __init__(self, *, html: str | None = None, rendered: str | None = None, payload: dict | None = None):
        self.html = html
        self.rendered = rendered
        self.payload = payload
        # Per-URL overrides for a paginated dom replay; anything absent falls back
        # to ``html`` / ``rendered``.
        self.pages: dict[str, str] = {}
        self.rendered_pages: dict[str, str] = {}
        self.fetched: list[str] = []
        self.rendered_fetched: list[str] = []
        self.rendered_calls = 0
        self.json_calls = 0

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        if url in self.pages:
            return self.pages[url]
        if self.html is None:
            raise RuntimeError("static fetch failed")
        return self.html

    def fetch_rendered(self, url: str) -> str:
        self.rendered_calls += 1
        self.rendered_fetched.append(url)
        if url in self.rendered_pages:
            return self.rendered_pages[url]
        if self.rendered is None:
            raise RuntimeError("render failed")
        return self.rendered

    def fetch_json(self, url: str) -> str:
        self.json_calls += 1
        if self.payload is None:
            raise RuntimeError("api fetch failed")
        return json.dumps(self.payload)


class FakeLLM:
    model = "test-model"

    def __init__(self, answer: dict):
        self.answer = answer
        self.calls = 0

    def complete_json(self, system: str, user: str) -> str:
        self.calls += 1
        return json.dumps(self.answer)


def _company(session, name: str = "Acme") -> Company:
    company = Company(name=name, status="accepted", careers_url=PAGE)
    session.add(company)
    session.commit()
    return company


def _run_source(session, company: Company) -> RunSource:
    run = JobRun(run_type="collect", status="running", params="{}")
    session.add(run)
    session.commit()
    row = RunSource(run_id=run.id, company_id=company.id, status="running")
    session.add(row)
    session.commit()
    return row


def _recipe(session, company: Company, body: dict, *, kind: str = "api", status: str = "verified") -> CompanyRecipe:
    now = datetime.now(UTC).replace(tzinfo=None)
    row = CompanyRecipe(
        scope="company_careers",
        company_id=company.id,
        kind=kind,
        status=status,
        body=json.dumps(body),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.commit()
    return row


def _api_payload() -> dict:
    return {
        "jobs": [
            {"url": "https://acme.example/jobs/1", "title": "Backend Engineer"},
            {"url": "https://acme.example/jobs/2", "title": "Data Engineer"},
        ]
    }


class TestLoadVerifiedRecipe:
    def test_returns_the_newest_verified_row(self, session):
        company = _company(session)
        _recipe(session, company, _api_body(), status="stale")
        newest = _recipe(session, company, _api_body())
        assert load_verified_recipe(session, company_id=company.id).id == newest.id

    def test_never_returns_a_stale_row(self, session):
        company = _company(session)
        _recipe(session, company, _api_body(), status="stale")
        assert load_verified_recipe(session, company_id=company.id) is None

    def test_requires_exactly_one_owner(self, session):
        with pytest.raises(ValueError):
            load_verified_recipe(session)
        with pytest.raises(ValueError):
            load_verified_recipe(session, company_id=1, target_url=PAGE)


class TestApiReplay:
    def test_records_the_verified_set_and_statistics(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _api_body())
        client = FakeClient(payload=_api_payload())

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=client,
        )
        session.commit()

        assert outcome.status == REPLAY_OK
        assert outcome.verified_count == 2
        assert [job.url for job in outcome.postings] == [
            "https://acme.example/jobs/1",
            "https://acme.example/jobs/2",
        ]
        assert outcome.statistics.count == 2
        assert outcome.signals == ()
        assert outcome.recipe_id == recipe.id
        session.refresh(run_source)
        assert json.loads(run_source.verified_urls) == [
            "https://acme.example/jobs/1",
            "https://acme.example/jobs/2",
        ]

    def test_replaces_the_previous_set(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        run_source.verified_urls = json.dumps(["https://acme.example/jobs/9"])
        session.commit()
        recipe = _recipe(session, company, _api_body())

        replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=FakeClient(payload=_api_payload()),
        )
        session.commit()

        session.refresh(run_source)
        assert "jobs/9" not in run_source.verified_urls

    def test_a_failed_fetch_writes_no_evidence(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _api_body())

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=FakeClient(payload=None),
        )
        session.commit()

        assert outcome.status == REPLAY_ERROR
        assert outcome.error
        session.refresh(run_source)
        assert run_source.verified_urls is None


class TestDomReplay:
    def test_a_paginated_dom_recipe_pages_and_agrees_on_page_one(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        body = _dom_body()
        body["pagination"] = {"kind": "page", "param": "page", "max_pages": 3}
        recipe = _recipe(session, company, body, kind="dom")
        page_two = '<ul><li class="job"><a href="/jobs/3"><h3>Ops Engineer</h3></a></li></ul>'
        client = FakeClient(html="<html><body><p>loading</p></body></html>", rendered=LISTING_HTML)
        client.rendered_pages[f"{PAGE}?page=2"] = page_two
        client.rendered_pages[f"{PAGE}?page=3"] = "<html><body><p>No more results</p></body></html>"

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=client,
        )

        assert outcome.status == REPLAY_OK
        assert outcome.verified_count == 3
        assert client.rendered_fetched == [PAGE, f"{PAGE}?page=2", f"{PAGE}?page=3"]
        assert outcome.statistics is not None
        assert outcome.statistics.page_counts == (2, 1)
        assert outcome.page_stop == "empty_page"
        # Page two's posting is not on the rendered first page, so agreement must
        # stay a subset rule over the first document only.
        assert outcome.agreement is not None
        assert outcome.agreement.agrees

    def test_retries_with_the_rendered_page_when_static_has_no_names(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _dom_body(), kind="dom")
        client = FakeClient(html="<html><body><p>loading</p></body></html>", rendered=LISTING_HTML)

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=client,
        )
        session.commit()

        assert outcome.status == REPLAY_OK
        assert outcome.rendered is True
        assert client.rendered_calls == 1
        assert outcome.agreement is not None
        assert outcome.agreement.agrees
        assert outcome.verified_count == 2

    def test_static_success_does_not_render(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _dom_body(), kind="dom")
        client = FakeClient(html=LISTING_HTML)

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=client,
        )

        assert outcome.status == REPLAY_OK
        assert outcome.rendered is False
        assert client.rendered_calls == 0
        assert outcome.agreement is None


class TestGate:
    def test_runs_on_the_first_accepted_replay_and_records_the_flag(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _api_body())
        llm = FakeLLM({"verdict": "postings", "items": []})

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=FakeClient(payload=_api_payload()),
            llm=llm,
        )
        session.commit()

        assert llm.calls == 1
        assert outcome.gate is not None
        assert outcome.gate.verdict == "postings"
        assert outcome.stopped is False
        flags = session.query(RecipeFlag).all()
        assert len(flags) == 1
        assert flags[0].body_sha256 == body_hash(recipe.body)
        assert flags[0].verdict == "postings"
        assert flags[0].recipe_id == recipe.id

    def test_skipped_without_an_llm(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _api_body())

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=FakeClient(payload=_api_payload()),
        )

        assert outcome.gate is None
        assert session.query(RecipeFlag).count() == 0

    def test_not_rerun_without_an_event(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _api_body())
        record_flag(
            session,
            body_sha256=body_hash(recipe.body),
            verdict="postings",
            analysis={"verdict": "postings", "items": []},
        )
        session.commit()
        llm = FakeLLM({"verdict": "postings", "items": []})

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=FakeClient(payload=_api_payload()),
            llm=llm,
        )

        assert llm.calls == 0
        assert outcome.gate is None
        assert session.query(RecipeFlag).count() == 1

    def test_a_repeat_none_stops_the_run(self, session):
        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _api_body())
        record_flag(
            session,
            body_sha256=body_hash(recipe.body),
            verdict="none",
            analysis={"verdict": "none", "items": []},
        )
        session.commit()
        llm = FakeLLM(
            {
                "verdict": "none",
                "items": [
                    {"url": "https://acme.example/jobs/1", "reason": "navigation link"},
                    {"url": "https://acme.example/jobs/2", "reason": "navigation link"},
                ],
            }
        )
        # A fired signal is the event that re-runs the gate; the prior "none" is
        # the independent agreement the stop rule needs.
        duplicate_titles = {
            "jobs": [
                {"url": "https://acme.example/jobs/1", "title": "Backend Engineer"},
                {"url": "https://acme.example/jobs/2", "title": "Backend Engineer"},
            ]
        }

        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source.id,
            recipe_row=recipe,
            page_url=PAGE,
            client=FakeClient(payload=duplicate_titles),
            llm=llm,
        )
        session.commit()

        assert outcome.gate is not None
        assert outcome.gate.verdict == "none"
        assert outcome.stopped is True
        latest = session.query(RecipeFlag).order_by(RecipeFlag.id.desc()).first()
        assert latest.stopped is True


class TestMarkStale:
    def test_retires_the_recipe(self, session):
        company = _company(session)
        recipe = _recipe(session, company, _api_body())
        mark_stale(session, recipe)
        session.commit()
        session.refresh(recipe)
        assert recipe.status == "stale"


class TestRF2JunkReplay:
    """RF2: a replay whose evidence disagrees with itself reaches the gate."""

    def _prior_run(self, session, company, urls):
        prior = _run_source(session, company)
        prior.verified_urls = json.dumps(sorted(urls))
        session.commit()

    def test_static_below_baseline_is_retried_rendered(self, session):
        company = _company(session)
        self._prior_run(session, company, [f"https://acme.example/jobs/{i}" for i in range(10, 16)])
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _dom_body(), kind="dom")
        rendered = "<ul>" + "".join(
            f'<li class="job"><a href="/jobs/{i}"><h3>Role {i}</h3></a></li>' for i in range(10, 16)
        ) + "</ul>"
        client = FakeClient(html=LISTING_HTML, rendered=rendered)

        outcome = replay_stored_recipe(
            session, run_source_id=run_source.id, recipe_row=recipe,
            page_url=PAGE, client=client, drift_checks=True,
        )

        assert outcome.ok
        assert outcome.rendered
        assert outcome.verified_count == 6
        assert "below_baseline" not in {s.name for s in outcome.signals}

    def test_below_baseline_after_render_fires_and_reaches_the_gate(self, session):
        company = _company(session)
        self._prior_run(session, company, [f"https://acme.example/jobs/{i}" for i in range(10, 20)])
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _dom_body(), kind="dom")
        client = FakeClient(html=LISTING_HTML, rendered=LISTING_HTML)
        llm = FakeLLM({"verdict": "none", "items": [
            {"url": "https://acme.example/jobs/1", "reason": "a nav link"},
        ]})
        # first_accepted would run the gate anyway: record a prior answer so only
        # the signal can trigger it.
        record_flag(
            session, body_sha256=body_hash(recipe.body), verdict="postings",
            analysis={"verdict": "postings"}, stopped=False, model_id="m",
            page_url=PAGE, recipe_id=recipe.id,
        )
        session.commit()

        outcome = replay_stored_recipe(
            session, run_source_id=run_source.id, recipe_row=recipe,
            page_url=PAGE, client=client, llm=llm, drift_checks=True,
        )

        assert "below_baseline" in {s.name for s in outcome.signals}
        assert llm.calls == 1
        assert outcome.gate_rejected

    def test_zero_dom_agreement_fires(self, session, monkeypatch):
        from jobsearch_core.ingest import recipe_replay as module
        from jobsearch_core.ingest.recipe_verify import DomAgreement

        company = _company(session)
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _dom_body(), kind="dom")
        # Static has no names -> rendered retry, so an agreement is computed.
        client = FakeClient(html="<p>shell</p>", rendered=LISTING_HTML)
        monkeypatch.setattr(
            module,
            "dom_agreement",
            lambda postings, html, url: DomAgreement(
                replay_count=2, rendered_count=5, matched=0, missing=("x", "y")
            ),
        )

        outcome = replay_stored_recipe(
            session, run_source_id=run_source.id, recipe_row=recipe,
            page_url=PAGE, client=client, drift_checks=True,
        )

        assert "no_dom_agreement" in {s.name for s in outcome.signals}

    def test_drift_checks_are_off_by_default(self, session):
        company = _company(session)
        self._prior_run(session, company, [f"https://acme.example/jobs/{i}" for i in range(10, 20)])
        run_source = _run_source(session, company)
        recipe = _recipe(session, company, _dom_body(), kind="dom")
        client = FakeClient(html=LISTING_HTML, rendered=LISTING_HTML)

        outcome = replay_stored_recipe(
            session, run_source_id=run_source.id, recipe_row=recipe,
            page_url=PAGE, client=client,
        )

        assert not outcome.rendered
        assert client.rendered_calls == 0
