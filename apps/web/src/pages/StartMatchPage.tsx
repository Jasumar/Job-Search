import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import { profileApi } from "@/api/profile-api";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err);
}

/**
 * Start a match: the profile path to companies. The search plan is computed
 * from the stored profile (role families x target countries), so this page
 * shows exactly what will be searched before spending a single query, and
 * points at My information when the profile is still empty. It ends in
 * reviewable company candidates, like the pasted-source path.
 */
export function StartMatchPage() {
  const [dryRun, setDryRun] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const preview = useQuery({
    queryKey: ["discovery", "preview"],
    queryFn: () => profileApi.discoveryPreview(),
  });

  const start = useMutation({
    mutationFn: () => api.startDiscoverRun({ dry_run: dryRun }),
    onSuccess: ({ run_id }) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/search/results/${run_id}`);
    },
    onError: (err) => {
      setLastError(null);
      setBlocked(null);
      // 409 = a discover run is already active: blocked, not failed.
      if (err instanceof ApiError && err.status === 409) {
        setBlocked(err.detail);
      } else {
        setLastError(errorDetail(err));
      }
    },
  });

  const plan = preview.data;
  // "default" means: no target countries or preferences in the profile yet, so
  // the plan is the unscoped baseline rather than the user's own search.
  const profileEmpty = plan?.profile_source === "default";

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold">Start a match</h1>
        <p className="text-sm text-maroon-soft">
          Search for companies that fit your profile. What is found becomes company
          candidates for{" "}
          <Link to="/search/review" className="text-maroon underline hover:text-maroon-soft">
            review
          </Link>{" "}
          — nothing is scraped until you accept it.
        </p>
      </div>

      <div className="rounded-lg border border-maroon/10 bg-cream-light p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="text-base font-medium">Search plan</h2>
          <Link
            to="/profile"
            className="text-sm text-maroon underline hover:text-maroon-soft"
          >
            My information →
          </Link>
        </div>

        {preview.isLoading && <p className="mt-2 text-sm text-maroon-soft">Loading plan…</p>}
        {preview.error && (
          <p className="mt-2 text-sm text-red-800">
            Failed to load the search plan: {errorDetail(preview.error)}
          </p>
        )}

        {plan && (
          <dl className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div>
              <dt className="text-maroon-soft">Queries</dt>
              <dd className="tabular-nums">
                {plan.baseline_query_count} of {plan.ceiling}
              </dd>
            </div>
            <div>
              <dt className="text-maroon-soft">Segments</dt>
              <dd className="tabular-nums">{plan.selected_segments.length}</dd>
            </div>
            <div>
              <dt className="text-maroon-soft">Countries</dt>
              <dd className="truncate">{plan.selected_countries.join(", ") || "—"}</dd>
            </div>
            <div>
              <dt className="text-maroon-soft">Based on</dt>
              <dd>{profileEmpty ? "no profile yet" : "your profile"}</dd>
            </div>
          </dl>
        )}

        {plan && plan.sample_queries.length > 0 && (
          <details className="mt-3 text-sm">
            <summary className="cursor-pointer text-maroon-soft">Sample queries</summary>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 font-mono text-xs text-maroon-soft">
              {plan.sample_queries.slice(0, 6).map((q) => (
                <li key={q}>{q}</li>
              ))}
            </ul>
          </details>
        )}

        {profileEmpty && (
          <p className="mt-3 rounded border border-maroon/20 p-3 text-sm text-maroon">
            Your profile has no target countries or role families yet, so this search
            uses the unscoped baseline instead of your own targets.{" "}
            <Link to="/profile" className="text-maroon underline">
              Add them in My information
            </Link>
            .
          </p>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-2 text-maroon-soft">
            <input
              type="checkbox"
              checked={dryRun}
              onChange={(e) => setDryRun(e.target.checked)}
            />
            dry run (write nothing)
          </label>
          <button
            type="button"
            disabled={start.isPending || preview.isLoading}
            onClick={() => {
              setLastError(null);
              setBlocked(null);
              start.mutate();
            }}
            className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
          >
            {start.isPending ? "Starting…" : "Start a match"}
          </button>
        </div>

        {blocked && (
          <p className="mt-2 text-sm text-maroon">
            Start blocked: {blocked}. Wait for it to finish, or cancel it from the results
            screen.
          </p>
        )}
        {lastError && <p className="mt-2 text-sm text-red-800">Start failed: {lastError}</p>}
      </div>
    </div>
  );
}
