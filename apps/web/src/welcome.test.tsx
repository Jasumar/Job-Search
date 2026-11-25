import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "@/components/Layout";
import { WelcomePage } from "@/pages/WelcomePage";
import { ProfileWizardPage } from "@/pages/wizard/ProfileWizardPage";
import { api } from "@/api/client";
import type { Run } from "@/api/types";

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

const run = (id: number, overrides: Partial<Run> = {}): Run => ({
  id,
  run_type: "discover",
  status: "completed",
  params: { dry_run: true, country: "fi", role: "backend" },
  triggered_by: "test",
  error: null,
  summary: null,
  created_at: "2026-09-20T10:00:00",
  started_at: "2026-09-20T10:00:01",
  finished_at: "2026-09-20T12:00:00",
  ...overrides,
});

function renderAt(path: string, state?: unknown) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[{ pathname: path, state }]}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<WelcomePage />} />
            <Route path="/profile" element={<ProfileWizardPage />} />
            {/* Layout renders the menu for any route; this stand-in keeps the
                drawer tests independent of a real page's data needs. */}
            <Route path="/search/review" element={<div>review stand-in</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const mockedApi = vi.mocked(api, true);

// ProfileWizardPage fetches /profile, /segments via profileApi (separately
// mocked in wizard.test.tsx); here we stub the module wholesale.
vi.mock("@/api/profile-api", () => ({
  profileApi: {
    getProfile: vi.fn().mockResolvedValue({ profile: null, has_cv: false, updated_at: null }),
    getSegments: vi.fn().mockResolvedValue({ countries: [], role_families: [] }),
    getRunSummary: vi.fn(),
    discoveryPreview: vi.fn(),
    parseCv: vi.fn(),
    uploadCvFile: vi.fn(),
    updateProfile: vi.fn(),
  },
}));

describe("welcome page (3.8i)", () => {
  it("renders headline, name input, and empty old-searches state", async () => {
    mockedApi.listRuns.mockResolvedValue([]);
    renderAt("/");
    expect(
      screen.getByRole("heading", { name: /Welcome to Job Search/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/Your name/i)).toBeInTheDocument();
    expect(await screen.findByText(/No searches yet/i)).toBeInTheDocument();
  });

  it("lists recent completed runs as old-search rows", async () => {
    mockedApi.listRuns.mockResolvedValue([run(11, { status: "completed" })]);
    renderAt("/");
    expect(await screen.findByText(/backend jobs in fi/i)).toBeInTheDocument();
    expect(screen.getByText(/Finished/)).toBeInTheDocument();
  });

  it("navigates to the wizard with the typed name", async () => {
    mockedApi.listRuns.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAt("/");
    await user.type(screen.getByLabelText(/Your name/i), "Alex");
    await user.keyboard("{Enter}");
    // Wizard welcome step renders with the handed-over name pre-filled.
    expect(await screen.findByDisplayValue("Alex")).toBeInTheDocument();
  });

  it("hamburger opens the drawer with main destinations and navigates", async () => {
    mockedApi.listRuns.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAt("/");
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    const drawer = screen.getByRole("navigation", { name: "Main menu" });
    expect(drawer).toBeInTheDocument();
    expect(screen.getByText("Companies")).toBeInTheDocument();
    await user.click(screen.getByText("Companies"));
    // The drawer closes on navigation.
    expect(screen.queryByRole("navigation", { name: "Main menu" })).not.toBeInTheDocument();
  });

  it("orders the drawer by task: profile, the two paths, the libraries, advanced", async () => {
    mockedApi.listRuns.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAt("/");
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    const drawer = screen.getByRole("navigation", { name: "Main menu" });

    // Ungrouped destinations are always visible; groups start collapsed when
    // the current route is outside them.
    expect(within(drawer).getAllByRole("link").map((el) => el.textContent)).toEqual([
      "My information",
      "Companies",
      "Jobs",
    ]);
    // Group headings say which path each destination belongs to, and each one
    // is a toggle rather than a label.
    for (const heading of ["Job matching", "Job search", "Advanced"]) {
      expect(
        within(drawer).getByRole("button", { name: new RegExp(heading) }),
      ).toHaveAttribute("aria-expanded", "false");
    }

    await user.click(within(drawer).getByRole("button", { name: /Job search/ }));
    expect(within(drawer).getAllByRole("link").map((el) => el.textContent)).toEqual([
      "My information",
      "Careers pages",
      "Review",
      "Find jobs",
      "Results",
      "Companies",
      "Jobs",
    ]);
    await user.click(within(drawer).getByRole("button", { name: /Job search/ }));
    expect(within(drawer).queryByRole("link", { name: "Careers pages" })).not.toBeInTheDocument();

    // Retired labels: "Wait" is gone (Results absorbs it), and the wizard is
    // reached as "My information" rather than "Profile".
    expect(within(drawer).queryByText("Wait")).not.toBeInTheDocument();
    expect(within(drawer).queryByText("Profile")).not.toBeInTheDocument();
  });

  it("opens the drawer with the group that owns the current route expanded", async () => {
    mockedApi.listRuns.mockResolvedValue([]);
    const user = userEvent.setup();
    renderAt("/search/review");
    await user.click(screen.getByRole("button", { name: "Open menu" }));
    const drawer = screen.getByRole("navigation", { name: "Main menu" });

    expect(within(drawer).getByRole("button", { name: /Job search/ })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(within(drawer).getByRole("button", { name: /Job matching/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(within(drawer).getByRole("link", { name: "Review" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});
