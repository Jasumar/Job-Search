import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "@/api/client";
import type { Company, CompanyDeleteItem } from "@/api/types";
import { ListToolbar, filterInput, filterLabel } from "@/components/ListToolbar";

const blockedText = (items: CompanyDeleteItem[]) =>
  items.map((c) => `${c.name}: ${c.reason ?? "blocked"}`).join("\n");

/**
 * Companies library: every row the system knows, with what is recorded about
 * it. Finding a careers page happens under Careers pages, and accepting a
 * company happens under Review. The one write here is delete (single or
 * bulk): it removes the company and every job under it, after a confirm that
 * names the counts.
 */
export function CompaniesPage() {
  const [status, setStatus] = useState<string | undefined>(undefined);
  const [country, setCountry] = useState("");
  const [needsSource, setNeedsSource] = useState(false);
  const [search, setSearch] = useState("");

  // No row limit: the name search runs in the browser over the full list, so a
  // cap would hide matches.
  const { data, isLoading, error } = useQuery({
    queryKey: ["companies", status, country, needsSource],
    queryFn: () =>
      api.listCompanies({
        status,
        country: country || undefined,
        no_careers: needsSource || undefined,
      }),
  });

  const needle = search.trim().toLowerCase();
  const rows: Company[] = (data ?? []).filter(
    (c) => !needle || c.name.toLowerCase().includes(needle),
  );
  const activeFilters = (status ? 1 : 0) + (country ? 1 : 0) + (needsSource ? 1 : 0);

  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [message, setMessage] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  const toggle = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const allSelected = rows.length > 0 && rows.every((c) => selected.has(c.id));
  // Only rows on screen count: a selection hidden by a search or filter is never deleted.
  const selectedVisible = rows.filter((c) => selected.has(c.id)).map((c) => c.id);

  async function deleteSelected() {
    const ids = selectedVisible;
    setMessage(null);
    setDeleting(true);
    try {
      const preview = await api.deleteCompanies(ids, true);
      if (preview.deleted.length === 0) {
        setMessage(blockedText(preview.blocked) || "Nothing to delete.");
        return;
      }
      const jobCount = preview.deleted.reduce((sum, c) => sum + c.jobs, 0);
      const names = preview.deleted.map((c) => `${c.name} (${c.jobs} jobs)`).join("\n");
      const blocked = blockedText(preview.blocked);
      const ok = window.confirm(
        `Delete ${preview.deleted.length} companies and ${jobCount} jobs? This cannot be undone.\n\n${names}` +
          (blocked ? `\n\nSkipped:\n${blocked}` : ""),
      );
      if (!ok) return;
      const result = await api.deleteCompanies(preview.deleted.map((c) => c.id));
      setSelected(new Set());
      setMessage(
        `Deleted ${result.deleted.length} companies.` +
          (result.blocked.length ? ` Skipped:\n${blockedText(result.blocked)}` : ""),
      );
      await queryClient.invalidateQueries({ queryKey: ["companies"] });
    } catch (e) {
      setMessage(`Delete failed: ${String(e)}`);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h1 className="text-lg font-semibold">Companies</h1>
        <Link
          to="/search/careers"
          className="text-sm text-maroon underline hover:text-maroon-soft"
        >
          Find careers pages →
        </Link>
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
        }}
        selectedCount={selectedVisible.length}
        deleting={deleting}
        onDeleteSelected={deleteSelected}
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
          </>
        }
      />
      {message && <p className="whitespace-pre-line text-sm text-maroon-soft">{message}</p>}

      {isLoading && <p className="text-sm text-maroon-soft">Loading…</p>}
      {error && <p className="text-sm text-red-800">Failed to load companies: {String(error)}</p>}

      {!isLoading && !error && (
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
              <th className="px-2 py-1">ID</th>
              <th className="px-2 py-1">Name</th>
              <th className="px-2 py-1">Status</th>
              <th className="px-2 py-1">Country</th>
              <th className="px-2 py-1">Careers URL</th>
              <th className="px-2 py-1">Entry</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id} className="border-t border-maroon/10">
                <td className="px-2 py-1">
                  <input
                    type="checkbox"
                    aria-label={`Select company ${c.id}`}
                    checked={selected.has(c.id)}
                    onChange={() => toggle(c.id)}
                  />
                </td>
                <td className="px-2 py-1 text-maroon-soft">{c.id}</td>
                <td className="px-2 py-1">{c.name}</td>
                <td className="px-2 py-1">{c.status}</td>
                <td className="px-2 py-1">{c.country ?? "—"}</td>
                <td className="max-w-72 truncate px-2 py-1">
                  {c.careers_url ? (
                    <a
                      href={c.careers_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-maroon underline hover:text-maroon-soft"
                    >
                      {c.careers_url}
                    </a>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="max-w-72 truncate px-2 py-1">
                  {c.open_positions_url ? (
                    <a
                      href={c.open_positions_url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-maroon underline hover:text-maroon-soft"
                    >
                      {c.open_positions_url}
                    </a>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="px-2 py-3 text-maroon-soft">
                  {needsSource
                    ? "No companies without a careers page match the filters."
                    : "No companies match the filters."}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
