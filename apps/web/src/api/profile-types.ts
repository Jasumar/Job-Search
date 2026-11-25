/**
 * Contract for the 3.8f user-profile endpoints, per master_plan.md
 * "User Profile in DB" (decisions 2026-09-11; reconciled with the live
 * backend 2026-09-11 — the backend now serves these shapes).
 *
 * - JSON string lists for non-categorical fields (skills, notes,
 *   job_preferences, role_families); enumerated fields are plain columns.
 * - GET /profile returns 200 with `{ profile: null }` on first run (no 404).
 * - PUT /profile is a full-replace of the structured fields, atomic.
 * - POST /profile/cv returns 202 {run_id}; the parse proposal arrives via
 *   GET /runs/{id} (run_type=profile_parse, proposal in run.summary) and is
 *   never auto-saved — the user confirms via PUT /profile.
 * - GET /segments returns role families (id/label/ordered segments), the
 *   ordered segment names, and the selectable countries.
 * - GET /discovery/preview returns the computed search plan (segments x
 *   countries vs ceiling) for the current profile.
 */

import type { Run } from "./types";

export interface UserProfile {
  name: string;
  education: string;
  experience: string;
  skills: string[];
  job_preferences: string[];
  target_countries: string[];
  target_cities: string[];
  /** Work-arrangement labels; absent/empty = no restriction. */
  work_arrangements: string[];
  notes: string[];
  /** Target roles: role-taxonomy ids ("technology/backend") or whole groups ("legal"). */
  role_families: string[];
  /** Roles the owner typed, each with the taxonomy ids it maps to. */
  custom_roles?: CustomRole[];
  // Phase 4.1a: candidate contact details are owned by user_profile and
  // deliberately excluded from the scoring prompt projection.
  location: string;
  citizenship: string;
  linkedin: string;
  gitlab: string;
  email: string;
  phone: string;
  visa_required: boolean;
  /** Read-only: a CV text is stored ("Read my CV again" needs it). */
  has_cv?: boolean;
}

export interface CustomRole {
  text: string;
  ids: string[];
}

export interface EvidenceExperienceItem {
  title: string;
  company: string;
  start: string;
  end: string;
  location?: string | null;
  responsibilities: string[];
  technologies: string[];
}

export interface EvidenceLanguageItem {
  language: string;
  proficiency: string;
}

/** Structured CV facts, the EvidenceData shape (PUT /profile/evidence body). */
export interface EvidenceDraft {
  skills: string[];
  experience: EvidenceExperienceItem[];
  languages: EvidenceLanguageItem[];
  education: Record<string, unknown>[];
  projects: Record<string, unknown>[];
  linked_year: number | null;
}

export interface ProfileResponse {
  profile: UserProfile | null;
}

export interface ProfileUpdate extends UserProfile {}

export interface CvParseRequest {
  cv_text: string;
  description: string;
}

/**
 * 3.8g contract extension of the profile_parse run summary: the LLM infers
 * proposed role families from CV + description + skills, validated against
 * the tech.yaml role_families vocabulary. Present only after a successful
 * parse; absence/empty means fall back to an empty chip grid.
 */
export interface ProfileParseSummary {
  suggested_role_families?: string[];
  /** Extracted CV text (3.8h file path) — shown as a preview before confirm. */
  extracted_cv_text?: string;
  /** MF1: proposed CV facts in the EvidenceData shape; saved only by Save. */
  evidence_proposal?: Partial<EvidenceDraft>;
  /** MF3 role_map run: typed roles with proposed taxonomy ids. */
  custom_roles?: CustomRole[];
  [key: string]: unknown;
}

/** 3.8h: accepted extensions for the multipart CV file upload. */
export const CV_FILE_EXTENSIONS = [".pdf", ".docx"] as const;
/** 3.8h client-side size cap before upload (bytes). */
export const CV_FILE_MAX_BYTES = 5 * 1024 * 1024;

export function validateCvFile(file: File): string | null {
  const ok = CV_FILE_EXTENSIONS.some((ext) => file.name.toLowerCase().endsWith(ext));
  if (!ok) return `Unsupported file type — use ${CV_FILE_EXTENSIONS.join(" or ")}`;
  if (file.size > CV_FILE_MAX_BYTES) return "File too large (max 5 MB)";
  if (file.size === 0) return "File is empty";
  return null;
}

export interface CvParseQueued {
  run_id: number;
  status: "queued";
}

/**
 * GET /runs/{id} shape as consumed by the wizard: the profile_parse proposal
 * (suggested families, extracted text) lives in run.summary.
 */
export interface RunSummaryResponse extends Omit<Run, "summary"> {
  summary: ProfileParseSummary | null;
}

export interface RoleFamily {
  /** Family id as used in user_profile.role_families (e.g. "backend"). */
  id: string;
  label: string;
  /** Ordered segment mapping; order decides the per-chip cap of 3. */
  segments: string[];
}

export interface RoleGroup {
  id: string;
  label: string;
  roles: { id: string; label: string }[];
}

export interface SegmentsResponse {
  role_families: RoleFamily[];
  /** Ordered segment template names (pre-wizard priority order). */
  segments: string[];
  /** Every ISO 3166-1 country name (config/countries.yaml). */
  countries: string[];
  /** The selectable role taxonomy (config/role_taxonomy.yaml). */
  role_taxonomy?: RoleGroup[];
}

export interface DiscoveryPreview {
  /** Segment names the run will query, in priority order. */
  selected_segments: string[];
  selected_countries: string[];
  baseline_query_count: number;
  ceiling: number;
  /** "db_profile" | "evidence_yaml" | "default" */
  profile_source: string;
  profile_reference: string | null;
  sample_queries: string[];
}
