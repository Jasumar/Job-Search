import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import type { Company } from "@/api/types";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err);
}

function blockNote(err: unknown, onBlocked: (detail: string) => void): string {
  // 409 = a run of this type is already active: a blocked start, not a failure.
  if (err instanceof ApiError && err.status === 409) {
    onBlocked(err.detail);
    return "";
  }
  return errorDetail(err);
}

/**
 * Careers pages: where a company's posting page is found.
 *
 * Two entry points feed one output. Pasted listing pages yield company names
 * (name_collect); the companies list yields careers pages for rows that still
 * need one (careers_search). Both write reviewable candidates, so both hand
 * over to Review, not to the library.
 */
export function CareersPage() {
  return (
    <div className="space-y-6">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold">Careers pages</h1>
        <p className="text-sm text-maroon-soft">
          Find the page each company posts its jobs on. Both searches below produce
          candidates for{" "}
          <Link to="/search/review" className="text-maroon underline hover:text-maroon-soft">
            review
          </Link>
          ; nothing is scraped until you accept it there.
        </p>
      </div>

      <NameCollectPanel />
      <CareersSearchPanel />
    </div>
  );
}

/**
 * Paste sheet half: every pasted line becomes a `sources` row plus one
 * `run_sources` manifest row; the worker owns all fetching.
 */
function NameCollectPanel() {
  const [text, setText] = useState("");
  const [batchSize, setBatchSize] = useState("");
  const [pageCeiling, setPageCeiling] = useState("");
  const [lastError, setLastError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const urls = useMemo(
    () =>
      text
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => line.length > 0),
    [text],
  );

  const start = useMutation({
    mutationFn: () =>
      api.startNameCollectRun({
        urls,
        batch_size: batchSize ? Number(batchSize) : null,
        page_ceiling: pageCeiling ? Number(pageCeiling) : null,
      }),
    onSuccess: ({ run_id }) => {
      setText("");
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/search/results/${run_id}`);
    },
    onError: (err) => {
      setLastError(null);
      setBlocked(null);
      setLastError(blockNote(err, setBlocked));
    },
  });

  return (
    <div className="rounded-lg border border-maroon/10 bg-cream-light p-4">
      <h2 className="text-base font-medium">Collect company names from listing pages</h2>
      <p className="mt-1 text-sm text-maroon-soft">
        Paste one listing-page URL per line. Each page becomes a source; the worker
        extracts the companies it lists as pending candidates. ATS board URLs are
        refused — paste the employer’s own listing page.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        aria-label="Listing page URLs"
        placeholder={"https://example.com/open-positions\nhttps://example.org/careers/all"}
        className="mt-3 w-full rounded border border-maroon/20 bg-cream-light px-2 py-1.5 font-mono text-sm text-maroon focus:border-maroon focus:outline-none"
      />
      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2 text-maroon-soft">
          Batch size:
          <input
            type="number"
            min={1}
            value={batchSize}
            onChange={(e) => setBatchSize(e.target.value)}
            placeholder="default"
            className="w-24 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon"
          />
        </label>
        <label className="flex items-center gap-2 text-maroon-soft">
          Page ceiling:
          <input
            type="number"
            min={1}
            value={pageCeiling}
            onChange={(e) => setPageCeiling(e.target.value)}
            placeholder="default"
            className="w-24 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon"
          />
        </label>
        <span className="text-maroon-soft">
          {urls.length} {urls.length === 1 ? "URL" : "URLs"}
        </span>
        <button
          type="button"
          disabled={urls.length === 0 || start.isPending}
          onClick={() => {
            setLastError(null);
            setBlocked(null);
            start.mutate();
          }}
          className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
        >
          {start.isPending ? "Starting…" : "Start name collection"}
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
  );
}

/**
 * Selection half: rows that still need a hiring source — no `careers_url`, and
 * no resolved entry on a row the review gate accepted. Only these may be
 * searched, so this list is always the `no_careers` view.
 */
function CareersSearchPanel() {
  const [country, setCountry] = useState("");
  const [limit, setLimit] = useState(100);
  const [selected, setSelected] = useState<number[]>([]);
  const [startError, setStartError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["companies", "no-careers", country, limit],
    queryFn: () =>
      api.listCompanies({
        no_careers: true,
        country: country || undefined,
        limit,
      }),
  });

  const rows: Company[] = data ?? [];

  const start = useMutation({
    mutationFn: (companyIds: number[]) =>
      api.startCareersSearchRun({ company_ids: companyIds }),
    onSuccess: ({ run_id }) => {
      setSelected([]);
      queryClient.invalidateQueries({ queryKey: ["companies"] });
      queryClient.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/search/results/${run_id}`);
    },
    onError: (err) => {
      setStartError(null);
      setBlocked(null);
      // 422 means the selection is stale (an id gained a careers_url), so
      // refresh the list the owner picked from.
      if (err instanceof ApiError && err.status === 422) {
        setStartError(`Selection is stale — the list was refreshed. ${err.detail}`);
        setSelected([]);
        queryClient.invalidateQueries({ queryKey: ["companies"] });
        return;
      }
      setStartError(blockNote(err, setBlocked));
    },
  });

  const toggle = (id: number) => {
    setSelected((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
  };

  const visibleIds = rows.map((c) => c.id);
  const allVisibleSelected =
    visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id));
  const toggleAll = () => setSelected(allVisibleSelected ? [] : visibleIds);

  const changeFilter = (fn: () => void) => {
    // The selection belongs to the current list; any filter change invalidates it.
    setSelected([]);
    setStartError(null);
    setBlocked(null);
    fn();
  };

  return (
    <div className="rounded-lg border border-maroon/10 bg-cream-light p-4">
      <h2 className="text-base font-medium">Search companies that need a careers page</h2>
      <p className="mt-1 text-sm text-maroon-soft">
        These companies have no hiring source yet: no careers page recorded, and no
        resolved posting page on an accepted row.
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
        <label className="flex items-center gap-2 text-maroon-soft">
          Country:
          <input
            value={country}
            onChange={(e) => changeFilter(() => setCountry(e.target.value))}
            placeholder="any"
            className="w-20 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon"
          />
        </label>
        <label className="flex items-center gap-2 text-maroon-soft">
          Limit:
          <input
            type="number"
            min={1}
            value={limit}
            onChange={(e) => changeFilter(() => setLimit(Number(e.target.value) || 100))}
            className="w-20 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon"
          />
        </label>
        <button
          type="button"
          onClick={toggleAll}
          disabled={visibleIds.length === 0}
          className="rounded border border-maroon/20 px-3 py-1 text-maroon hover:bg-cream disabled:opacity-50"
        >
          {allVisibleSelected ? "Clear page" : "Select page"}
        </button>
        <span className="text-maroon-soft">
          {selected.length} of {rows.length} selected
        </span>
        <button
          type="button"
          disabled={selected.length === 0 || start.isPending}
          onClick={() => {
            setStartError(null);
            setBlocked(null);
            start.mutate(selected);
          }}
          className="rounded bg-maroon px-4 py-1.5 text-sm font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
        >
          {start.isPending ? "Starting…" : `Start careers search (${selected.length})`}
        </button>
      </div>

      {blocked && (
        <p className="mt-2 text-sm text-maroon">
          Start blocked: {blocked}. Wait for it to finish, or cancel it from the results
          screen.
        </p>
      )}
      {startError && <p className="mt-2 text-sm text-red-800">Start failed: {startError}</p>}
      {isLoading && <p className="mt-2 text-sm text-maroon-soft">Loading companies…</p>}
      {error && (
        <p className="mt-2 text-sm text-red-800">Failed to load companies: {String(error)}</p>
      )}

      {!isLoading && !error && (
        <table className="mt-3 w-full text-left text-sm">
          <thead className="text-maroon-soft">
            <tr>
              <th className="w-8 px-2 py-1" />
              <th className="px-2 py-1">Name</th>
              <th className="px-2 py-1">Status</th>
              <th className="px-2 py-1">Country</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id} className="border-t border-maroon/10">
                <td className="px-2 py-1">
                  <input
                    type="checkbox"
                    aria-label={`Select ${c.name}`}
                    checked={selected.includes(c.id)}
                    onChange={() => toggle(c.id)}
                  />
                </td>
                <td className="px-2 py-1">{c.name}</td>
                <td className="px-2 py-1">{c.status}</td>
                <td className="px-2 py-1">{c.country ?? "—"}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={4} className="px-2 py-3 text-maroon-soft">
                  No companies need a careers page right now.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
