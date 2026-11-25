/**
 * Types hand-mirrored from jobsearch_api.schemas (Phase 4.2b).
 * Contract drift is caught by TypeScript; codegen is a later option.
 */

export interface CompanyDeleteItem {
  id: number;
  name: string;
  jobs: number;
  reason?: string | null;
}

export interface CompanyDeleteResult {
  deleted: CompanyDeleteItem[];
  blocked: CompanyDeleteItem[];
  unknown_ids: number[];
  dry_run: boolean;
}

export interface Company {
  id: number;
  name: string;
  careers_url: string | null;
  status: string;
  country: string | null;
  verification_status: string;
  ats_platform: string | null;
  notes: string;
  verified_at: string | null;
  /** Resolved open-positions entry (4.0d): reviewable evidence, never an input. */
  open_positions_url: string | null;
  open_positions_landing_url: string | null;
  open_positions_link_text: string | null;
  /** The run whose listing produced this row (null for discovery rows). */
  run_id: number | null;
  /**
   * The careers-search run that last resolved `open_positions_url`, and when it
   * finished — derived from its newest completed `run_sources` row. Null when
   * the entry was not written by a run (or predates the manifest record).
   */
  entry_run_id: number | null;
  entry_resolved_at: string | null;
  /**
   * Newest finished gather (`collect` run) over this company: when, how it
   * ended (completed | zero_yield | failed) and which run. Null when no run
   * recorded a scrape — older scrapes leave only `job_count`.
   */
  last_scraped_at: string | null;
  last_scrape_status: string | null;
  last_scrape_run_id: number | null;
  /** What that scrape dropped as non-postings; null when nothing was dropped. */
  last_scrape_dropped?: ScrapeDropped | null;
  /** Jobs stored under this company. */
  job_count: number;
}

/** The scrape filter's record of links it judged not postings (`run_sources.dropped`). */
export interface ScrapeDropped {
  total: number;
  dropped: number;
  reasons: Record<string, number>;
  examples: { title: string; url: string }[];
}

export interface CompanyCounts {
  by_status: Record<string, number>;
  by_country: Record<string, number>;
}

export type ReviewStatus = "accepted" | "rejected" | "pending";

export interface CompanyReview {
  status: ReviewStatus;
  /** Careers page URL. Supply only one URL; the API fills the other. */
  careers_url?: string | null;
  /** Open-positions page URL. Supply only one URL; the API fills the other. */
  open_positions_url?: string | null;
}

export interface ReviewDiscoveryResponse {
  updated: number;
}

/** Bulk review (demo gate): status only, never a careers_url. */
export interface CompanyBatchReview {
  company_ids: number[];
  status: "accepted" | "rejected";
}

export interface ReviewBatchResponse {
  updated: number;
  /** Ids the request named that no longer exist — the screen refreshes its list. */
  unknown_ids: number[];
}

export interface Job {
  id: number;
  url: string;
  status: string;
  country: string | null;
  total_score: number | null;
  normalized_title: string | null;
  normalized_company: string | null;
  normalized_location: string | null;
  has_applications: boolean;
  company_id?: number | null;
  /** Derived from the score, the blockers and the configured thresholds. */
  recommendation?: "worth" | "consider" | "skip" | null;
  fit_summary?: string | null;
  fit_strengths?: string[];
  fit_gaps?: string[];
  fit_blockers?: string[];
  /** Match components the last scoring compared, 0..1 each (empty ones are absent). */
  fit_components?: Record<string, number>;
  /** Why Matches hides the job by default (role outside the target roles). */
  fit_hidden_reason?: string | null;
  /** Ingest filter hits: facts about the posting, not verdicts. */
  fit_flags?: { reason: string | null; detail: string | null }[];
  /** Why the row looks like a non-posting (article, category page); a hint, not a status. */
  suspect_reason?: string | null;
}

/** Beside the ranked list: is it stale, and how much is left unscored. */
export interface MatchStatus {
  profile_updated_at: string | null;
  last_match_finished_at: string | null;
  profile_changed: boolean;
  /** A match run completed after the last information save; Matches shows results only then. */
  results_current?: boolean;
  /** Saved CV information has skills or experience, so scoring can compare. */
  evidence_ready?: boolean;
  /** Jobs that pass prefit and still have no job profile. */
  unprofiled_remaining: number;
  /** Group boundaries from the server config; blockers force "skip". */
  worth_threshold: number;
  consider_threshold: number;
}

export interface JobQuery {
  status?: string;
  country?: string;
  min_score?: number;
  scored?: boolean;
  tailored?: boolean;
  order?: "id" | "score_desc";
  limit?: number;
}

export interface CompanyQuery {
  status?: string;
  /** Repeatable multi-status scope (`accepted` + `known` for resolved entries). */
  statuses?: string[];
  country?: string;
  /** Only rows with no careers_url (M1 careers-search selection list). */
  no_careers?: boolean;
  /** true = has a resolved open_positions_url; false = has none. */
  has_entry?: boolean;
  order?: "id" | "id_desc";
  limit?: number;
}

/**
 * Hand-mirrored from jobsearch_api.schemas RunResponse (Phase 4.2c).
 * queued → running → completed/failed/cancelled are the terminal states;
 * `paused` is both the pause request and the held state, and
 * `cancelled_requested` is a cancel observed at the next checkpoint
 * (apps/worker/.../consumer.py request_pause/request_cancel).
 */
export type RunStatus =
  | "queued"
  | "running"
  | "paused"
  | "cancelled_requested"
  | "completed"
  | "failed"
  | "cancelled";

export interface Run {
  id: number;
  run_type: string;
  status: RunStatus;
  params: Record<string, unknown>;
  triggered_by: string;
  error: string | null;
  summary: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunDiscoverPayload {
  dry_run: boolean;
  country?: string | null;
  role?: string | null;
  max_baseline_queries?: number | null;
  max_companies?: number | null;
  scope?: string | null;
}

/**
 * Collect run: scrape accepted companies (plus known ones when
 * `refresh_known`), promote them, and score what it found. `dry_run` writes
 * nothing and skips scoring.
 */
export interface RunCollectPayload {
  dry_run: boolean;
  batch?: number | null;
  refresh_known?: boolean | null;
  limit?: number | null;
  scope?: string | null;
  /** Exactly these companies, any status; `batch`/`refresh_known` are ignored. */
  company_ids?: number[] | null;
}

/**
 * Match run: every run scores every profiled job. The server config owns the
 * ceiling on new profile extractions, so the request carries no limit.
 */
export interface RunMatchPayload {
  llm_notes?: boolean;
  output?: string | null;
}

export interface RunQueued {
  run_id: number;
  status: "queued";
}

export interface RunCancelResponse {
  id: number;
  status: string;
}

/** Result of pause/resume: the run's status after the request applied. */
export interface RunControlResponse {
  id: number;
  status: string;
}

/** M1: careers search over owner-selected companies lacking a careers page. */
export interface RunCareersSearchPayload {
  company_ids: number[];
  batch_size?: number | null;
  page_ceiling?: number | null;
}

/** M1: name collection over pasted listing-page URLs (http(s), never ATS boards). */
export interface RunNameCollectPayload {
  urls: string[];
  batch_size?: number | null;
  page_ceiling?: number | null;
}

/**
 * One manifest row of a source-driven run (RunSourceOut): a `name_collect`
 * source or a `careers_search` company. Status is constrained by
 * ck_run_sources_status: queued | running | paused | completed | zero_yield |
 * failed | cancelled.
 */
export interface RunSource {
  id: number;
  run_id: number;
  source_id: number | null;
  company_id: number | null;
  company_name: string | null;
  source_url: string | null;
  status: string;
  identities_found: number;
  candidates_added: number;
  candidates_duplicate: number;
  extraction: string | null;
  recipe_id: number | null;
  error: string | null;
  /** JSON string: the entry descent's evidence, careers_search rows only. */
  entry_trace: string | null;
  /** JSON string: the authoring trials of a name_collect row (status, kind,
   *  and per trial the number, pass, reason and body tried). */
  author_trace: string | null;
  /** JSON string of a `ScrapeDropped`: the links the scrape filter dropped. */
  dropped?: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

