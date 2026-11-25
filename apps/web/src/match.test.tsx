import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MatchesPage } from "@/pages/MatchesPage";
import { StartMatchPage } from "@/pages/StartMatchPage";
import { api } from "@/api/client";
import { profileApi } from "@/api/profile-api";
import type { DiscoveryPreview } from "@/api/profile-types";
import type { Job, MatchStatus } from "@/api/types";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, api: { ...actual.api } };
});

vi.mock("@/api/profile-api", () => ({
  profileApi: { discoveryPreview: vi.fn(), getProfile: vi.fn() },
}));

beforeEach(() => {
  vi.clearAllMocks();
  // Matches only show once a profile is saved; most tests assume one.
  mockedProfileApi.getProfile.mockResolvedValue(savedProfile);
  for (const key of Object.keys(api) as (keyof typeof api)[]) {
    vi.spyOn(api, key).mockImplementation(() => {
      throw new Error(`api.${String(key)} not mocked`);
    });
  }
});

const mockedApi = vi.mocked(api, true);
const mockedProfileApi = vi.mocked(profileApi, true);

const savedProfile = {
  profile: {
    name: "Test User",
    education: "",
    experience: "",
    skills: [],
    job_preferences: [],
    target_countries: ["Finland"],
    target_cities: [],
    work_arrangements: [],
    notes: [],
    role_families: ["technology/backend"],
    location: "",
    citizenship: "",
    linkedin: "",
    gitlab: "",
    email: "",
    phone: "",
    visa_required: false,
  },
};

const plan = (overrides: Partial<DiscoveryPreview> = {}): DiscoveryPreview => ({
  selected_segments: ["saas", "fintech"],
  selected_countries: ["fi", "ee"],
  baseline_query_count: 4,
  ceiling: 12,
  profile_source: "db_profile",
  profile_reference: "2026-11-01T10:00:00",
  sample_queries: ["saas companies finland", "fintech companies estonia"],
  ...overrides,
});

const job = (id: number, score: number, overrides: Partial<Job> = {}): Job => ({
  id,
  url: `https://example.com/jobs/${id}`,
  status: "new",
  country: "fi",
  total_score: score,
  normalized_title: `Job ${id}`,
  normalized_company: `Company ${id}`,
  normalized_location: "Helsinki",
  has_applications: false,
  recommendation: score >= 0.7 ? "worth" : score >= 0.4 ? "consider" : "skip",
  fit_summary: `${Math.round(score * 100)}% match`,
  fit_strengths: ["Python evidenced"],
  fit_gaps: ["Kubernetes not evidenced"],
  fit_blockers: [],
  fit_components: { required_technologies: 0.9, location: 1 },
  ...overrides,
});

const matchStatus = (overrides: Partial<MatchStatus> = {}): MatchStatus => ({
  profile_updated_at: null,
  last_match_finished_at: null,
  profile_changed: false,
  results_current: true,
  evidence_ready: true,
  unprofiled_remaining: 0,
  worth_threshold: 0.7,
  consider_threshold: 0.4,
  ...overrides,
});

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/match" element={<StartMatchPage />} />
          <Route path="/matches" element={<MatchesPage />} />
          <Route path="/search/results/:runId" element={<div>run results</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("start a match", () => {
  it("shows the plan derived from the profile and starts a discover run", async () => {
    mockedProfileApi.discoveryPreview.mockResolvedValue(plan());
    mockedApi.startDiscoverRun.mockResolvedValue({ run_id: 40, status: "queued" });
    const user = userEvent.setup();
    renderAt("/match");

    expect(await screen.findByText("4 of 12")).toBeInTheDocument();
    expect(screen.getByText("fi, ee")).toBeInTheDocument();
    expect(screen.getByText("your profile")).toBeInTheDocument();
    expect(screen.queryByText(/no target countries/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Start a match" }));
    await waitFor(() => expect(mockedApi.startDiscoverRun).toHaveBeenCalledWith({ dry_run: false }));
    expect(await screen.findByText("run results")).toBeInTheDocument();
  });

  it("says the plan is unscoped when the profile has no targets yet", async () => {
    mockedProfileApi.discoveryPreview.mockResolvedValue(
      plan({ profile_source: "default", selected_countries: [] }),
    );
    renderAt("/match");

    expect(await screen.findByText(/no target countries or role families yet/)).toBeInTheDocument();
    expect(screen.getByText("no profile yet")).toBeInTheDocument();
  });
});

describe("matches", () => {
  beforeEach(() => {
    mockedApi.matchStatus.mockResolvedValue(matchStatus());
  });

  it("lists scored jobs best first and shows the selected job", async () => {
    mockedApi.listJobs.mockResolvedValue([job(2, 0.91), job(1, 0.42)]);
    renderAt("/matches");

    const detail = await screen.findByLabelText("Selected job");
    expect(within(detail).getByText("Job 2")).toBeInTheDocument();
    expect(within(detail).getByText("Python evidenced")).toBeInTheDocument();
    expect(within(detail).getByText("Kubernetes not evidenced")).toBeInTheDocument();
    expect(within(detail).getByText("Required technologies")).toBeInTheDocument();
    expect(within(detail).getByText(/Threshold 0.70/)).toBeInTheDocument();
    expect(mockedApi.listJobs).toHaveBeenCalledWith({
      scored: true,
      order: "score_desc",
      limit: 500,
    });
  });

  it("hides suspected non-postings and lists them last on request", async () => {
    mockedApi.listJobs.mockResolvedValue([
      job(1, 0.95, { normalized_title: "Privacy page", suspect_reason: "no_posting_signal" }),
      job(2, 0.6),
    ]);
    const user = userEvent.setup();
    renderAt("/matches");

    const detail = await screen.findByLabelText("Selected job");
    expect(within(detail).getByText("Job 2")).toBeInTheDocument();
    expect(screen.queryByText("Privacy page")).not.toBeInTheDocument();
    expect(screen.getByText(/1 suspected non-postings/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Show them" }));
    const items = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    expect(items[0]).toContain("Job 2");
    expect(items[1]).toContain("Privacy page");
    expect(items[1]).toContain("Suspected: not a posting");
  });

  it("filters by group and by country", async () => {
    mockedApi.listJobs.mockResolvedValue([
      job(1, 0.91),
      job(2, 0.55),
      job(3, 0.8, { country: "de" }),
    ]);
    const user = userEvent.setup();
    renderAt("/matches");

    await screen.findByLabelText("Selected job");
    await user.click(screen.getByRole("button", { name: /^Consider/ }));
    expect(screen.getAllByText("Job 2").length).toBeGreaterThan(0);
    expect(screen.queryByText("Job 1")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^All/ }));
    await user.selectOptions(screen.getByLabelText("Country:"), "de");
    expect(screen.queryByText("Job 1")).not.toBeInTheDocument();
    expect(screen.getAllByText("Job 3").length).toBeGreaterThan(0);
  });

  it("shows nothing until a run made with the current information, and the unscored count", async () => {
    mockedApi.listJobs.mockResolvedValue([job(1, 0.5)]);
    mockedApi.matchStatus.mockResolvedValue(
      matchStatus({ profile_changed: true, results_current: false, unprofiled_remaining: 37 }),
    );
    renderAt("/matches");

    expect(
      await screen.findByText("Your information changed since the last scoring"),
    ).toBeInTheDocument();
    expect(screen.getByText(/37 more jobs not scored yet/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Selected job")).not.toBeInTheDocument();
    expect(screen.queryByText("Job 1")).not.toBeInTheDocument();
  });

  it("MF5: lists the top 5, then the rest on Show more", async () => {
    mockedApi.listJobs.mockResolvedValue(
      [0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65].map((score, i) => job(i + 1, score)),
    );
    const user = userEvent.setup();
    renderAt("/matches");

    await screen.findByLabelText("Selected job");
    expect(screen.getAllByText("Job 5").length).toBeGreaterThan(0);
    expect(screen.queryByText("Job 6")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show more (2)" }));
    expect(screen.getByText("Job 7")).toBeInTheDocument();
  });

  it("MF4: hides jobs outside the target roles behind a toggle that names the reason", async () => {
    const reason = "Role is Legal & compliance › Licensing & IP, not in your target roles";
    mockedApi.listJobs.mockResolvedValue([
      job(1, 0.9, { normalized_title: "Licensing Manager", fit_hidden_reason: reason }),
      job(2, 0.6),
    ]);
    const user = userEvent.setup();
    renderAt("/matches");

    await screen.findByLabelText("Selected job");
    expect(screen.queryByText("Licensing Manager")).not.toBeInTheDocument();
    expect(screen.getByText(/1 hidden as not your role/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show hidden" }));
    expect(screen.getAllByText("Licensing Manager").length).toBeGreaterThan(0);
    expect(screen.getAllByText(new RegExp(reason)).length).toBeGreaterThan(0);
  });

  it("MF1: Score jobs is disabled and says why when no skills or experience are saved", async () => {
    mockedApi.listJobs.mockResolvedValue([]);
    mockedApi.matchStatus.mockResolvedValue(matchStatus({ evidence_ready: false, results_current: false }));
    renderAt("/matches");

    expect(await screen.findByText(/no skills and no experience/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Score jobs" })).toBeDisabled();
  });

  it("starts a run, shows the wait state, then the list when it finishes", async () => {
    mockedApi.listJobs.mockResolvedValue([job(1, 0.5)]);
    mockedApi.startMatchRun.mockResolvedValue({ run_id: 41, status: "queued" });
    mockedApi.getRun.mockResolvedValue({
      id: 41,
      run_type: "match",
      status: "completed",
      params: {},
      triggered_by: "api",
      error: null,
      summary: null,
      created_at: "2026-11-23T10:00:00",
      started_at: "2026-11-23T10:00:01",
      finished_at: "2026-11-23T10:00:05",
    });
    const user = userEvent.setup();
    renderAt("/matches");

    await user.click(await screen.findByRole("button", { name: "Score jobs" }));

    await waitFor(() => expect(mockedApi.startMatchRun).toHaveBeenCalledWith({}));
    await waitFor(() => expect(mockedApi.getRun).toHaveBeenCalledWith(41));
    expect(await screen.findByRole("button", { name: "Score jobs" })).toBeEnabled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("deletes a posting that is not a job", async () => {
    mockedApi.listJobs.mockResolvedValue([job(1, 0.5)]);
    mockedApi.deleteJob.mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderAt("/matches");

    await user.click(await screen.findByRole("button", { name: "Not a job — delete" }));
    await waitFor(() => expect(mockedApi.deleteJob).toHaveBeenCalledWith(1));
  });

  it("shows no matches until the profile is saved", async () => {
    mockedProfileApi.getProfile.mockResolvedValue({ profile: null });
    mockedApi.listJobs.mockResolvedValue([job(1, 0.9)]);
    renderAt("/matches");

    expect(await screen.findByText("No matches yet")).toBeInTheDocument();
    expect(screen.queryByText("Job 1")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set up My information" })).toHaveAttribute(
      "href",
      "/profile",
    );
  });

  it("says where to get jobs when nothing is scored yet", async () => {
    mockedApi.listJobs.mockResolvedValue([]);
    renderAt("/matches");

    expect(await screen.findByText(/No scored jobs yet/)).toBeInTheDocument();
  });
});
