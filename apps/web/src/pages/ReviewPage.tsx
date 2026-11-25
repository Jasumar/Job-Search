import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "@/api/client";
import type { Company, ReviewStatus } from "@/api/types";

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err);
}

function resolvedAt(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

/**
 * What the system thinks each link is, stated on the link itself.
 *
 * The review gate accepts a *company*; the resolved entry is evidence, never an
 * input. Two links can exist: the careers page the search surfaced, and the
 * open-positions page the entry descent resolved under it. When they are the
 * same URL, the careers page itself listed the postings.
 */
function EntrySuspects({ company }: { company: Company }) {
  const entry = company.open_positions_url;
  const landing = company.open_positions_landing_url;
  const linkText = company.open_positions_link_text?.trim() || null;

  if (!entry) {
    return (
      <span className="text-maroon-soft">
        No entry resolved — no page under the search result listed postings.
      </span>
    );
  }

  return (
    <div className="space-y-1">
      {landing && landing !== entry && (
        <div>
          <span className="mr-1 rounded bg-maroon/10 px-1.5 py-0.5 text-xs text-maroon-soft">
            Careers page (search result)
          </span>
          <a
            href={landing}
            target="_blank"
            rel="noreferrer"
            className="block truncate text-maroon underline hover:text-maroon-soft"
          >
            {landing}
          </a>
        </div>
      )}
      <div>
        <span className="mr-1 rounded bg-lime/40 px-1.5 py-0.5 text-xs text-maroon">
          Open positions page — system suspect
        </span>
        <a
          href={entry}
          target="_blank"
          rel="noreferrer"
          className="block truncate text-maroon underline hover:text-maroon-soft"
        >
          {entry}
        </a>
        <div className="text-xs text-maroon-soft">
          {linkText === "sitemap"
            ? "the site's sitemap lists these postings"
            : linkText
              ? `linked as “${linkText}”`
              : "the page the search surfaced itself lists postings"}
        </div>
      </div>
    </div>
  );
}

/**
 * Demo review screen (master_plan §The Demo, plan-frontend.md §Demo review
 * screen). Two scopes, because the two questions have different answers:
 *
 * - Candidates: `pending` rows, the human gate. Acceptance writes status only,
 *   singly or in batch; the reviewable evidence is the resolved open-positions
 *   entry (`open_positions_url`, read-only), opened in a real browser tab. Each
 *   link states what the system believes it is — the careers page the search
 *   surfaced, or the open-positions page resolved under it.
 * - Resolved entries: `accepted` + `known` rows that carry an entry. Read-only
 *   in v1 — `careers_search` runs only over accepted companies, so a pending
 *   row can never show its entry, and the entry is corrected by re-running the
 *   search (a hit overwrites it), not by a second gate here.
 *
 * `includeNoEntry` drops the demo's `has_entry=1` view so pending rows that
 * never resolved an entry can still be rejected or given a URL — otherwise
 * they would be un-reviewable forever.
 */
export function ReviewPage() {
  const [scope, setScope] = useState<"candidates" | "resolved">("candidates");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-lg font-semibold">Review</h1>
          <p className="text-sm text-maroon-soft">
            Accept or reject what the careers searches found. Accepted companies go on to{" "}
            <Link to="/search/jobs" className="text-maroon underline hover:text-maroon-soft">
              Find jobs
            </Link>
            .
          </p>
        </div>
        <div role="tablist" aria-label="Review scope" className="flex gap-1 rounded border border-maroon/20 p-1">
          {(
            [
              ["candidates", "Candidates"],
              ["resolved", "Resolved entries"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={scope === value}
              onClick={() => setScope(value)}
              className={`rounded px-3 py-1 text-sm ${
                scope === value ? "bg-maroon text-cream-light" : "text-maroon hover:bg-maroon/10"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {scope === "candidates" ? <CandidatesScope /> : <ResolvedEntriesScope />}
    </div>
  );
}

function CandidatesScope() {
  const [includeNoEntry, setIncludeNoEntry] = useState(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [cursor, setCursor] = useState(0);
  const [careersUrl, setCareersUrl] = useState("");
  const [openPositionsUrl, setOpenPositionsUrl] = useState("");
  const [lastError, setLastError] = useState<string | null>(null);
  const [batchNote, setBatchNote] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const queryKey = ["review", "pending", includeNoEntry] as const;
  const { data, isLoading, error } = useQuery({
    queryKey,
    queryFn: () =>
      api.listCompanies({
        status: "pending",
        order: "id_desc",
        has_entry: includeNoEntry ? undefined : true,
      }),
  });

  const rows: Company[] = data ?? [];
  const clampedCursor = Math.min(cursor, Math.max(rows.length - 1, 0));
  const current = rows[clampedCursor];

  const review = useMutation({
    mutationFn: ({
      id,
      status,
      careersUrl,
      openPositionsUrl,
    }: {
      id: number;
      status: ReviewStatus;
      careersUrl?: string;
      openPositionsUrl?: string;
    }) =>
      api.reviewCompany(id, {
        status,
        careers_url: careersUrl ?? null,
        open_positions_url: openPositionsUrl ?? null,
      }),
    onMutate: async ({ id }) => {
      // Only the decided row is optimistic; a batch waits for its refetch.
      await queryClient.cancelQueries({ queryKey });
      const previous = queryClient.getQueryData<Company[]>(queryKey);
      queryClient.setQueryData<Company[]>(queryKey, (old = []) => old.filter((c) => c.id !== id));
      return { previous };
    },
    onError: (err, _vars, context) => {
      if (context?.previous) queryClient.setQueryData(queryKey, context.previous);
      setLastError(errorDetail(err));
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["review"] });
    },
  });

  const batch = useMutation({
    mutationFn: ({ ids, status }: { ids: number[]; status: "accepted" | "rejected" }) =>
      api.reviewBatch({ company_ids: ids, status }),
    onSuccess: ({ updated, unknown_ids }) => {
      setSelected([]);
      setBatchNote(
        unknown_ids.length > 0
          ? `Updated ${updated} rows; ${unknown_ids.length} id(s) no longer exist — list refreshed.`
          : `Updated ${updated} rows.`,
      );
      queryClient.invalidateQueries({ queryKey: ["review"] });
    },
    onError: (err) => setLastError(errorDetail(err)),
  });

  const decide = useCallback(
    (
      status: ReviewStatus,
      urls?: { careersUrl?: string; openPositionsUrl?: string },
    ) => {
      if (!current || review.isPending) return;
      review.mutate({
        id: current.id,
        status,
        careersUrl: status === "accepted" ? urls?.careersUrl || undefined : undefined,
        openPositionsUrl: status === "accepted" ? urls?.openPositionsUrl || undefined : undefined,
      });
      setCareersUrl("");
      setOpenPositionsUrl("");
      setLastError(null);
      setBatchNote(null);
    },
    [current, review],
  );

  // Moving to another row resets the URL fields (derived-state reset during
  // render, as the previous queue did).
  const [lastCursor, setLastCursor] = useState(clampedCursor);
  if (lastCursor !== clampedCursor) {
    setLastCursor(clampedCursor);
    setCareersUrl("");
    setOpenPositionsUrl("");
  }

  // Keyboard-first review: j/k move, a/r decide (review speed dominates).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) return;
      switch (e.key) {
        case "j":
          setCursor((c) => Math.min(c + 1, Math.max(rows.length - 1, 0)));
          break;
        case "k":
          setCursor((c) => Math.max(c - 1, 0));
          break;
        case "a":
          decide("accepted");
          break;
        case "r":
          decide("rejected");
          break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [decide, rows.length]);

  const toggle = (id: number) =>
    setSelected((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));

  const visibleIds = rows.map((c) => c.id);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id));
  const togglePage = () => setSelected(allVisibleSelected ? [] : visibleIds);

  const changeView = (value: boolean) => {
    // The selection belongs to the list it was made in.
    setSelected([]);
    setCursor(0);
    setCareersUrl("");
    setOpenPositionsUrl("");
    setLastError(null);
    setBatchNote(null);
    setIncludeNoEntry(value);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-maroon-soft">Pending candidates</h2>
        <label className="flex items-center gap-2 text-sm text-maroon-soft">
          <input
            type="checkbox"
            checked={includeNoEntry}
            onChange={(e) => changeView(e.target.checked)}
          />
          Include rows without an entry
        </label>
      </div>

      <p className="text-xs text-maroon-soft">
        Accepting records the company's status only. Each link below is labelled with what
        the system believes it is: the careers page the search surfaced, and the open
        positions page it resolved under that page by finding postings on it.
      </p>

      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-maroon/10 bg-cream-light p-3 text-sm">
        <button
          type="button"
          onClick={togglePage}
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
          disabled={selected.length === 0 || batch.isPending}
          onClick={() => {
            setLastError(null);
            setBatchNote(null);
            batch.mutate({ ids: selected, status: "accepted" });
          }}
          className="rounded bg-lime px-4 py-1.5 text-sm font-medium text-maroon hover:bg-lime/80 disabled:opacity-50"
        >
          {batch.isPending ? "Accepting…" : `Accept selected (${selected.length})`}
        </button>
        <button
          type="button"
          disabled={selected.length === 0 || batch.isPending}
          onClick={() => {
            setLastError(null);
            setBatchNote(null);
            batch.mutate({ ids: selected, status: "rejected" });
          }}
          className="rounded border border-maroon px-4 py-1.5 text-sm font-medium text-maroon hover:bg-maroon/10 disabled:opacity-50"
        >
          Reject selected ({selected.length})
        </button>
        <span className="self-center text-xs text-maroon-soft">
          j/k move · a accept · r reject
        </span>
      </div>

      {current && (
        <div className="flex flex-wrap items-center gap-3 rounded-lg border border-maroon/10 bg-cream-light p-3 text-sm">
          <span className="text-maroon-soft">For {current.name}:</span>
          <label className="flex items-center gap-2 text-maroon-soft">
            Careers page:
            <input
              aria-label="Careers page URL"
              value={careersUrl}
              onChange={(e) => setCareersUrl(e.target.value)}
              placeholder="https://…"
              className="w-64 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-sm text-maroon focus:border-maroon focus:outline-none"
            />
          </label>
          <label className="flex items-center gap-2 text-maroon-soft">
            Open positions:
            <input
              aria-label="Open positions page URL"
              value={openPositionsUrl}
              onChange={(e) => setOpenPositionsUrl(e.target.value)}
              placeholder="https://…"
              className="w-64 rounded border border-maroon/20 bg-cream-light px-2 py-1 text-sm text-maroon focus:border-maroon focus:outline-none"
            />
          </label>
          <button
            type="button"
            disabled={review.isPending || (!careersUrl && !openPositionsUrl)}
            onClick={() => decide("accepted", { careersUrl, openPositionsUrl })}
            className="rounded bg-maroon px-3 py-1 text-xs font-medium text-cream-light hover:bg-maroon-soft disabled:opacity-50"
          >
            Accept with URLs
          </button>
          <span className="text-xs text-maroon-soft">
            Paste one or both. If you give only one, the other is accepted with the same URL.
          </span>
        </div>
      )}

      {batchNote && <p className="text-sm text-maroon-soft">{batchNote}</p>}
      {lastError && <p className="text-sm text-red-800">Review failed: {lastError}</p>}
      {isLoading && <p className="text-sm text-maroon-soft">Loading pending candidates…</p>}
      {error && <p className="text-sm text-red-800">Failed to load pending rows: {String(error)}</p>}

      {!isLoading && !error && (
        <table className="w-full text-left text-sm">
          <thead className="text-maroon-soft">
            <tr>
              <th className="w-8 px-2 py-1" />
              <th className="px-2 py-1">Company</th>
              <th className="px-2 py-1">System suspects</th>
              <th className="px-2 py-1">Source</th>
              <th className="px-2 py-1">Decision</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c, index) => (
              <tr
                key={c.id}
                onClick={() => setCursor(index)}
                className={`border-t border-maroon/10 ${
                  index === clampedCursor ? "bg-lime/20" : ""
                }`}
              >
                <td className="px-2 py-2">
                  <input
                    type="checkbox"
                    aria-label={`Select ${c.name}`}
                    checked={selected.includes(c.id)}
                    onChange={() => toggle(c.id)}
                    onClick={(e) => e.stopPropagation()}
                  />
                </td>
                <td className="px-2 py-2">
                  <div className="font-medium">{c.name}</div>
                  <div className="text-xs text-maroon-soft">{c.country ?? "—"}</div>
                </td>
                <td className="max-w-96 px-2 py-2">
                  <EntrySuspects company={c} />
                </td>
                <td className="px-2 py-2">
                  {c.run_id != null ? (
                    <Link to={`/search/results/${c.run_id}`} className="text-maroon underline hover:text-maroon-soft">
                      Run #{c.run_id}
                    </Link>
                  ) : (
                    <span className="text-maroon-soft">discovery</span>
                  )}
                </td>
                <td className="px-2 py-2">
                  <div className="flex gap-2">
                    <button
                      type="button"
                      disabled={review.isPending && current?.id === c.id}
                      onClick={() => {
                        setCursor(index);
                        review.mutate({ id: c.id, status: "accepted" });
                      }}
                      className="rounded bg-lime px-3 py-1 text-xs font-medium text-maroon hover:bg-lime/80 disabled:opacity-50"
                    >
                      Accept
                    </button>
                    <button
                      type="button"
                      disabled={review.isPending && current?.id === c.id}
                      onClick={() => {
                        setCursor(index);
                        review.mutate({ id: c.id, status: "rejected" });
                      }}
                      className="rounded border border-maroon px-3 py-1 text-xs text-maroon hover:bg-maroon/10 disabled:opacity-50"
                    >
                      Reject
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={5} className="px-2 py-3 text-maroon-soft">
                  {includeNoEntry
                    ? "No pending companies. Run name collection to find more."
                    : "No pending rows with a resolved entry. Resolve entries with a careers search, or include rows without one."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

    </div>
  );
}

/**
 * Resolved entries: what a careers search actually found, and which run found
 * it. Display only — the entry is written by `record_open_positions_entry` and
 * corrected by re-running the search, so there is no second gate here.
 */
function ResolvedEntriesScope() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["review", "resolved"],
    queryFn: () =>
      api.listCompanies({
        statuses: ["accepted", "known"],
        order: "id_desc",
        has_entry: true,
      }),
  });

  const rows: Company[] = data ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-sm font-medium text-maroon-soft">Resolved entries</h2>
        <span className="text-xs text-maroon-soft">
          Read-only. To correct an entry, re-run a careers search over the company — a hit
          overwrites it.
        </span>
      </div>

      {isLoading && <p className="text-sm text-maroon-soft">Loading resolved entries…</p>}
      {error && <p className="text-sm text-red-800">Failed to load resolved entries: {String(error)}</p>}

      {!isLoading && !error && (
        <table className="w-full text-left text-sm">
          <thead className="text-maroon-soft">
            <tr>
              <th className="px-2 py-1">Company</th>
              <th className="px-2 py-1">System suspects</th>
              <th className="px-2 py-1">Resolved by</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id} className="border-t border-maroon/10">
                <td className="px-2 py-2">
                  <div className="font-medium">{c.name}</div>
                  <div className="text-xs text-maroon-soft">
                    {c.country ?? "—"} · {c.status}
                  </div>
                </td>
                <td className="max-w-96 px-2 py-2">
                  <EntrySuspects company={c} />
                </td>
                <td className="px-2 py-2">
                  {c.entry_run_id != null ? (
                    <Link
                      to={`/search/results/${c.entry_run_id}`}
                      className="text-maroon underline hover:text-maroon-soft"
                    >
                      Run #{c.entry_run_id}
                    </Link>
                  ) : (
                    <span className="text-maroon-soft">no run recorded</span>
                  )}
                  <div className="text-xs text-maroon-soft">{resolvedAt(c.entry_resolved_at)}</div>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={3} className="px-2 py-3 text-maroon-soft">
                  No resolved entries yet. A careers search writes one per company it resolves.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
