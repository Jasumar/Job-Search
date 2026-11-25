import type {
  Company,
  CompanyBatchReview,
  CompanyCounts,
  CompanyDeleteResult,
  CompanyQuery,
  CompanyReview,
  Job,
  JobQuery,
  MatchStatus,
  ReviewBatchResponse,
  ReviewDiscoveryResponse,
  ReviewStatus,
  Run,
  RunCancelResponse,
  RunCareersSearchPayload,
  RunCollectPayload,
  RunControlResponse,
  RunDiscoverPayload,
  RunMatchPayload,
  RunNameCollectPayload,
  RunQueued,
  RunSource,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`API ${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

/**
 * API base for all requests. From env (product-shaped config rule: no hardcoded
 * localhost in components); in dev the default "" keeps requests same-origin so
 * the vite /api proxy (path-rewritten to 127.0.0.1:8000) serves them.
 */
export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}/api${path}`, {
    // JSON requests get the content type; FormData bodies (multipart uploads)
    // must keep the browser's own multipart boundary header.
    headers:
      typeof init?.body === "string" ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = String(body.detail);
    } catch {
      // non-JSON error body
    }
    throw new ApiError(res.status, detail);
  }
  // 204 responses carry no body; parsing one would throw on empty input.
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function query(
  params: Record<string, string | number | boolean | readonly string[] | undefined>,
): string {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    // Array params are repeatable query keys (`?statuses=accepted&statuses=known`),
    // which is how the API's multi-status filter is spelled.
    if (Array.isArray(value)) {
      for (const item of value) qs.append(key, item);
    } else {
      qs.set(key, String(value));
    }
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

export const api = {
  listCompanies(params: CompanyQuery = {}): Promise<Company[]> {
    return request(`/companies${query({ ...params })}`);
  },
  getCompany(id: number): Promise<Company> {
    return request(`/companies/${id}`);
  },
  companyCount(): Promise<CompanyCounts> {
    return request(`/companies/count`);
  },
  createCompany(payload: {
    name: string;
    careers_url: string;
    notes?: string;
    country?: string | null;
  }): Promise<Company> {
    return request(`/companies`, { method: "POST", body: JSON.stringify(payload) });
  },
  reviewCompany(id: number, payload: CompanyReview): Promise<ReviewDiscoveryResponse> {
    return request(`/companies/${id}/review`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  /** Demo gate: one verdict for a whole selection; writes status only. */
  reviewBatch(payload: CompanyBatchReview): Promise<ReviewBatchResponse> {
    return request(`/companies/review-batch`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  listJobs(params: JobQuery = {}): Promise<Job[]> {
    return request(`/jobs${query({ ...params })}`);
  },
  /** Staleness and the not-yet-scored count for the results screen. */
  matchStatus(): Promise<MatchStatus> {
    return request(`/matches/status`);
  },
  /**
   * Delete a posting a human judged not to be a job. The server deletes the
   * row and keeps its fingerprint, so a later collect cannot re-add it.
   */
  /** Delete companies and their jobs; `dry_run` only returns the counts. */
  deleteCompanies(company_ids: number[], dry_run = false): Promise<CompanyDeleteResult> {
    return request(`/companies/delete`, {
      method: "POST",
      body: JSON.stringify({ company_ids, dry_run }),
    });
  },
  deleteJob(jobId: number): Promise<void> {
    return request(`/jobs/${jobId}`, { method: "DELETE" });
  },
  listDiscovery(status = "pending"): Promise<Company[]> {
    return request(`/discovery${query({ status })}`);
  },
  /** Scrape accepted (+ known) sources, promote them, and score the postings. */
  startCollectRun(payload: RunCollectPayload): Promise<RunQueued> {
    return request(`/runs/collect`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  /** Score persisted jobs against the stored profile. */
  startMatchRun(payload: RunMatchPayload = {}): Promise<RunQueued> {
    return request(`/runs/match`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  listRuns(status?: string, limit?: number): Promise<Run[]> {
    return request(`/runs${query({ status, limit })}`);
  },
  getRun(id: number): Promise<Run> {
    return request(`/runs/${id}`);
  },
  startDiscoverRun(payload: RunDiscoverPayload): Promise<RunQueued> {
    return request(`/runs/discover`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  cancelRun(id: number): Promise<RunCancelResponse> {
    return request(`/runs/${id}/cancel`, { method: "POST", body: JSON.stringify({}) });
  },
  pauseRun(id: number): Promise<RunControlResponse> {
    return request(`/runs/${id}/pause`, { method: "POST", body: JSON.stringify({}) });
  },
  resumeRun(id: number): Promise<RunControlResponse> {
    return request(`/runs/${id}/resume`, { method: "POST", body: JSON.stringify({}) });
  },
  /** M1: queue a careers search over the owner's selected companies. */
  startCareersSearchRun(payload: RunCareersSearchPayload): Promise<RunQueued> {
    return request(`/runs/careers-search`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  /** M1: queue a name collection over pasted listing-page URLs. */
  startNameCollectRun(payload: RunNameCollectPayload): Promise<RunQueued> {
    return request(`/runs/name-collect`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  /** M1: per-source/per-company manifest of a source-driven run. */
  listRunSources(id: number): Promise<RunSource[]> {
    return request(`/runs/${id}/sources`);
  },
};

export type { ReviewStatus };

/**
 * Request passthrough used by wizard API surfaces that exist only in the
 * 3.8f contract so far (profile, segments, preview). Same error handling as
 * the typed calls; the path is relative to the API base.
 */
async function requestRaw<T>(path: string, init?: RequestInit): Promise<T> {
  return request<T>(path, init);
}

export { requestRaw };
