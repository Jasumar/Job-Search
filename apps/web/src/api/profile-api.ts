import { requestRaw } from "@/api/client";
import type {
  CvParseQueued,
  EvidenceDraft,
  EvidenceLanguageItem,
  DiscoveryPreview,
  ProfileResponse,
  ProfileUpdate,
  RunSummaryResponse,
  SegmentsResponse,
} from "@/api/profile-types";

/**
 * Wizard-side API calls. The /profile, /segments and /discovery/preview
 * endpoints belong to the 3.8f backend half (not yet implemented) — until it
 * lands these run against the MSW mock handlers (see src/mocks). The calls
 * already target the decided contract, so no frontend rewrite is needed when
 * the backend arrives.
 */
export const profileApi = {
  getProfile(): Promise<ProfileResponse> {
    return requestRaw("/profile");
  },
  updateProfile(payload: ProfileUpdate): Promise<ProfileResponse> {
    return requestRaw("/profile", { method: "PUT", body: JSON.stringify(payload) });
  },
  parseCv(payload: CvParseRequestBody): Promise<CvParseQueued> {
    return requestRaw("/profile/cv", { method: "POST", body: JSON.stringify(payload) });
  },
  // 3.8h multipart file upload; the backend extracts text, persists only the
  // extracted text (transient-file storage rule) and enqueues profile_parse.
  uploadCvFile(
    file: File,
    description: string,
    additionalExperience = "",
  ): Promise<CvParseQueued> {
    const form = new FormData();
    form.append("file", file);
    form.append("description", description);
    // Phase 4.1a: user-authored prose, stored verbatim and never LLM-parsed.
    form.append("additional_experience", additionalExperience);
    return requestRaw("/profile/cv/file", { method: "POST", body: form });
  },
  getEvidence(): Promise<EvidenceResponse> {
    return requestRaw("/profile/evidence");
  },
  /** MF1: full replace of the CV facts; only the owner's Save sends it. */
  updateEvidence(payload: EvidenceDraft): Promise<EvidenceResponse> {
    return requestRaw("/profile/evidence", { method: "PUT", body: JSON.stringify(payload) });
  },
  /** MF1: parse the stored CV again; the proposal arrives in the run summary. */
  reparseCv(): Promise<CvParseQueued> {
    return requestRaw("/profile/cv/reparse", { method: "POST" });
  },
  /** MF3: map typed roles to taxonomy ids in a role_map run. */
  mapRoles(texts: string[]): Promise<CvParseQueued> {
    return requestRaw("/profile/roles/map", { method: "POST", body: JSON.stringify({ texts }) });
  },
  setAdditionalExperience(additionalExperience: string): Promise<AdditionalExperienceResponse> {
    return requestRaw("/profile/additional-experience", {
      method: "PUT",
      body: JSON.stringify({ additional_experience: additionalExperience }),
    });
  },
  getRunSummary(runId: number): Promise<RunSummaryResponse> {
    return requestRaw(`/runs/${runId}`);
  },
  getSegments(): Promise<SegmentsResponse> {
    return requestRaw("/segments");
  },
  discoveryPreview(): Promise<DiscoveryPreview> {
    return requestRaw("/discovery/preview");
  },
};

export interface CvParseRequestBody {
  cv_text: string;
  description: string;
  /** Phase 4.1a: user-authored, stored verbatim, never parsed by an LLM. */
  additional_experience?: string;
}

export interface EvidenceResponse {
  additional_experience: string;
  education: Record<string, unknown>[];
  experience: Record<string, unknown>[];
  projects: Record<string, unknown>[];
  skills: string[];
  languages: EvidenceLanguageItem[];
  linked_year: number | null;
}

export interface AdditionalExperienceResponse {
  additional_experience: string;
}