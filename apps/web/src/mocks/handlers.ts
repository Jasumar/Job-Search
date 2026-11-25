import { http, HttpResponse, delay } from "msw";
import type {
  CustomRole,
  CvParseQueued,
  EvidenceDraft,
  DiscoveryPreview,
  ProfileResponse,
  ProfileUpdate,
  SegmentsResponse,
} from "@/api/profile-types";

/**
 * MSW handlers for the 3.8f contract surfaces (profile, segments, discovery
 * preview) plus the 3.8g/3.8h extensions: POST /profile/cv/file (multipart,
 * 202 → profile_parse run whose summary carries suggested_role_families and
 * extracted_cv_text) and GET /runs/{id} serving that summary. Mirrors
 * master_plan.md "User Profile in DB" (2026-09-11/12): GET /profile is
 * 200+null on first run, PUT is full-replace, the parse proposal is never
 * auto-saved, and the uploaded CV binary is transient (extract, persist text,
 * discard file).
 *
 * Also serves the existing 4.2b/c endpoints in tests so Vitest exercises the
 * real fetch layer instead of stubbing the api module.
 */

// Mutable in-test store, seeded per scenario via createHandlers overrides.
export interface MockState {
  profile: ProfileResponse["profile"];
  queuedRunId: number;
  /** 3.8g: suggested families the mock parse run reports in its summary. */
  suggestedFamilies: string[];
  /** 3.8h: extracted text the mock file-upload parse reports (null = extraction failed). */
  extractedCvText: string | null;
  /** MF1: stored CV facts (GET/PUT /profile/evidence). */
  evidence?: EvidenceDraft & { additional_experience?: string };
  /** MF1: the evidence the mock parse run proposes. */
  evidenceProposal?: Partial<EvidenceDraft>;
  /** MF3: ids the mock role_map run proposes for any typed role. */
  mappedRoleIds?: string[];
}

export const emptyProfile: ProfileUpdate = {
  name: "",
  education: "",
  experience: "",
  skills: [],
  job_preferences: [],
  target_countries: [],
  target_cities: [],
  work_arrangements: [],
  notes: [],
  role_families: [],
  custom_roles: [],
  // Phase 4.1a: contact details on user_profile (tailor prompt only).
  location: "",
  citizenship: "",
  linkedin: "",
  gitlab: "",
  email: "",
  phone: "",
  visa_required: false,
};

export const mockSegments: SegmentsResponse = {
  role_families: [
    { id: "backend", label: "Backend & Python", segments: ["software", "technology", "startup"] },
    { id: "data", label: "Data engineering & analytics", segments: ["software", "fintech", "finance"] },
    { id: "ai", label: "AI / machine learning", segments: ["ai", "software", "startup"] },
    { id: "automation", label: "Automation & DevOps", segments: ["industrial", "software", "automation"] },
  ],
  segments: ["software", "technology", "startup", "scaleup"],
  countries: ["Finland", "Sweden", "Germany", "Netherlands", "Estonia", "Norway", "India"],
  role_taxonomy: [
    {
      id: "technology",
      label: "Technology & software",
      roles: [
        { id: "technology/backend", label: "Backend developer" },
        { id: "technology/data-engineer", label: "Data engineer" },
        { id: "technology/ml-engineer", label: "AI / machine learning engineer" },
        { id: "technology/solutions-engineer", label: "Solutions engineer" },
      ],
    },
    {
      id: "sales",
      label: "Sales",
      roles: [{ id: "sales/pre-sales", label: "Pre-sales" }],
    },
    {
      id: "legal",
      label: "Legal & compliance",
      roles: [{ id: "legal/counsel", label: "Lawyer / counsel" }],
    },
  ],
};

export const emptyEvidenceResponse = {
  additional_experience: "",
  skills: [] as string[],
  experience: [],
  languages: [],
  education: [],
  projects: [],
  linked_year: null,
};

export const mockPreview: DiscoveryPreview = {
  selected_segments: ["software", "technology", "startup", "scaleup"],
  selected_countries: ["Finland", "Sweden"],
  baseline_query_count: 8,
  ceiling: 15,
  profile_source: "default",
  profile_reference: null,
  sample_queries: [],
};

// Run id of the most recent profile_parse enqueue (mocks the parse result).
// Exported so tests that override the CV endpoints can keep run polling alive.
export const mockParseState = {
  lastRunId: null as number | null,
  /** MF3: the last role_map run and the texts it was given. */
  lastRoleRunId: null as number | null,
  lastRoleTexts: [] as string[],
};

let runCounter = 100;

export function createHandlers(state: MockState = {
  profile: null,
  queuedRunId: 101,
  suggestedFamilies: ["technology/backend"],
  extractedCvText: "Extracted CV text from file",
}) {
  runCounter = state.queuedRunId;
  return [
    http.get("*/api/profile", async () => {
      await delay(10);
      return HttpResponse.json({ profile: state.profile });
    }),
    http.put("*/api/profile", async ({ request }) => {
      await delay(10);
      const body = (await request.json()) as ProfileUpdate;
      state.profile = body;
      return HttpResponse.json({ profile: body });
    }),
    http.post("*/api/profile/cv", async () => {
      await delay(10);
      mockParseState.lastRunId = ++runCounter;
      return HttpResponse.json({ run_id: mockParseState.lastRunId, status: "queued" } satisfies CvParseQueued, { status: 202 });
    }),
    // 3.8h multipart variant: same downstream profile_parse flow.
    http.post("*/api/profile/cv/file", async () => {
      await delay(10);
      mockParseState.lastRunId = ++runCounter;
      return HttpResponse.json({ run_id: mockParseState.lastRunId, status: "queued" } satisfies CvParseQueued, { status: 202 });
    }),
    http.get("*/api/profile/evidence", async () => {
      await delay(10);
      return HttpResponse.json({ ...emptyEvidenceResponse, ...(state.evidence ?? {}) });
    }),
    http.put("*/api/profile/evidence", async ({ request }) => {
      await delay(10);
      const body = (await request.json()) as EvidenceDraft;
      state.evidence = { ...body, additional_experience: state.evidence?.additional_experience ?? "" };
      return HttpResponse.json({ ...emptyEvidenceResponse, ...state.evidence });
    }),
    http.post("*/api/profile/cv/reparse", async () => {
      await delay(10);
      mockParseState.lastRunId = ++runCounter;
      return HttpResponse.json({ run_id: mockParseState.lastRunId, status: "queued" } satisfies CvParseQueued, { status: 202 });
    }),
    http.post("*/api/profile/roles/map", async ({ request }) => {
      await delay(10);
      mockParseState.lastRoleTexts = ((await request.json()) as { texts: string[] }).texts;
      mockParseState.lastRoleRunId = ++runCounter;
      return HttpResponse.json({ run_id: mockParseState.lastRoleRunId, status: "queued" } satisfies CvParseQueued, { status: 202 });
    }),
    http.get("*/api/segments", async () => {
      await delay(10);
      return HttpResponse.json(mockSegments satisfies SegmentsResponse);
    }),
    http.get("*/api/discovery/preview", async () => {
      await delay(10);
      return HttpResponse.json(mockPreview satisfies DiscoveryPreview);
    }),
    // 3.8g/3.8h: the wizard polls the parse run for its summary proposal.
    http.get("*/api/runs/:runId", async ({ params }) => {
      await delay(10);
      if (Number(params.runId) === mockParseState.lastRoleRunId) {
        const roles: CustomRole[] = mockParseState.lastRoleTexts.map((text) => ({
          text,
          ids: state.mappedRoleIds ?? [],
        }));
        return HttpResponse.json({
          id: mockParseState.lastRoleRunId,
          run_type: "role_map",
          status: "completed",
          params: {},
          triggered_by: "api",
          error: null,
          created_at: new Date().toISOString(),
          started_at: new Date().toISOString(),
          finished_at: new Date().toISOString(),
          summary: { custom_roles: roles },
        });
      }
      if (Number(params.runId) !== mockParseState.lastRunId) {
        return HttpResponse.json({ detail: "Run not found" }, { status: 404 });
      }
      return HttpResponse.json({
        id: mockParseState.lastRunId,
        run_type: "profile_parse",
        status: "completed",
        params: {},
        triggered_by: "wizard",
        error: null,
        created_at: new Date().toISOString(),
        started_at: new Date().toISOString(),
        finished_at: new Date().toISOString(),
        summary: {
          suggested_role_families: state.suggestedFamilies,
          ...(state.evidenceProposal ? { evidence_proposal: state.evidenceProposal } : {}),
          ...(state.extractedCvText !== null ? { extracted_cv_text: state.extractedCvText } : {}),
        },
      });
    }),
  ];
}

export const handlers = createHandlers();
