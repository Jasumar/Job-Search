import { useState, type ReactNode } from "react";

/**
 * Shared header row for the Companies and Jobs libraries: a name/word search
 * on the left, a filter icon that opens a panel of the less used filters, and
 * the bulk delete button on the right.
 */
export function ListToolbar(props: {
  search: string;
  onSearch: (value: string) => void;
  searchPlaceholder: string;
  /** Number of filters currently narrowing the list, shown on the icon. */
  activeFilters: number;
  onClearFilters: () => void;
  filters: ReactNode;
  selectedCount: number;
  deleting?: boolean;
  /** Omit on a list with no bulk delete (Find jobs): the button is not drawn. */
  onDeleteSelected?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3 text-sm">
        <input
          type="search"
          aria-label="Search"
          value={props.search}
          onChange={(e) => props.onSearch(e.target.value)}
          placeholder={props.searchPlaceholder}
          className="w-72 max-w-full rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon"
        />
        <button
          type="button"
          aria-label="Filters"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-1 rounded border border-maroon/20 px-2 py-1 text-maroon hover:bg-cream"
        >
          <svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor" aria-hidden="true">
            <path d="M3 4h14l-5.5 6.5V16l-3-1.5v-4L3 4z" />
          </svg>
          {props.activeFilters > 0 && (
            <span className="rounded-full bg-maroon px-1.5 text-xs text-cream-light">
              {props.activeFilters}
            </span>
          )}
        </button>
        {props.onDeleteSelected && (
          <button
            type="button"
            disabled={props.selectedCount === 0 || props.deleting}
            onClick={props.onDeleteSelected}
            className="ml-auto rounded border border-red-800 px-3 py-1 text-red-800 disabled:opacity-40"
          >
            Delete selected ({props.selectedCount})
          </button>
        )}
      </div>
      {open && (
        <div
          role="group"
          aria-label="Filter options"
          className="flex flex-wrap items-center gap-4 rounded border border-maroon/20 bg-cream-light p-3 text-sm"
        >
          {props.filters}
          {props.activeFilters > 0 && (
            <button
              type="button"
              onClick={props.onClearFilters}
              className="text-maroon underline hover:text-maroon-soft"
            >
              Clear filters
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export const filterLabel = "flex items-center gap-2 text-maroon-soft";
export const filterInput =
  "rounded border border-maroon/20 bg-cream-light px-2 py-1 text-maroon";
