import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import { profileApi } from "@/api/profile-api";
import type { Job, MatchStatus } from "@/api/types";
import { hasProfileInformation } from "@/pages/wizard/wizard-shared";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err);
}

const LIMIT = 500;
/** MF5: the best few first; the rest behind "Show more". */
const TOP = 5;
const TERMINAL = new Set(["completed", "failed", "cancelled"]);

type Group = "all" | "worth" | "consider";

const GROUPS: { id: Group; label: string }[] = [
  { id: "all", label: "All" },
  { id: "worth", label: "Worth applying" },
  { id: "consider", label: "Consider" },
];

const RECOMMENDATION_LABEL: Record<string, string> = {
  worth: "Worth applying",
  consider: "Consider",
  skip: "Skip",
};

const COMPONENT_LABEL: Record<string, string> = {
  role_fit: "Role",
  required_technologies: "Required technologies",
  experience: "Experience",
  location: "Location",
  nice_to_have: "Nice to have",
  languages: "Languages",
};

function threshold(job: Job, status: MatchStatus | undefined): string | null {
  if (!status) return null;
  if (job.recommendation === "worth") return status.worth_threshold.toFixed(2);
  if (job.recommendation === "consider") return status.consider_threshold.toFixed(2);
  return null;
}

/**
 * Results: the gathered jobs scored against the profile. Scoring is a step of
 * its own, so "Score jobs" starts a match run and this screen shows the run in
 * flight, then the ranked list. The richer filters live on /jobs.
 *
 * MF5: nothing is listed until a match run started after the last information
 * save (the scores would describe older information). Then the top 5 by
 * score, "Show more" for the rest, and jobs whose role is outside the target
 * roles hidden behind a toggle that names the reason.
 */
export function MatchesPage() {
  const [group, setGroup] = useState<Group>("all");
  const [country, setCountry] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [showSuspects, setShowSuspects] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [runId, setRunId] = useState<number | null>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const jobsQuery = useQuery({
    queryKey: ["jobs", "matches"],
    queryFn: () => api.listJobs({ scored: true, order: "score_desc", limit: LIMIT }),
  });

  // No saved profile = nothing to match against: show no matches at all.
  const profileQuery = useQuery({
    queryKey: ["profile"],
    queryFn: () => profileApi.getProfile(),
    retry: false,
  });

  const statusQuery = useQuery({
    queryKey: ["matches", "status"],
    queryFn: () => api.matchStatus(),
  });

  const runQuery = useQuery({
    queryKey: ["runs", runId],
    queryFn: () => api.getRun(runId as number),
    enabled: runId !== null,
    refetchInterval: (query) => {
      const state = query.state.data?.status;
      return state && TERMINAL.has(state) ? false : 2000;
    },
  });

  const runStatus = runQuery.data?.status;
  const running = runId !== null && !(runStatus && TERMINAL.has(runStatus));

  // The run is over: show its result in the list instead of the wait state.
  useEffect(() => {
    if (runStatus && TERMINAL.has(runStatus)) {
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["matches", "status"] });
    }
  }, [runStatus, queryClient]);

  const start = useMutation({
    mutationFn: () => api.startMatchRun({}),
    onSuccess: ({ run_id }) => {
      setRunId(run_id);
      queryClient.invalidateQueries({ queryKey: ["runs"] });
    },
    onError: (err) => {
      setLastError(null);
      setBlocked(null);
      // 409 = a match run is already active; 422 = nothing to compare yet
      // (no saved skills or experience). Blocked, not failed.
      if (err instanceof ApiError && (err.status === 409 || err.status === 422)) {
        setBlocked(err.detail);
      } else {
        setLastError(errorDetail(err));
      }
    },
  });

  const remove = useMutation({
    mutationFn: (jobId: number) => api.deleteJob(jobId),
    onSuccess: () => {
      setSelectedId(null);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
      queryClient.invalidateQueries({ queryKey: ["matches", "status"] });
    },
    onError: (err) => setLastError(errorDetail(err)),
  });

  // Suspected non-postings (articles, category pages) are hidden by default and,
  // when shown, listed after every real-looking job. The owner confirms them
  // with the delete button; nothing is removed here.
  const fetched: Job[] = useMemo(() => jobsQuery.data ?? [], [jobsQuery.data]);
  const suspectCount = useMemo(() => fetched.filter((j) => j.suspect_reason).length, [fetched]);
  // MF4: a role outside the target roles is hidden here only; /jobs keeps it.
  const hiddenCount = useMemo(
    () => fetched.filter((j) => j.fit_hidden_reason && !j.suspect_reason).length,
    [fetched],
  );
  const all: Job[] = useMemo(() => {
    const real = fetched.filter((j) => !j.suspect_reason && (showHidden || !j.fit_hidden_reason));
    return showSuspects ? [...real, ...fetched.filter((j) => j.suspect_reason)] : real;
  }, [fetched, showSuspects, showHidden]);
  const status = statusQuery.data;

  const countries = useMemo(
    () => [...new Set(all.map((j) => j.country).filter((c): c is string => !!c))].sort(),
    [all],
  );

  const rows = useMemo(
    () =>
      all.filter(
        (j) =>
          (group === "all" || j.recommendation === group) &&
          (country === "" || j.country === country),
      ),
    [all, group, country],
  );

  const shown = showAll ? rows : rows.slice(0, TOP);
  const selected = shown.find((j) => j.id === selectedId) ?? shown[0] ?? null;

  function count(g: Group): number {
    return g === "all" ? all.length : all.filter((j) => j.recommendation === g).length;
  }

  if (profileQuery.isLoading) {
    return <p className="text-sm text-maroon-soft">Loading…</p>;
  }
  if (!hasProfileInformation(profileQuery.data?.profile)) {
    return (
      <div className="space-y-4">
        <h1 className="text-lg font-semibold">Matches</h1>
        <div className="rounded-lg border border-maroon/10 bg-cream-light p-4 text-sm">
          <p className="font-medium">No matches yet</p>
          <p className="mt-1 text-maroon-soft">
            Matches are scored against your information. Save it first, then come back here.
          </p>
          <Link
            to="/profile"
            className="mt-3 inline-block rounded bg-maroon px-4 py-1.5 text-sm font-semibold text-cream-light hover:bg-maroon-soft"
          >
            Set up My information
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Matches</h1>
          <p className="text-sm text-maroon-soft">Jobs are scored against your profile.</p>
        </div>
        <div className="flex-1" />
        <Link
          to="/profile"
          className="rounded border border-maroon px-4 py-1.5 text-sm font-semibold text-maroon hover:bg-cream-light"
        >
          Update profile
        </Link>
        <button
          type="button"
          disabled={start.isPending || running || status?.evidence_ready === false}
          onClick={() => {
            setLastError(null);
            setBlocked(null);
            start.mutate();
          }}
          className="rounded bg-maroon px-4 py-1.5 text-sm font-semibold text-cream-light hover:bg-maroon-soft disabled:opacity-50"
        >
          {start.isPending ? "Starting…" : running ? "Scoring…" : "Score jobs"}
        </button>
      </div>

      {status?.evidence_ready === false && (
        <p className="text-sm text-maroon">
          Your saved CV information has no skills and no experience, so there is nothing to compare
          jobs against.{" "}
          <Link to="/profile" className="underline hover:text-maroon-soft">
            Open My information
          </Link>
          , read your CV and save, then score.
        </p>
      )}

      {blocked && <p className="text-sm text-maroon">Start blocked: {blocked}. Wait for it to finish.</p>}
      {lastError && <p className="text-sm text-red-800">Failed: {lastError}</p>}

      {running && (
        <div role="status" className="rounded-lg border border-maroon/10 bg-cream-light p-4 text-sm">
          <p className="font-medium">
            {runStatus === "queued" || runStatus === undefined
              ? "Waiting for the worker…"
              : "Scoring jobs…"}
          </p>
          <p className="text-maroon-soft">
            The best new jobs are read first, then every job is scored. The list updates when the
            run finishes.
          </p>
        </div>
      )}

      {runStatus === "failed" && (
        <p className="text-sm text-red-800">
          Scoring failed: {runQuery.data?.error?.split("\n")[0] ?? "unknown error"}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2 text-sm">
        {GROUPS.map((g) => (
          <button
            key={g.id}
            type="button"
            aria-pressed={group === g.id}
            onClick={() => setGroup(g.id)}
            className={`rounded-full border border-maroon/15 px-3 py-0.5 ${
              group === g.id ? "bg-maroon text-cream-light" : "bg-cream-light text-maroon"
            }`}
          >
            {g.label} {count(g.id)}
          </button>
        ))}
        <label className="flex items-center gap-1 text-maroon-soft">
          Country:
          <select
            value={country}
            onChange={(e) => setCountry(e.target.value)}
            className="rounded border border-maroon/20 bg-cream-light px-2 py-0.5 text-maroon"
          >
            <option value="">All</option>
            {countries.map((c) => (
              <option key={c} value={c}>
                {c.toUpperCase()}
              </option>
            ))}
          </select>
        </label>
        <span className="ml-auto text-xs text-maroon-soft">
          More filters (role, company, date, source) are under{" "}
          <Link to="/jobs" className="underline hover:text-maroon">
            Jobs
          </Link>
          .
        </span>
      </div>

      {status && status.unprofiled_remaining > 0 && (
        <p className="text-sm text-maroon-soft">
          {status.unprofiled_remaining} more jobs not scored yet. The next run takes the next batch.
        </p>
      )}

      {suspectCount > 0 && (
        <p className="text-sm text-maroon-soft">
          {suspectCount} suspected non-postings (pages, press releases) are{" "}
          {showSuspects ? "listed last" : "hidden"}.{" "}
          <button
            type="button"
            onClick={() => setShowSuspects((v) => !v)}
            className="underline hover:text-maroon"
          >
            {showSuspects ? "Hide them" : "Show them"}
          </button>
        </p>
      )}

      {jobsQuery.error && (
        <p className="text-sm text-red-800">Failed to load matches: {String(jobsQuery.error)}</p>
      )}

      {!jobsQuery.isLoading && !jobsQuery.error && fetched.length === 0 && !running && (
        <p className="text-sm text-maroon-soft">
          No scored jobs yet. Gather jobs under{" "}
          <Link to="/search/jobs" className="text-maroon underline hover:text-maroon-soft">
            Find jobs
          </Link>
          , then score them here.
        </p>
      )}

      {status && !status.results_current && !running && (
        <div className="rounded-lg border border-maroon/10 bg-cream-light p-4 text-sm">
          <p className="font-medium">
            {status.profile_changed ? "Your information changed since the last scoring" : "No current matches"}
          </p>
          <p className="mt-1 text-maroon-soft">
            Matches show the results of a scoring made with your current information. Press Score
            jobs to make one.
          </p>
        </div>
      )}

      {status?.results_current && hiddenCount > 0 && (
        <p className="text-sm text-maroon-soft">
          {hiddenCount} hidden as not your role.{" "}
          <button
            type="button"
            onClick={() => setShowHidden((v) => !v)}
            className="underline hover:text-maroon"
          >
            {showHidden ? "Hide them" : "Show hidden"}
          </button>
        </p>
      )}

      {status?.results_current && all.length > 0 && (
        <div className="grid gap-4 md:grid-cols-[minmax(300px,420px)_1fr] md:items-start">
          <ul className="overflow-hidden rounded-lg border border-maroon/10 bg-cream-light">
            {shown.map((j) => (
              <li key={j.id} className="border-b border-maroon/10 last:border-0">
                <button
                  type="button"
                  onClick={() => setSelectedId(j.id)}
                  aria-current={selected?.id === j.id}
                  className={`grid w-full grid-cols-[44px_1fr] items-center gap-3 px-3 py-2.5 text-left ${
                    selected?.id === j.id ? "bg-lime" : "hover:bg-cream"
                  }`}
                >
                  <span className="text-center text-base font-bold tabular-nums">
                    {j.total_score !== null ? j.total_score.toFixed(2) : "—"}
                  </span>
                  <span>
                    <span className="block font-semibold">{j.normalized_title ?? j.url}</span>
                    {j.suspect_reason && (
                      <span className="block text-xs text-red-800">Suspected: not a posting</span>
                    )}
                    {j.fit_hidden_reason && (
                      <span className="block text-xs text-amber-700">Hidden: {j.fit_hidden_reason}</span>
                    )}
                    <span className="block text-xs text-maroon-soft">
                      {[j.normalized_company, j.normalized_location].filter(Boolean).join(" · ")}
                    </span>
                  </span>
                </button>
              </li>
            ))}
            {rows.length === 0 && (
              <li className="px-3 py-4 text-sm text-maroon-soft">No jobs match these filters.</li>
            )}
            {rows.length > TOP && (
              <li>
                <button
                  type="button"
                  onClick={() => setShowAll((v) => !v)}
                  className="w-full px-3 py-2 text-left text-sm text-maroon underline hover:bg-cream"
                >
                  {showAll ? "Show top 5 only" : `Show more (${rows.length - TOP})`}
                </button>
              </li>
            )}
          </ul>

          {selected && (
            <section
              aria-label="Selected job"
              className="space-y-3 rounded-lg border border-maroon/10 bg-cream-light p-4 md:sticky md:top-3"
            >
              <div>
                <h2 className="text-lg font-semibold">{selected.normalized_title ?? selected.url}</h2>
                <p className="text-sm text-maroon-soft">
                  {[selected.normalized_company, selected.normalized_location]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <b className="rounded-lg bg-lime px-3 text-3xl tabular-nums">
                  {selected.total_score !== null ? selected.total_score.toFixed(2) : "—"}
                </b>
                <span className="text-sm text-maroon-soft">
                  {selected.recommendation ? RECOMMENDATION_LABEL[selected.recommendation] : ""}
                  {threshold(selected, status) && (
                    <>
                      <br />
                      Threshold {threshold(selected, status)}
                    </>
                  )}
                </span>
              </div>
              {selected.fit_summary && <p className="text-sm">{selected.fit_summary}</p>}
              {selected.fit_hidden_reason && (
                <p className="text-sm text-amber-700">Hidden by default: {selected.fit_hidden_reason}</p>
              )}

              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-3 text-sm">
                  <Section title="Strengths" items={selected.fit_strengths} mark="+" />
                  <Section title="Gaps" items={selected.fit_gaps} mark="−" />
                  <Section title="Blockers" items={selected.fit_blockers} mark="!" />
                </div>
                <div>
                  <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-maroon-soft">
                    Score breakdown
                  </h3>
                  <table className="w-full text-sm">
                    <tbody>
                      {Object.entries(selected.fit_components ?? {}).map(([name, value]) => (
                        <tr key={name} className="border-t border-maroon/10">
                          <td className="py-0.5">{COMPONENT_LABEL[name] ?? name}</td>
                          <td className="py-0.5 text-right tabular-nums">{value.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <div className="flex gap-2">
                <a
                  href={selected.url}
                  target="_blank"
                  rel="noreferrer"
                  className="rounded bg-maroon px-3 py-1 text-sm text-cream-light hover:bg-maroon-soft"
                >
                  Open posting ↗
                </a>
                <button
                  type="button"
                  disabled={remove.isPending}
                  onClick={() => {
                    if (window.confirm("Delete this posting? It will not come back on later runs.")) {
                      remove.mutate(selected.id);
                    }
                  }}
                  className="rounded border border-maroon px-3 py-1 text-sm hover:bg-cream disabled:opacity-50"
                >
                  Not a job — delete
                </button>
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function Section({ title, items, mark }: { title: string; items?: string[]; mark: string }) {
  if (!items || items.length === 0) return null;
  return (
    <div>
      <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-maroon-soft">
        {title}
      </h3>
      <ul className="space-y-0.5">
        {items.map((item) => (
          <li key={item}>
            <span className="mr-1 font-bold text-maroon-soft">{mark}</span>
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}
