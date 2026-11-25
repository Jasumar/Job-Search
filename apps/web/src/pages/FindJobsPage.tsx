import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import type { Company } from "@/api/types";
import { ListToolbar, filterInput, filterLabel } from "@/components/ListToolbar";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err);
}

const hasSource = (c: Company) => Boolean(c.open_positions_url || c.careers_url);

/** One cell of scrape state: when the newest gather finished and how it ended. */
function lastScrape(c: Company): string {
  if (!c.last_scraped_at) {
    return c.job_count > 0 ? "before run records" : "never";
  }
  const day = new Date(c.last_scraped_at).toLocaleDateString();
  if (c.last_scrape_status === "failed") return `${day} · failed`;
  const d = c.last_scrape_dropped;
  if (d) return `${day} · ${d.total} links, ${d.total - d.dropped} postings`;
  if (c.last_scrape_status === "zero_yield") return `${day} · no postings`;
  return day;
}

/** The scrape kept nothing: the filter judged every link a non-posting. */
const keptNothing = (c: Company) =>
  c.last_scrape_status === "zero_yield" && Boolean(c.last_scrape_dropped);

/** Tooltip for the drop record: reasons, then up to five examples. */
function dropDetail(c: Company): string | undefined {
  const d = c.last_scrape_dropped;
  if (!d) return undefined;
  const reasons = Object.entries(d.reasons)
    .map(([reason, n]) => `${reason}: ${n}`)
    .join(", ");
  const examples = d.examples.map((e) => `${e.title} — ${e.url}`).join("\n");
  return `Dropped ${d.dropped} of ${d.total} links (${reasons})\n${examples}`;
}

/**
 * Find jobs: the gathering step. Lists every company with its scrape state and
 * gathers either the ticked companies (any status) or, with nothing ticked,
 * the accepted set. Accepted rows are promoted to known after a scrape that
 * added jobs, then a match run scores what was collected.
 */
export function FindJobsPage() {
  const [dryRun, setDryRun] = useState(false);
  const [refreshKnown, setRefreshKnown] = useState(false);
  const [limit, setLimit] = useState("");
  const [batch, setBatch] = useState("");
  const [lastError, setLastError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const [status, setStatus] = useState<string | undefined>(undefined);
  const [country, setCountry] = useState("");
  const [needsSource, setNeedsSource] = useState(false);
  const [neverScraped, setNeverScraped] = useState(false);
  const [nothingKept, setNothingKept] = useState(false);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const counts = useQuery({
    queryKey: ["companies", "count"],
    queryFn: () => api.companyCount(),
  });
  // No row limit: the name search runs in the browser over the full list.
  const list = useQuery({
    queryKey: ["companies", "find-jobs", status, country, needsSource],
    queryFn: () =>
      api.listCompanies({
        status,
        country: country || undefined,
        no_careers: needsSource || undefined,
      }),
  });

  const needle = search.trim().toLowerCase();
  const rows: Company[] = (list.data ?? []).filter(
    (c) =>
      (!needle || c.name.toLowerCase().includes(needle)) &&
      (!neverScraped || (!c.last_scraped_at && c.job_count === 0)) &&
      (!nothingKept || keptNothing(c)),
  );
  const activeFilters =
    (status ? 1 : 0) +
    (country ? 1 : 0) +
    (needsSource ? 1 : 0) +
    (neverScraped ? 1 : 0) +
    (nothingKept ? 1 : 0);

  const acceptedCount = counts.data?.by_status?.accepted ?? 0;
  const knownCount = counts.data?.by_status?.known ?? 0;

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allSelected = rows.length > 0 && rows.every((c) => selected.has(c.id));
  // Only rows on screen count: a tick hidden by a search or filter is never gathered.
  const selectedRows = rows.filter((c) => selected.has(c.id));
  const selectedIds = selectedRows.map((c) => c.id);
  const unscrapable = selectedRows.filter((c) => !hasSource(c)).length;

  const gathering =
    selectedIds.length > 0 ? selectedIds.length : acceptedCount + (refreshKnown ? knownCount : 0);

  const start = useMutation({
    mutationFn: () =>
      api.startCollectRun(
        selectedIds.length > 0
          ? { dry_run: dryRun, limit: limit ? Number(limit) : null, company_ids: selectedIds }
          : {
              dry_run: dryRun,
              refresh_known: refreshKnown,
              limit: limit ? Number(limit) : null,
              batch: batch ? Number(batch) : null,
            },
      ),
    onSuccess: ({ run_id }) => {
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.invalidateQueries({ queryKey: ["companies"] });
      navigate(`/search/results/${run_id}`);
    },
    onError: (err) => {
      setLastError(null);
      setBlocked(null);
      // 409 = a collect run is already active: blocked, not failed.
      if (err instanceof ApiError && err.status === 409) {
        setBlocked(err.detail);
      } else {
        setLastError(errorDetail(err));
      }
    },
  });

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold">Find jobs</h1>
        <p className="text-sm text-maroon-soft">
          Tick the companies to scrape, or tick none to gather every company accepted in{" "}
          <Link to="/search/review" className="text-maroon underline hover:text-maroon-soft">
            review
          </Link>
          . Companies without a hiring source are skipped — give them one under{" "}
          <Link to="/search/careers" className="text-maroon underline hover:text-maroon-soft">
            careers pages
          </Link>
          .
        </p>
      </div>

      <div className="rounded-lg border border-maroon/10 bg-cream-light p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h2 className="text-base font-medium">Gather jobs</h2>
          <span className="text-sm text-maroon-soft">
            {acceptedCount} accepted · {knownCount} known
          </span>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-2 text-maroon-soft">
            Max companies:
            <input
              type="number"
              min={1}
              value={limit}
              onChange={(e) => setLimit(e.target.value)}
              placeholder="all"
              className="w-24 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon"
            />
          </label>
          {selectedIds.length === 0 && (
            <>
              <label className="flex items-center gap-2 text-maroon-soft">
                Batch:
                <input
                  type="number"
                  min={1}
                  value={batch}
                  onChange={(e) => setBatch(e.target.value)}
                  placeholder="default"
                  className="w-24 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon"
                />
              </label>
              <label className="flex items-center gap-2 text-maroon-soft">
                <input
                  type="checkbox"
                  checked={refreshKnown}
                  onChange={(e) => setRefreshKnown(e.target.checked)}
                />
                Re-scrape known companies too
              </label>
            </>
          )}
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
            disabled={start.isPending || gathering === 0}
            onClick={() => {
              setLastError(null);
              setBlocked(null);
              start.mutate();
            }}
            className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
          >
            {start.isPending
              ? "Starting…"
              : selectedIds.length > 0
                ? `Gather selected (${gathering})`
                : `Gather jobs (${gathering})`}
          </button>
        </div>

        {gathering === 0 && (
          <p className="mt-2 text-sm text-maroon-soft">
            Nothing to gather yet — tick companies below, or accept companies in review first.
          </p>
        )}
        {unscrapable > 0 && (
          <p className="mt-2 text-sm text-maroon-soft">
            {unscrapable} selected {unscrapable === 1 ? "company has" : "companies have"} no
            hiring source and will be skipped.
          </p>
        )}
        {dryRun && (
          <p className="mt-2 text-sm text-maroon-soft">
            A dry run finds postings but writes nothing to the database and does not score.
          </p>
        )}
        {blocked && (
          <p className="mt-2 text-sm text-maroon">
            Start blocked: {blocked}. Wait for it to finish, or cancel it from the results
            screen.
          </p>
        )}
        {lastError && <p className="mt-2 text-sm text-red-800">Start failed: {lastError}</p>}
      </div>

      <ListToolbar
        search={search}
        onSearch={setSearch}
        searchPlaceholder="Search company name"
        activeFilters={activeFilters}
        onClearFilters={() => {
          setStatus(undefined);
          setCountry("");
          setNeedsSource(false);
          setNeverScraped(false);
          setNothingKept(false);
        }}
        selectedCount={selectedIds.length}
        filters={
          <>
            <label className={filterLabel}>
              Status:
              <select
                value={status ?? ""}
                onChange={(e) => setStatus(e.target.value || undefined)}
                className={filterInput}
              >
                <option value="">All</option>
                <option value="pending">Pending</option>
                <option value="accepted">Accepted</option>
                <option value="known">Known</option>
                <option value="rejected">Rejected</option>
              </select>
            </label>
            <label className={filterLabel}>
              Country:
              <input
                value={country}
                onChange={(e) => setCountry(e.target.value)}
                placeholder="any"
                className={`w-24 ${filterInput}`}
              />
            </label>
            <label className={filterLabel}>
              <input
                type="checkbox"
                checked={needsSource}
                onChange={(e) => setNeedsSource(e.target.checked)}
              />
              Needs a hiring source
            </label>
            <label className={filterLabel}>
              <input
                type="checkbox"
                checked={neverScraped}
                onChange={(e) => setNeverScraped(e.target.checked)}
              />
              Never scraped
            </label>
            <label className={filterLabel}>
              <input
                type="checkbox"
                checked={nothingKept}
                onChange={(e) => setNothingKept(e.target.checked)}
              />
              Nothing kept
            </label>
          </>
        }
      />

      {list.isLoading && <p className="text-sm text-maroon-soft">Loading…</p>}
      {list.error && (
        <p className="text-sm text-red-800">Failed to load companies: {String(list.error)}</p>
      )}

      {!list.isLoading && !list.error && (
        <table className="w-full text-left text-sm">
          <thead className="text-maroon-soft">
            <tr>
              <th className="px-2 py-1">
                <input
                  type="checkbox"
                  aria-label="Select all companies"
                  checked={allSelected}
                  onChange={() =>
                    setSelected(allSelected ? new Set() : new Set(rows.map((c) => c.id)))
                  }
                />
              </th>
              <th className="px-2 py-1">Name</th>
              <th className="px-2 py-1">Status</th>
              <th className="px-2 py-1">Country</th>
              <th className="px-2 py-1">Hiring source</th>
              <th className="px-2 py-1">Last scraped</th>
              <th className="px-2 py-1">Jobs</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => {
              const source = c.open_positions_url || c.careers_url;
              return (
                <tr key={c.id} className="border-t border-maroon/10">
                  <td className="px-2 py-1">
                    <input
                      type="checkbox"
                      aria-label={`Select company ${c.id}`}
                      checked={selected.has(c.id)}
                      onChange={() => toggle(c.id)}
                    />
                  </td>
                  <td className="px-2 py-1">{c.name}</td>
                  <td className="px-2 py-1">{c.status}</td>
                  <td className="px-2 py-1">{c.country ?? "—"}</td>
                  <td className="max-w-72 truncate px-2 py-1">
                    {source ? (
                      <a
                        href={source}
                        target="_blank"
                        rel="noreferrer"
                        className="text-maroon underline hover:text-maroon-soft"
                      >
                        {source}
                      </a>
                    ) : (
                      <span className="text-maroon-soft">none — skipped</span>
                    )}
                  </td>
                  <td
                    className={`px-2 py-1 ${keptNothing(c) ? "text-red-800" : ""}`}
                    title={dropDetail(c)}
                  >
                    {c.last_scrape_run_id != null ? (
                      <Link
                        to={`/search/results/${c.last_scrape_run_id}`}
                        className="text-maroon underline hover:text-maroon-soft"
                      >
                        {lastScrape(c)}
                      </Link>
                    ) : (
                      lastScrape(c)
                    )}
                  </td>
                  <td className="px-2 py-1">{c.job_count}</td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-2 py-3 text-maroon-soft">
                  No companies match the filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
