import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { Run, RunStatus } from "@/api/types";

const POLL_MS = 3000;
const HISTORY_LIMIT = 20;
const ACTIVE: RunStatus[] = ["queued", "running", "paused", "cancelled_requested"];
/** Run types with a `run_sources` manifest (GET /runs/{id}/sources). */
const MANIFEST_TYPES = new Set(["careers_search", "name_collect"]);
const SOURCE_TERMINAL = new Set(["completed", "zero_yield", "failed", "cancelled"]);

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-lime text-maroon",
  running: "bg-maroon text-cream-light",
  paused: "bg-cream text-maroon",
  cancelled_requested: "bg-cream text-maroon-soft",
  completed: "bg-lime text-maroon",
  failed: "bg-red-800 text-cream-light",
  cancelled: "bg-cream-light text-maroon",
};

/** What the run did, in the words of the menu rather than the pipeline. */
const RUN_LABELS: Record<string, string> = {
  careers_search: "Careers pages",
  name_collect: "Company names",
  collect: "Job gathering",
  discover: "Match search",
  match: "Scoring",
  profile_parse: "Profile parse",
};

function runLabel(runType: string): string {
  return RUN_LABELS[runType] ?? runType;
}

function RunProgress({ run }: { run: Run }) {
  const active = ACTIVE.includes(run.status);
  const { data } = useQuery({
    queryKey: ["run-sources", run.id],
    queryFn: () => api.listRunSources(run.id),
    enabled: MANIFEST_TYPES.has(run.run_type),
    refetchInterval: active ? POLL_MS : false,
  });

  if (!MANIFEST_TYPES.has(run.run_type)) {
    return <span className="text-maroon-soft">—</span>;
  }
  if (!data || data.length === 0) {
    return <span className="text-maroon-soft">no manifest yet</span>;
  }
  const done = data.filter((row) => SOURCE_TERMINAL.has(row.status)).length;
  const failed = data.filter((row) => row.status === "failed").length;
  return (
    <span className="text-maroon-soft">
      {done} / {data.length} done
      {failed > 0 && <span className="text-red-800"> · {failed} failed</span>}
    </span>
  );
}

function ResultRow({ run }: { run: Run }) {
  return (
    <tr className="border-t border-maroon/10">
      <td className="px-3 py-2 font-mono">#{run.id}</td>
      <td className="px-3 py-2">{runLabel(run.run_type)}</td>
      <td className="px-3 py-2">
        <span
          className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_STYLES[run.status] ?? ""}`}
        >
          {run.status}
        </span>
      </td>
      <td className="px-3 py-2 text-maroon-soft">
        <RunProgress run={run} />
      </td>
      <td className="px-3 py-2 text-maroon-soft">
        {run.started_at ? new Date(run.started_at).toLocaleString() : "—"}
      </td>
      <td className="px-3 py-2 text-right">
        <Link
          to={`/search/results/${run.id}`}
          className="text-maroon underline hover:text-maroon-soft"
        >
          Open
        </Link>
      </td>
    </tr>
  );
}

/**
 * Results: every run this owner started, newest first. An active run says so
 * at the top ("waiting for results") and update its own row while it works;
 * opening one shows the detail, live while it runs and as a result when it
 * finishes. This replaces the old separate Wait screen.
 */
export function ResultsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["runs", HISTORY_LIMIT],
    queryFn: () => api.listRuns(undefined, HISTORY_LIMIT),
    refetchInterval: (query) =>
      query.state.data?.some((run) => ACTIVE.includes(run.status)) ? POLL_MS : false,
  });

  if (isLoading) return <p className="text-sm text-maroon-soft">Loading results…</p>;
  if (error) {
    return <p className="text-sm text-red-800">Failed to load results: {String(error)}</p>;
  }

  const runs = data ?? [];
  const active = runs.find((run) => ACTIVE.includes(run.status));

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">Results</h1>
        <span className="text-sm text-maroon-soft">last {HISTORY_LIMIT}</span>
      </div>

      {active && (
        <Link
          to={`/search/results/${active.id}`}
          className="flex items-center gap-3 rounded-lg border border-maroon/10 bg-cream-light p-4 hover:border-maroon/30"
        >
          <div className="h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-maroon/30 border-t-maroon" />
          <p className="text-sm text-maroon">
            Run #{active.id} · {runLabel(active.run_type)} — waiting for results.
          </p>
          <span aria-hidden="true" className="ml-auto text-maroon-soft">
            →
          </span>
        </Link>
      )}

      {runs.length === 0 ? (
        <p className="text-sm text-maroon-soft">
          No results yet. Start with{" "}
          <Link to="/search/careers" className="text-maroon underline hover:text-maroon-soft">
            careers pages
          </Link>{" "}
          or{" "}
          <Link to="/match" className="text-maroon underline hover:text-maroon-soft">
            a match
          </Link>
          .
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-maroon-soft">
            <tr>
              <th className="px-3 py-2 font-medium">ID</th>
              <th className="px-3 py-2 font-medium">What</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Progress</th>
              <th className="px-3 py-2 font-medium">Started</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <ResultRow key={run.id} run={run} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
