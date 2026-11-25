import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import type { Job } from "@/api/types";
import { ListToolbar, filterInput, filterLabel } from "@/components/ListToolbar";

export function JobsPage() {
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState<string | undefined>(undefined);
  const [country, setCountry] = useState("");
  const [suspectsOnly, setSuspectsOnly] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const queryClient = useQueryClient();

  // No row limit: the word search runs in the browser over the full list, so a
  // cap would hide matches.
  const { data, isLoading, error } = useQuery({
    queryKey: ["jobs", status, country],
    queryFn: () =>
      api.listJobs({
        status,
        country: country || undefined,
      }),
  });

  // Deleting is the one write this library allows: a posting that is not a job
  // has no other exit. The server keeps its fingerprint, so a later collect run
  // cannot re-add it.
  const remove = useMutation({
    mutationFn: (jobId: number) => api.deleteJob(jobId),
    onSuccess: () => {
      setLastError(null);
      queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
    onError: (err) => setLastError(String(err)),
  });

  const words = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const rows: Job[] = (data ?? []).filter((j) => {
    if (suspectsOnly && !j.suspect_reason) return false;
    const haystack = [j.normalized_title, j.normalized_company, j.normalized_location, j.url]
      .join(" ")
      .toLowerCase();
    return words.every((w) => haystack.includes(w));
  });
  const activeFilters =
    (status ? 1 : 0) +
    (country ? 1 : 0) +
    (suspectsOnly ? 1 : 0);

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allSelected = rows.length > 0 && rows.every((j) => selected.has(j.id));
  // Only rows on screen count: a selection hidden by a search or filter is never deleted.
  const selectedVisible = rows.filter((j) => selected.has(j.id)).map((j) => j.id);

  async function deleteSelected() {
    const ids = selectedVisible;
    if (!window.confirm(`Delete ${ids.length} jobs? They will not be added back by later searches.`))
      return;
    setDeleting(true);
    setLastError(null);
    const failed: string[] = [];
    for (const id of ids) {
      try {
        await api.deleteJob(id);
      } catch (e) {
        failed.push(`${id}: ${String(e)}`);
      }
    }
    setSelected(new Set());
    if (failed.length) setLastError(failed.join("\n"));
    await queryClient.invalidateQueries({ queryKey: ["jobs"] });
    setDeleting(false);
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Jobs</h1>
      <p className="text-sm text-maroon-soft">
        Collected postings. Delete removes a row and remembers it, so no later
        search adds it back. Scoring belongs to Job matching.
      </p>

      <ListToolbar
        search={search}
        onSearch={setSearch}
        searchPlaceholder="Search title, company, location"
        activeFilters={activeFilters}
        onClearFilters={() => {
          setStatus(undefined);
          setCountry("");
          setSuspectsOnly(false);
        }}
        selectedCount={selectedVisible.length}
        deleting={deleting}
        onDeleteSelected={deleteSelected}
        filters={
          <>
            <label className={filterLabel}>
              Status:
              <input
                value={status ?? ""}
                onChange={(e) => setStatus(e.target.value || undefined)}
                placeholder="new"
                className={`w-28 ${filterInput}`}
              />
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
                checked={suspectsOnly}
                onChange={(e) => setSuspectsOnly(e.target.checked)}
              />
              Suspected non-postings only
            </label>
          </>
        }
      />

      {isLoading && <p className="text-sm text-maroon-soft">Loading…</p>}
      {error && <p className="text-sm text-red-800">Failed to load jobs: {String(error)}</p>}
      {lastError && (
        <p className="whitespace-pre-line text-sm text-red-800">Delete failed: {lastError}</p>
      )}

      {!isLoading && !error && (
        <table className="w-full text-left text-sm">
          <thead className="text-maroon-soft">
            <tr>
              <th className="px-2 py-1">
                <input
                  type="checkbox"
                  aria-label="Select all jobs"
                  checked={allSelected}
                  onChange={() =>
                    setSelected(allSelected ? new Set() : new Set(rows.map((j) => j.id)))
                  }
                />
              </th>
              <th className="px-2 py-1">ID</th>
              <th className="px-2 py-1">Title</th>
              <th className="px-2 py-1">Company</th>
              <th className="px-2 py-1">Location</th>
              <th className="px-2 py-1">Tailored</th>
              <th className="px-2 py-1" />
            </tr>
          </thead>
          <tbody>
            {rows.map((j) => (
              <tr key={j.id} className="border-t border-maroon/10">
                <td className="px-2 py-1">
                  <input
                    type="checkbox"
                    aria-label={`Select job ${j.id}`}
                    checked={selected.has(j.id)}
                    onChange={() => toggle(j.id)}
                  />
                </td>
                <td className="px-2 py-1 text-maroon-soft">{j.id}</td>
                <td className="max-w-72 truncate px-2 py-1">
                  <a
                    href={j.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-maroon underline hover:text-maroon-soft"
                  >
                    {j.normalized_title ?? j.url}
                  </a>
                </td>
                <td className="px-2 py-1">{j.normalized_company ?? "—"}</td>
                <td className="px-2 py-1">{j.normalized_location ?? "—"}</td>
                <td className="px-2 py-1">{j.has_applications ? "yes" : "—"}</td>
                <td className="px-2 py-1 text-right">
                  <button
                    type="button"
                    aria-label={`Delete job ${j.id}`}
                    disabled={remove.isPending}
                    onClick={() => {
                      // Irreversible from the UI, so one confirmation.
                      if (!window.confirm(`Delete "${j.normalized_title ?? j.url}"?`)) return;
                      remove.mutate(j.id);
                    }}
                    className="rounded border border-maroon/20 px-2 py-1 text-maroon hover:bg-cream disabled:opacity-50"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-2 py-3 text-maroon-soft">
                  No jobs match the filters.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
