from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from jobsearch_core.cli import main
from jobsearch_core.config import Settings
from jobsearch_core.run import RunSummary, print_run_summary, run_workflow
from jobsearch_core.search.careers_search import PacedSearchClient


def test_print_run_summary_output(capsys):
    summary = RunSummary(
        dry_run=True,
        remaining_actions=["one action", "two action"],
    )
    print_run_summary(summary)
    out = capsys.readouterr().out
    assert "RUN SUMMARY" in out
    assert "Dry-run: True" in out
    assert "Remaining manual actions" in out
    assert "one action" in out
    assert "two action" in out


def test_print_run_summary_no_actions(capsys):
    print_run_summary(RunSummary(dry_run=False))
    out = capsys.readouterr().out
    assert "No remaining manual actions" in out


@patch("jobsearch_core.run.Settings")
def test_run_workflow_dry_run_is_discovery_only(mock_settings, db_url):
    from jobsearch_core.db.session import init_db

    init_db(db_url)
    settings = mock_settings.return_value
    settings.llm_cache_dir = "/tmp/cache"
    settings.llm_min_interval_seconds = 0
    settings.llm_max_retries = 0
    settings.llm_base_url = None
    settings.analysis_model = "m"
    settings.openrouter_api_key = "k"
    settings.database_url = db_url

    fake_discovery_summary = MagicMock()
    fake_discovery_summary.search_requests_used = 3
    fake_discovery_summary.known_companies = 1
    fake_discovery_summary.known_alternate_urls = 0
    fake_discovery_summary.new_companies = 1
    fake_discovery_summary.identity_signals = 2
    fake_discovery_summary.sources_selected = 1
    fake_discovery_summary.candidates_persisted = 0

    with (
        patch("jobsearch_core.run.discover_with_summary", return_value=([], fake_discovery_summary)) as mock_disc,
        patch(
            "jobsearch_core.ingest.scrapers.pipeline.scrape_company_entries",
            return_value={"companies": 99, "total_added": 99, "promoted": 0},
        ) as mock_scrape,
    ):
        summary = run_workflow(dry_run=True, search_backend="ddgs", country="fi", role="backend")

    assert mock_disc.call_count == 1
    # Discovery queries are spaced: the client handed to the pass is paced.
    assert isinstance(mock_disc.call_args.args[0], PacedSearchClient)
    assert summary.dry_run is True
    assert summary.mode == "discover"
    # Confirm-first: a discovery pass stops at the confirmation gate and must
    # never scrape confirmed positions.
    mock_scrape.assert_not_called()


@patch("jobsearch_core.run.Settings")
def test_collect_workflow_scrapes_accepted_and_promotes(mock_settings, db_url):
    from jobsearch_core.db.session import init_db
    from jobsearch_core.run import collect_workflow

    init_db(db_url)
    settings = mock_settings.return_value
    settings.llm_cache_dir = "/tmp/cache"
    settings.llm_min_interval_seconds = 0
    settings.llm_max_retries = 0
    settings.llm_base_url = None
    settings.analysis_model = "m"
    settings.openrouter_api_key = "k"
    settings.database_url = db_url

    with (
        patch(
            "jobsearch_core.ingest.scrapers.pipeline.scrape_company_entries",
            return_value={"companies": 2, "total_added": 3, "total_rejected": 0,
                          "total_dup": 0, "total_no_desc": 0, "promoted": 1},
        ) as mock_scrape,
    ):
        summary = collect_workflow(dry_run=True, refresh_known=True, batch=1)

    assert summary.mode == "collect"
    assert summary.sources_scraped == 2
    assert summary.jobs_discovered == 3
    assert summary.jobs_with_descriptions == 3
    assert summary.promoted == 1
    assert summary.jobs_scored == 0  # dry-run never scores
    # Batch and refresh-known are forwarded to the promotion-aware scraper.
    _, kwargs = mock_scrape.call_args
    assert kwargs["accepted_batch"] == 1
    assert kwargs["refresh_known"] is True


def test_run_cli_command_registered(runner):
    result = CliRunner().invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    # Phase 4.0a: run is a deprecated alias for `jobsearch_core discover`.
    assert "Deprecated alias" in result.output
    assert "--country" in result.output
    assert "--max-companies" in result.output
    assert "--expand-queries" in result.output
    assert "--max-sources" not in result.output
    assert "--limit" not in result.output


def test_collect_cli_command_registered(runner):
    result = CliRunner().invoke(main, ["collect", "--help"])
    assert result.exit_code == 0
    # Phase 4.0a: collect is a deprecated alias for `jobsearch_core scrape`.
    assert "Deprecated alias" in result.output
    assert "scrape" in result.output
    assert "--max-sources" in result.output
    assert "--limit" in result.output
    assert "--no-details" in result.output
    assert "--batch" in result.output
    assert "--refresh-known" in result.output
    assert "--country" not in result.output


def test_scrape_phase_command_registered(runner):
    result = CliRunner().invoke(main, ["scrape", "--help"])
    assert result.exit_code == 0
    assert "Phase 3" in result.output
    assert "--max-sources" in result.output
    assert "--limit" in result.output
    assert "--no-details" in result.output
    assert "--batch" in result.output
    assert "--refresh-known" in result.output


def test_run_cli_exposes_mine_listicles_options(runner):
    result = CliRunner().invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--mine-listicles" in result.output
    assert "--no-mine-listicles" in result.output
    assert "--max-listicles" in result.output
    assert "--max-companies-per-listicle" in result.output
    assert "--scope" in result.output


def test_collect_cli_exposes_scope_option(runner):
    result = CliRunner().invoke(main, ["collect", "--help"])
    assert result.exit_code == 0
    assert "--scope" in result.output


def test_discover_search_cli_exposes_scope_option(runner):
    result = CliRunner().invoke(main, ["discover", "search", "--help"])
    assert result.exit_code == 0
    assert "--scope" in result.output


def test_discover_search_defaults_to_identity_first_and_mining(runner):
    result = CliRunner().invoke(main, ["discover", "search", "--help"])
    assert result.exit_code == 0
    # Phase 3.6c: identity-first and listicle mining are default-on via flag
    # pairs; classic mode is an explicit opt-out and mining is ignored there.
    assert "--identity-first" in result.output
    assert "--no-identity-first" in result.output
    assert "--mine-listicles" in result.output
    assert "--no-mine-listicles" in result.output
    assert "default" in result.output


# ---------------------------------------------------------------------------
# Phase 4.0a: four-phase CLI, defaults config, deprecated aliases
# ---------------------------------------------------------------------------


def test_phase_commands_registered(runner):
    for name in ["discover", "verify", "scrape", "match"]:
        result = CliRunner().invoke(main, [name, "--help"])
        assert result.exit_code == 0, f"{name} not registered"
    assert "Phase 1" in CliRunner().invoke(main, ["discover", "--help"]).output
    assert "Phase 2" in CliRunner().invoke(main, ["verify", "--help"]).output
    assert "Phase 3" in CliRunner().invoke(main, ["scrape", "--help"]).output
    assert "Phase 4" in CliRunner().invoke(main, ["match", "--help"]).output


def test_pipeline_cli_command_is_deprecated_alias(runner):
    result = CliRunner().invoke(main, ["pipeline", "--help"])
    assert result.exit_code == 0
    assert "Deprecated alias" in result.output
    assert "jobsearch_core match" in result.output


def test_discover_bare_group_has_phase_options(runner):
    result = CliRunner().invoke(main, ["discover", "--help"])
    assert result.exit_code == 0
    assert "--expand-queries" in result.output
    assert "--no-expand-queries" in result.output
    assert "--mine-listicles" in result.output
    assert "--no-mine-listicles" in result.output
    assert "--dry-run" in result.output
    assert "config/defaults.yaml" in result.output


def test_load_defaults_missing_file_returns_builtins(tmp_path, monkeypatch):
    from jobsearch_core.config import PipelineDefaults, load_defaults

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBSEARCH_DEFAULTS", raising=False)
    defaults = load_defaults()
    assert defaults == PipelineDefaults()
    assert defaults.search_backend == "brave"
    assert defaults.expand_queries is True
    assert defaults.identity_first is True
    assert defaults.mine_listicles is True
    assert defaults.max_baseline_queries == 15


def test_load_defaults_reads_config_file(tmp_path, monkeypatch):
    from jobsearch_core.config import load_defaults

    path = tmp_path / "defaults.yaml"
    path.write_text(
        "search_backend: searxng\nmax_companies: 5\nexpand_queries: false\n",
        encoding="utf-8",
    )
    defaults = load_defaults(path=path)
    assert defaults.search_backend == "searxng"
    assert defaults.max_companies == 5
    assert defaults.expand_queries is False
    # Unspecified keys keep built-in defaults (Phase 4.3 ceiling).
    assert defaults.max_baseline_queries == 15


def test_defaults_yaml_ships_approved_values():
    from jobsearch_core.config import load_defaults

    defaults = load_defaults()
    # User-approved defaults (2026-09-01; backend switched to the paid Brave API
    # 2026-11-09 — the free engines answered challenge pages, not results).
    assert defaults.search_backend == "brave"
    assert defaults.identity_first is True
    assert defaults.mine_listicles is True
    assert defaults.expand_queries is True
    assert defaults.max_baseline_queries == 15
    assert defaults.max_companies == 10
    assert defaults.max_listicles == 3
    assert defaults.max_variants == 2
    assert defaults.max_companies_per_listicle == 15


def test_resolve_defaults_precedence():
    from jobsearch_core.config import PipelineDefaults, resolve_defaults

    defaults = PipelineDefaults(max_companies=5, expand_queries=False)
    resolved = resolve_defaults(
        defaults,
        search_backend=None,          # not given -> defaults config
        max_companies=3,              # explicit flag wins over defaults config
        expand_queries=None,          # not given -> defaults config (False)
    )
    assert resolved["search_backend"] == defaults.search_backend
    assert resolved["max_companies"] == 3
    assert resolved["expand_queries"] is False


def test_run_summary_carries_effective_defaults(capsys):
    summary = RunSummary(dry_run=True, effective_defaults={"max_companies": 10, "search_backend": "ddgs"})
    print_run_summary(summary)
    out = capsys.readouterr().out
    assert "Effective settings:" in out
    assert "max_companies=10" in out
    assert "search_backend=ddgs" in out


def test_company_cli_count_and_list(tmp_path, runner, monkeypatch):
    from jobsearch_core.db.models import Company
    from jobsearch_core.db.session import get_session, init_db

    db_url = f"sqlite:///{tmp_path / 'company.db'}"
    init_db(db_url)
    session = get_session()
    session.add(Company(name="KnownCo", status="known", country="FI"))
    session.add(Company(name="PendingCo", status="pending", country="EE"))
    session.commit()
    session.close()
    monkeypatch.setattr("jobsearch_core.cli.Settings", lambda: Settings(database_url=db_url))

    result = runner.invoke(main, ["company", "count"])
    assert result.exit_code == 0, result.output
    assert "known" in result.output
    assert "pending" in result.output

    result = runner.invoke(main, ["company", "list", "--status", "known"])
    assert result.exit_code == 0, result.output
    assert "KnownCo" in result.output


def test_company_cli_add_creates_known(tmp_path, runner, monkeypatch):
    from jobsearch_core.db.models import Company
    from jobsearch_core.db.session import get_session, init_db

    db_url = f"sqlite:///{tmp_path / 'company2.db'}"
    init_db(db_url)
    monkeypatch.setattr("jobsearch_core.cli.Settings", lambda: Settings(database_url=db_url))

    result = runner.invoke(
        main,
        ["company", "add", "--name", "NewCo", "--careers-url", "https://new.example/careers",
         "--country", "FI"],
    )
    assert result.exit_code == 0, result.output
    assert "as known" in result.output

    session = get_session()
    company = session.query(Company).first()
    session.close()
    assert company.name == "NewCo"
    assert company.status == "known"
