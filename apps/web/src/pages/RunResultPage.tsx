import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import type { Run, RunSource, RunStatus, ScrapeDropped } from "@/api/types";

const TERMINAL: RunStatus[] = ["completed", "failed", "cancelled"];
const POLL_MS = 2000;
/** Run types with a `run_sources` manifest (GET /runs/{id}/sources). */
const MANIFEST_TYPES = new Set(["careers_search", "name_collect"]);
const SOURCE_TERMINAL = new Set(["completed", "zero_yield", "failed", "cancelled"]);

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err);
}

function time(s: string | null): string {
  if (!s) return "—";
  return new Date(s).toLocaleTimeString();
}

/** One fetched candidate of an entry descent, as stored in `entry_trace`. */
interface TriedEntry {
  url: string;
  score: number;
  outcome: string;
}

interface EntryTrace {
  reason: string;
  rendered: boolean;
  sitemap: string | null;
  tried: TriedEntry[];
  candidates: { url: string; score: number; text: string }[];
}

function parseTrace(raw: string | null): EntryTrace | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<EntryTrace>;
    return {
      reason: parsed.reason ?? "",
      rendered: Boolean(parsed.rendered),
      sitemap: parsed.sitemap ?? null,
      tried: parsed.tried ?? [],
      candidates: parsed.candidates ?? [],
    };
  } catch {
    return null;
  }
}

function shortUrl(url: string): string {
  try {
    const parsed = new URL(url);
    return `${parsed.host}${parsed.pathname}`;
  } catch {
    return url;
  }
}

/**
 * The entry descent's evidence for one company row (Phase 4.0d): what was
 * fetched, what it scored, and the shortlist that was ranked. A miss is
 * reviewable here instead of only in the worker log.
 */
function EntryTraceLine({ trace }: { trace: string }) {
  const parsed = parseTrace(trace);
  if (!parsed) return null;
  return (
    <div className="mt-1 max-w-96 text-xs text-maroon-soft">
      {parsed.tried.length > 0 && (
        <p>
          tried{" "}
          {parsed.tried
            .map((t) => `${shortUrl(t.url)} (${t.score}) → ${t.outcome}`)
            .join("; ")}
        </p>
      )}
      {parsed.tried.length === 0 && parsed.candidates.length > 0 && (
        <p>
          no fetch:{" "}
          {parsed.candidates
            .map((c) => `${shortUrl(c.url)} (${c.score})`)
            .join("; ")}
        </p>
      )}
      {(parsed.rendered || parsed.sitemap) && (
        <p>
          {parsed.rendered ? "rendered retry used" : ""}
          {parsed.rendered && parsed.sitemap ? " · " : ""}
          {parsed.sitemap ? `sitemap ${shortUrl(parsed.sitemap)}` : ""}
        </p>
      )}
    </div>
  );
}

/**
 * The authoring trials behind one listing row (R5b): which body was proposed
 * and what the replay said about it. A listing that failed to author carried a
 * single sentence before this, and a recipe that took three tries showed
 * nothing at all.
 */
interface AuthorTrial {
  number: number;
  passed: boolean;
  reason: string | null;
  body: string;
}

function parseAuthorTrace(raw: string | null): AuthorTrial[] | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { trials?: AuthorTrial[] };
    return parsed.trials ?? [];
  } catch {
    return null;
  }
}

function AuthorTraceLine({ trace }: { trace: string }) {
  const trials = parseAuthorTrace(trace);
  if (!trials || trials.length === 0) return null;
  return (
    <div className="mt-1 max-w-96 text-xs text-maroon-soft">
      {trials.map((trial) => (
        <p key={trial.number}>
          trial {trial.number} {trial.passed ? "passed" : `failed: ${trial.reason ?? "unknown"}`}
        </p>
      ))}
      {trials.some((trial) => !trial.passed && trial.body) && (
        <details className="mt-1">
          <summary className="cursor-pointer">tried body</summary>
          <pre className="mt-1 max-h-40 overflow-auto font-mono text-[11px] whitespace-pre-wrap">
            {trials.filter((trial) => !trial.passed).map((trial) => trial.body).join("\n\n")}
          </pre>
        </details>
      )}
    </div>
  );
}

const RUN_STATUS_STYLES: Record<string, string> = {
  queued: "bg-lime text-maroon",
  running: "bg-maroon text-cream-light",
  paused: "bg-cream text-maroon",
  cancelled_requested: "bg-cream text-maroon-soft",
  completed: "bg-lime text-maroon",
  failed: "bg-red-800 text-cream-light",
  cancelled: "bg-cream-light text-maroon",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`rounded px-2 py-0.5 text-xs font-medium ${RUN_STATUS_STYLES[status] ?? ""}`}
    >
      {status}
    </span>
  );
}

/** The scrape filter's drops: counts and reasons inline, examples on hover. */
function DroppedLine({ dropped }: { dropped: string }) {
  let d: ScrapeDropped;
  try {
    d = JSON.parse(dropped) as ScrapeDropped;
  } catch {
    return null;
  }
  const reasons = Object.entries(d.reasons)
    .map(([reason, n]) => `${reason} ${n}`)
    .join(", ");
  const examples = d.examples.map((e) => `${e.title} — ${e.url}`).join("\n");
  return (
    <p className="mt-1 max-w-80 text-xs text-maroon-soft" title={examples}>
      {d.total} links, {d.total - d.dropped} postings · dropped {d.dropped} ({reasons})
    </p>
  );
}

function SourceRow({ row, runType }: { row: RunSource; runType: string }) {
  const label =
    runType === "careers_search"
      ? row.company_name ?? `company #${row.company_id ?? "?"}`
      : row.source_url ?? `source #${row.source_id ?? "?"}`;
  return (
    <tr className="border-t border-maroon/10 align-top">
      <td className="max-w-80 truncate px-3 py-2">
        {runType === "name_collect" && row.source_url ? (
          <a
            href={row.source_url}
            target="_blank"
            rel="noreferrer"
            className="text-maroon underline hover:text-maroon-soft"
          >
            {label}
          </a>
        ) : (
          label
        )}
      </td>
      <td className="px-3 py-2">
        <StatusBadge status={row.status} />
        {row.error && <p className="mt-1 max-w-80 text-xs text-red-800">{row.error}</p>}
        {row.dropped && <DroppedLine dropped={row.dropped} />}
        {runType === "careers_search" && row.entry_trace && (
          <EntryTraceLine trace={row.entry_trace} />
        )}
        {runType === "name_collect" && row.author_trace && (
          <AuthorTraceLine trace={row.author_trace} />
        )}
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{row.identities_found}</td>
      <td className="px-3 py-2 text-right tabular-nums">{row.candidates_added}</td>
      <td className="px-3 py-2 text-right tabular-nums">{row.candidates_duplicate}</td>
      <td className="px-3 py-2 text-maroon-soft">{row.extraction ?? "—"}</td>
      <td className="px-3 py-2 text-maroon-soft">{time(row.finished_at)}</td>
    </tr>
  );
}

function ManifestTable({ runId, runType }: { runId: number; runType: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["run-sources", runId],
    queryFn: () => api.listRunSources(runId),
    // Keep polling while any item is still open; stop once every row is terminal.
    refetchInterval: (query) =>
      query.state.data?.some((row) => !SOURCE_TERMINAL.has(row.status)) ? POLL_MS : false,
  });

  const rows = data ?? [];

  return (
    <div className="rounded-lg border border-maroon/10 bg-cream-light p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium">
          {runType === "careers_search" ? "Companies" : "Sources"}
        </h2>
        <span className="text-sm text-maroon-soft">
          {rows.filter((row) => SOURCE_TERMINAL.has(row.status)).length} / {rows.length} done
        </span>
      </div>
      {isLoading && <p className="mt-2 text-sm text-maroon-soft">Loading manifest…</p>}
      {error && (
        <p className="mt-2 text-sm text-red-800">Failed to load manifest: {errorDetail(error)}</p>
      )}
      {!isLoading && !error && rows.length === 0 && (
        <p className="mt-2 text-sm text-maroon-soft">No manifest rows for this run.</p>
      )}
      {rows.length > 0 && (
        <table className="mt-3 w-full text-left text-sm">
          <thead className="text-maroon-soft">
            <tr>
              <th className="px-3 py-1 font-medium">
                {runType === "careers_search" ? "Company" : "Source"}
              </th>
              <th className="px-3 py-1 font-medium">Status</th>
              <th className="px-3 py-1 text-right font-medium">Identities</th>
              <th className="px-3 py-1 text-right font-medium">Added</th>
              <th className="px-3 py-1 text-right font-medium">Dup</th>
              <th className="px-3 py-1 font-medium">Extraction</th>
              <th className="px-3 py-1 font-medium">Finished</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <SourceRow key={row.id} row={row} runType={runType} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function RunDetail({ run }: { run: Run }) {
  return (
    <div className="rounded-lg border border-maroon/10 bg-cream-light p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-medium">
          Run #{run.id} · {run.run_type}
        </h2>
        <StatusBadge status={run.status} />
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-2 text-sm">
        <div>
          <dt className="text-maroon-soft">Created</dt>
          <dd>{time(run.created_at)}</dd>
        </div>
        <div>
          <dt className="text-maroon-soft">Started</dt>
          <dd>{time(run.started_at)}</dd>
        </div>
        <div>
          <dt className="text-maroon-soft">Finished</dt>
          <dd>{time(run.finished_at)}</dd>
        </div>
        <div>
          <dt className="text-maroon-soft">Triggered by</dt>
          <dd>{run.triggered_by}</dd>
        </div>
        {Object.keys(run.params).length > 0 && (
          <div className="col-span-2">
            <dt className="text-maroon-soft">Params</dt>
            <dd className="font-mono text-xs break-all">{JSON.stringify(run.params)}</dd>
          </div>
        )}
        {run.summary && (
          <div className="col-span-2">
            <dt className="text-maroon-soft">Summary</dt>
            <dd className="font-mono text-xs whitespace-pre-wrap">
              {JSON.stringify(run.summary, null, 2)}
            </dd>
          </div>
        )}
        {run.error && (
          <div className="col-span-2">
            <dt className="text-maroon-soft">Error</dt>
            <dd className="text-red-800">{run.error}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}

/**
 * One run's result: the run row, its per-source manifest, and the controls.
 * While the run is not terminal this screen says it is waiting for results and
 * polls; when it is terminal it shows the outcome. The list it belongs to is
 * Results (/search/results).
 */
export function RunResultPage() {
  const { runId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const {
    data: run,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.getRun(Number(runId)),
    enabled: runId !== undefined,
    // Poll while queued/running; stop once terminal.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && TERMINAL.includes(status) ? false : POLL_MS;
    },
  });

  const runKey = ["run", runId];
  const cancel = useMutation({
    mutationFn: (id: number) => api.cancelRun(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKey }),
  });
  const pause = useMutation({
    mutationFn: (id: number) => api.pauseRun(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKey }),
  });
  const resume = useMutation({
    mutationFn: (id: number) => api.resumeRun(id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: runKey }),
  });
  const controlError = cancel.error ?? pause.error ?? resume.error;
  const controlPending = cancel.isPending || pause.isPending || resume.isPending;

  if (runId === undefined) {
    return (
      <div className="space-y-3">
        <h1 className="text-lg font-semibold">Result</h1>
        <p className="text-sm text-maroon-soft">
          No run selected. Pick one from the{" "}
          <Link to="/search/results" className="text-maroon underline hover:text-maroon-soft">
            results list
          </Link>
          .
        </p>
      </div>
    );
  }
  if (isLoading) return <p className="text-sm text-maroon-soft">Loading run…</p>;
  if (error) {
    return (
      <p className="text-sm text-red-800">Failed to load run: {errorDetail(error)}</p>
    );
  }
  if (!run) return null;

  const active = !TERMINAL.includes(run.status);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-semibold">
          {active ? "Waiting for results" : "Run finished"}
        </h1>
        <Link
          to="/search/results"
          className="text-sm text-maroon underline hover:text-maroon-soft"
        >
          All results
        </Link>
      </div>

      {active && (
        <div className="flex items-center gap-3 rounded-lg border border-maroon/10 bg-cream-light p-4">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-maroon/30 border-t-maroon" />
          <p className="text-sm text-maroon">
            {run.status === "queued" && "Run queued, waiting for the worker to pick it up."}
            {run.status === "running" && "Run in progress. This screen updates automatically."}
            {run.status === "paused" &&
              "Paused — the worker holds at its next checkpoint. Resume to continue."}
            {run.status === "cancelled_requested" &&
              "Cancel requested — the worker stops at its next checkpoint."}
          </p>
        </div>
      )}

      <RunDetail run={run} />

      {MANIFEST_TYPES.has(run.run_type) && (
        <ManifestTable runId={run.id} runType={run.run_type} />
      )}

      {controlError && (
        <p className="text-sm text-red-800">Control failed: {errorDetail(controlError)}</p>
      )}

      <div className="flex flex-wrap gap-2">
        {(run.status === "queued" || run.status === "running") && (
          <button
            type="button"
            disabled={controlPending}
            onClick={() => pause.mutate(run.id)}
            className="rounded border border-maroon/20 px-4 py-1.5 text-sm text-maroon hover:bg-cream disabled:opacity-50"
          >
            Pause
          </button>
        )}
        {run.status === "paused" && (
          <button
            type="button"
            disabled={controlPending}
            onClick={() => resume.mutate(run.id)}
            className="rounded bg-lime px-4 py-1.5 text-sm font-medium text-maroon hover:bg-lime/80 disabled:opacity-50"
          >
            Resume
          </button>
        )}
        {active && run.status !== "cancelled_requested" && (
          <button
            type="button"
            disabled={controlPending}
            onClick={() => cancel.mutate(run.id)}
            className="rounded border border-red-800 px-4 py-1.5 text-sm text-red-300 hover:bg-red-950 disabled:opacity-50"
          >
            Cancel run
          </button>
        )}
        {run.status === "completed" && MANIFEST_TYPES.has(run.run_type) && (
          <button
            type="button"
            onClick={() => navigate("/search/review")}
            className="rounded bg-lime px-4 py-1.5 text-sm font-medium text-maroon hover:bg-lime/80"
          >
            Go to review
          </button>
        )}
        {run.status === "completed" && run.run_type === "collect" && (
          <button
            type="button"
            onClick={() => navigate("/matches")}
            className="rounded bg-lime px-4 py-1.5 text-sm font-medium text-maroon hover:bg-lime/80"
          >
            See matches
          </button>
        )}
        <button
          type="button"
          onClick={() => navigate("/search/results")}
          className="rounded border border-maroon/20 px-4 py-1.5 text-sm text-maroon hover:bg-cream"
        >
          All results
        </button>
      </div>
    </div>
  );
}
