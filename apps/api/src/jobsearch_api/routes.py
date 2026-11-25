"""Thin HTTP routes over the existing core service functions."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from jobsearch_core.config import load_defaults, load_scope
from jobsearch_core.cv.extract import CvExtractionError, extract_cv_text
from jobsearch_core.db.models import Company, JobRun, RunSource, Source
from jobsearch_core.ingest.scrapers.posting_check import suspect_reason
from jobsearch_core.profile import (
    ProfileData,
    get_profile_row,
    profile_snapshot,
    profile_to_data,
    replace_profile,
)
from jobsearch_core.profile_evidence import (
    EvidenceData,
    get_additional_experience,
    get_evidence,
    load_candidate_evidence,
    replace_evidence,
    store_additional_experience,
)
from jobsearch_core.report import classify_for_report
from jobsearch_core.role_taxonomy import load_taxonomy
from jobsearch_core.run import delete_companies, delete_job, list_jobs, match_status
from jobsearch_core.scoring.match import match_config_from_defaults
from jobsearch_core.search.discover import (
    add_company,
    build_company_segment_queries_for_selection,
    count_companies,
    get_company,
    list_companies,
    review_companies_batch,
    review_discoveries,
    review_discovery,
    select_baseline_countries,
)
from jobsearch_core.search.templates import COUNTRIES, DEFAULT_COUNTRIES
from jobsearch_worker.consumer import (
    ActiveRunExistsError,
    AtsSourceError,
    CompaniesHaveSourceError,
    InvalidSourceURLsError,
    RunStateError,
    UnknownCompaniesError,
    enqueue_careers_search,
    enqueue_name_collect,
    enqueue_run,
    request_cancel,
    request_pause,
    request_resume,
)
from sqlalchemy.orm import Session

from jobsearch_api.database import get_db

from .schemas import (
    ARRANGEMENT_TO_FLAG,
    AdditionalExperienceRequest,
    AdditionalExperienceResponse,
    CompanyBatchReview,
    CompanyCounts,
    CompanyCreate,
    CompanyDeleteRequest,
    CompanyDeleteResponse,
    CompanyResponse,
    CompanyReview,
    CustomRole,
    DiscoveryPreviewResponse,
    EvidenceResponse,
    EvidenceUpdateRequest,
    JobResponse,
    MatchStatusResponse,
    ProfileCvRequest,
    ProfileResponse,
    ProfileUpdateRequest,
    ReviewBatchResponse,
    ReviewDiscoveryResponse,
    RoleFamilyOut,
    RoleGroupOut,
    RoleMapRequest,
    RoleOut,
    RunCancelResponse,
    RunCareersSearchRequest,
    RunCollectRequest,
    RunControlResponse,
    RunDiscoverRequest,
    RunMatchRequest,
    RunNameCollectRequest,
    RunQueuedResponse,
    RunResponse,
    RunSourceOut,
    SegmentsResponse,
    UserProfileOut,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/companies", response_model=list[CompanyResponse])
def companies(
    _db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    statuses: Annotated[list[str] | None, Query()] = None,
    country: str | None = None,
    no_careers: bool = False,
    has_entry: bool | None = None,
    order: Literal["id", "id_desc"] = "id",
    limit: int | None = Query(default=None, ge=1),
):
    return list_companies(
        status=status_filter,
        statuses=statuses,
        country=country,
        no_careers=no_careers,
        has_entry=has_entry,
        order=order,
        limit=limit,
    )


@router.get("/companies/count", response_model=CompanyCounts)
def company_count(_db: DbSession):
    return count_companies()


@router.get("/companies/{company_id}", response_model=CompanyResponse)
def company(company_id: int, _db: DbSession):
    result = get_company(company_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return result


@router.post("/companies", response_model=CompanyResponse, status_code=status.HTTP_201_CREATED)
def create_company(payload: CompanyCreate, _db: DbSession):
    return add_company(
        payload.name,
        payload.careers_url,
        notes=payload.notes,
        country=payload.country,
    )


@router.post("/companies/review-batch", response_model=ReviewBatchResponse)
def review_companies(payload: CompanyBatchReview, _db: DbSession):
    """Bulk accept/reject for the review screen: status only, never a URL.

    Ids that no longer exist come back in ``unknown_ids`` (the screen refreshes
    that selection); the batch itself still applies to the rest, in one
    transaction.
    """
    updated, unknown_ids = review_companies_batch(payload.company_ids, payload.status)
    return {"updated": updated, "unknown_ids": unknown_ids}


@router.post("/companies/{company_id}/review", response_model=ReviewDiscoveryResponse)
def review_company(company_id: int, payload: CompanyReview, _db: DbSession):
    updated = review_discovery(
        company_id, payload.status, payload.careers_url, payload.open_positions_url
    )
    if updated == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return {"updated": updated}


@router.get("/jobs", response_model=list[JobResponse])
def jobs(
    _db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    country: str | None = None,
    min_score: float | None = None,
    scored: bool | None = None,
    tailored: bool | None = None,
    company_id: int | None = Query(default=None, ge=1),
    unattributed: bool = False,
    order: Literal["id", "score_desc"] = "id",
    limit: int | None = Query(default=None, ge=1),
):
    records = list_jobs(
        status=status_filter,
        country=country,
        min_score=min_score,
        scored=scored,
        tailored=tailored,
        company_id=company_id,
        unattributed=unattributed,
        order=order,
        limit=limit,
    )
    config = match_config_from_defaults(load_defaults())
    return [_job_response(record, config) for record in records]


def _json_value(raw: str | None, kind: type, default):
    if not raw:
        return default
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default
    return value if isinstance(value, kind) else default


def _job_response(record, config) -> JobResponse:
    blockers = _json_value(record.fit_blockers, list, [])
    recommendation = (
        classify_for_report(record.total_score, bool(blockers), config)
        if record.total_score is not None
        else None
    )
    return JobResponse.model_validate(
        {
            **record.__dict__,
            "has_applications": bool(record.applications),
            "recommendation": recommendation,
            "fit_strengths": _json_value(record.fit_strengths, list, []),
            "fit_gaps": _json_value(record.fit_gaps, list, []),
            "fit_blockers": blockers,
            "fit_components": _json_value(record.fit_components, dict, {}),
            "fit_hidden_reason": record.fit_hidden_reason,
            "fit_flags": _json_value(record.fit_flags, list, []),
            "suspect_reason": suspect_reason(
                record.raw_title or record.normalized_title, record.url
            ),
        }
    )


@router.get("/matches/status", response_model=MatchStatusResponse)
def matches_status(_db: DbSession):
    """Staleness and the not-yet-scored count for the results screen."""
    return match_status()


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job_route(job_id: int, _db: DbSession):
    """Delete a posting a human judged not to be a job.

    Hard delete of the row, plus its dedup fingerprint in ``job_exclusions`` so
    a later collect run cannot re-add the same posting. 404 when the id is gone,
    so a double click reports "already deleted" rather than a silent success.
    """
    if not delete_job(job_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return None


@router.post("/companies/delete", response_model=CompanyDeleteResponse)
def delete_companies_route(payload: CompanyDeleteRequest, _db: DbSession):
    """Delete companies and all their jobs; ``dry_run`` only reports the counts.

    Job fingerprints are kept in ``job_exclusions``. A company with an
    application on one of its jobs, or still in a live run, is reported under
    ``blocked`` and left untouched; the others are deleted.
    """
    report = delete_companies(payload.company_ids, dry_run=payload.dry_run)
    return CompanyDeleteResponse(
        deleted=report.deleted,
        blocked=report.blocked,
        unknown_ids=report.unknown_ids,
        dry_run=payload.dry_run,
    )


@router.get("/discovery", response_model=list[CompanyResponse])
def discovery(
    _db: DbSession,
    status_filter: str = Query(default="pending", alias="status"),
):
    return review_discoveries(status_filter)


def _run_params(payload: RunDiscoverRequest | RunCollectRequest) -> dict:
    params = payload.model_dump()
    defaults = load_defaults()
    # API requests may lower configured caps, but never raise them.
    cap_names = ("max_baseline_queries", "max_companies", "max_variants", "max_listicles", "max_companies_per_listicle")
    for name in cap_names:
        if params.get(name) is not None:
            params[name] = min(params[name], getattr(defaults, name))
    return params


def _queued_response(run: JobRun) -> dict[str, int | str]:
    """The enqueue ack: the run was accepted in the ``queued`` state.

    A worker can claim the row between the enqueue's final commit and this
    response being serialised, so ``run.status`` may already read ``"running"``
    (and ``RunQueuedResponse`` only permits ``"queued"``). The endpoint promises
    the accepted state; callers poll ``GET /runs/{run_id}`` for the live status.
    """
    return {"run_id": run.id, "status": "queued"}


def _run_response(run: JobRun) -> RunResponse:
    return RunResponse(
        id=run.id,
        run_type=run.run_type,
        status=run.status,
        params=json.loads(run.params),
        triggered_by=run.triggered_by,
        error=run.error,
        summary=json.loads(run.summary) if run.summary else None,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


@router.post("/runs/discover", response_model=RunQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
def enqueue_discover(payload: RunDiscoverRequest, db: DbSession):
    try:
        run = enqueue_run(db, "discover", _run_params(payload))
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A discover run is already active") from None
    return _queued_response(run)


@router.post("/runs/collect", response_model=RunQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
def enqueue_collect(payload: RunCollectRequest, db: DbSession):
    try:
        run = enqueue_run(db, "collect", _run_params(payload))
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A collect run is already active") from None
    return _queued_response(run)


@router.post("/runs/match", response_model=RunQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
def enqueue_match(payload: RunMatchRequest, db: DbSession):
    """Queue a match run (Phase 4.1).

    The request is recorded verbatim in ``params`` so a run's provenance is
    inspectable later. The extraction ceiling is server config, so a caller can
    neither raise nor lower it.
    """
    # MF1: with no saved skills and no experience every technology comparison
    # scores 0 and the ranking means nothing. Refuse, and say what to do.
    if not load_candidate_evidence(db).can_score:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Your saved CV information has no skills and no experience, so there is "
                "nothing to compare jobs against. Open My information, read your CV "
                "and save the skills and experience, then score again."
            ),
        )
    params = payload.model_dump()
    try:
        run = enqueue_run(db, "match", params)
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A match run is already active") from None
    return _queued_response(run)


@router.post(
    "/runs/careers-search",
    response_model=RunQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_careers_search_run(payload: RunCareersSearchRequest, db: DbSession):
    """Queue a careers search over the owner's selected companies (M1 B1).

    Enqueue-only: this creates the run and its per-company ``run_sources``
    manifest. The worker owns all fetching, resolving and recording.
    """
    try:
        run = enqueue_careers_search(
            db,
            payload.company_ids,
            batch_size=payload.batch_size,
            page_ceiling=payload.page_ceiling,
        )
    except UnknownCompaniesError as exc:
        raise HTTPException(
            status_code=422, detail=f"Unknown company ids: {exc.company_ids}"
        ) from None
    except CompaniesHaveSourceError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Companies already have a hiring source: {exc.company_ids}",
        ) from None
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A careers_search run is already active") from None
    return _queued_response(run)


@router.post(
    "/runs/name-collect",
    response_model=RunQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_name_collect_run(payload: RunNameCollectRequest, db: DbSession):
    """Queue a name collection over the owner's pasted listing URLs (M1 B3).

    Enqueue-only: this creates the run, one ``sources`` row per URL and the
    per-source ``run_sources`` manifest. The worker owns all fetching,
    replaying, authoring and candidate writes.
    """
    try:
        run = enqueue_name_collect(
            db,
            payload.urls,
            batch_size=payload.batch_size,
            page_ceiling=payload.page_ceiling,
        )
    except InvalidSourceURLsError as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid source URLs ({exc.reason}): {exc.urls}"
        ) from None
    except AtsSourceError as exc:
        raise HTTPException(
            status_code=422, detail=f"ATS board URLs cannot be sources: {exc.urls}"
        ) from None
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A name_collect run is already active") from None
    return _queued_response(run)


@router.get("/runs", response_model=list[RunResponse])
def runs(
    db: DbSession,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
):
    query = db.query(JobRun)
    if status_filter is not None:
        query = query.filter(JobRun.status == status_filter)
    query = query.order_by(JobRun.id.desc()).limit(limit)
    return [_run_response(run) for run in query.all()]


@router.get("/runs/{run_id}", response_model=RunResponse)
def run(run_id: int, db: DbSession):
    record = db.get(JobRun, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _run_response(record)


@router.post("/runs/{run_id}/cancel", response_model=RunCancelResponse)
def cancel_run(run_id: int, db: DbSession):
    try:
        current_status = request_cancel(db, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found") from None
    return {"id": run_id, "status": current_status}


@router.post("/runs/{run_id}/pause", response_model=RunControlResponse)
def pause_run(run_id: int, db: DbSession):
    """Pause a run at its next checkpoint; a paused run holds its type slot."""
    try:
        current_status = request_pause(db, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found") from None
    except RunStateError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot pause a run in status {exc.current_status}",
        ) from None
    return {"id": run_id, "status": current_status}


@router.post("/runs/{run_id}/resume", response_model=RunControlResponse)
def resume_run(run_id: int, db: DbSession):
    try:
        current_status = request_resume(db, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Run not found") from None
    except RunStateError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resume a run in status {exc.current_status}",
        ) from None
    return {"id": run_id, "status": current_status}


@router.get("/runs/{run_id}/sources", response_model=list[RunSourceOut])
def run_sources(run_id: int, db: DbSession):
    """The run's per-company/per-source manifest, in selection order."""
    if db.get(JobRun, run_id) is None:
        raise HTTPException(status_code=404, detail="Run not found")
    rows = db.query(RunSource).filter(RunSource.run_id == run_id).order_by(RunSource.id).all()

    company_names = dict(
        db.query(Company.id, Company.name)
        .filter(Company.id.in_([row.company_id for row in rows if row.company_id]))
        .all()
    ) if any(row.company_id for row in rows) else {}
    source_urls = dict(
        db.query(Source.id, Source.url)
        .filter(Source.id.in_([row.source_id for row in rows if row.source_id]))
        .all()
    ) if any(row.source_id for row in rows) else {}

    return [
        RunSourceOut(
            id=row.id,
            run_id=row.run_id,
            source_id=row.source_id,
            company_id=row.company_id,
            company_name=company_names.get(row.company_id),
            source_url=source_urls.get(row.source_id),
            status=row.status,
            identities_found=row.identities_found,
            candidates_added=row.candidates_added,
            candidates_duplicate=row.candidates_duplicate,
            extraction=row.extraction,
            recipe_id=row.recipe_id,
            error=row.error,
            entry_trace=row.entry_trace,
            author_trace=row.author_trace,
            dropped=row.dropped,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )
        for row in rows
    ]


def _profile_out(data, row) -> UserProfileOut:
    flags = {name: getattr(data, name) for name in ARRANGEMENT_TO_FLAG.values()}
    return UserProfileOut(
        **{f: getattr(data, f) for f in ("name", "education", "experience", "skills",
           "job_preferences", "notes", "target_countries", "target_cities", "role_families")},
        custom_roles=[CustomRole(**entry) for entry in data.custom_roles],
        work_arrangements=[label for label, flag in ARRANGEMENT_TO_FLAG.items() if flags[flag]],
        has_cv=bool(row.cv_text),
        updated_at=row.updated_at,
        location=data.location,
        citizenship=data.citizenship,
        linkedin=data.linkedin,
        gitlab=data.gitlab,
        email=data.email,
        phone=data.phone,
        visa_required=data.visa_required,
    )


def _profile_response(db: Session) -> ProfileResponse:
    row = get_profile_row(db)
    if row is None:
        # First-run state is normal: 200 with a null profile (2026-09-11).
        return ProfileResponse()
    data = profile_to_data(row)
    return ProfileResponse(profile=_profile_out(data, row))


def _data_from_update(payload: ProfileUpdateRequest) -> ProfileData:
    dumped = payload.model_dump()
    arrangements = {label: False for label in ARRANGEMENT_TO_FLAG.values()}
    for label in payload.work_arrangements:
        flag = ARRANGEMENT_TO_FLAG.get(label)
        if flag:
            arrangements[flag] = True
    dumped.pop("work_arrangements")
    dumped.update(arrangements)
    dumped["custom_roles"] = [entry.model_dump() for entry in payload.custom_roles]
    return ProfileData(**dumped)


@router.get("/profile", response_model=ProfileResponse)
def get_user_profile(db: DbSession):
    return _profile_response(db)


@router.put("/profile", response_model=ProfileResponse)
def put_user_profile(payload: ProfileUpdateRequest, db: DbSession):
    data = _data_from_update(payload)
    replace_profile(db, data)
    return _profile_response(db)


@router.get("/profile/evidence", response_model=EvidenceResponse)
def get_user_evidence(db: DbSession):
    """Read the ``user_evidence`` document."""
    data = get_evidence(db)
    if data is None:
        return EvidenceResponse(
            additional_experience=get_additional_experience(db)
        )
    # ``additional_experience`` is deliberately not part of ``EvidenceData``:
    # it has its own single writer and its own read path, so a structured
    # replace can never overwrite it (invariant b).
    return EvidenceResponse(
        additional_experience=get_additional_experience(db),
        **data.to_dict(),
    )


@router.put("/profile/evidence", response_model=EvidenceResponse)
def put_user_evidence(payload: EvidenceUpdateRequest, db: DbSession):
    """MF1: full replace of the structured CV facts.

    The wizard sends it on the owner's Save, with the proposal as the owner
    edited it (Phase 4.1a invariant (c): a proposal is written only by that
    Save; nothing calls this on its own). ``additional_experience`` is
    untouched. The row's ``updated_at`` moves, so Matches knows the scores are
    out of date (MF5).
    """
    replace_evidence(
        db,
        EvidenceData(
            skills=[s.strip() for s in payload.skills if s.strip()],
            education=[item.model_dump() for item in payload.education],
            experience=[item.model_dump() for item in payload.experience],
            projects=[item.model_dump() for item in payload.projects],
            languages=[item.model_dump() for item in payload.languages],
            linked_year=payload.linked_year,
        ),
    )
    return get_user_evidence(db)


@router.post("/profile/cv/reparse", response_model=RunQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
def reparse_profile_cv(db: DbSession):
    """MF1 "Read my CV again": a new ``profile_parse`` run on the stored CV text.

    The proposal lands in the run summary as for a new CV; nothing is saved
    until the owner's Save.
    """
    row = get_profile_row(db)
    if row is None or not (row.cv_text or "").strip():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No CV is stored yet")
    try:
        run = enqueue_run(
            db,
            "profile_parse",
            {"cv_text": row.cv_text, "description": row.description, "additional_experience": None},
        )
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A profile_parse run is already active") from None
    return _queued_response(run)


@router.post("/profile/roles/map", response_model=RunQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
def map_profile_roles(payload: RoleMapRequest, db: DbSession):
    """MF3: map typed roles to taxonomy ids in a ``role_map`` run.

    The API never calls the LLM itself; the worker does, one cached call per
    role. The mapping lands in the run summary for the owner to edit and save.
    """
    texts = [text.strip() for text in payload.texts if text.strip()]
    if not texts:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No role text given")
    try:
        run = enqueue_run(db, "role_map", {"texts": texts})
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A role mapping is already running") from None
    return _queued_response(run)


@router.put("/profile/additional-experience", response_model=AdditionalExperienceResponse)
def put_additional_experience(payload: AdditionalExperienceRequest, db: DbSession):
    """Store supplementary experience verbatim.

    This is the single writer for ``user_evidence.additional_experience``
    outside the CV submission. The text is never trimmed, normalized, or sent
    through an LLM (Phase 4.1a invariant (b)).
    """
    row = store_additional_experience(db, payload.additional_experience)
    return AdditionalExperienceResponse(additional_experience=row.additional_experience)


@router.post("/profile/cv", response_model=RunQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
def submit_profile_cv(payload: ProfileCvRequest, db: DbSession):
    try:
        run = enqueue_run(
            db,
            "profile_parse",
            {
                "cv_text": payload.cv_text,
                "description": payload.description,
                "additional_experience": payload.additional_experience,
            },
        )
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A profile_parse run is already active") from None
    return _queued_response(run)


@router.post("/profile/cv/file", response_model=RunQueuedResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_profile_cv_file(
    db: DbSession,
    file: Annotated[UploadFile, File(...)],
    description: str | None = None,
    additional_experience: str | None = None,
):
    """3.8h: multipart CV upload — extract text, enqueue the same profile_parse run.

    The uploaded binary is transient: only the extracted text is passed to
    the run (it becomes the persisted raw CV); the file itself is never
    stored anywhere.
    """
    try:
        data = await file.read()
        filename = file.filename or ""
        cv_text = extract_cv_text(data, filename)
    except CvExtractionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from None
    finally:
        await file.close()
    try:
        run = enqueue_run(
            db,
            "profile_parse",
            {
                "cv_text": cv_text,
                "description": description,
                "additional_experience": additional_experience,
            },
        )
    except ActiveRunExistsError:
        raise HTTPException(status_code=409, detail="A profile_parse run is already active") from None
    return _queued_response(run)


@router.get("/segments", response_model=SegmentsResponse)
def segments():
    scope = load_scope("tech")
    return SegmentsResponse(
        role_families=[
            RoleFamilyOut(
                id=family_id,
                label=mapping.label or family_id,
                segments=mapping.segments,
            )
            for family_id, mapping in scope.role_family_segments.items()
        ],
        segments=scope.company_segment_names or list(scope.company_segment_templates),
        countries=list(COUNTRIES.values()),
        role_taxonomy=[
            RoleGroupOut(
                id=group.id,
                label=group.label,
                roles=[RoleOut(id=role.id, label=role.label) for role in group.roles],
            )
            for group in load_taxonomy().groups
        ],
    )


@router.get("/discovery/preview", response_model=DiscoveryPreviewResponse)
def discovery_preview(
    db: DbSession,
    max_baseline_queries: Annotated[int | None, Query(ge=1)] = None,
):
    defaults = load_defaults()
    # The API may lower the ceiling, never raise it (4.2c caps invariant).
    ceiling = defaults.max_baseline_queries
    if max_baseline_queries is not None:
        ceiling = min(max_baseline_queries, ceiling)

    scope = load_scope("tech")
    snap = profile_snapshot(db)
    # Wizard selection (decided 2026-09-11): ordered intersection of the
    # profile's role-family segment mappings, per-chip cap 3. Empty selection
    # = all templates (pre-wizard narrowing rule).
    segments: list[str] | None = None
    if snap.role_families:
        per_family = [
            scope.role_family_segments[f].segments[:3]
            for f in snap.role_families
            if f in scope.role_family_segments
        ]
        if per_family:
            shared = set(per_family[0]).intersection(*(set(s) for s in per_family[1:]))
            segments = [s for s in per_family[0] if s in shared]
    if snap.countries or snap.preferences:
        countries = snap.countries or list(DEFAULT_COUNTRIES.values())
        source = "db_profile"
        reference = snap.reference
    else:
        # Phase 4.1a: DB is the only source. There is no evidence.yaml
        # fallback, so a fresh profile means "no country narrowing chosen
        # yet", not "read a file off disk".
        countries = select_baseline_countries(None, [])
        source = "default"
        reference = None

    queries = build_company_segment_queries_for_selection(
        countries, scope, max_queries=ceiling, segments=segments
    )
    selected_templates = scope.company_segment_names or list(scope.company_segment_templates)
    if segments is not None:
        selected_templates = [name for name in selected_templates if name in set(segments)]
    return DiscoveryPreviewResponse(
        selected_segments=selected_templates,
        selected_countries=countries,
        baseline_query_count=len(queries),
        ceiling=ceiling,
        profile_source=source,
        profile_reference=reference,
        sample_queries=queries,
    )
