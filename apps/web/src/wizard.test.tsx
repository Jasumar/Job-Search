import "@testing-library/jest-dom/vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeAll, afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";
import { setupServer } from "msw/node";
import { ProfileWizardPage } from "@/pages/wizard/ProfileWizardPage";
import { createHandlers, mockParseState, type MockState } from "@/mocks/handlers";
import { http, HttpResponse } from "msw";
import {
  emptyDraft,
  ingestCityInput,
  isValidCity,
  splitCityTokens,
} from "@/pages/wizard/wizard-shared";

/**
 * Wizard tests run against MSW handlers (real fetch layer, no api-module
 * stubbing). Covers the decided contract invariants:
 * - GET /profile returning {profile: null} renders the wizard (first run is
 *   normal, not an error).
 * - 3.8g: the roles step is free text; the profile_parse run's summary
 *   suggestion (suggested_role_families) pre-fills editable confirm-step
 *   chips; PUT /profile still happens only at save, nothing auto-saved.
 * - 3.8h: a dropped file POSTs multipart to /profile/cv/file and the flow
 *   polls the same profile_parse run; absent suggestion falls back to an
 *   empty chip grid (never dead-ends).
 * - "Save & start" PUTs the profile BEFORE enqueueing discovery (save, then
 *   start — ordering rule 2026-09-11), then navigates to /search/results/:runId.
 */

const server = setupServer();

let state: MockState;
const putBodies: unknown[] = [];
const evidencePutBodies: unknown[] = [];
const cvPostBodies: { contentType: string; body: unknown }[] = [];
const discoverBodies: unknown[] = [];
const callOrder: string[] = [];

beforeAll(() => {
  server.listen({ onUnhandledRequest: "error" });
});

afterAll(() => server.close());

beforeEach(() => {
  state = {
    profile: null,
    queuedRunId: 101,
    suggestedFamilies: ["technology/backend", "technology/data-engineer"],
    extractedCvText: "Extracted CV text from file",
    evidenceProposal: {
      skills: ["Python", "SQL"],
      languages: [{ language: "Finnish", proficiency: "native" }],
      experience: [
        { title: "Developer", company: "Acme", start: "2019", end: "present", responsibilities: [], technologies: ["Python"] },
      ],
    },
    mappedRoleIds: ["technology/solutions-engineer", "sales/pre-sales"],
  };
  putBodies.length = 0;
  evidencePutBodies.length = 0;
  cvPostBodies.length = 0;
  discoverBodies.length = 0;
  callOrder.length = 0;
  // Base handlers minus the recording overrides below (so each write is
  // captured exactly once).
  const baseHandlers = createHandlers(state).filter(
    (h) =>
      h.info.method !== "PUT" &&
      !(h.info.method === "POST" && String(h.info.path).includes("cv")) &&
      !(h.info.method === "POST" && String(h.info.path).includes("discover")),
  );
  server.use(
    ...baseHandlers,
    // Recording overrides: capture PUT ordering, the CV POSTs, and run enqueue.
    http.put("*/api/profile/evidence", async ({ request }) => {
      callOrder.push("PUT /profile/evidence");
      evidencePutBodies.push(await request.json());
      return HttpResponse.json({
        additional_experience: "",
        education: [],
        experience: [],
        projects: [],
        skills: [],
        languages: [],
        linked_year: null,
      });
    }),
    http.put("*/api/profile", async ({ request }) => {
      callOrder.push("PUT /profile");
      const body = await request.json();
      putBodies.push(body);
      state.profile = body as never;
      return HttpResponse.json({ profile: state.profile });
    }),
    http.post("*/api/profile/cv", async ({ request }) => {
      callOrder.push("POST /profile/cv");
      cvPostBodies.push({ contentType: request.headers.get("content-type") ?? "", body: await request.json() });
      mockParseState.lastRunId = 201;
      return HttpResponse.json({ run_id: 201, status: "queued" }, { status: 202 });
    }),
    http.post("*/api/profile/cv/file", async ({ request }) => {
      callOrder.push("POST /profile/cv/file");
      // Multipart body: content-type check only (parsing FormData in the
      // intercepted request is flaky under jsdom/undici).
      cvPostBodies.push({
        contentType: request.headers.get("content-type") ?? "",
        body: "multipart",
      });
      mockParseState.lastRunId = 202;
      return HttpResponse.json({ run_id: 202, status: "queued" }, { status: 202 });
    }),
    http.post("*/api/runs/discover", async ({ request }) => {
      callOrder.push("POST /runs/discover");
      discoverBodies.push(await request.json());
      return HttpResponse.json({ run_id: 777, status: "queued" }, { status: 202 });
    }),
  );
});

afterEach(() => {
  server.resetHandlers();
});

function renderWizard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/profile"]}>
        <Routes>
          <Route path="/profile" element={<ProfileWizardPage />} />
          <Route path="/search/results/:runId" element={<div>results page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function pickCountry(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.type(screen.getByLabelText("Search countries"), name.slice(0, 3));
  await user.click(await screen.findByRole("option", { name }));
}

async function walkToCv(user: ReturnType<typeof userEvent.setup>) {
  renderWizard();
  await screen.findByText("Welcome");
  await user.type(screen.getByPlaceholderText("Your name"), "Test User");
  await user.click(screen.getByRole("button", { name: "Next" }));
  await screen.findByText("Where do you want to work?");
  await pickCountry(user, "Finland");
  await user.click(screen.getByRole("button", { name: "Next" }));
  await screen.findByText("What kind of work are you looking for?");
  await user.type(
    screen.getByPlaceholderText(/backend roles in product companies/),
    "backend and data engineering roles",
  );
  await user.click(screen.getByRole("button", { name: "Next" }));
  await screen.findByText("Your background");
}

describe("profile wizard (3.8g/3.8h frontend)", () => {
  it("renders the welcome step on first run (profile null)", async () => {
    renderWizard();
    expect(await screen.findByText("Set up your profile")).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 5")).toBeInTheDocument();
    expect(screen.getByText("Welcome")).toBeInTheDocument();
  });

  it("city chip input: space and comma commit chips, invalid tokens are rejected", async () => {
    const user = userEvent.setup();
    renderWizard();
    await screen.findByText("Welcome");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Where do you want to work?");

    const input = screen.getByLabelText("Cities");
    await user.type(input, "Helsinki Espoo");
    // Space committed "Helsinki"; "Espoo" is the pending fragment.
    expect(screen.getByText("Helsinki")).toBeInTheDocument();
    await user.type(input, ",");
    expect(screen.getByText("Espoo")).toBeInTheDocument();
    await user.type(input, "Tampere{enter}");
    expect(screen.getByText("Tampere")).toBeInTheDocument();
    expect(screen.queryByText(/Ignored/)).not.toBeInTheDocument();
  });

  it("city ingest logic: unicode names valid, symbols stripped, paste split", () => {
    expect(isValidCity("Helsinki")).toBe(true);
    expect(isValidCity("Nurmesniemi")).toBe(true);
    expect(isValidCity("Äänekoski")).toBe(true);
    expect(isValidCity("Bad<City>!")).toBe(false);
    const { complete, pending } = splitCityTokens("Helsinki, Espoo Vantaa");
    expect(complete).toEqual(["Helsinki", "Espoo"]);
    expect(pending).toBe("Vantaa");
    // The trailing fragment stays pending, so it is not in `add` yet.
    const ingest = ingestCityInput(["Helsinki"], "Espoo; Vantaa");
    expect(ingest.add).toEqual(["Espoo"]);
    expect(ingest.pending).toBe("Vantaa");
    expect(ingestCityInput(["Helsinki"], "Espoo Vantaa ").add).toEqual(["Espoo", "Vantaa"]);
    // Dedup against existing chips.
    expect(ingestCityInput(["Espoo"], "Espoo Espoo ").add).toEqual([]);
  });

  it("walks location → roles with free text (no static chip grid on the roles step)", async () => {
    const user = userEvent.setup();
    renderWizard();
    await screen.findByText("Welcome");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Where do you want to work?");
    await pickCountry(user, "Finland");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("What kind of work are you looking for?");
    // No role chips here — only the free-text description.
    expect(screen.queryByRole("button", { name: "Backend developer" })).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox"), "backend roles");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.type(screen.getByPlaceholderText("Paste your CV here…"), "My CV");
    await user.click(screen.getByRole("button", { name: "Analyze & continue" }));
    // Confirm step: proposed roles appear as editable pre-selected chips.
    await screen.findByText("Confirm your information");
    const backendChip = await screen.findByRole("button", { name: "Backend developer" });
    expect(backendChip).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Data engineer" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Lawyer / counsel" })).toHaveAttribute("aria-pressed", "false");
  });

  it("MF1: shows the proposed CV facts, editable, and Save writes them after the profile", async () => {
    const user = userEvent.setup();
    await walkToCv(user);
    await user.type(screen.getByPlaceholderText("Paste your CV here…"), "My CV text");
    await user.click(screen.getByRole("button", { name: "Analyze & continue" }));
    await screen.findByText("Confirm your information");
    // Proposed skills, language and experience are shown before anything is saved.
    expect(await screen.findByText("Python")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Finnish")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Developer")).toBeInTheDocument();
    expect(evidencePutBodies).toHaveLength(0);
    // Edit: drop a skill, add one.
    await user.click(screen.getByRole("button", { name: "Remove SQL" }));
    await user.type(screen.getByLabelText("Add skills"), "Go{enter}");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("My information");
    expect(callOrder).toEqual(["POST /profile/cv", "PUT /profile", "PUT /profile/evidence"]);
    const saved = evidencePutBodies[0] as { skills: string[]; experience: { title: string }[] };
    expect(saved.skills).toEqual(["Python", "Go"]);
    expect(saved.experience[0].title).toBe("Developer");
  });

  it("MF3: a typed role is mapped in a run and saved with its editable ids", async () => {
    const user = userEvent.setup();
    await walkToCv(user);
    await user.type(screen.getByPlaceholderText("Paste your CV here…"), "My CV text");
    await user.click(screen.getByRole("button", { name: "Analyze & continue" }));
    await screen.findByText("Confirm your information");
    await user.type(screen.getByLabelText("Add your own role"), "solutions consultant");
    await user.click(screen.getByRole("button", { name: "Add role" }));
    expect(await screen.findByText("“solutions consultant”")).toBeInTheDocument();
    expect(mockParseState.lastRoleTexts).toEqual(["solutions consultant"]);
    await user.click(screen.getByRole("button", { name: "Remove Sales › Pre-sales" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("My information");
    const putBody = putBodies[0] as { custom_roles: { text: string; ids: string[] }[] };
    expect(putBody.custom_roles).toEqual([
      { text: "solutions consultant", ids: ["technology/solutions-engineer"] },
    ]);
  });

  it("MF6: the country search matches names, not every country containing the letters", async () => {
    const user = userEvent.setup();
    renderWizard();
    await screen.findByText("Welcome");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Where do you want to work?");
    await user.type(screen.getByLabelText("Search countries"), "nor");
    expect(await screen.findByRole("option", { name: "Norway" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Finland" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Norway" }));
    expect(screen.getByRole("button", { name: "Remove Norway" })).toBeInTheDocument();
  });

  it("text CV path: POSTs /profile/cv, polls the run, applies suggested families before PUT", async () => {
    const user = userEvent.setup();
    await walkToCv(user);
    await user.type(screen.getByPlaceholderText("Paste your CV here…"), "My CV text");
    await user.click(screen.getByRole("button", { name: "Analyze & continue" }));
    await screen.findByText("Confirm your information");
    await waitFor(() =>
      expect(cvPostBodies[0]).toMatchObject({ body: { cv_text: "My CV text" } }),
    );
    await user.click(screen.getByRole("button", { name: "Save & start discovery" }));
    await waitFor(() => expect(screen.getByText("results page")).toBeInTheDocument());
    expect(callOrder).toEqual([
      "POST /profile/cv",
      "PUT /profile",
      "PUT /profile/evidence",
      "POST /runs/discover",
    ]);
    const putBody = putBodies[0] as { role_families: string[]; name: string };
    // Suggested roles are pre-selected and persisted only via the explicit save.
    expect(putBody.role_families).toEqual(["technology/backend", "technology/data-engineer"]);
    expect(putBody.name).toBe("Test User");
  });

  it("Save stores the profile without starting discovery, then shows the saved information", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/profile/evidence", () =>
        HttpResponse.json({
          additional_experience: "",
          education: [],
          experience: [],
          projects: [],
          skills: ["Python"],
          languages: [],
          linked_year: null,
        }),
      ),
    );
    state.evidenceProposal = undefined;
    await walkToCv(user);
    await user.type(screen.getByPlaceholderText("Paste your CV here…"), "My CV text");
    await user.click(screen.getByRole("button", { name: "Analyze & continue" }));
    await screen.findByText("Confirm your information");
    await user.click(screen.getByRole("button", { name: "Save" }));
    // The read-only view replaces the wizard.
    expect(await screen.findByText("My information")).toBeInTheDocument();
    expect(await screen.findByText("Python")).toBeInTheDocument();
    expect(callOrder).toEqual(["POST /profile/cv", "PUT /profile", "PUT /profile/evidence"]);
    expect(discoverBodies).toHaveLength(0);
  });

  it("shows the saved information instead of the wizard when a profile exists", async () => {
    state.profile = { ...emptyDraft().profile, name: "Saved Person", target_countries: ["Finland"] };
    server.use(
      http.get("*/api/profile/evidence", () =>
        HttpResponse.json({
          additional_experience: "",
          education: [],
          experience: [],
          projects: [],
          skills: [],
          languages: [],
          linked_year: null,
        }),
      ),
    );
    renderWizard();
    expect(await screen.findByText("Saved Person")).toBeInTheDocument();
    expect(screen.queryByText("Set up your profile")).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Edit information" }));
    expect(await screen.findByText("Edit your information")).toBeInTheDocument();
  });

  it("file CV path: drops a file, POSTs multipart to /profile/cv/file, shows extracted-text preview", async () => {
    const user = userEvent.setup();
    await walkToCv(user);
    const file = new File(["%PDF-1.4 fake"], "cv.pdf", { type: "application/pdf" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    await user.upload(input, file);
    // The file replaces the textarea (alternative inputs, not combined).
    expect(screen.queryByPlaceholderText("Paste your CV here…")).not.toBeInTheDocument();
    expect(screen.getByText("cv.pdf")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Analyze & continue" }));
    await screen.findByText("Confirm your information");
    // Extracted-text preview from the run summary.
    await screen.findByText("Extracted CV text from file");
    expect(callOrder[0]).toBe("POST /profile/cv/file");
    // Multipart: the browser sets a multipart boundary content-type; the body
    // itself is opaque to the interceptor (content-type asserted below).
    expect(cvPostBodies[0].contentType).toContain("multipart/form-data");
    await user.click(screen.getByRole("button", { name: "Save & start discovery" }));
    await waitFor(() => expect(screen.getByText("results page")).toBeInTheDocument());
    const putBody = putBodies[0] as { role_families: string[] };
    expect(putBody.role_families).toEqual(["technology/backend", "technology/data-engineer"]);
  });

  it("rejects unsupported files client-side without uploading", async () => {
    const user = userEvent.setup();
    await walkToCv(user);
    const file = new File(["x"], "cv.txt", { type: "text/plain" });
    const dropZone = screen.getByRole("button", { name: "Upload CV file" });
    const drop = (file: File) => {
      // jsdom lacks DragEvent/DataTransfer; a generic Event with a
      // dataTransfer property satisfies the component's use.
      const evt = new Event("drop", { bubbles: true, cancelable: true }) as DragEvent;
      Object.defineProperty(evt, "dataTransfer", { value: { files: [file] } });
      dropZone.dispatchEvent(evt);
    };
    drop(file);
    expect(await screen.findByText(/Unsupported file type/)).toBeInTheDocument();
    expect(cvPostBodies).toHaveLength(0);
  });

  it("falls back to an empty role picker when no roles are suggested (never dead-ends)", async () => {
    state.suggestedFamilies = [];
    const user = userEvent.setup();
    await walkToCv(user);
    await user.type(screen.getByPlaceholderText("Paste your CV here…"), "My CV text");
    await user.click(screen.getByRole("button", { name: "Analyze & continue" }));
    await screen.findByText("Confirm your information");
    await screen.findByText(/No target roles were proposed/);
    const backendChip = screen.getByRole("button", { name: "Backend developer" });
    expect(backendChip).toHaveAttribute("aria-pressed", "false");
    // User can still select manually and proceed.
    await user.click(backendChip);
    await user.click(screen.getByRole("button", { name: "Save & start discovery" }));
    await waitFor(() => expect(screen.getByText("results page")).toBeInTheDocument());
    const putBody = putBodies[0] as { role_families: string[] };
    expect(putBody.role_families).toEqual(["technology/backend"]);
  });

  it("MF1: Read my CV again opens the confirm step with a new proposal", async () => {
    state.profile = { ...emptyDraft().profile, name: "Saved Person", target_countries: ["Finland"], has_cv: true };
    server.use(
      http.post("*/api/profile/cv/reparse", () => {
        callOrder.push("POST /profile/cv/reparse");
        mockParseState.lastRunId = 301;
        return HttpResponse.json({ run_id: 301, status: "queued" }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    renderWizard();
    expect(await screen.findByText(/No skills or experience saved/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Read my CV again" }));
    await screen.findByText("Confirm your information");
    expect(await screen.findByText("Python")).toBeInTheDocument();
    expect(callOrder).toEqual(["POST /profile/cv/reparse"]);
  });

  it("continues past the CV step without a new CV when CV information is saved", async () => {
    state.evidence = {
      skills: ["Rust"], experience: [], languages: [], education: [], projects: [], linked_year: null,
    };
    const user = userEvent.setup();
    await walkToCv(user);
    await user.click(screen.getByRole("button", { name: "Continue with saved CV information" }));
    await screen.findByText("Confirm your information");
    expect(screen.getByText("Rust")).toBeInTheDocument();
    expect(cvPostBodies).toHaveLength(0);
  });

  it("blocks Next on the location step until a country is chosen", async () => {
    const user = userEvent.setup();
    renderWizard();
    await screen.findByText("Welcome");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("Where do you want to work?");
    const next = screen.getByRole("button", { name: "Next" });
    expect(next).toBeDisabled();
    await pickCountry(user, "Finland");
    expect(next).toBeEnabled();
  });
});
