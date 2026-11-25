from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="queued", nullable=False)
    params: Mapped[str] = mapped_column(Text, default="{}", server_default=text("'{}'"), nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, default="api", server_default=text("'api'"), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    authority_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # legacy alias
    verification_status: Mapped[str] = mapped_column(Text, default="unverified", nullable=False)
    verified_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    raw_title: Mapped[str] = mapped_column(Text, nullable=False)
    raw_company: Mapped[str] = mapped_column(Text, nullable=False)
    raw_location: Mapped[str] = mapped_column(Text, nullable=True)

    normalized_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_company: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_location: Mapped[str | None] = mapped_column(Text, nullable=True)

    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Gap 12: the owning company row. Resolved at ingest by exact careers-URL
    # or exact normalized-name match (db/company_link.py). Nullable because a
    # job scraped from a raw URL may have no company record behind it, and
    # because existing rows are only backfilled when provably resolvable.
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id"), nullable=True, index=True
    )

    posted_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    closing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    seniority: Mapped[str | None] = mapped_column(Text, nullable=True)
    languages_required: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_authorization: Mapped[str | None] = mapped_column(Text, nullable=True)
    sponsorship: Mapped[str | None] = mapped_column(Text, nullable=True)
    salary: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_requirements: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 3.6d: extraction-first structured job profile (JSON) plus link
    # liveness tracking for stale/closed postings.
    job_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    # RF8: set when the model's answer for this description was not a valid
    # profile. Profile extraction skips the job until a manual rerun clears it.
    profile_failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    link_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    link_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    dedup_hash: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    status: Mapped[str] = mapped_column(Text, default="new", nullable=False)

    # 3.8c M0: ingest filter hits as facts about the posting, a JSON list of
    # {"reason", "detail"}. A flag is never a verdict: the match run judges.
    fit_flags: Mapped[str | None] = mapped_column(Text, nullable=True)

    included_families: Mapped[str | None] = mapped_column(Text, nullable=True)
    include_signals: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    fit_gaps: Mapped[str | None] = mapped_column(Text, nullable=True)
    fit_strengths: Mapped[str | None] = mapped_column(Text, nullable=True)
    fit_blockers: Mapped[str | None] = mapped_column(Text, nullable=True)
    fit_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The match components the last scoring compared (JSON name -> 0..1). A
    # component with nothing to compare is absent (MF2).
    fit_components: Mapped[str | None] = mapped_column(Text, nullable=True)
    # MF4: why Matches hides the job by default (role outside the target roles).
    # Written by the match run; never a blocker and never a status.
    fit_hidden_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    discovered_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    scores: Mapped[list[Score]] = relationship(
        "Score", back_populates="job", cascade="all, delete-orphan"
    )
    applications: Mapped[list[Application]] = relationship(
        "Application", back_populates="job", cascade="all, delete-orphan"
    )
    company: Mapped[Company | None] = relationship("Company", back_populates="jobs")


class Score(Base):
    __tablename__ = "scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_prompt_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

    job: Mapped[Job] = relationship("Job", back_populates="scores")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    base_variant: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="draft", nullable=False)
    tailored_typst_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[Job] = relationship("Job", back_populates="applications")
    revisions: Mapped[list[CVRevision]] = relationship(
        "CVRevision", back_populates="application", cascade="all, delete-orphan"
    )


class CVRevision(Base):
    __tablename__ = "cv_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False, index=True
    )
    diff_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_prompt_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

    application: Mapped[Application] = relationship("Application", back_populates="revisions")


class UserProfile(Base):
    """The candidate profile (Phase 3.8f backend, decided 2026-09-11).

    Single-row-by-convention table: the wizard does a full-replace ``PUT``
    through :mod:`jobsearch_core.profile`. ``skills``, ``notes``, and
    ``job_preferences`` are JSON lists of plain strings (non-categorical,
    usable directly as LLM prompt input and deterministic filters); only
    enumerated fields are first-class columns. Raw CV text lives here too
    (written at parse time by the ``profile_parse`` run) as the later
    tailoring baseline.

    Phase 4.1a extended this table with the contact fields (location,
    citizenship, linkedin, gitlab, email, phone), which retire
    ``data/evidence.yaml``, plus ``visa_required``. The CV facts themselves
    moved to :class:`UserEvidence` — this table is search preferences and
    contact details, not CV facts.
    """

    __tablename__ = "user_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    education: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    experience: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    skills: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    job_preferences: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    target_countries: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    target_cities: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    role_families: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    # MF3: roles the owner typed, each with the taxonomy ids it maps to:
    # JSON list of {"text", "ids"} (migration 0023).
    custom_roles: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    full_time: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    part_time: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    onsite: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    hybrid: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    remote: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    # Phase 4.1a contact fields: the single owner for contact details. They
    # have no programmatic consumer (the tailor template is the only real
    # reader) and are kept out of LLM prompt projection where convenient.
    location: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    citizenship: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    linkedin: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    gitlab: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    email: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    phone: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    # Phase 4.1a: work-eligibility flag used by the location scorer. Owned
    # here because it is a preference, not a CV fact (not in `user_evidence`).
    visa_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    cv_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class UserEvidence(Base):
    """The candidate's CV facts (Phase 4.1a, decided 2026-09-29).

    A single-row document, deliberately *separate* from :class:`UserProfile`:
    every field with more than one consumer has exactly one owner table, so
    there is no cross-table duplication (invariant (a) of Phase 4.1a).

    Ownership split (Phase 4.1a):

    - ``user_evidence`` — CV facts consumed by the tailor and the structured
      scoring prompts: education, experience, projects, skills, languages,
      ``linked_year``, and the verbatim ``additional_experience``;
    - ``user_profile`` — search/discovery preferences (countries, cities,
      role families, work-arrangement flags, job preferences) plus contact
      details and the raw CV text.

    Every column is a JSON string except ``additional_experience`` and
    ``linked_year``, mirroring the retired ``Evidence`` model shape so an
    LLM-parsed proposal lands exactly where the code expects it. Writes go
    through :mod:`jobsearch_core.profile_evidence` (full replace, one
    transaction) — never partial state.
    """

    __tablename__ = "user_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    education: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    experience: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    projects: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    skills: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    languages: Mapped[str] = mapped_column(Text, default="[]", server_default=text("'[]'"), nullable=False)
    linked_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # User-authored, stored and sent byte-identical. Never LLM-parsed,
    # paraphrased, or summarized (Phase 4.1a invariant (b)).
    additional_experience: Mapped[str] = mapped_column(
        Text, default="", server_default=text("''"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class Company(Base):
    """A company hiring source. ``discovery_queue`` was renamed to ``companies``.

    Lifecycle: ``pending`` (discovered, unreviewed) -> ``accepted`` (human
    approved hiring path) -> ``known`` (promoted after a successful scrape).
    ``rejected`` records are kept for history but never scraped.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", server_default=text("''"), nullable=False)
    country: Mapped[str | None] = mapped_column(Text, nullable=True)
    careers_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 4.0d: the open-positions page a landing page resolved to, with the
    # landing page and link text it was reached from (provenance for the human
    # gate). NULL = never resolved; scraping then falls back to careers_url.
    # All three are written together by record_open_positions_entry and only
    # ever from a page the extractor confirmed has postings.
    open_positions_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_positions_landing_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_positions_link_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The run whose listing produced this candidate (migration 0015), so the
    # review screen can say "this row came from that pasted page". Set by
    # name_collect as it inserts candidates; NULL for discovery/search rows and
    # for every row written before 0015. Deleting a run must never delete a
    # company — the FK is SET NULL on a fresh schema, and SQLite does not
    # enforce it at all (no PRAGMA foreign_keys anywhere in this codebase).
    run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("job_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolution_status: Mapped[str] = mapped_column(Text, default="unresolved", nullable=False)
    resolution_page_type: Mapped[str] = mapped_column(Text, default="unknown", nullable=False)
    verification_status: Mapped[str] = mapped_column(Text, default="unverified", nullable=False)
    verified_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)
    ats_platform: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 4.3: top website-type class (A-E) and the ranked evidence JSON.
    # NULL = never classified; a guessed class is never written.
    site_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    site_class_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_source_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    triage_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    triage_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    identity_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    employer_domain: Mapped[str | None] = mapped_column(Text, nullable=True)
    followup_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipe_state: Mapped[str] = mapped_column(
        Text, default="ok", server_default=text("'ok'"), nullable=False
    )
    recipe_trials: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Gap 12. No delete-orphan cascade on purpose: a match run and an
    # application history should survive the company row going away, and
    # deleting a company is a review-gate action, not a routine operation.
    jobs: Mapped[list[Job]] = relationship("Job", back_populates="company")

    __table_args__ = (
        # Name lookup is the fallback resolver path (db/company_link.py), which
        # compares canonicalized names in Python. An index here keeps the
        # candidate fetch bounded as the table grows.
        Index("ix_companies_name", "name"),
    )


class CompanyXhrCapture(Base):
    """One observed job-list XHR for a company (Phase 4.3 evidence).

    Evidence only: a row never sets ``companies.site_class``. ``url`` is the
    request URL as the browser made it, with no cookies or Authorization.
    ``replay_matches`` is NULL until a PoliteClient replay has been compared.

    R1: rows are a timeline, not a snapshot. ``record_capture_rows`` appends a
    new ``capture_batch`` per render instead of replacing the previous set, so
    two runs can be compared. ``body_sha256`` identifies the body without
    storing it; ``body_truncated`` says the evidence itself is partial.
    """

    __tablename__ = "company_xhr_capture"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("companies.id"), nullable=False, index=True
    )
    captured_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_titles: Mapped[str | None] = mapped_column(Text, nullable=True)
    replay_matches: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    replay_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    class_a_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # R1 evidence. NULL on rows written before the columns existed; never guessed.
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    # One render writes one batch; the highest batch for a company is current.
    capture_batch: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )


class CompanyRecipe(Base):
    """A recorded scrape path (plan-scraping §Recipe authoring, class F).

    ``scope`` decides the owner: ``company_careers`` rows set ``company_id``;
    ``m1_url`` rows set ``target_url``. The CHECK constraint in migration 0009
    enforces exactly that. ``body`` is JSON; its shape is validated in code
    (F1), not here. A recipe is trusted only at ``status='verified'``.
    """

    __tablename__ = "company_recipes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    company_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("companies.id"), nullable=True, index=True
    )
    target_url: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, default="candidate", server_default=text("'candidate'"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "scope IN ('company_careers', 'm1_url')", name="ck_company_recipes_scope"
        ),
        CheckConstraint("kind IN ('api', 'dom')", name="ck_company_recipes_kind"),
        CheckConstraint(
            "status IN ('candidate', 'verified', 'stale')",
            name="ck_company_recipes_status",
        ),
        CheckConstraint(
            "(scope = 'company_careers' AND company_id IS NOT NULL)"
            " OR (scope = 'm1_url' AND target_url IS NOT NULL)",
            name="ck_company_recipes_owner",
        ),
    )


class Source(Base):
    """A saved listing-page URL for ``name_collect`` (plan-source-runs §Model).

    Saved sources have their own lifecycle: no FK points here from ``job_runs``,
    so pruning a run never removes a source. Removed only explicitly.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    recipe_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("company_recipes.id"), nullable=True)
    last_verified_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replay_check: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RunSource(Base):
    """Per-item progress row for a run: one source (``name_collect``) or one
    company (``careers_search``). The CHECK constraint enforces exactly one owner.
    """

    __tablename__ = "run_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, ForeignKey("job_runs.id"), nullable=False, index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sources.id"), nullable=True)
    company_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("companies.id"), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="queued", server_default=text("'queued'"), nullable=False)
    identities_found: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    candidates_added: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    candidates_duplicate: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    extraction: Mapped[str | None] = mapped_column(Text, nullable=True)
    recipe_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("company_recipes.id"), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Entry-resolution evidence for a `careers_search` row (Phase 4.0d): the
    # JSON of `EntryReport.as_trace()` — every candidate the descent fetched
    # with its score and outcome, the shortlist it ranked, and the miss reason.
    # A miss is readable here instead of only in the log.
    entry_trace: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Authoring evidence for an M1 listing row: the JSON of every trial
    # (number, passed, the failure reason, and the body that was tried). A
    # listing whose authoring fails carried only one sentence in ``error``
    # before this, and a pass that took three trials showed nothing at all.
    author_trace: Mapped[str | None] = mapped_column(Text, nullable=True)
    # What the scrape filter dropped as non-postings: JSON
    # ``{"total", "dropped", "reasons", "examples"}``. NULL = nothing dropped or
    # not judged (recipe, sitemap, name_collect rows).
    dropped: Mapped[str | None] = mapped_column(Text, nullable=True)
    # R1 drift baseline: the verified posting URL set this run produced, as a
    # JSON array. ``db/source_evidence.py`` compares run N against the previous
    # run's set; counts are derived, never the comparison key.
    verified_urls: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'completed',"
            " 'zero_yield', 'failed', 'cancelled')",
            name="ck_run_sources_status",
        ),
        CheckConstraint(
            "extraction IS NULL OR extraction IN ('recipe', 'llm')",
            name="ck_run_sources_extraction",
        ),
        CheckConstraint(
            "(source_id IS NOT NULL AND company_id IS NULL)"
            " OR (source_id IS NULL AND company_id IS NOT NULL)",
            name="ck_run_sources_owner",
        ),
    )


class RecipeFlag(Base):
    """One posting-existence gate answer about one recipe body (R3).

    Keyed by ``body_sha256`` rather than by ``recipe_id``: a re-authored body
    must never inherit the analysis written against its predecessor, and a flag
    stays readable after the recipe row is replaced. ``recipe_id`` is kept as a
    nullable provenance pointer, not as the key.
    """

    __tablename__ = "recipe_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipe_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("company_recipes.id"), nullable=True, index=True
    )
    body_sha256: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON: {"items": [{"url", "reason"}], ...}. Never a reference comparison.
    analysis: Mapped[str] = mapped_column(Text, nullable=False)
    # True only when an independent agreement backed the stop (R3 stop rule).
    stopped: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("verdict IN ('none', 'postings')", name="ck_recipe_flags_verdict"),
    )


class SearchCache(Base):
    """One cached careers search per (company, query) (plan-source-runs §Model)."""

    __tablename__ = "search_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(Integer, ForeignKey("companies.id"), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("company_id", "query", name="uq_search_cache_company_query"),)


class JobExclusion(Base):
    """A posting a human deleted, remembered by its fingerprint.

    Deleting the ``jobs`` row alone would let the next scrape re-add the same
    posting, because duplicate detection reads ``jobs.dedup_hash`` and the row
    is gone. The exclusion keeps the fingerprint, so a run can skip it. The
    stored title and URL are the human's evidence for why the row exists.
    """

    __tablename__ = "job_exclusions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedup_hash: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    raw_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)

    __table_args__ = (UniqueConstraint("dedup_hash", name="uq_job_exclusions_dedup_hash"),)
