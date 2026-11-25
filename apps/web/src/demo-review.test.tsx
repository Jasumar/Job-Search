import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ReviewPage } from "@/pages/ReviewPage";
import { api } from "@/api/client";
import type { Company } from "@/api/types";

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
  open_positions_url: `https://example.com/${id}/jobs`,
  open_positions_landing_url: `https://example.com/${id}/careers`,
  open_positions_link_text: "Open positions",
  run_id: 3,
  entry_run_id: 17,
  entry_resolved_at: "2026-11-07T10:00:00Z",
  last_scraped_at: null,
  last_scrape_status: null,
  last_scrape_run_id: null,
  job_count: 0,
  ...overrides,
});

function renderReview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/review"]}>
        <Routes>
          <Route path="/review" element={<ReviewPage />} />
          <Route path="/search/results/:runId" element={<div>run results</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("demo review screen", () => {
  it("asks for pending rows with an entry, newest first", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(2), company(1)]);
    renderReview();

    await waitFor(() => expect(screen.getByText("Company 2")).toBeInTheDocument());
    expect(mockedApi.listCompanies).toHaveBeenCalledWith({
      status: "pending",
      order: "id_desc",
      has_entry: true,
    });
  });

  it("shows the resolved entry with its landing page and link text", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1)]);
    renderReview();

    const entry = await screen.findByRole("link", { name: "https://example.com/1/jobs" });
    expect(entry).toHaveAttribute("href", "https://example.com/1/jobs");
    expect(entry).toHaveAttribute("target", "_blank");
    expect(screen.getByText("linked as “Open positions”")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "https://example.com/1/careers" }),
    ).toHaveAttribute("href", "https://example.com/1/careers");
    expect(screen.getByRole("link", { name: "Run #3" })).toHaveAttribute("href", "/search/results/3");
  });

  it("accepts the active row from the keyboard", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(2), company(1)]);
    mockedApi.reviewCompany.mockResolvedValue({ updated: 1 });
    const user = userEvent.setup();
    renderReview();

    await waitFor(() => expect(screen.getByText("Company 2")).toBeInTheDocument());
    // cursor starts on the first row; "j" moves it to the next one
    await user.keyboard("a");
    await waitFor(() =>
      expect(mockedApi.reviewCompany).toHaveBeenCalledWith(2, {
        status: "accepted",
        careers_url: null,
        open_positions_url: null,
      }),
    );
    await user.keyboard("j");
    await user.keyboard("r");
    await waitFor(() =>
      expect(mockedApi.reviewCompany).toHaveBeenCalledWith(1, {
        status: "rejected",
        careers_url: null,
        open_positions_url: null,
      }),
    );
  });

  it("accepts one row with a corrected URL", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1)]);
    mockedApi.reviewCompany.mockResolvedValue({ updated: 1 });
    const user = userEvent.setup();
    renderReview();

    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Careers page URL"), "https://fixed.example/jobs");
    await user.click(screen.getByText("Accept with URLs"));

    await waitFor(() =>
      expect(mockedApi.reviewCompany).toHaveBeenCalledWith(1, {
        status: "accepted",
        careers_url: "https://fixed.example/jobs",
        open_positions_url: null,
      }),
    );
  });

  it("accepts with only the open-positions URL when the careers field is empty", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1)]);
    mockedApi.reviewCompany.mockResolvedValue({ updated: 1 });
    const user = userEvent.setup();
    renderReview();

    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    await user.type(screen.getByLabelText("Open positions page URL"), "https://fixed.example/open");
    await user.click(screen.getByText("Accept with URLs"));

    await waitFor(() =>
      expect(mockedApi.reviewCompany).toHaveBeenCalledWith(1, {
        status: "accepted",
        careers_url: null,
        open_positions_url: "https://fixed.example/open",
      }),
    );
  });

  it("reports stale ids from a batch and refreshes the list", async () => {
    mockedApi.listCompanies.mockResolvedValue([company(1), company(2)]);
    mockedApi.reviewBatch.mockResolvedValue({ updated: 1, unknown_ids: [99] });
    const user = userEvent.setup();
    renderReview();

    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    await user.click(screen.getByText("Select page"));
    await user.click(screen.getByText("Accept selected (2)"));

    expect(await screen.findByText(/1 id\(s\) no longer exist/)).toBeInTheDocument();
    expect(screen.getByText("0 of 2 selected")).toBeInTheDocument();
    // initial load + the post-batch refetch
    expect(mockedApi.listCompanies).toHaveBeenCalledTimes(2);
  });

  it("shows resolved entries read-only, with the run that resolved them", async () => {
    mockedApi.listCompanies.mockResolvedValue([
      company(4, { status: "accepted", entry_run_id: 12 }),
      company(5, { status: "known", entry_run_id: 17 }),
    ]);
    const user = userEvent.setup();
    renderReview();

    await waitFor(() => expect(screen.getByText("Company 4")).toBeInTheDocument());
    await user.click(screen.getByRole("tab", { name: "Resolved entries" }));

    await waitFor(() =>
      expect(mockedApi.listCompanies).toHaveBeenLastCalledWith({
        statuses: ["accepted", "known"],
        order: "id_desc",
        has_entry: true,
      }),
    );
    expect(await screen.findByText("Company 5")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Run #17" })).toHaveAttribute("href", "/search/results/17");
    // Read-only scope: no gate, no batch controls.
    expect(screen.queryByText("Accept")).not.toBeInTheDocument();
    expect(screen.queryByText(/Accept selected/)).not.toBeInTheDocument();
  });

  it("says so when no entry has been resolved yet", async () => {
    mockedApi.listCompanies.mockResolvedValue([]);
    const user = userEvent.setup();
    renderReview();

    await user.click(screen.getByRole("tab", { name: "Resolved entries" }));
    expect(
      await screen.findByText(/No resolved entries yet/),
    ).toBeInTheDocument();
  });

  it("can include pending rows that never resolved an entry", async () => {
    mockedApi.listCompanies.mockResolvedValue([
      company(1),
      company(2, {
        open_positions_url: null,
        open_positions_landing_url: null,
        open_positions_link_text: null,
      }),
    ]);
    const user = userEvent.setup();
    renderReview();

    await waitFor(() => expect(screen.getByText("Company 1")).toBeInTheDocument());
    await user.click(screen.getByLabelText("Include rows without an entry"));

    await waitFor(() =>
      expect(mockedApi.listCompanies).toHaveBeenLastCalledWith({
        status: "pending",
        order: "id_desc",
        has_entry: undefined,
      }),
    );
    expect(await screen.findByText(/No entry resolved/)).toBeInTheDocument();
    expect(screen.getByText("0 of 2 selected")).toBeInTheDocument();
  });
});
