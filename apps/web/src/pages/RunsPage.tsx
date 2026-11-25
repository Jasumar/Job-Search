import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import type { Run, RunStatus } from "@/api/types";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err);
}

const STATUS_STYLES: Record<string, string> = {
  queued: "bg-lime text-maroon",
  running: "bg-maroon text-cream-light",
  paused: "bg-cream text-maroon",
  cancelled_requested: "bg-cream text-maroon-soft",
  completed: "bg-lime text-maroon",
  failed: "bg-red-800 text-cream-light",
  cancelled: "bg-cream-light text-maroon",
};

const ACTIVE: RunStatus[] = ["queued", "running", "paused", "cancelled_requested"];
/** Run types with a `run_sources` manifest (GET /runs/{id}/sources). */
const MANIFEST_TYPES = new Set(["careers_search", "name_collect"]);
const SOURCE_TERMINAL = new Set(["completed", "zero_yield", "failed", "cancelled"]);
const HISTORY_LIMIT = 10;

function RunProgress({ run }: { run: Run }) {
  const active = ACTIVE.includes(run.status);
  const { data } = useQuery({
    queryKey: ["run-sources", run.id],
    queryFn: () => api.listRunSources(run.id),
    enabled: MANIFEST_TYPES.has(run.run_type),
    refetchInterval: active ? 3000 : false,
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

function RunRow({ run }: { run: Run }) {
  return (
    <tr className="border-t border-maroon/10">
      <td className="px-3 py-2 font-mono">#{run.id}</td>
      <td className="px-3 py-2">{run.run_type}</td>
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
          View
        </Link>
      </td>
    </tr>
  );
}

function StartDiscoverRun() {
  const [country, setCountry] = useState("fi");
  const [role, setRole] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [lastError, setLastError] = useState<string | null>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const start = useMutation({
    mutationFn: () =>
      api.startDiscoverRun({
        dry_run: dryRun,
        country: country || null,
        role: role || null,
      }),
    onSuccess: ({ run_id }) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/search/results/${run_id}`);
    },
    onError: (err) => setLastError(errorDetail(err)),
  });

  return (
    <div className="rounded-lg border border-maroon/10 bg-cream-light p-4">
      <h2 className="text-base font-medium">Start discovery run</h2>
      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2">
          Country:
          <input
            value={country}
            onChange={(e) => setCountry(e.target.value)}
            placeholder="fi"
            className="w-16 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-sm focus:border-maroon focus:outline-none"
          />
        </label>
        <label className="flex items-center gap-2">
          Role:
          <input
            value={role}
            onChange={(e) => setRole(e.target.value)}
            placeholder="backend"
            className="w-32 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-sm focus:border-maroon focus:outline-none"
          />
        </label>
        <label className="flex items-center gap-2 text-maroon-soft">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
          />
          dry run
        </label>
        <button
          type="button"
          disabled={start.isPending}
          onClick={() => {
            setLastError(null);
            start.mutate();
          }}
          className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
        >
          {start.isPending ? "Starting…" : "Start"}
        </button>
      </div>
      {lastError && (
        <p className="mt-2 text-sm text-red-800">Start failed: {lastError}</p>
      )}
    </div>
  );
}

export function RunsPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["runs", HISTORY_LIMIT],
    queryFn: () => api.listRuns(undefined, HISTORY_LIMIT),
  });

  if (isLoading) return <p className="text-sm text-maroon-soft">Loading runs…</p>;
  if (error) {
    return <p className="text-sm text-red-800">Failed to load runs: {errorDetail(error)}</p>;
  }

  const runs = data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Runs</h1>
          <p className="text-sm text-maroon-soft">
            Raw run log: every action in the menu appears here, whatever started it. The
            user-facing view is{" "}
            <Link to="/search/results" className="text-maroon underline hover:text-maroon-soft">
              Results
            </Link>
            .
          </p>
        </div>
        <span className="text-sm text-maroon-soft">last {HISTORY_LIMIT}</span>
      </div>

      <StartDiscoverRun />

      {runs.length === 0 ? (
        <p className="text-sm text-maroon-soft">No runs yet. Start one above.</p>
      ) : (
        <table className="w-full text-sm">
          <thead className="text-left text-maroon-soft">
            <tr>
              <th className="px-3 py-2 font-medium">ID</th>
              <th className="px-3 py-2 font-medium">Type</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Progress</th>
              <th className="px-3 py-2 font-medium">Started</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}