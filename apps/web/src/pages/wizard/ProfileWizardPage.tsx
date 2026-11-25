import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import { profileApi, type EvidenceResponse } from "@/api/profile-api";
import type {
  CustomRole,
  EvidenceDraft,
  EvidenceExperienceItem,
  ProfileParseSummary,
  RoleGroup,
  RunSummaryResponse,
  UserProfile,
} from "@/api/profile-types";
import { validateCvFile } from "@/api/profile-types";
import {
  emptyDraft,
  emptyEvidence,
  hasProfileInformation,
  ingestCityInput,
  useSegments,
  type WizardDraft,
} from "./wizard-shared";

/**
 * 3.8f profile wizard (MVP flow step 2), extended per 3.8g/3.8h and the match
 * fix (MF1, MF3, MF6):
 * - Location: a searchable list of every country.
 * - CV step accepts pasted text OR a dropped/picked file (PDF/docx); both feed
 *   the same profile_parse run. With CV information already saved, the step
 *   can be passed without a new CV.
 * - Confirm step: the proposed target roles (role taxonomy, editable), typed
 *   roles mapped to taxonomy ids (editable), and the proposed CV facts
 *   (skills, languages, experience; editable). Nothing is stored until Save:
 *   Save writes the profile and then the CV facts (LLM proposes, form decides).
 *
 * Ordering rule (master_plan 2026-09-11): the profile PUT completes BEFORE a
 * discovery run is enqueued (save, then start).
 */

const ARRANGEMENTS = ["full-time", "part-time", "on-site", "hybrid", "remote"] as const;

type Step = "welcome" | "location" | "roles" | "cv" | "confirm";

const STEP_ORDER: Step[] = ["welcome", "location", "roles", "cv", "confirm"];

const INPUT =
  "w-full rounded border border-maroon/20 bg-cream-light px-2 py-1.5 text-sm text-maroon focus:border-maroon focus:outline-none";

function Chips({
  options,
  selected,
  onToggle,
  label,
}: {
  options: { id: string; label: string }[];
  selected: string[];
  onToggle: (id: string) => void;
  label: string;
}) {
  return (
    <fieldset className="space-y-2">
      <legend className="text-sm text-maroon-soft">{label}</legend>
      <div className="flex flex-wrap gap-2">
        {options.map((o) => {
          const on = selected.includes(o.id);
          return (
            <button
              key={o.id}
              type="button"
              aria-pressed={on}
              onClick={() => onToggle(o.id)}
              className={`rounded-full border px-3 py-1.5 text-sm ${
                on
                  ? "border-maroon bg-lime text-maroon"
                  : "border-maroon/20 text-maroon hover:bg-cream"
              }`}
            >
              {o.label}
            </button>
          );
        })}
      </div>
    </fieldset>
  );
}

function toggle(list: string[], id: string): string[] {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block space-y-1 text-sm">
      <span className="text-maroon-soft">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={INPUT}
      />
    </label>
  );
}

function TextArea({
  label,
  value,
  onChange,
  rows = 6,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
}) {
  return (
    <label className="block space-y-1 text-sm">
      <span className="text-maroon-soft">{label}</span>
      <textarea
        value={value}
        rows={rows}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={INPUT}
      />
    </label>
  );
}

function RemovableChip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="flex items-center gap-1 rounded-full bg-lime px-2 py-0.5 text-xs text-maroon">
      {label}
      <button
        type="button"
        aria-label={`Remove ${label}`}
        onClick={onRemove}
        className="text-maroon-soft hover:text-maroon"
      >
        ×
      </button>
    </span>
  );
}

/**
 * 3.8g city chip input: space and comma commit a chip (the input accepts only
 * the pending fragment), pasted text is stripped to valid characters, and
 * invalid chips are surfaced inline instead of silently accepted.
 */
function CityChipInput({
  cities,
  onChange,
}: {
  cities: string[];
  onChange: (cities: string[]) => void;
}) {
  const [pending, setPending] = useState("");
  const [invalid, setInvalid] = useState<string[]>([]);

  const commit = (raw: string, keepTrailing: boolean) => {
    const ingest = ingestCityInput(cities, keepTrailing ? raw : `${raw} `);
    if (ingest.add.length > 0) onChange([...cities, ...ingest.add]);
    setInvalid(ingest.invalid);
    setPending(keepTrailing ? ingest.pending : "");
  };

  return (
    <div className="space-y-1 text-sm">
      <span className="text-maroon-soft">Cities</span>
      <div className="flex flex-wrap items-center gap-2 rounded border border-maroon/20 bg-cream-light px-2 py-1.5 focus-within:border-maroon">
        {cities.map((c) => (
          <RemovableChip key={c} label={c} onRemove={() => onChange(cities.filter((x) => x !== c))} />
        ))}
        <input
          value={pending}
          onChange={(e) => {
            const v = e.target.value;
            if (/[\s,]/.test(v)) commit(v, true);
            else setPending(v);
          }}
          onPaste={(e) => {
            e.preventDefault();
            commit(e.clipboardData.getData("text"), true);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commit(pending, false);
            }
            if (e.key === "Backspace" && pending === "" && cities.length > 0) {
              onChange(cities.slice(0, -1));
            }
          }}
          placeholder={cities.length === 0 ? "Helsinki, Espoo" : ""}
          className="min-w-24 flex-1 bg-transparent text-sm text-maroon focus:outline-none"
          aria-label="Cities"
        />
      </div>
      {invalid.length > 0 && (
        <p className="text-xs text-amber-700">
          Ignored (unsupported characters): {invalid.join(", ")}
        </p>
      )}
      <p className="text-xs text-maroon-soft">A job in one of these cities gets a strength line; the score does not change.</p>
    </div>
  );
}

/** MF6: search every country by name; selected countries are chips. */
function CountryPicker({
  countries,
  selected,
  onChange,
}: {
  countries: string[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLocaleLowerCase();
  const matches = q
    ? countries.filter((c) => c.toLocaleLowerCase().includes(q) && !selected.includes(c)).slice(0, 8)
    : [];
  return (
    <div className="space-y-2 text-sm">
      <span className="text-maroon-soft">Countries</span>
      {selected.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {selected.map((c) => (
            <RemovableChip key={c} label={c} onRemove={() => onChange(selected.filter((x) => x !== c))} />
          ))}
        </div>
      )}
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && matches.length > 0) {
            e.preventDefault();
            onChange([...selected, matches[0]]);
            setQuery("");
          }
        }}
        placeholder="Search countries…"
        aria-label="Search countries"
        className={INPUT}
      />
      {matches.length > 0 && (
        <ul className="overflow-hidden rounded border border-maroon/20 bg-cream-light" role="listbox">
          {matches.map((c) => (
            <li key={c}>
              <button
                type="button"
                role="option"
                aria-selected={false}
                onClick={() => {
                  onChange([...selected, c]);
                  setQuery("");
                }}
                className="block w-full px-2 py-1 text-left hover:bg-cream"
              >
                {c}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Labels for taxonomy ids: "Legal & compliance › Counsel", or the group label. */
function roleLabelMap(groups: RoleGroup[] | undefined): Map<string, string> {
  const labels = new Map<string, string>();
  for (const g of groups ?? []) {
    labels.set(g.id, `${g.label} (all)`);
    for (const r of g.roles) labels.set(r.id, `${g.label} › ${r.label}`);
  }
  return labels;
}

/** MF3: target roles from the taxonomy, one collapsible section per group. */
function RolePicker({
  groups,
  selected,
  onChange,
}: {
  groups: RoleGroup[];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <fieldset className="space-y-2">
      <legend className="text-sm text-maroon-soft">Target roles (editable)</legend>
      <div className="space-y-1">
        {groups.map((g) => {
          const inGroup = selected.filter((id) => id === g.id || id.startsWith(`${g.id}/`));
          return (
            <details key={g.id} open={inGroup.length > 0} className="rounded border border-maroon/10 px-2 py-1">
              <summary className="cursor-pointer text-sm">
                {g.label}
                {inGroup.length > 0 && <span className="ml-2 text-xs text-maroon-soft">{inGroup.length} selected</span>}
              </summary>
              <div className="mt-2 flex flex-wrap gap-2 pb-1">
                <ChipButton on={selected.includes(g.id)} label={`All of ${g.label}`} onClick={() => onChange(toggle(selected, g.id))} />
                {g.roles.map((r) => (
                  <ChipButton key={r.id} on={selected.includes(r.id)} label={r.label} onClick={() => onChange(toggle(selected, r.id))} />
                ))}
              </div>
            </details>
          );
        })}
      </div>
    </fieldset>
  );
}

function ChipButton({ on, label, onClick }: { on: boolean; label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      className={`rounded-full border px-3 py-1 text-xs ${
        on ? "border-maroon bg-lime text-maroon" : "border-maroon/20 text-maroon hover:bg-cream"
      }`}
    >
      {label}
    </button>
  );
}

/** MF3: roles in the owner's words, each mapped once to taxonomy ids. */
function CustomRoles({
  roles,
  labels,
  onChange,
}: {
  roles: CustomRole[];
  labels: Map<string, string>;
  onChange: (next: CustomRole[]) => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const map = useMutation({
    mutationFn: async (value: string) => {
      const queued = await profileApi.mapRoles([value]);
      const summary = await pollRun(queued.run_id, "Role mapping");
      return summary?.custom_roles?.[0] ?? { text: value, ids: [] };
    },
    onSuccess: (role) => {
      setError(null);
      setText("");
      onChange([...roles.filter((r) => r.text.toLowerCase() !== role.text.toLowerCase()), role]);
    },
    onError: (err) => setError(String(err)),
  });
  const add = () => {
    const value = text.trim();
    if (value && !map.isPending) map.mutate(value);
  };
  return (
    <div className="space-y-2 text-sm">
      <span className="text-maroon-soft">Your own roles</span>
      {roles.map((role) => (
        <div key={role.text} className="rounded border border-maroon/10 px-2 py-1.5">
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">“{role.text}”</span>
            <button
              type="button"
              onClick={() => onChange(roles.filter((r) => r !== role))}
              className="text-xs text-maroon underline hover:text-maroon-soft"
            >
              Remove
            </button>
          </div>
          <div className="mt-1 flex flex-wrap gap-1">
            {role.ids.length === 0 && (
              <span className="text-xs text-maroon-soft">No matching roles; the title words still match jobs.</span>
            )}
            {role.ids.map((id) => (
              <RemovableChip
                key={id}
                label={labels.get(id) ?? id}
                onRemove={() =>
                  onChange(roles.map((r) => (r === role ? { ...r, ids: r.ids.filter((x) => x !== id) } : r)))
                }
              />
            ))}
          </div>
        </div>
      ))}
      <div className="flex gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          placeholder="e.g. solutions consultant"
          aria-label="Add your own role"
          className={INPUT}
        />
        <button
          type="button"
          aria-label="Add role"
          disabled={!text.trim() || map.isPending}
          onClick={add}
          className="rounded border border-maroon px-3 py-1 text-sm hover:bg-cream disabled:opacity-50"
        >
          {map.isPending ? "Mapping…" : "Add"}
        </button>
      </div>
      <p className="text-xs text-maroon-soft">
        A typed role is matched to the roles above once, now, and also matches job titles by its words.
      </p>
      {error && <p className="text-xs text-red-800">{error}</p>}
    </div>
  );
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** MF1: the CV facts the score compares, editable before Save. */
function EvidenceEditor({
  evidence,
  onChange,
}: {
  evidence: EvidenceDraft;
  onChange: (next: EvidenceDraft) => void;
}) {
  const [skill, setSkill] = useState("");
  const addSkill = () => {
    const values = splitList(skill).filter(
      (s) => !evidence.skills.some((x) => x.toLowerCase() === s.toLowerCase()),
    );
    if (values.length > 0) onChange({ ...evidence, skills: [...evidence.skills, ...values] });
    setSkill("");
  };
  const setExperience = (index: number, patch: Partial<EvidenceExperienceItem>) =>
    onChange({
      ...evidence,
      experience: evidence.experience.map((e, i) => (i === index ? { ...e, ...patch } : e)),
    });
  return (
    <div className="space-y-4 rounded border border-maroon/10 bg-cream-light p-3">
      <div>
        <p className="text-sm font-medium">Your CV information</p>
        <p className="text-xs text-maroon-soft">
          Jobs are scored against these skills, languages and work history. Check them before you save.
        </p>
      </div>

      <div className="space-y-2 text-sm">
        <span className="text-maroon-soft">Skills ({evidence.skills.length})</span>
        <div className="flex flex-wrap gap-1">
          {evidence.skills.map((s) => (
            <RemovableChip
              key={s}
              label={s}
              onRemove={() => onChange({ ...evidence, skills: evidence.skills.filter((x) => x !== s) })}
            />
          ))}
        </div>
        <div className="flex gap-2">
          <input
            value={skill}
            onChange={(e) => setSkill(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addSkill();
              }
            }}
            placeholder="Add skills, comma separated"
            aria-label="Add skills"
            className={INPUT}
          />
          <button type="button" aria-label="Add skill" onClick={addSkill} className="rounded border border-maroon px-3 py-1 text-sm hover:bg-cream">
            Add
          </button>
        </div>
      </div>

      <div className="space-y-2 text-sm">
        <span className="text-maroon-soft">Languages</span>
        {evidence.languages.map((l, i) => (
          <div key={i} className="flex gap-2">
            <input
              value={l.language}
              onChange={(e) =>
                onChange({
                  ...evidence,
                  languages: evidence.languages.map((x, j) => (j === i ? { ...x, language: e.target.value } : x)),
                })
              }
              aria-label={`Language ${i + 1}`}
              placeholder="Language"
              className={INPUT}
            />
            <input
              value={l.proficiency}
              onChange={(e) =>
                onChange({
                  ...evidence,
                  languages: evidence.languages.map((x, j) => (j === i ? { ...x, proficiency: e.target.value } : x)),
                })
              }
              aria-label={`Proficiency ${i + 1}`}
              placeholder="Level (native, fluent, B2…)"
              className={INPUT}
            />
            <button
              type="button"
              aria-label={`Remove language ${i + 1}`}
              onClick={() => onChange({ ...evidence, languages: evidence.languages.filter((_, j) => j !== i) })}
              className="px-2 text-maroon-soft hover:text-maroon"
            >
              ×
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={() => onChange({ ...evidence, languages: [...evidence.languages, { language: "", proficiency: "" }] })}
          className="text-xs text-maroon underline hover:text-maroon-soft"
        >
          Add a language
        </button>
      </div>

      <div className="space-y-2 text-sm">
        <span className="text-maroon-soft">Experience</span>
        {evidence.experience.map((e, i) => (
          <div key={i} className="space-y-2 rounded border border-maroon/10 p-2">
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Title" value={e.title} onChange={(v) => setExperience(i, { title: v })} />
              <Field label="Company" value={e.company} onChange={(v) => setExperience(i, { company: v })} />
              <Field label="Start" value={e.start} onChange={(v) => setExperience(i, { start: v })} placeholder="2019" />
              <Field label="End" value={e.end} onChange={(v) => setExperience(i, { end: v })} placeholder="present" />
            </div>
            <Field
              label="Technologies (comma separated)"
              value={e.technologies.join(", ")}
              onChange={(v) => setExperience(i, { technologies: splitList(v) })}
            />
            <button
              type="button"
              onClick={() => onChange({ ...evidence, experience: evidence.experience.filter((_, j) => j !== i) })}
              className="text-xs text-maroon underline hover:text-maroon-soft"
            >
              Remove this job
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={() =>
            onChange({
              ...evidence,
              experience: [
                ...evidence.experience,
                { title: "", company: "", start: "", end: "", responsibilities: [], technologies: [] },
              ],
            })
          }
          className="text-xs text-maroon underline hover:text-maroon-soft"
        >
          Add a job
        </button>
      </div>
    </div>
  );
}

/** 3.8h drag-and-drop + file-picker zone for the CV file path. */
function CvFileDrop({
  fileName,
  error,
  onFile,
  disabled,
}: {
  fileName: string | null;
  error: string | null;
  onFile: (file: File) => void;
  disabled?: boolean;
}) {
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div className="space-y-1">
      <div
        role="button"
        tabIndex={0}
        aria-label="Upload CV file"
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => e.key === "Enter" && !disabled && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          if (!disabled) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          if (disabled) return;
          const file = e.dataTransfer.files[0];
          if (file) onFile(file);
        }}
        className={`rounded border border-dashed p-4 text-center text-sm ${
          dragOver
            ? "border-maroon bg-lime/40"
            : "border-maroon/20 bg-cream-light hover:bg-cream"
        } ${disabled ? "opacity-50" : "cursor-pointer"}`}
      >
        {fileName ? (
          <span className="text-maroon">{fileName}</span>
        ) : (
          <span className="text-maroon-soft">Drop your CV file here (PDF or .docx), or click to choose</span>
        )}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,.docx"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) onFile(file);
          e.target.value = "";
        }}
      />
      {error && <p className="text-xs text-red-800">{error}</p>}
    </div>
  );
}

/** Poll a run until terminal; resolve with its summary. */
async function pollRun(runId: number, what = "CV parsing"): Promise<ProfileParseSummary | null> {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 500));
    const run: RunSummaryResponse = await profileApi.getRunSummary(runId);
    if (run.status === "completed") return run.summary;
    if (run.status === "failed" || run.status === "cancelled") {
      throw new Error(run.error ?? `${what} failed`);
    }
  }
  throw new Error(`${what} timed out`);
}

/** The stored evidence document in the editable draft shape. */
function evidenceFromResponse(ev: EvidenceResponse | Partial<EvidenceDraft> | undefined | null): EvidenceDraft {
  if (!ev) return emptyEvidence();
  const experience = (ev.experience ?? []).map((raw) => {
    const e = raw as unknown as Partial<EvidenceExperienceItem>;
    return {
      title: e.title ?? "",
      company: e.company ?? "",
      start: e.start ?? "",
      end: e.end ?? "",
      location: e.location ?? null,
      responsibilities: e.responsibilities ?? [],
      technologies: e.technologies ?? [],
    };
  });
  const languages = (ev.languages ?? []).map((l) =>
    typeof l === "string" ? { language: l, proficiency: "" } : { language: l.language ?? "", proficiency: l.proficiency ?? "" },
  );
  return {
    skills: ev.skills ?? [],
    experience,
    languages,
    education: (ev.education ?? []) as Record<string, unknown>[],
    projects: (ev.projects ?? []) as Record<string, unknown>[],
    linked_year: ev.linked_year ?? null,
  };
}

/** Drop rows the server would refuse (an experience entry needs a title). */
function evidenceForSave(ev: EvidenceDraft): EvidenceDraft {
  return {
    ...ev,
    experience: ev.experience.filter((e) => e.title.trim() !== ""),
    languages: ev.languages.filter((l) => l.language.trim() !== ""),
  };
}

function evidenceHasContent(ev: EvidenceDraft): boolean {
  return ev.skills.length > 0 || ev.experience.length > 0;
}

function ProfileWizard({
  onSaved,
  onCancel,
  reparseRunId,
}: {
  onSaved: () => void;
  onCancel: (() => void) | null;
  /** MF1 "Read my CV again": a profile_parse run on the stored CV to open with. */
  reparseRunId?: number | null;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // 3.8i welcome-page handoff: the name typed on / seeds the welcome step.
  const welcomeName = (useLocation().state as { name?: string } | null)?.name ?? "";

  // Existing profile = returning user; empty profile (or error) = first run.
  const existing = useQuery({
    queryKey: ["profile"],
    queryFn: () => profileApi.getProfile(),
    retry: false,
  });
  const savedEvidence = useQuery({
    queryKey: ["evidence"],
    queryFn: () => profileApi.getEvidence(),
    retry: false,
  });

  const segments = useSegments();
  const labels = useMemo(() => roleLabelMap(segments.data?.role_taxonomy), [segments.data]);

  const [draft, setDraft] = useState<WizardDraft | null>(null);
  const [step, setStep] = useState<Step>(reparseRunId ? "confirm" : "welcome");
  const [lastError, setLastError] = useState<string | null>(null);
  // 3.8h file path state (alternative to the textarea, never combined).
  const [cvFile, setCvFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  // 3.8h extracted-text preview from the parse-run summary.
  const [extractedText, setExtractedText] = useState<string | null>(null);
  const [parseFailed, setParseFailed] = useState(false);

  // Seed the draft from the loaded profile and evidence exactly once; a name
  // handed over from the welcome page pre-fills the welcome step (3.8i).
  const evidenceSettled = savedEvidence.isSuccess || savedEvidence.isError;
  if (draft === null && existing.data !== undefined && evidenceSettled) {
    const p = existing.data.profile;
    const evidence = evidenceFromResponse(savedEvidence.data);
    setDraft(
      p
        ? {
            profile: { ...p, name: p.name || welcomeName, custom_roles: p.custom_roles ?? [] },
            cvText: "",
            description: "",
            additionalExperience: "",
            evidence,
          }
        : { ...emptyDraft(), profile: { ...emptyDraft().profile, name: welcomeName }, evidence },
    );
  }

  const preview = useQuery({
    queryKey: ["discovery-preview"],
    queryFn: () => profileApi.discoveryPreview(),
    enabled: step === "confirm",
  });

  const knownRoleIds = useMemo(() => {
    const ids = new Set<string>();
    for (const g of segments.data?.role_taxonomy ?? []) {
      ids.add(g.id);
      for (const r of g.roles) ids.add(r.id);
    }
    return ids;
  }, [segments.data]);

  // Apply a parse proposal to the draft: proposed roles and CV facts. Nothing
  // is saved here; Save writes them.
  const applyProposal = (summary: ProfileParseSummary | null) => {
    const suggested = summary?.suggested_role_families ?? [];
    const proposed = suggested.filter((id) => knownRoleIds.has(id));
    if (summary?.extracted_cv_text) setExtractedText(summary.extracted_cv_text);
    setDraft((d) =>
      d
        ? {
            ...d,
            profile: { ...d.profile, role_families: proposed.length > 0 ? proposed : d.profile.role_families },
            evidence: summary?.evidence_proposal ? evidenceFromResponse(summary.evidence_proposal) : d.evidence,
          }
        : d,
    );
    setParseFailed(proposed.length === 0);
    setStep("confirm");
  };

  // 3.8g/3.8h: submit CV (text or file), poll the parse run, apply the
  // proposal, then move to confirm. Nothing is auto-saved.
  const analyze = useMutation({
    mutationFn: async (): Promise<ProfileParseSummary | null> => {
      const queued = cvFile
        ? await profileApi.uploadCvFile(
            cvFile,
            draft?.description ?? "",
            draft?.additionalExperience ?? "",
          )
        : await profileApi.parseCv({
            cv_text: draft?.cvText ?? "",
            description: draft?.description ?? "",
            additional_experience: draft?.additionalExperience ?? "",
          });
      return pollRun(queued.run_id);
    },
    onSuccess: applyProposal,
    onError: (err) => {
      setLastError(String(err));
      // Never dead-end: fall back to the editable selections.
      setParseFailed(true);
      setStep("confirm");
    },
  });

  // MF1 "Read my CV again": the run was queued by My information; wait for it
  // once the draft and the taxonomy are loaded.
  const reparse = useMutation({
    mutationFn: (runId: number) => pollRun(runId),
    onSuccess: applyProposal,
    onError: (err) => {
      setLastError(String(err));
      setParseFailed(true);
    },
  });
  const reparseStarted = useRef(false);
  useEffect(() => {
    if (reparseRunId && draft !== null && segments.data && !reparseStarted.current) {
      reparseStarted.current = true;
      reparse.mutate(reparseRunId);
    }
  }, [reparseRunId, draft, segments.data, reparse]);

  const saveAll = async (d: WizardDraft) => {
    await profileApi.updateProfile(d.profile);
    await profileApi.updateEvidence(evidenceForSave(d.evidence));
  };

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["profile"] });
    queryClient.invalidateQueries({ queryKey: ["evidence"] });
    queryClient.invalidateQueries({ queryKey: ["matches", "status"] });
    queryClient.invalidateQueries({ queryKey: ["discovery", "preview"] });
  };

  const startRun = useMutation({
    // Enqueue only after the profile and evidence PUTs succeeded (save, then start).
    mutationFn: async (d: WizardDraft) => {
      await saveAll(d);
      return api.startDiscoverRun({ dry_run: false });
    },
    onSuccess: ({ run_id }) => {
      invalidate();
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/search/results/${run_id}`);
    },
    onError: (err) => setLastError(String(err)),
  });

  // Save only: store the information and go back to the read-only view.
  // Gathering and scoring jobs are separate, explicit steps.
  const saveOnly = useMutation({
    mutationFn: (d: WizardDraft) => saveAll(d),
    onSuccess: () => {
      invalidate();
      onSaved();
    },
    onError: (err) => setLastError(String(err)),
  });

  if (existing.isLoading || draft === null) {
    return <p className="text-sm text-maroon-soft">Loading profile…</p>;
  }

  const d = draft;
  const setProfile = (patch: Partial<UserProfile>) =>
    setDraft({ ...d, profile: { ...d.profile, ...patch } });
  const hasCvInput = d.cvText.trim().length > 0 || cvFile !== null;
  const canSkipCv = !hasCvInput && evidenceHasContent(d.evidence);
  const canNext =
    step === "welcome" ||
    (step === "location" && d.profile.target_countries.length > 0) ||
    step === "roles" ||
    (step === "cv" && (hasCvInput || canSkipCv)) ||
    step === "confirm";
  const idx = STEP_ORDER.indexOf(step);

  const goNext = () => {
    if (step === "cv" && hasCvInput && !analyze.isPending) {
      setLastError(null);
      analyze.mutate();
      return;
    }
    setStep(STEP_ORDER[idx + 1]);
  };

  const acceptFile = (file: File) => {
    const problem = validateCvFile(file);
    if (problem) {
      setFileError(problem);
      return;
    }
    setFileError(null);
    setCvFile(file);
    // Alternative inputs, not combined: a file replaces pasted text.
    setDraft({ ...d, cvText: "" });
  };

  const busy = analyze.isPending || reparse.isPending;

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <header className="space-y-1">
        <div className="flex items-baseline justify-between gap-3">
          <h1 className="text-lg font-semibold">
            {onCancel ? "Edit your information" : "Set up your profile"}
          </h1>
          {onCancel && (
            <button
              type="button"
              onClick={onCancel}
              className="text-sm text-maroon underline hover:text-maroon-soft"
            >
              Cancel
            </button>
          )}
        </div>
        <p className="text-sm text-maroon-soft">
          Step {idx + 1} of {STEP_ORDER.length}
        </p>
      </header>

      {step === "welcome" && (
        <section className="space-y-3 rounded-lg border border-maroon/10 bg-cream-light p-4">
          <h2 className="text-base font-medium">Welcome</h2>
          <p className="text-sm text-maroon-soft">
            A few questions so discovery knows what to look for: where you want
            to work, which kinds of roles interest you, and your background.
            Your answers drive the search queries — you&apos;ll confirm the
            computed search plan before anything runs.
          </p>
          <Field
            label="Name"
            value={d.profile.name}
            onChange={(v) => setProfile({ name: v })}
            placeholder="Your name"
          />
        </section>
      )}

      {step === "location" && (
        <section className="space-y-4 rounded-lg border border-maroon/10 bg-cream-light p-4">
          <h2 className="text-base font-medium">Where do you want to work?</h2>
          {segments.data && (
            <CountryPicker
              countries={segments.data.countries}
              selected={d.profile.target_countries}
              onChange={(next) => setProfile({ target_countries: next })}
            />
          )}
          <CityChipInput
            cities={d.profile.target_cities}
            onChange={(cities) => setProfile({ target_cities: cities })}
          />
          <Chips
            label="Work arrangements"
            options={ARRANGEMENTS.map((a) => ({ id: a, label: a }))}
            selected={d.profile.work_arrangements}
            onToggle={(id) => setProfile({ work_arrangements: toggle(d.profile.work_arrangements, id) })}
          />
        </section>
      )}

      {step === "roles" && (
        <section className="space-y-3 rounded-lg border border-maroon/10 bg-cream-light p-4">
          <h2 className="text-base font-medium">What kind of work are you looking for?</h2>
          <p className="text-sm text-maroon-soft">
            Describe in your own words what kind of work you&apos;re looking
            for. After you add your CV, we propose target roles from it and
            your description — you confirm and edit them before anything is saved.
          </p>
          <TextArea
            label="What are you looking for?"
            value={d.description}
            onChange={(v) => setDraft({ ...d, description: v })}
            rows={4}
            placeholder="e.g. backend roles in product companies, hybrid around Helsinki…"
          />
        </section>
      )}

      {step === "cv" && (
        <section className="space-y-3 rounded-lg border border-maroon/10 bg-cream-light p-4">
          <h2 className="text-base font-medium">Your background</h2>
          {!cvFile && (
            <TextArea
              label="CV (paste as text)"
              value={d.cvText}
              onChange={(v) => setDraft({ ...d, cvText: v })}
              rows={10}
              placeholder="Paste your CV here…"
            />
          )}
          {!d.cvText.trim() && (
            <>
              <p className="text-center text-xs text-maroon-soft">— or —</p>
              <CvFileDrop
                fileName={cvFile?.name ?? null}
                error={fileError}
                onFile={acceptFile}
                disabled={analyze.isPending}
              />
            </>
          )}
          {canSkipCv && (
            <p className="text-xs text-maroon-soft">
              Your saved CV information is kept if you continue without a new CV.
            </p>
          )}
          <TextArea
            label="Anything else worth mentioning? (optional)"
            value={d.additionalExperience}
            onChange={(v) => setDraft({ ...d, additionalExperience: v })}
            rows={4}
            placeholder="e.g. side projects, freelance work, a gap year, languages…"
          />
          <p className="text-xs text-maroon-soft">
            The CV is parsed into proposed information; you confirm the
            proposal before anything is stored. If you uploaded a file, the
            extracted text is shown for you to verify — the original file is
            never kept.
          </p>
          <p className="text-xs text-maroon-soft">
            The extra text above is saved exactly as you wrote it and is used
            verbatim when your CV is tailored — it is never read or rewritten
            by a model.
          </p>
          {lastError && <p className="text-sm text-red-800">{lastError}</p>}
        </section>
      )}

      {step === "confirm" && (
        <section className="space-y-4 rounded-lg border border-maroon/10 bg-cream-light p-4">
          <h2 className="text-base font-medium">Confirm your information</h2>
          {extractedText && (
            <div className="space-y-1">
              <p className="text-sm text-maroon-soft">Extracted CV text</p>
              <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap rounded border border-maroon/20 bg-cream-light p-2 text-xs text-maroon">
                {extractedText}
              </pre>
              <p className="text-xs text-maroon-soft">
                Check the extraction quality — this text (not the original
                file) is what gets stored and parsed.
              </p>
            </div>
          )}
          {busy && <p className="text-sm text-maroon-soft">Reading your CV…</p>}
          {parseFailed && !busy && (
            <p className="text-sm text-amber-700">
              No target roles were proposed — choose them below.
            </p>
          )}
          {segments.data?.role_taxonomy && (
            <RolePicker
              groups={segments.data.role_taxonomy}
              selected={d.profile.role_families}
              onChange={(next) => setProfile({ role_families: next })}
            />
          )}
          <CustomRoles
            roles={d.profile.custom_roles ?? []}
            labels={labels}
            onChange={(next) => setProfile({ custom_roles: next })}
          />
          <EvidenceEditor evidence={d.evidence} onChange={(evidence) => setDraft({ ...d, evidence })} />
          {!evidenceHasContent(d.evidence) && (
            <p className="text-sm text-amber-700">
              No skills or experience yet. Jobs cannot be scored until you add some.
            </p>
          )}
          {/* Phase 4.1a: contact details live on user_profile, are used to fill
              the tailored CV header, and are excluded from scoring prompts. */}
          <div className="space-y-3 rounded border border-maroon/10 bg-cream-light p-3">
            <p className="text-sm font-medium">Contact details (optional)</p>
            <p className="text-xs text-maroon-soft">
              These go on your tailored CV. They are never sent to job scoring.
            </p>
            <Field
              label="Location"
              value={d.profile.location}
              onChange={(v) => setProfile({ location: v })}
              placeholder="City, Country"
            />
            <Field
              label="Citizenship"
              value={d.profile.citizenship}
              onChange={(v) => setProfile({ citizenship: v })}
              placeholder="Country"
            />
            <Field
              label="LinkedIn"
              value={d.profile.linkedin}
              onChange={(v) => setProfile({ linkedin: v })}
              placeholder="https://linkedin.com/in/..."
            />
            <Field
              label="GitLab"
              value={d.profile.gitlab}
              onChange={(v) => setProfile({ gitlab: v })}
              placeholder="https://gitlab.com/..."
            />
            <Field
              label="Email"
              value={d.profile.email}
              onChange={(v) => setProfile({ email: v })}
              placeholder="you@example.com"
            />
            <Field
              label="Phone"
              value={d.profile.phone}
              onChange={(v) => setProfile({ phone: v })}
              placeholder="+358 ..."
            />
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={d.profile.visa_required}
                onChange={(e) => setProfile({ visa_required: e.target.checked })}
              />
              I need visa sponsorship
            </label>
          </div>
          {preview.isLoading && <p className="text-sm text-maroon-soft">Computing preview…</p>}
          {preview.data && (
            <dl className="space-y-2 text-sm">
              <div>
                <dt className="text-maroon-soft">Search segments (from your last saved information)</dt>
                <dd>{preview.data.selected_segments.join(", ")}</dd>
              </div>
              <div>
                <dt className="text-maroon-soft">Countries</dt>
                <dd>{d.profile.target_countries.join(", ") || preview.data.selected_countries.join(", ")}</dd>
              </div>
              <div>
                <dt className="text-maroon-soft">Queries</dt>
                <dd>
                  {preview.data.baseline_query_count} of ceiling {preview.data.ceiling}
                </dd>
              </div>
            </dl>
          )}
          <p className="text-xs text-maroon-soft">
            "Save" only stores your information. "Save & start discovery"
            also starts a company search with these selections. You can start
            discovery later from Start a match.
          </p>
          {lastError && <p className="text-sm text-red-800">{lastError}</p>}
        </section>
      )}

      <footer className="flex items-center justify-between">
        <button
          type="button"
          disabled={step === "welcome"}
          onClick={() => setStep(STEP_ORDER[Math.max(idx - 1, 0)])}
          className="rounded border border-maroon/20 px-4 py-1.5 text-sm text-maroon hover:bg-cream disabled:opacity-40"
        >
          Back
        </button>
        {step === "confirm" ? (
          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={startRun.isPending || saveOnly.isPending || busy}
              onClick={() => startRun.mutate(d)}
              className="rounded border border-maroon px-4 py-1.5 text-sm font-medium text-maroon hover:bg-cream disabled:opacity-50"
            >
              {startRun.isPending ? "Saving & starting…" : "Save & start discovery"}
            </button>
            <button
              type="button"
              disabled={startRun.isPending || saveOnly.isPending || busy}
              onClick={() => saveOnly.mutate(d)}
              className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
            >
              {saveOnly.isPending ? "Saving…" : "Save"}
            </button>
          </div>
        ) : (
          <button
            type="button"
            disabled={!canNext || analyze.isPending}
            onClick={goNext}
            className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
          >
            {step === "cv"
              ? analyze.isPending
                ? "Analyzing…"
                : hasCvInput
                  ? "Analyze & continue"
                  : "Continue with saved CV information"
              : "Next"}
          </button>
        )}
      </footer>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-3 py-1.5 text-sm">
      <dt className="text-maroon-soft">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

function listOrDash(items: string[]): string {
  return items.length > 0 ? items.join(", ") : "—";
}

/** Read-only view of the saved information (profile + CV evidence). */
function ProfileSummary({
  profile,
  onEdit,
  onReparse,
}: {
  profile: UserProfile;
  onEdit: () => void;
  onReparse: (runId: number) => void;
}) {
  const segments = useSegments();
  const evidence = useQuery({
    queryKey: ["evidence"],
    queryFn: () => profileApi.getEvidence(),
    retry: false,
  });
  const labels = roleLabelMap(segments.data?.role_taxonomy);
  const ev = evidence.data ? evidenceFromResponse(evidence.data) : null;
  const [error, setError] = useState<string | null>(null);
  const reparse = useMutation({
    mutationFn: () => profileApi.reparseCv(),
    onSuccess: ({ run_id }) => onReparse(run_id),
    onError: (err) => setError(String(err)),
  });

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <header className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-lg font-semibold">My information</h1>
        <div className="flex gap-2">
          {profile.has_cv && (
            <button
              type="button"
              disabled={reparse.isPending}
              onClick={() => reparse.mutate()}
              className="rounded border border-maroon px-4 py-1.5 text-sm font-medium text-maroon hover:bg-cream disabled:opacity-50"
            >
              {reparse.isPending ? "Starting…" : "Read my CV again"}
            </button>
          )}
          <button
            type="button"
            onClick={onEdit}
            className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft"
          >
            Edit information
          </button>
        </div>
      </header>
      {error && <p className="text-sm text-red-800">{error}</p>}

      <section className="rounded-lg border border-maroon/10 bg-cream-light p-4">
        <h2 className="text-base font-medium">Search targets</h2>
        <dl className="mt-1 divide-y divide-maroon/10">
          <Row label="Name">{profile.name || "—"}</Row>
          <Row label="Countries">{listOrDash(profile.target_countries)}</Row>
          <Row label="Cities">{listOrDash(profile.target_cities)}</Row>
          <Row label="Work arrangements">{listOrDash(profile.work_arrangements)}</Row>
          <Row label="Target roles">
            {listOrDash(profile.role_families.map((id) => labels.get(id) ?? id))}
          </Row>
          <Row label="Your own roles">
            {(profile.custom_roles ?? []).length === 0
              ? "—"
              : (profile.custom_roles ?? []).map((r) => (
                  <span key={r.text} className="block">
                    “{r.text}” → {r.ids.length > 0 ? r.ids.map((id) => labels.get(id) ?? id).join(", ") : "title words only"}
                  </span>
                ))}
          </Row>
        </dl>
      </section>

      <section className="rounded-lg border border-maroon/10 bg-cream-light p-4">
        <h2 className="text-base font-medium">CV</h2>
        {evidence.isLoading && <p className="mt-1 text-sm text-maroon-soft">Loading…</p>}
        {ev && evidence.data && (
          <dl className="mt-1 divide-y divide-maroon/10">
            <Row label="Skills">{listOrDash(ev.skills)}</Row>
            <Row label="Languages">
              {listOrDash(ev.languages.map((l) => (l.proficiency ? `${l.language} (${l.proficiency})` : l.language)))}
            </Row>
            <Row label="Experience">
              {ev.experience.length === 0
                ? "—"
                : ev.experience.map((e, i) => (
                    <span key={i} className="block">
                      {[e.title, e.company].filter(Boolean).join(", ")}
                      {(e.start || e.end) && ` · ${e.start || "?"}–${e.end || "?"}`}
                    </span>
                  ))}
            </Row>
            <Row label="Education">{ev.education.length} entries</Row>
            <Row label="Projects">{ev.projects.length} entries</Row>
            <Row label="Additional experience">
              <span className="whitespace-pre-line">{evidence.data.additional_experience || "—"}</span>
            </Row>
          </dl>
        )}
        {ev && !evidenceHasContent(ev) && (
          <p className="mt-2 text-sm text-amber-700">
            No skills or experience saved, so jobs cannot be scored yet.
            {profile.has_cv ? " Press “Read my CV again” and save." : " Edit your information and add your CV."}
          </p>
        )}
        {evidence.error && (
          <p className="mt-1 text-sm text-maroon-soft">No CV information saved yet.</p>
        )}
      </section>

      <section className="rounded-lg border border-maroon/10 bg-cream-light p-4">
        <h2 className="text-base font-medium">Contact details</h2>
        <dl className="mt-1 divide-y divide-maroon/10">
          <Row label="Location">{profile.location || "—"}</Row>
          <Row label="Citizenship">{profile.citizenship || "—"}</Row>
          <Row label="LinkedIn">{profile.linkedin || "—"}</Row>
          <Row label="GitLab">{profile.gitlab || "—"}</Row>
          <Row label="Email">{profile.email || "—"}</Row>
          <Row label="Phone">{profile.phone || "—"}</Row>
          <Row label="Visa sponsorship">{profile.visa_required ? "Needed" : "Not needed"}</Row>
        </dl>
      </section>
    </div>
  );
}

/**
 * My information: shows the saved information when there is one, and the setup
 * wizard only when there is none (or when the user chooses to edit). Saving
 * never starts job gathering by itself.
 */
export function ProfileWizardPage() {
  const welcomeName = (useLocation().state as { name?: string } | null)?.name ?? "";
  const existing = useQuery({
    queryKey: ["profile"],
    queryFn: () => profileApi.getProfile(),
    retry: false,
  });
  // A name typed on the welcome page means "start setting up".
  const [editing, setEditing] = useState(welcomeName !== "");
  const [reparseRunId, setReparseRunId] = useState<number | null>(null);

  if (existing.isLoading) {
    return <p className="text-sm text-maroon-soft">Loading profile…</p>;
  }
  const profile = existing.data?.profile ?? null;
  const saved = hasProfileInformation(profile) ? profile : null;
  if (saved && !editing) {
    return (
      <ProfileSummary
        profile={saved}
        onEdit={() => setEditing(true)}
        onReparse={(runId) => {
          setReparseRunId(runId);
          setEditing(true);
        }}
      />
    );
  }
  const close = () => {
    setEditing(false);
    setReparseRunId(null);
  };
  return (
    <ProfileWizard
      key={reparseRunId ?? "edit"}
      onSaved={close}
      onCancel={saved ? close : null}
      reparseRunId={reparseRunId}
    />
  );
}
