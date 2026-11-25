import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "@/components/Layout";
import { CompaniesPage } from "@/pages/CompaniesPage";
import { FindJobsPage } from "@/pages/FindJobsPage";
import { JobsPage } from "@/pages/JobsPage";
import { ResultsPage } from "@/pages/ResultsPage";
import { ReviewPage } from "@/pages/ReviewPage";
import { RunResultPage } from "@/pages/RunResultPage";
import { RunsPage } from "@/pages/RunsPage";
import { api } from "@/api/client";
import type { Company, Job, Run } from "@/api/types";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, api: { ...actual.api } };
});

// Spy on the api object's methods (it's a plain object, not auto-mocked).
beforeEach(() => {
  vi.clearAllMocks();
  for (const key of Object.keys(api) as (keyof typeof api)[]) {
    vi.spyOn(api, key).mockImplementation(() => {
      throw new Error(`api.${String(key)} not mocked`);
    });
  }
});

const company = (id: number, overrides: Partial<Company> = {}): Company => ({
  id,
  name: `Company ${id}`,
  careers_url: `https://example.com/${id}/careers`,
  status: "pending",
  country: "fi",
  verification_status: "unverified",
  ats_platform: null,
  notes: "",
  verified_at: null,
  open_positions_url: null,
  open_positions_landing_url: null,
  open_positions_link_text: null,
  run_id: null,
  entry_run_id: null,
  entry_resolved_at: null,
  last_scraped_at: null,
  last_scrape_status: null,
  last_scrape_run_id: null,
  job_count: 0,
  ...overrides,
});

const job = (id: number, overrides: Partial<Job> = {}): Job => ({
  id,
  url: `https://example.com/jobs/${id}`,
  status: "new",
  country: "fi",
  total_score: 0.8,
  normalized_title: `Job ${id}`,
  normalized_company: `Company ${id}`,
  normalized_location: "Helsinki",
  has_applications: false,
  ...overrides,
});

const run = (id: number, overrides: Partial<Run> = {}): Run => ({
  id,
  run_type: "discover",
  status: "running",
  params: { dry_run: true },
  triggered_by: "test",
  error: null,
  summary: null,
  created_at: "2026-09-08T10:00:00",
  started_at: "2026-09-08T10:00:01",
  finished_at: null,
  ...overrides,
});

function renderAt(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/search/review" element={<ReviewPage />} />
            <Route path="/search/jobs" element={<FindJobsPage />} />
            <Route path="/search/results" element={<ResultsPage />} />
            <Route path="/search/results/:runId" element={<RunResultPage />} />
            <Route path="/companies" element={<CompaniesPage />} />
            <Route path="/jobs" element={<JobsPage />} />
            <Route path="/runs" element={<RunsPage />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mockedApi = vi.mocked(api, true);

describe("frontend smoke", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("review page lists pending rows with their resolved entry", async () => {
    mockedApi.listCompanies.mockResolvedValue([
      company(1, {
        open_positions_url: "https://a.example/jobs",
        open_positions_landing_url: "https://a.example/careers",
        open_positions_link_text: "Open positions",
      }),
      company(2, { open_positions_url: "https://b.example/jobs" }),
    ]);
    renderAt("/search/review");
    expect(await screen.findByRole("tab", { name: "Candidates" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    expect(mockedApi.listCompanies).toHaveBeenCalledWith({
      status: "pending",
      order: "id_desc",
      has_entry: true,
    });
    expect(screen.getByText("linked as “Open positions”")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "https://a.example/jobs" })).toHaveAttribute(
      "href",
      "https://a.example/jobs",
    );
  });

  it("review page bulk-accepts the selection through review-batch", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1), company(2)]);
    mockedApi.reviewBatch.mockResolvedValue({ updated: 2, unknown_ids: [] });
    renderAt("/search/review");
    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    const user = userEvent.setup();

    await user.click(screen.getByText("Select page"));
    await user.click(screen.getByText("Accept selected (2)"));

    await waitFor(() =>
      expect(mockedApi.reviewBatch).toHaveBeenCalledWith({
        company_ids: [1, 2],
        status: "accepted",
      }),
    );
  });

  it("companies page renders the library without any start action", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(3, { status: "known" })]);
    renderAt("/companies");
    await waitFor(() => expect(screen.getByText("Company 3")).toBeInTheDocument());
    expect(screen.getByText("known")).toBeInTheDocument();
    // Actions moved out of the library: no run can be started from here.
    expect(screen.queryByText(/Start careers search/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Select page/)).not.toBeInTheDocument();
  });

  it("companies page deletes selected companies after a confirm with counts", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(3), company(4)]);
    mockedApi.deleteCompanies
      .mockResolvedValueOnce({
        deleted: [{ id: 3, name: "Company 3", jobs: 5 }],
        blocked: [],
        unknown_ids: [],
        dry_run: true,
      })
      .mockResolvedValueOnce({
        deleted: [{ id: 3, name: "Company 3", jobs: 5 }],
        blocked: [],
        unknown_ids: [],
        dry_run: false,
      });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderAt("/companies");
    await waitFor(() => expect(screen.getByText("Company 3")).toBeInTheDocument());

    await user.click(screen.getByLabelText("Select company 3"));
    await user.click(screen.getByRole("button", { name: "Delete selected (1)" }));

    await waitFor(() => expect(mockedApi.deleteCompanies).toHaveBeenCalledTimes(2));
    expect(mockedApi.deleteCompanies).toHaveBeenNthCalledWith(1, [3], true);
    expect(mockedApi.deleteCompanies).toHaveBeenNthCalledWith(2, [3]);
    expect(confirm.mock.calls[0][0]).toContain("5 jobs");
    confirm.mockRestore();
  });

  it("jobs page renders jobs from /jobs", async () => {
    mockedApi.listJobs.mockResolvedValue([job(7)]);
    renderAt("/jobs");
    await waitFor(() => expect(screen.getByText("Job 7")).toBeInTheDocument());
    // Scores can be stale, so the all-jobs list never shows them.
    expect(screen.queryByText("0.80")).not.toBeInTheDocument();
    expect(screen.queryByText("Score")).not.toBeInTheDocument();
  });

  it("jobs page deletes a posting after confirmation", async () => {
    mockedApi.listJobs.mockResolvedValue([job(7)]);
    mockedApi.deleteJob.mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderAt("/jobs");

    await waitFor(() => expect(screen.getByText("Job 7")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Delete job 7" }));

    expect(confirm).toHaveBeenCalled();
    expect(mockedApi.deleteJob).toHaveBeenCalledWith(7);
    confirm.mockRestore();
  });

  it("jobs page keeps the row when the delete is not confirmed", async () => {
    mockedApi.listJobs.mockResolvedValue([job(7)]);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    renderAt("/jobs");

    await waitFor(() => expect(screen.getByText("Job 7")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Delete job 7" }));

    expect(mockedApi.deleteJob).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("jobs page searches by word and bulk-deletes only the visible selection", async () => {
    mockedApi.listJobs.mockResolvedValue([job(7), job(8)]);
    mockedApi.deleteJob.mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    renderAt("/jobs");

    await waitFor(() => expect(screen.getByText("Job 7")).toBeInTheDocument());
    await user.click(screen.getByLabelText("Select all jobs"));
    await user.type(screen.getByLabelText("Search"), "Job 7");
    expect(screen.queryByText("Job 8")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete selected (1)" }));
    expect(mockedApi.deleteJob).toHaveBeenCalledTimes(1);
    expect(mockedApi.deleteJob).toHaveBeenCalledWith(7);
    confirm.mockRestore();
  });

  it("runs page shows the runs list and the manual start form", async () => {
    mockedApi.listRuns.mockResolvedValue([run(4, { status: "completed" })]);
    renderAt("/runs");
    expect(await screen.findByText("Start discovery run")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("completed")).toBeInTheDocument());
  });

  it("results page lists runs and says waiting for an active one", async () => {
    mockedApi.listRuns.mockResolvedValue([
      run(9, { status: "running" }),
      run(8, { status: "completed" }),
    ]);
    renderAt("/search/results");
    expect(await screen.findByText(/waiting for results/)).toBeInTheDocument();
    expect(screen.getAllByText("Match search").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("link", { name: "Open" })[0]).toHaveAttribute(
      "href",
      "/search/results/9",
    );
  });

  it("results page prompts with a way to start when there is nothing yet", async () => {
    mockedApi.listRuns.mockResolvedValue([]);
    renderAt("/search/results");
    expect(await screen.findByText(/No results yet/)).toBeInTheDocument();
  });

  it("run result page names the waiting state while polling", async () => {
    mockedApi.getRun.mockResolvedValue(run(9, { status: "running" }));
    renderAt("/search/results/9");
    expect(await screen.findByText("Waiting for results")).toBeInTheDocument();
    expect(screen.getByText("Run #9 · discover")).toBeInTheDocument();
    expect(screen.getByText("Cancel run")).toBeInTheDocument();
  });

  it("run result page shows the finished state and the review path", async () => {
    mockedApi.getRun.mockResolvedValue(
      run(5, { status: "completed", run_type: "careers_search" }),
    );
    mockedApi.listRunSources.mockResolvedValue([]);
    renderAt("/search/results/5");
    expect(await screen.findByText("Run finished")).toBeInTheDocument();
    expect(screen.getByText("Go to review")).toBeInTheDocument();
    expect(screen.queryByText("Cancel run")).not.toBeInTheDocument();
  });

  it("find jobs page gathers over accepted companies from /runs/collect", async () => {
    mockedApi.companyCount.mockResolvedValue({
      by_status: { accepted: 2, known: 5 },
      by_country: { fi: 7 },
    });
    mockedApi.listCompanies.mockResolvedValue([company(1, { status: "accepted" })]);
    mockedApi.startCollectRun.mockResolvedValue({ run_id: 30, status: "queued" });
    mockedApi.getRun.mockResolvedValue(run(30, { run_type: "collect", status: "running" }));
    const user = userEvent.setup();
    renderAt("/search/jobs");

    await user.click(await screen.findByText("Gather jobs (2)"));
    await waitFor(() =>
      expect(mockedApi.startCollectRun).toHaveBeenCalledWith({
        dry_run: false,
        refresh_known: false,
        limit: null,
        batch: null,
      }),
    );
    expect(await screen.findByText("Run #30 · collect")).toBeInTheDocument();
  });

  it("find jobs page gathers exactly the ticked companies and shows scrape state", async () => {
    mockedApi.companyCount.mockResolvedValue({
      by_status: { accepted: 0, known: 1 },
      by_country: { se: 1 },
    });
    mockedApi.listCompanies.mockResolvedValue([
      company(1, { name: "Asabloy", status: "known", job_count: 4 }),
      company(2, {
        name: "Fresh Oy",
        status: "pending",
        last_scraped_at: "2026-11-20T10:00:00Z",
        last_scrape_status: "zero_yield",
        last_scrape_run_id: 8,
      }),
    ]);
    mockedApi.startCollectRun.mockResolvedValue({ run_id: 31, status: "queued" });
    mockedApi.getRun.mockResolvedValue(run(31, { run_type: "collect", status: "running" }));
    const user = userEvent.setup();
    renderAt("/search/jobs");

    expect(await screen.findByText("before run records")).toBeInTheDocument();
    expect(screen.getByText(/no postings/)).toBeInTheDocument();
    await user.click(screen.getByLabelText("Select company 1"));
    await user.click(await screen.findByText("Gather selected (1)"));
    await waitFor(() =>
      expect(mockedApi.startCollectRun).toHaveBeenCalledWith({
        dry_run: false,
        limit: null,
        company_ids: [1],
      }),
    );
  });

  it("find jobs page refuses to start when nothing is accepted", async () => {
    mockedApi.companyCount.mockResolvedValue({ by_status: {}, by_country: {} });
    mockedApi.listCompanies.mockResolvedValue([]);
    renderAt("/search/jobs");
    expect(await screen.findByText(/Nothing to gather yet/)).toBeInTheDocument();
    expect(screen.getByText("Gather jobs (0)")).toBeDisabled();
  });
});
