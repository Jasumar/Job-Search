"""Pydantic models exposed by the local API."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from jobsearch_core.profile_evidence import (
    EvidenceEducation,
    EvidenceExperience,
    EvidenceLanguage,
    EvidenceProject,
)
from pydantic import BaseModel, ConfigDict, Field


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    careers_url: str | None
    status: str
    country: str | None
    verification_status: str
    ats_platform: str | None
    notes: str
    verified_at: datetime | None
    # The resolved entry (4.0d) and the page + link text it came from. Read-only
    # on every write path: the entry is evidence the reviewer sees, never an
    # input (plan-source-runs §API).
    open_positions_url: str | None = None
    open_positions_landing_url: str | None = None
    open_positions_link_text: str | None = None
    # The run whose listing produced this row; NULL for discovery rows and for
    # anything written before migration 0015.
    run_id: int | None = None
    # The careers-search run that last resolved this entry and when it finished,
    # derived from its newest completed `run_sources` row (no column of its own:
    # the manifest row is the record). NULL when no run ever resolved the entry.
    entry_run_id: int | None = None
    entry_resolved_at: datetime | None = None
    # Scrape state, derived from the newest finished `collect` run_sources row
    # and the jobs table (no columns). `last_scraped_at` is NULL when no run
    # recorded a scrape; `job_count` can still be > 0 for older scrapes.
    last_scraped_at: datetime | None = None
    last_scrape_status: str | None = None
    last_scrape_run_id: int | None = None
    # What that scrape dropped as non-postings: {"total", "dropped", "reasons",
    # "examples"}. NULL when nothing was dropped or the scrape was not judged.
    last_scrape_dropped: dict | None = None
    job_count: int = 0


class CompanyCreate(BaseModel):
    name: str = Field(min_length=1)
    careers_url: str = Field(min_length=1)
    notes: str = ""
    country: str | None = None
    # ats_platform is deliberately absent: it is scrape-time provenance
    # (Phase 4.1e) and can only be derived by scraping, never written in.


class CompanyReview(BaseModel):
    status: Literal["accepted", "rejected", "pending"]
    careers_url: str | None = None
    # The owner may paste either page or both. Supplying only one makes the
    # core review function accept the same URL for both fields.
    open_positions_url: str | None = None


class CompanyBatchReview(BaseModel):
    """The demo review screen's bulk accept/reject (plan-source-runs §API).

    Status only: no ``careers_url``, no notes. Repeated ids are collapsed by the
    core function; the request cap keeps one call inside one bounded
    transaction.
    """

    company_ids: list[int] = Field(min_length=1, max_length=500)
    status: Literal["accepted", "rejected"]


class ReviewBatchResponse(BaseModel):
    updated: int
    # Ids the request named that no longer exist. Reported, never silently
    # dropped, so a stale selection can be refreshed instead of half-applied.
    unknown_ids: list[int]


class CompanyDeleteRequest(BaseModel):
    """Bulk company delete; ``dry_run`` previews the counts for the confirm step."""

    company_ids: list[int] = Field(min_length=1, max_length=500)
    dry_run: bool = False


class CompanyDeleteItem(BaseModel):
    id: int
    name: str
    jobs: int
    reason: str | None = None


class CompanyDeleteResponse(BaseModel):
    deleted: list[CompanyDeleteItem]
    blocked: list[CompanyDeleteItem]
    unknown_ids: list[int]
    dry_run: bool


class CompanyCounts(BaseModel):
    by_status: dict[str, int]
    by_country: dict[str, int]


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    status: str
    country: str | None
    total_score: float | None
    normalized_title: str | None
    normalized_company: str | None
    normalized_location: str | None
    has_applications: bool
    # 3.8c: the last scoring, as the results screen shows it. ``recommendation``
    # is derived from the score and blockers with the configured thresholds.
    recommendation: Literal["worth", "consider", "skip"] | None = None
    fit_summary: str | None = None
    fit_strengths: list[str] = []
    fit_gaps: list[str] = []
    fit_blockers: list[str] = []
    fit_components: dict[str, float] = {}
    # MF4: why Matches hides this job by default (role outside the target
    # roles). Not a blocker; /jobs lists the job as usual.
    fit_hidden_reason: str | None = None
    fit_flags: list[dict[str, str | None]] = []
    # MQ: why the row looks like a non-posting (``posting_check`` obstacle name),
    # derived on read. A hint for the owner to confirm with the human delete,
    # never a status.
    suspect_reason: str | None = None
    # Gap 12: owning company. Null for a job scraped from a bare URL with no
    # company record behind it.
    company_id: int | None


class MatchStatusResponse(BaseModel):
    """Beside the ranked list: is it stale, and how much is left unscored."""

    profile_updated_at: datetime | None
    last_match_finished_at: datetime | None
    profile_changed: bool
    # MF5: a match run completed after the last information save. Matches
    # shows results only then.
    results_current: bool = False
    # MF1: saved evidence has skills or experience, so scoring can compare.
    evidence_ready: bool = True
    # Jobs that pass prefit and still have no job profile (3.8c M4).
    unprofiled_remaining: int
    # Group boundaries from config/defaults.yaml, so the screen can name them.
    worth_threshold: float
    consider_threshold: float


class ReviewDiscoveryResponse(BaseModel):
    updated: int


class RunDiscoverRequest(BaseModel):
    dry_run: bool
    country: str | None = None
    role: str | None = None
    search_backend: Literal["brave", "ddgs", "searxng"] | None = None
    max_baseline_queries: int | None = Field(default=None, ge=1)
    max_companies: int | None = Field(default=None, ge=1)
    expand_queries: bool | None = None
    max_variants: int | None = Field(default=None, ge=1)
    mine_listicles: bool | None = None
    max_listicles: int | None = Field(default=None, ge=1)
    max_companies_per_listicle: int | None = Field(default=None, ge=1)
    scope: str | None = None


class RunCollectRequest(BaseModel):
    dry_run: bool
    batch: int | None = Field(default=None, ge=1)
    refresh_known: bool | None = None
    limit: int | None = Field(default=None, ge=1)
    scope: str | None = None
    # Exactly the companies the owner ticked on Find jobs. When set, the run
    # scrapes these rows whatever their status (a tick is the decision) and
    # `batch` / `refresh_known` are ignored. Unset = the accepted set.
    company_ids: list[int] | None = Field(default=None, min_length=1, max_length=500)


class RunMatchRequest(BaseModel):
    """Phase 4.1 match run.

    ``job_types`` and ``cv_variant`` are accepted and recorded for traceability
    but do not filter anything: every run scores every profiled job against the
    stored evidence, and extracts at most ``max_profile_extractions`` new
    profiles (3.8c). The server config owns that ceiling, not the request.
    """

    llm_notes: bool = False
    output: str | None = None
    job_types: list[str] | None = None
    cv_variant: str | None = None


class RunCareersSearchRequest(BaseModel):
    """M1 careers search over an owner-selected list of companies.

    ``company_ids`` is exactly what the owner ticked in the no-careers list;
    duplicates are collapsed in order and every id must still lack a
    ``careers_url`` when the run is created.
    """

    company_ids: list[int] = Field(min_length=1)
    batch_size: int | None = Field(default=None, ge=1)
    page_ceiling: int | None = Field(default=None, ge=1)


class RunNameCollectRequest(BaseModel):
    """M1 name collection over pasted listing-page URLs (B3).

    ``urls`` is exactly what the owner pasted: duplicates collapse in order,
    each must be an http(s) URL, and an ATS board URL is refused. The two
    controls are the same pair a careers search takes; a ``name_collect``
    source is processed to completion inside one batch.
    """

    urls: list[str] = Field(min_length=1)
    batch_size: int | None = Field(default=None, ge=1)
    page_ceiling: int | None = Field(default=None, ge=1)


class RunResponse(BaseModel):
    id: int
    run_type: str
    status: str
    params: dict
    triggered_by: str
    error: str | None
    summary: dict | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunQueuedResponse(BaseModel):
    run_id: int
    status: Literal["queued"]


class RunCancelResponse(BaseModel):
    id: int
    status: str


class RunControlResponse(BaseModel):
    """Result of a pause/resume request: the run's status after it applied."""

    id: int
    status: str


class RunSourceOut(BaseModel):
    """One manifest row of a source-driven run (plan-source-runs §Model)."""

    id: int
    run_id: int
    source_id: int | None
    company_id: int | None
    company_name: str | None
    source_url: str | None
    status: str
    identities_found: int
    candidates_added: int
    candidates_duplicate: int
    extraction: str | None
    recipe_id: int | None
    error: str | None
    #: JSON string: the entry descent's evidence (``EntryReport.as_trace()``).
    #: Present on ``careers_search`` rows only; ``None`` on legacy and
    #: ``name_collect`` rows.
    entry_trace: str | None = None
    #: JSON string: the authoring trials of an M1 listing row (status, kind,
    #: and per trial the number, pass, reason and the raw body tried).
    #: ``None`` on ``careers_search`` rows, which never author a listing.
    author_trace: str | None = None
    #: JSON string: what the scrape filter dropped as non-postings
    #: (``{"total", "dropped", "reasons", "examples"}``). ``None`` when nothing
    #: was dropped or the row was not judged.
    dropped: str | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CustomRole(BaseModel):
    """MF3: a role the owner typed and the taxonomy ids it maps to."""

    text: str = Field(min_length=1, max_length=200)
    ids: list[str] = Field(default_factory=list)


class UserProfileOut(BaseModel):
    name: str = ""
    education: str = ""
    experience: str = ""
    skills: list[str] = Field(default_factory=list)
    job_preferences: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    target_countries: list[str] = Field(default_factory=list)
    target_cities: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    custom_roles: list[CustomRole] = Field(default_factory=list)
    work_arrangements: list[str] = Field(default_factory=list)
    has_cv: bool = False
    updated_at: datetime | None = None
    # Phase 4.1a contact fields: single owner is the user_profile row. No
    # programmatic consumer; they exist so the tailor step can render the CV
    # header and are kept out of the scoring prompt projection.
    location: str = ""
    citizenship: str = ""
    linkedin: str = ""
    gitlab: str = ""
    email: str = ""
    phone: str = ""
    visa_required: bool = False


class ProfileResponse(BaseModel):
    """The user profile wrapped in ``{profile: ...}`` — ``profile: null`` is the
    first-run state (decided 2026-09-11: 200, not 404). Raw CV text is
    deliberately not exposed here; work arrangements are a ``work_arrangements``
    string list and ``role_families`` the wizard chip selections (wizard
    contract reconciled 2026-09-11)."""

    profile: UserProfileOut | None = None


ARRANGEMENT_TO_FLAG: dict[str, str] = {
    "full-time": "full_time",
    "part-time": "part_time",
    "on-site": "onsite",
    "hybrid": "hybrid",
    "remote": "remote",
}


class ProfileUpdateRequest(BaseModel):
    """Full-replace PUT body. All fields are required-or-defaulted as a whole
    document; the write is one atomic full-replace (decided 2026-09-11).
    Work arrangements arrive as the wizard's string list."""

    name: str = ""
    education: str = ""
    experience: str = ""
    skills: list[str] = Field(default_factory=list)
    job_preferences: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    target_countries: list[str] = Field(default_factory=list)
    target_cities: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    custom_roles: list[CustomRole] = Field(default_factory=list)
    work_arrangements: list[str] = Field(default_factory=list)
    # Phase 4.1a contact fields (single owner: the user_profile row).
    location: str = ""
    citizenship: str = ""
    linkedin: str = ""
    gitlab: str = ""
    email: str = ""
    phone: str = ""
    visa_required: bool = False


class ProfileCvRequest(BaseModel):
    cv_text: str = Field(min_length=1)
    description: str | None = None
    # Phase 4.1a: user-authored supplementary experience, stored verbatim
    # and never LLM-parsed. It travels with the CV submission because that is
    # the wizard step where the user writes it; it is NOT part of the LLM
    # proposal, which may only contain structured CV facts.
    additional_experience: str | None = None


class AdditionalExperienceRequest(BaseModel):
    """Stand-alone write of the supplementary-experience text.

    Split from ``ProfileCvRequest`` so editing it does not require re-uploading
    a CV. The value is stored byte-identical to what the user wrote.
    """

    additional_experience: str


class EvidenceResponse(BaseModel):
    """The ``user_evidence`` document (Phase 4.1a).

    ``additional_experience`` is returned exactly as stored. Structured facts
    are written only by ``PUT /profile/evidence``, which the owner's Save
    sends (MF1); an LLM proposal never reaches the DB without it.
    """

    additional_experience: str = ""
    education: list[dict] = Field(default_factory=list)
    experience: list[dict] = Field(default_factory=list)
    projects: list[dict] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[dict] = Field(default_factory=list)
    linked_year: int | None = None


class EvidenceUpdateRequest(BaseModel):
    """MF1: full replace of the structured CV facts (the owner's Save).

    Same shape as ``EvidenceData``. ``additional_experience`` is not here: it
    has its own writer, so this replace never touches it.
    """

    skills: list[str] = Field(default_factory=list)
    education: list[EvidenceEducation] = Field(default_factory=list)
    experience: list[EvidenceExperience] = Field(default_factory=list)
    projects: list[EvidenceProject] = Field(default_factory=list)
    languages: list[EvidenceLanguage] = Field(default_factory=list)
    linked_year: int | None = None


class RoleMapRequest(BaseModel):
    """MF3: typed roles to map to taxonomy ids (one cached LLM call each)."""

    texts: list[str] = Field(min_length=1, max_length=20)


class RoleOut(BaseModel):
    id: str
    label: str


class RoleGroupOut(BaseModel):
    id: str
    label: str
    roles: list[RoleOut]


class AdditionalExperienceResponse(BaseModel):
    additional_experience: str


class RoleFamilyOut(BaseModel):
    id: str
    label: str
    segments: list[str]


class SegmentsResponse(BaseModel):
    """Wizard chip/segment/country data (decided 2026-09-11: from data, not
    hardcoded in the frontend). ``role_families`` are the wizard chips with
    their ordered segment mappings; ``segments`` are the ordered segment
    template names; ``countries`` the selectable target country names."""

    role_families: list[RoleFamilyOut]
    segments: list[str]
    countries: list[str]
    # MF3: the selectable role taxonomy (config/role_taxonomy.yaml).
    role_taxonomy: list[RoleGroupOut] = Field(default_factory=list)


class DiscoveryPreviewRequest(BaseModel):
    max_baseline_queries: int | None = Field(default=None, ge=1)


class DiscoveryPreviewResponse(BaseModel):
    """Computed search profile the user confirms before a run (2026-09-11).
    Field names match the wizard contract (reconciled 2026-09-11)."""

    selected_segments: list[str]
    selected_countries: list[str]
    baseline_query_count: int
    ceiling: int
    profile_source: str  # "db_profile" | "evidence_yaml" | "default"
    profile_reference: str | None = None
    sample_queries: list[str]
