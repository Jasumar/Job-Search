from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    openrouter_api_key: str = ""
    # Paid search backend (owner decision 2026-11-09). Read from .env as
    # BRAVE_SEARCH_KEY; empty means the brave backend cannot be constructed.
    brave_search_key: str = ""
    typesafe_model: str = "typesafe/jev-1.13"
    analysis_model: str = "google/gemma-4-26b-a4b-it"
    application_model: str = "openai/gpt-5.6-luna"
    llm_base_url: str | None = None
    llm_min_interval_seconds: float = 2.0
    llm_max_retries: int = 2
    llm_cache_dir: Path = Path("data/cache")
    database_url: str = "sqlite:///data/jobs.db"
    browser_channel: str = "chromium"


class RejectRule(BaseModel):
    field: str
    pattern: str
    reason: str


class IncludeRule(BaseModel):
    family: str
    patterns: list[str]


class FiltersConfig(BaseModel):
    reject_rules: list[RejectRule]
    include_rules: list[IncludeRule] = Field(default_factory=list)


class EntryVocabulary(BaseModel):
    """Jobs-link vocabulary for careers-page entry resolution (Phase 4.0d).

    Grouped by language only so a human can review it; matching uses the
    flattened, accent-folded term list (see
    ``ingest/scrapers/entry_resolution.py``). Missing sections are empty, which
    disables the descent instead of guessing.
    """

    link_terms: dict[str, list[str]] = Field(default_factory=dict)
    path_terms: list[str] = Field(default_factory=list)
    #: Section scoping words that may close a listing path
    #: (``/avoimet-tyopaikat/kaikki``). They never make a page an entry alone.
    section_modifiers: list[str] = Field(default_factory=list)
    #: Path segments marking a *subset* listing (one category, team, location or
    #: audience). Such a page may rescue an empty descent but never displaces a
    #: page that already lists postings — see ``entry_resolution``.
    subset_segments: list[str] = Field(default_factory=list)


class PipelineDefaults(BaseModel):
    """User-editable pipeline defaults (Phase 4.0a), loaded from
    ``config/defaults.yaml``.

    Precedence: explicit CLI flag > these defaults > built-in code defaults.
    CLI commands therefore pass ``None`` for an unset flag and resolve through
    :func:`resolve_defaults`. Dry-run is deliberately not represented here —
    it must never be a default.
    """

    search_backend: str = "brave"
    identity_first: bool = True
    mine_listicles: bool = True
    expand_queries: bool = True
    max_baseline_queries: int = 15
    max_companies: int = 10
    max_listicles: int = 3
    max_variants: int = 2
    max_companies_per_listicle: int = 15
    max_liveness_checks: int = 10  # Phase 3.6d: bounded job-link HEAD checks
    # Phase 4.1: recommendation-group boundaries and the LLM-notes budget.
    # A job with a hard blocker is always `skip` regardless of score, so these
    # thresholds classify only unblocked jobs.
    worth_threshold: float = 0.70
    consider_threshold: float = 0.40
    max_llm_notes: int = 10
    # 3.8c M2: ceiling on paid job-profile extractions per match run. Scoring is
    # free and always covers every profiled job, so only extraction is bounded.
    max_profile_extractions: int = 100


DEFAULTS_ENV_VAR = "JOBSEARCH_DEFAULTS"
DEFAULT_DEFAULTS_PATH = Path("config/defaults.yaml")


def load_defaults(path: Path | None = None) -> PipelineDefaults:
    """Load pipeline defaults; missing file yields built-in defaults."""
    if path is None:
        path = Path(os.environ.get(DEFAULTS_ENV_VAR, DEFAULT_DEFAULTS_PATH))
    if not path.exists():
        return PipelineDefaults()
    data = load_yaml(path)
    return PipelineDefaults(**data)


def resolve_defaults(
    defaults: PipelineDefaults,
    **overrides: Any,
) -> dict[str, Any]:
    """Merge CLI overrides over the defaults config.

    Each keyword is the setting name mapped to the CLI-provided value, where
    ``None`` means "flag not given" and falls back to the defaults config.
    Returns a dict of fully resolved values.
    """
    resolved: dict[str, Any] = {}
    for name, value in overrides.items():
        if value is None:
            resolved[name] = getattr(defaults, name)
        else:
            resolved[name] = value
    return resolved


class RoleFamilySegments(BaseModel):
    """One wizard chip: display label + its ordered segment mapping.

    The segment order decides which segments survive the per-chip cap of 3
    (master_plan 2026-09-11); lists must be human-reviewed.
    """

    label: str = ""
    segments: list[str] = Field(default_factory=list)


class SearchScope(BaseModel):
    """Structured domain input to discovery prompts, templates, and dry-run scoring.

    Decouples the pipeline's hardcoded tech orientation (Phase 3.6a). The tech
    scope in ``config/scopes/tech.yaml`` is the bootstrap default; a future
    phase generates a ``SearchScope`` from user inputs. ``role_titles`` is
    defined for a future include-gate seeding path but is not yet wired into the
    gate (which stays driven by ``config/filters.yaml``).
    """

    name: str
    employer_scope: str = ""
    role_families: dict[str, list[str]] = Field(default_factory=dict)
    role_titles: list[str] = Field(default_factory=list)
    company_segment_templates: list[str] = Field(default_factory=list)
    # Segment name per template, positionally aligned with
    # ``company_segment_templates`` (Phase 3.8f wizard mapping data). When the
    # list is absent or misaligned, template filtering is disabled.
    company_segment_names: list[str] = Field(default_factory=list)
    # Ordered role→segment mapping for the wizard chips (decided 2026-09-11).
    role_family_segments: dict[str, RoleFamilySegments] = Field(default_factory=dict)
    # MF3: discovery family -> taxonomy ids that select it.
    role_family_taxonomy: dict[str, list[str]] = Field(default_factory=dict)


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def load_filters_config(path: Path | None = None) -> FiltersConfig:
    if path is None:
        path = Path("config/filters.yaml")
    data = load_yaml(path)
    return FiltersConfig(**data)


def load_entry_vocabulary(path: Path | None = None) -> EntryVocabulary:
    """Load the jobs-link vocabulary from ``config/entry_vocabulary.yaml``.

    A missing file raises: an empty vocabulary silently disables the whole
    descent, which would look like "no company has a deep jobs page".
    """
    if path is None:
        path = Path("config/entry_vocabulary.yaml")
    data = load_yaml(path)
    return EntryVocabulary(**data)


def load_scope(name: str = "tech", path: Path | None = None) -> SearchScope:
    """Load a ``SearchScope`` from ``config/scopes/<name>.yaml``.

    Defaults to the tech scope, which is the bootstrap default for the whole
    pipeline. A missing scope file raises ``FileNotFoundError``.
    """
    if path is None:
        path = Path("config/scopes") / f"{name}.yaml"
    data = load_yaml(path)
    return SearchScope(name=name, **data)
