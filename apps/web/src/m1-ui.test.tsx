import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "@/components/Layout";
import { CareersPage } from "@/pages/CareersPage";
import { ReviewPage } from "@/pages/ReviewPage";
import { ResultsPage } from "@/pages/ResultsPage";
import { RunResultPage } from "@/pages/RunResultPage";
import { RunsPage } from "@/pages/RunsPage";
import { ApiError, api } from "@/api/client";
import type { Company, Run, RunSource } from "@/api/types";

vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return { ...actual, api: { ...actual.api } };
});

beforeEach(() => {
  vi.clearAllMocks();
  for (const key of Object.keys(api) as (keyof typeof api)[]) {
    vi.spyOn(api, key).mockImplementation(() => {
      throw new Error(`api.${String(key)} not mocked`);
    });
  }
});

const mockedApi = vi.mocked(api, true);

const company = (id: number, overrides: Partial<Company> = {}): Company => ({
  id,
  name: `Company ${id}`,
  careers_url: null,
  status: "pending",
  country: "fi",
  verification_status: "unverified",
  ats_platform: null,
  notes: "",
  verified_at: null,
  open_positions_url: null,
  open_positions_landing_url: null,
  open_positions_link_text: null,
  run_id: 3,
  entry_run_id: null,
  entry_resolved_at: null,
  last_scraped_at: null,
  last_scrape_status: null,
  last_scrape_run_id: null,
  job_count: 0,
  ...overrides,
});

const run = (id: number, overrides: Partial<Run> = {}): Run => ({
  id,
  run_type: "name_collect",
  status: "running",
  params: {},
  triggered_by: "owner",
  error: null,
  summary: null,
  created_at: "2026-11-05T10:00:00",
  started_at: "2026-11-05T10:00:01",
  finished_at: null,
  ...overrides,
});

const sourceRow = (id: number, overrides: Partial<RunSource> = {}): RunSource => ({
  id,
  run_id: 7,
  source_id: null,
  company_id: id,
  company_name: `Company ${id}`,
  source_url: null,
  status: "queued",
  identities_found: 0,
  candidates_added: 0,
  candidates_duplicate: 0,
  extraction: null,
  recipe_id: null,
  error: null,
  entry_trace: null,
  author_trace: null,
  created_at: "2026-11-05T10:00:00",
  started_at: null,
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
            <Route path="/search/careers" element={<CareersPage />} />
            <Route path="/search/results" element={<ResultsPage />} />
            <Route path="/search/results/:runId" element={<RunResultPage />} />
            <Route path="/runs" element={<RunsPage />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("M1-UI careers-page search", () => {
  it("lists the no-careers view and starts a careers search over the selection", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1), company(2)]);
    mockedApi.startCareersSearchRun.mockResolvedValue({ run_id: 12, status: "queued" });
    mockedApi.getRun.mockResolvedValue(run(12, { run_type: "careers_search", status: "queued" }));
    mockedApi.listRunSources.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAt("/search/careers");

    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    await waitFor(() =>
      expect(mockedApi.listCompanies).toHaveBeenLastCalledWith(
        expect.objectContaining({ no_careers: true }),
      ),
    );

    await user.click(await screen.findByText("Select page"));
    expect(screen.getByText("2 of 2 selected")).toBeInTheDocument();
    await user.click(screen.getByText("Start careers search (2)"));

    await waitFor(() =>
      expect(mockedApi.startCareersSearchRun).toHaveBeenCalledWith({
        company_ids: [1, 2],
      }),
    );
    expect(await screen.findByText("Run #12 · careers_search")).toBeInTheDocument();
  });

  it("treats a 409 start as blocked, not as a failure", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1)]);
    mockedApi.startCareersSearchRun.mockRejectedValue(
      new ApiError(409, "a careers_search run is already active"),
    );
    const user = userEvent.setup();
    renderAt("/search/careers");

    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    await user.click(await screen.findByText("Select page"));
    await user.click(screen.getByText("Start careers search (1)"));

    expect(await screen.findByText(/Start blocked/)).toHaveTextContent(
      "a careers_search run is already active",
    );
    expect(screen.queryByText(/Start failed/)).not.toBeInTheDocument();
  });

  it("refreshes the list and clears the selection on a stale 422", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1)]);
    mockedApi.startCareersSearchRun.mockRejectedValue(
      new ApiError(422, "Companies already have a hiring source: [1]"),
    );
    const user = userEvent.setup();
    renderAt("/search/careers");

    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    await user.click(await screen.findByText("Select page"));
    await user.click(screen.getByText("Start careers search (1)"));

    expect(await screen.findByText(/Selection is stale/)).toBeInTheDocument();
    expect(screen.getByText("0 of 1 selected")).toBeInTheDocument();
    // initial load + the 422 refresh (the other panel makes no company call)
    expect(mockedApi.listCompanies).toHaveBeenCalledTimes(2);
  });
});

describe("M1-UI review suspects", () => {
  it("labels the search result and the resolved entry with what the system thinks they are", async () => {
    mockedApi.listCompanies.mockResolvedValue([
      company(1, {
        open_positions_url: "https://acme.example/careers/avoimet",
        open_positions_landing_url: "https://acme.example/careers",
        open_positions_link_text: "Avoimet työpaikat",
      }),
    ]);
    renderAt("/search/review");

    expect(
      await screen.findByText("Careers page (search result)"),
    ).toBeInTheDocument();
    expect(screen.getByText("Open positions page — system suspect")).toBeInTheDocument();
    expect(screen.getByText("linked as “Avoimet työpaikat”")).toBeInTheDocument();
    expect(
      screen.getByText(/Accepting records the company's status only/),
    ).toBeInTheDocument();
  });

  it("says so when the search-result page itself listed the postings", async () => {
    mockedApi.listCompanies.mockResolvedValue([
      company(1, {
        open_positions_url: "https://acme.example/careers",
        open_positions_landing_url: "https://acme.example/careers",
        open_positions_link_text: "",
      }),
    ]);
    renderAt("/search/review");

    expect(await screen.findByText("Open positions page — system suspect")).toBeInTheDocument();
    // One link only: the careers page and the entry are the same URL.
    expect(screen.queryByText("Careers page (search result)")).not.toBeInTheDocument();
    expect(
      screen.getByText("the page the search surfaced itself lists postings"),
    ).toBeInTheDocument();
  });

  it("names a sitemap listing as such", async () => {
    mockedApi.listCompanies.mockResolvedValue([
      company(1, {
        open_positions_url: "https://acme.example/sitemap.xml",
        open_positions_landing_url: "https://acme.example/careers",
        open_positions_link_text: "sitemap",
      }),
    ]);
    renderAt("/search/review");

    expect(
      await screen.findByText("the site's sitemap lists these postings"),
    ).toBeInTheDocument();
  });
});

describe("M1-UI listing paste sheet", () => {
  it("starts a name collection with the pasted non-empty lines", async () => {
    mockedApi.startNameCollectRun.mockResolvedValue({ run_id: 20, status: "queued" });
    mockedApi.getRun.mockResolvedValue(run(20, { status: "queued" }));
    mockedApi.listRunSources.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAt("/search/careers");

    await user.type(
      screen.getByLabelText("Listing page URLs"),
      "https://a.example/list{enter}{enter}https://b.example/jobs{enter}",
    );
    expect(screen.getByText("2 URLs")).toBeInTheDocument();
    await user.click(screen.getByText("Start name collection"));

    await waitFor(() =>
      expect(mockedApi.startNameCollectRun).toHaveBeenCalledWith({
        urls: ["https://a.example/list", "https://b.example/jobs"],
        batch_size: null,
        page_ceiling: null,
      }),
    );
    expect(await screen.findByText("Run #20 · name_collect")).toBeInTheDocument();
  });

  it("shows a blocked start when a name_collect run is already active", async () => {
    mockedApi.startNameCollectRun.mockRejectedValue(
      new ApiError(409, "a name_collect run is already active"),
    );
    const user = userEvent.setup();
    renderAt("/search/careers");

    await user.type(screen.getByLabelText("Listing page URLs"), "https://a.example/list");
    await user.click(screen.getByText("Start name collection"));

    expect(await screen.findByText(/Start blocked/)).toHaveTextContent(
      "a name_collect run is already active",
    );
  });
});

describe("M1-UI run history", () => {
  it("asks for the last 10 runs and shows per-source progress", async () => {
    mockedApi.listRuns.mockResolvedValue([
      run(7, { run_type: "careers_search" }),
      run(6, { run_type: "discover", status: "completed" }),
    ]);
    mockedApi.listRunSources.mockResolvedValue([
      sourceRow(1, { status: "completed" }),
      sourceRow(2, { status: "running" }),
      sourceRow(3, { status: "failed" }),
    ]);
    renderAt("/search/results");

    await waitFor(() =>
      expect(screen.getAllByText(/2 \/ 3 done/)[0]).toHaveTextContent("1 failed"),
    );
    expect(mockedApi.listRuns).toHaveBeenCalledWith(undefined, 20);
    // discover has no manifest, so no extra request is made for it.
    expect(mockedApi.listRunSources).toHaveBeenCalledTimes(1);
    expect(mockedApi.listRunSources).toHaveBeenCalledWith(7);
  });
});

describe("M1-UI run result screen", () => {
  it("shows the per-company manifest and pauses a running run", async () => {
    mockedApi.getRun.mockResolvedValue(run(7, { run_type: "careers_search" }));
    mockedApi.listRunSources.mockResolvedValue([
      sourceRow(1, { status: "completed", identities_found: 5, candidates_added: 4 }),
      sourceRow(2, { status: "running" }),
    ]);
    mockedApi.pauseRun.mockResolvedValue({ id: 7, status: "paused" });
    const user = userEvent.setup();
    renderAt("/search/results/7");

    expect(await screen.findByText("Company 1")).toBeInTheDocument();
    expect(screen.getByText("1 / 2 done")).toBeInTheDocument();
    expect(screen.getByText("Company 2")).toBeInTheDocument();
    await user.click(screen.getByText("Pause"));
    await waitFor(() => expect(mockedApi.pauseRun).toHaveBeenCalledWith(7));
  });

  it("makes an entry-resolution miss readable from the manifest", async () => {
    const trace = JSON.stringify({
      reason: "no page under https://acme.example/careers yields postings",
      rendered: true,
      sitemap: "https://acme.example/sitemap.xml",
      tried: [
        { url: "https://acme.example/careers/jobs", score: 6, outcome: "no_postings" },
        { url: "https://acme.example/ura", score: 4, outcome: "unreachable" },
      ],
      candidates: [{ url: "https://acme.example/careers/jobs", score: 6, text: "Jobs" }],
    });
    mockedApi.getRun.mockResolvedValue(run(7, { run_type: "careers_search" }));
    mockedApi.listRunSources.mockResolvedValue([
      sourceRow(1, { status: "zero_yield", error: "no postings", entry_trace: trace }),
    ]);
    renderAt("/search/results/7");

    expect(await screen.findByText("Company 1")).toBeInTheDocument();
    expect(
      screen.getByText(/acme\.example\/careers\/jobs \(6\) → no_postings/)
    ).toBeInTheDocument();
    expect(screen.getByText(/rendered retry used/)).toBeInTheDocument();
  });

  it("makes a listing's authoring trials readable from the manifest", async () => {
    const trace = JSON.stringify({
      status: "failed",
      kind: null,
      recipe_id: null,
      trials: [
        {
          number: 1,
          passed: false,
          reason: "replay failed: container .card matched no elements",
          body: '{"kind": "dom", "container": ".card"}',
        },
      ],
    });
    mockedApi.getRun.mockResolvedValue(run(7, { run_type: "name_collect" }));
    mockedApi.listRunSources.mockResolvedValue([
      sourceRow(1, {
        status: "failed",
        source_url: "https://acme.example/jobs",
        company_id: null,
        company_name: null,
        error: "recipe authoring failed",
        author_trace: trace,
      }),
    ]);
    renderAt("/search/results/7");

    expect(await screen.findByText("https://acme.example/jobs")).toBeInTheDocument();
    expect(
      screen.getByText(/trial 1 failed: replay failed: container \.card matched no elements/)
    ).toBeInTheDocument();
    expect(screen.getByText("tried body")).toBeInTheDocument();
  });

  it("offers resume for a paused run and names the held state", async () => {
    mockedApi.getRun.mockResolvedValue(run(7, { run_type: "careers_search", status: "paused" }));
    mockedApi.listRunSources.mockResolvedValue([sourceRow(1, { status: "paused" })]);
    mockedApi.resumeRun.mockResolvedValue({ id: 7, status: "queued" });
    const user = userEvent.setup();
    renderAt("/search/results/7");

    expect(await screen.findByText(/Paused — the worker holds/)).toBeInTheDocument();
    expect(screen.queryByText("Pause")).not.toBeInTheDocument();
    await user.click(screen.getByText("Resume"));
    await waitFor(() => expect(mockedApi.resumeRun).toHaveBeenCalledWith(7));
  });

  it("shows a cancel request as pending at the next checkpoint", async () => {
    mockedApi.getRun.mockResolvedValue(
      run(7, { run_type: "careers_search", status: "cancelled_requested" }),
    );
    mockedApi.listRunSources.mockResolvedValue([sourceRow(1, { status: "running" })]);
    renderAt("/search/results/7");

    expect(
      await screen.findByText(/Cancel requested — the worker stops/),
    ).toBeInTheDocument();
    expect(screen.queryByText("Cancel run")).not.toBeInTheDocument();
  });
});