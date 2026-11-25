"""M1 B3: the ``name_collect`` worker and its enqueue surface.

The properties under test (plan-source-runs.md §Run flow, §Open work 2):

- a source with no recipe is read by one authored recipe, and the names the
  replay yields become ``pending`` candidates;
- a stored verified recipe is replayed with no model call and no capture;
- a replay that shrinks past the tolerance is drift: the recipe is retired and
  re-authored;
- names are deduped by canonical key against the whole ``companies`` table, so
  a second run adds nothing and reports duplicates instead;
- the source boundary is the checkpoint: a pause stops there, keeps the rest
  queued, and still writes the names the finished sources found;
- a page whose markup carries no name list is ``zero_yield``, not a failure;
- the API refuses non-http(s) URLs and ATS board URLs, and enqueues one
  manifest row per pasted URL.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from jobsearch_api import routes as api_routes
from jobsearch_api.app import create_app
from jobsearch_core.db.models import Company, CompanyRecipe, JobRun, RunSource, Source
from jobsearch_core.db.session import get_session
from jobsearch_core.source_runs import _collect_source, run_name_collect
from jobsearch_worker.consumer import claim_next_run, enqueue_name_collect, execute_run

SOURCE = "https://dir.example/companies"
OTHER = "https://dir.example/startups"
_NAMES = [
    "Acme Oy",
    "Beta SIA",
    "Gamma Ltd",
    "Delta Oy",
    "Epsilon AB",
    "Zeta GmbH",
    "Eta Oy",
    "Theta SIA",
    "Iota Ltd",
    "Kappa Oy",
    "Lambda AB",
    "Mu GmbH",
]


def _listing(names: list[str]) -> str:
    rows = "\n".join(
        f'<li class="co"><a href="/companies/co{chr(ord("a") + index)}">{name}</a></li>'
        for index, name in enumerate(names)
    )
    return f"<html><body><ul>{rows}</ul></body></html>"


def _recipe_reply() -> dict:
    return {
        "kind": "dom",
        "container": "li.co",
        "link": {"selector": "a", "attr": "href"},
        "title": {"selector": "a"},
    }


class FakeClient:
    """The capture and replay reads, scripted per test."""

    def __init__(
        self,
        html: str = "<html></html>",
        *,
        rendered: str | None = None,
        error: Exception | None = None,
    ):
        self.html = html
        self.rendered = rendered
        self.error = error
        self.fetched: list[str] = []
        self.rendered_calls = 0
        self.xhr_calls = 0
        self.closed = False

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        if self.error is not None:
            raise self.error
        return self.html

    def fetch_rendered(self, url: str) -> str:
        self.rendered_calls += 1
        return self.rendered or self.html

    def fetch_json(self, url: str) -> str:
        raise AssertionError("no api recipe is authored in these tests")

    def fetch_rendered_with_xhr(self, url: str):
        self.xhr_calls += 1
        if self.error is not None:
            raise self.error
        return self.rendered or self.html, []

    def reset_browser(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeLLM:
    model = "fake/author"

    def __init__(self, reply: dict | None = None):
        self.reply = reply if reply is not None else _recipe_reply()
        self.calls = 0

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return json.dumps(self.reply)


def _started_run(session, urls: list[str], **kwargs) -> JobRun:
    run = enqueue_name_collect(session, urls, **kwargs)
    claimed = claim_next_run(session)
    assert claimed is not None and claimed.id == run.id
    return claimed


def _finish(session, run: JobRun) -> None:
    """Close a run executed directly, so the next enqueue is not a conflict."""
    session.expire_all()
    stored = session.get(JobRun, run.id)
    stored.status = "completed"
    session.commit()


def _rows(session, run: JobRun) -> list[RunSource]:
    session.expire_all()
    return (
        session.query(RunSource)
        .filter(RunSource.run_id == run.id)
        .order_by(RunSource.id)
        .all()
    )


def _companies(session) -> list[Company]:
    session.expire_all()
    return session.query(Company).order_by(Company.id).all()


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = create_app(database_url)
    with TestClient(app) as client:
        yield client


# --- worker -------------------------------------------------------------------


def test_a_source_without_a_recipe_is_authored_once_and_its_names_are_inserted(session):
    run = _started_run(session, [SOURCE], batch_size=10)
    llm = FakeLLM()
    client = FakeClient(_listing(_NAMES))

    summary = run_name_collect(session, run, client=client, llm=llm)

    assert llm.calls == 1
    assert summary["authored"] == 1
    assert summary["replayed"] == 0
    assert summary["identities"] == len(_NAMES)
    assert summary["added"] == len(_NAMES)

    companies = _companies(session)
    assert [company.name for company in companies] == _NAMES
    assert {company.status for company in companies} == {"pending"}
    # Every candidate names the run whose listing produced it (0015): that is
    # what the review screen shows as "these came from that page".
    assert {company.run_id for company in companies} == {run.id}
    # Suffix-derived country only where the suffix is unambiguous.
    by_name = {company.name: company.country for company in companies}
    assert by_name["Acme Oy"] == "FI"
    assert by_name["Beta SIA"] == "LV"
    assert by_name["Epsilon AB"] == "SE"
    assert by_name["Zeta GmbH"] == "DE"
    assert by_name["Gamma Ltd"] is None

    row = _rows(session, run)[0]
    assert row.status == "completed"
    assert row.extraction == "llm"
    assert row.identities_found == len(_NAMES)
    assert row.candidates_added == len(_NAMES)
    assert row.candidates_duplicate == 0

    recipe = session.get(CompanyRecipe, row.recipe_id)
    assert recipe.scope == "m1_url"
    assert recipe.target_url == SOURCE
    assert recipe.company_id is None
    assert recipe.status == "verified"

    source = session.query(Source).filter(Source.url == SOURCE).one()
    assert source.recipe_id == row.recipe_id
    assert source.last_verified_count == len(_NAMES)
    assert "first verified run" in source.replay_check
    # The authoring trial is evidence on the row, not only in the log.
    trace = json.loads(row.author_trace)
    assert trace["status"] == "verified" and trace["kind"] == "dom"
    assert trace["trials"][0]["passed"] is True
    assert trace["trials"][0]["body"]


def test_a_failed_listing_authoring_keeps_the_trial_and_its_body(session):
    """A listing that could not be authored says why, and what was tried."""
    run = _started_run(session, [SOURCE], batch_size=10)
    llm = FakeLLM({"kind": "dom", "container": ".nope", "link": {"selector": "a", "attr": "href"},
                   "title": {"selector": "h3"}})

    summary = run_name_collect(session, run, client=FakeClient(_listing(_NAMES)), llm=llm)

    assert summary["failures"] == 1
    row = _rows(session, run)[0]
    assert row.status == "failed"
    assert "recipe authoring failed" in row.error
    trace = json.loads(row.author_trace)
    assert trace["status"] == "failed" and trace["recipe_id"] is None
    assert trace["trials"][0]["passed"] is False
    assert "matched no elements" in trace["trials"][0]["reason"]
    assert '"container": ".nope"' in trace["trials"][0]["body"]


def test_a_verified_recipe_is_replayed_with_no_model_call_and_no_capture(session):
    first = _started_run(session, [SOURCE], batch_size=10)
    run_name_collect(session, first, client=FakeClient(_listing(_NAMES)), llm=FakeLLM())
    _finish(session, first)

    second = _started_run(session, [SOURCE], batch_size=10)
    client = FakeClient(_listing(_NAMES))
    summary = run_name_collect(session, second, client=client, llm=None)

    assert summary["replayed"] == 1
    assert summary["authored"] == 0
    # A replay reads the page; a capture would have rendered it as well.
    assert client.xhr_calls == 0
    assert client.rendered_calls == 0
    assert len(_companies(session)) == len(_NAMES)

    row = _rows(session, second)[0]
    assert row.extraction == "recipe"
    assert row.candidates_duplicate == len(_NAMES)
    assert row.candidates_added == 0


def test_a_shrunken_replay_is_drift_and_retires_the_recipe(session):
    first = _started_run(session, [SOURCE], batch_size=10)
    run_name_collect(session, first, client=FakeClient(_listing(_NAMES)), llm=FakeLLM())
    _finish(session, first)

    source = session.query(Source).filter(Source.url == SOURCE).one()
    source.last_verified_count = len(_NAMES) * 4
    session.commit()

    second = _started_run(session, [SOURCE], batch_size=10)
    llm = FakeLLM()
    summary = run_name_collect(
        session, second, client=FakeClient(_listing(_NAMES)), llm=llm
    )

    assert llm.calls == 1
    assert summary["authored"] == 1
    recipes = session.query(CompanyRecipe).order_by(CompanyRecipe.id).all()
    assert [recipe.status for recipe in recipes] == ["stale", "verified"]
    assert _rows(session, second)[0].extraction == "llm"


def test_a_drifted_replay_keeps_its_names_when_re_authoring_fails(session):
    """A page that listed names is not lost because a new recipe could not be written."""
    first = _started_run(session, [SOURCE], batch_size=10)
    run_name_collect(session, first, client=FakeClient(_listing(_NAMES)), llm=FakeLLM())
    _finish(session, first)

    source = session.query(Source).filter(Source.url == SOURCE).one()
    source.last_verified_count = len(_NAMES) * 4
    session.commit()

    second = _started_run(session, [SOURCE], batch_size=10)
    summary = run_name_collect(
        session,
        second,
        client=FakeClient(_listing(_NAMES)),
        llm=FakeLLM(reply={"kind": "api"}),
    )

    row = _rows(session, second)[0]
    assert row.status == "completed"
    assert "drift" in row.error and "re-authoring failed" in row.error
    assert summary["authored"] == 0
    assert len(_companies(session)) == len(_NAMES)
    # The failed replay never becomes the baseline the next run measures against.
    assert session.get(Source, source.id).last_verified_count == len(_NAMES) * 4


def test_a_page_with_no_name_list_is_zero_yield_not_a_failure(session):
    run = _started_run(session, [SOURCE])
    html = '<html><body><a href="/about">About us</a><a href="/contact">Contact</a></body></html>'

    summary = run_name_collect(session, run, client=FakeClient(html), llm=FakeLLM())

    row = _rows(session, run)[0]
    assert row.status == "zero_yield"
    assert "no company names" in row.error
    assert summary["zero_yield"] == 1
    assert summary["failures"] == 0
    assert _companies(session) == []


_ARTICLE = (
    "<html><body><article><h1>Finnish tech to watch</h1>"
    "<p>Reaktor, Supercell and Wolt are all hiring engineers in Helsinki.</p>"
    "</article></body></html>"
)


class MiningLLM:
    """Answers the PX4 mining prompt and records every call."""

    model = "fake/mine"

    def __init__(self, names: list[str]):
        self.names = names
        self.calls = 0

    def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        return json.dumps(
            {
                "companies": [
                    {"company_name": n, "tech_employer": "yes", "confidence": 0.9}
                    for n in self.names
                ]
            }
        )


def test_a_prose_page_is_mined_once_and_saves_no_recipe(session, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    run = _started_run(session, [SOURCE], batch_size=10)
    llm = MiningLLM(["Reaktor", "Supercell", "Wolt"])

    summary = run_name_collect(session, run, client=FakeClient(_ARTICLE), llm=llm)

    row = _rows(session, run)[0]
    assert llm.calls == 1
    assert row.status == "completed"
    assert row.extraction == "llm"
    assert row.recipe_id is None
    assert "mined by the model" in row.error
    assert summary["authored"] == 0
    assert sorted(c.name for c in _companies(session)) == ["Reaktor", "Supercell", "Wolt"]
    assert session.query(CompanyRecipe).count() == 0
    assert session.query(Source).one().last_verified_count in (None, 0)


def test_a_prose_page_with_no_names_stays_zero_yield(session, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "cache"))
    run = _started_run(session, [SOURCE])

    summary = run_name_collect(session, run, client=FakeClient(_ARTICLE), llm=MiningLLM([]))

    row = _rows(session, run)[0]
    assert row.status == "zero_yield"
    assert "no company names" in row.error
    assert summary["failures"] == 0
    assert _companies(session) == []


def test_a_page_with_a_name_list_is_never_mined(session):
    run = _started_run(session, [SOURCE], batch_size=10)
    llm = FakeLLM()

    run_name_collect(session, run, client=FakeClient(_listing(_NAMES)), llm=llm)

    assert llm.calls == 1  # the recipe authoring call only
    assert _rows(session, run)[0].recipe_id is not None


def test_a_sitemap_source_fails_before_any_authoring_trial(session):
    """A sitemap is not a name-collect listing: fail the row, name the fix."""
    run = _started_run(session, [SOURCE])
    xml = (
        '<?xml version="1.0"?><urlset>'
        "<url><loc>https://dir.example/jobs/engineer-1234</loc></url>"
        "</urlset>"
    )
    llm = FakeLLM()

    summary = run_name_collect(session, run, client=FakeClient(xml), llm=llm)

    assert llm.calls == 0
    assert summary["failures"] == 1
    row = _rows(session, run)[0]
    assert row.status == "failed"
    assert "sitemap source: not supported" in row.error


def test_a_fetch_failure_is_one_failed_row_and_the_run_continues(session):
    broken = "https://dir.example/broken"
    run = _started_run(session, [broken, SOURCE], batch_size=10)

    class SplitClient(FakeClient):
        def fetch(self, url: str) -> str:
            if url == broken:
                raise RuntimeError("connection refused")
            return super().fetch(url)

        def fetch_rendered_with_xhr(self, url: str):
            if url == broken:
                raise RuntimeError("connection refused")
            return super().fetch_rendered_with_xhr(url)

    summary = run_name_collect(
        session, run, client=SplitClient(_listing(_NAMES)), llm=FakeLLM()
    )

    rows = _rows(session, run)
    assert [row.status for row in rows] == ["failed", "completed"]
    assert "connection refused" in rows[0].error
    assert summary["failures"] == 1
    assert len(_companies(session)) == len(_NAMES)


def test_a_pause_stops_at_the_source_boundary_and_keeps_the_rest_queued(session, monkeypatch):
    run = _started_run(session, [SOURCE, OTHER], batch_size=1)
    original = _collect_source
    calls = {"count": 0}

    def pause_after_first(*args, **kwargs):
        result = original(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            # End this session's transaction before another connection writes.
            args[0].commit()
            other = get_session()
            try:
                stored = other.get(JobRun, run.id)
                stored.status = "paused"
                other.commit()
            finally:
                other.close()
        return result

    monkeypatch.setattr("jobsearch_core.source_runs._collect_source", pause_after_first)

    summary = run_name_collect(
        session,
        run,
        client=FakeClient(_listing(_NAMES)),
        llm=FakeLLM(),
    )

    assert summary["stopped"] == "paused"
    rows = _rows(session, run)
    assert [row.status for row in rows] == ["completed", "queued"]
    # The finished batch was flushed before the stop: its names are not lost.
    assert len(_companies(session)) == len(_NAMES)
    assert session.get(JobRun, run.id).status == "paused"


# --- API ----------------------------------------------------------------------


def test_name_collect_enqueues_one_manifest_row_per_url(api_client):
    response = api_client.post(
        "/runs/name-collect",
        json={"urls": [SOURCE, OTHER, SOURCE], "batch_size": 5, "page_ceiling": 3},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    run = api_client.get(f"/runs/{run_id}").json()
    assert run["run_type"] == "name_collect"
    assert run["params"]["batch_size"] == 5
    assert run["params"]["page_ceiling"] == 3
    assert len(run["params"]["source_ids"]) == 2

    sources = api_client.get(f"/runs/{run_id}/sources").json()
    assert [row["source_url"] for row in sources] == [SOURCE, OTHER]
    assert {row["status"] for row in sources} == {"queued"}
    assert all(row["source_id"] is not None and row["company_id"] is None for row in sources)


def test_name_collect_reuses_a_known_source_row(api_client):
    first = api_client.post("/runs/name-collect", json={"urls": [SOURCE]}).json()["run_id"]
    session = get_session()
    try:
        run = session.get(JobRun, first)
        run.status = "cancelled"
        session.commit()
        source_id = session.query(Source).filter(Source.url == SOURCE).one().id
    finally:
        session.close()

    second = api_client.post("/runs/name-collect", json={"urls": [SOURCE]}).json()["run_id"]
    sources = api_client.get(f"/runs/{second}/sources").json()
    assert [row["source_id"] for row in sources] == [source_id]


def test_name_collect_refuses_bad_and_ats_urls(api_client):
    bad = api_client.post("/runs/name-collect", json={"urls": ["ftp://dir.example/x"]})
    assert bad.status_code == 422
    assert "Invalid source URLs" in bad.json()["detail"]

    ats = api_client.post(
        "/runs/name-collect", json={"urls": ["https://jobs.lever.co/acme/1234"]}
    )
    assert ats.status_code == 422
    assert "ATS board" in ats.json()["detail"]


def test_a_second_name_collect_run_is_conflict_while_one_is_queued(api_client):
    assert api_client.post("/runs/name-collect", json={"urls": [SOURCE]}).status_code == 202
    second = api_client.post("/runs/name-collect", json={"urls": [OTHER]})
    assert second.status_code == 409
    assert "already active" in second.json()["detail"]


def test_name_collect_ack_stays_queued_when_a_worker_claims_first(api_client, monkeypatch):
    """The 202 ack promises "queued" even if a worker claims the row first.

    The worker polls independently of the API request; it can set the run to
    ``running`` between the enqueue's final commit and the response being
    serialised. ``RunQueuedResponse`` only permits ``"queued"``, so the ack must
    not leak the live status — ``GET /runs/{id}`` is where that belongs.
    """
    def claimed_enqueue(session, urls, **kwargs):
        run = enqueue_name_collect(session, urls, **kwargs)
        run.status = "running"
        session.commit()
        return run

    monkeypatch.setattr(api_routes, "enqueue_name_collect", claimed_enqueue)

    response = api_client.post("/runs/name-collect", json={"urls": [SOURCE]})
    assert response.status_code == 202
    assert response.json()["status"] == "queued"

    run_id = response.json()["run_id"]
    assert api_client.get(f"/runs/{run_id}").json()["status"] == "running"


def test_execute_run_dispatches_name_collect(session, monkeypatch):
    run = enqueue_name_collect(session, [SOURCE])
    claimed = claim_next_run(session)
    assert claimed is not None

    seen: list[int] = []

    def fake_collect(session_, run_):
        seen.append(run_.id)
        return {"run_type": "name_collect", "sources": 1}

    monkeypatch.setattr("jobsearch_worker.consumer.run_name_collect", fake_collect)

    execute_run(session, claimed)

    assert seen == [run.id]
    session.expire_all()
    assert session.get(JobRun, run.id).status == "completed"
