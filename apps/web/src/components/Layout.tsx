import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

/**
 * Left drawer, grouped by what the user is doing rather than by what the
 * backend calls the job. Two paths produce companies: the profile path
 * ("Job matching") and the pasted-source path ("Job search"). Both then share
 * Review -> Find jobs -> Results. "Advanced" holds the raw run log, which is a
 * debugging surface and not a destination anyone should have to reason about.
 *
 * Headed groups are dropdowns: the heading toggles its child links. A group
 * starts open only when the current route lives inside it, so the drawer
 * stays short without hiding where you are.
 */
interface NavItem {
  to: string;
  label: string;
  /** Section link: active only on an exact match, so a child route wins. */
  end?: boolean;
}

interface NavSection {
  heading?: string;
  items: NavItem[];
}

const DRAWER_SECTIONS: NavSection[] = [
  {
    items: [{ to: "/profile", label: "My information" }],
  },
  {
    heading: "Job matching",
    items: [
      { to: "/match", label: "Start a match", end: true },
      { to: "/matches", label: "Matches" },
    ],
  },
  {
    heading: "Job search",
    items: [
      { to: "/search/careers", label: "Careers pages" },
      { to: "/search/review", label: "Review" },
      { to: "/search/jobs", label: "Find jobs" },
      { to: "/search/results", label: "Results" },
    ],
  },
  {
    items: [
      { to: "/companies", label: "Companies" },
      { to: "/jobs", label: "Jobs" },
    ],
  },
  {
    heading: "Advanced",
    items: [{ to: "/runs", label: "Runs" }],
  },
];

function sectionKey(section: NavSection, index: number): string {
  return section.heading ?? `section-${index}`;
}

/** Route belongs to a group when it is one of the group's destinations. */
function ownsRoute(section: NavSection, pathname: string): boolean {
  return section.items.some(
    (item) => pathname === item.to || pathname.startsWith(`${item.to}/`),
  );
}

function initialExpanded(pathname: string): Record<string, boolean> {
  const expanded: Record<string, boolean> = {};
  DRAWER_SECTIONS.forEach((section, index) => {
    if (section.heading) expanded[sectionKey(section, index)] = ownsRoute(section, pathname);
  });
  return expanded;
}

export function Layout() {
  const [open, setOpen] = useState(false);
  const location = useLocation();
  // Only headed groups are collapsible; the rest are always visible.
  const [expanded, setExpanded] = useState<Record<string, boolean>>(() =>
    initialExpanded(location.pathname),
  );

  const openDrawer = () => {
    setExpanded(initialExpanded(location.pathname));
    setOpen(true);
  };

  const toggleSection = (key: string) =>
    setExpanded((current) => ({ ...current, [key]: !current[key] }));

  return (
    <div className="min-h-screen bg-cream text-maroon">
      {/* Three-column grid: hamburger left, brand truly centred, empty right
          column mirrors the button width so the centring is exact. */}
      <header className="grid grid-cols-[2.5rem_1fr_2.5rem] items-center px-4 py-3">
        <button
          type="button"
          aria-label="Open menu"
          aria-expanded={open}
          onClick={openDrawer}
          className="flex h-10 w-10 flex-col items-center justify-center gap-1.5 rounded-md hover:bg-cream-light"
        >
          <span className="h-0.5 w-5 bg-maroon" />
          <span className="h-0.5 w-5 bg-maroon" />
          <span className="h-0.5 w-5 bg-maroon" />
        </button>
        <NavLink
          to="/"
          className="justify-self-center text-lg font-bold tracking-tight text-maroon"
        >
          Job Search
        </NavLink>
      </header>

      {/* Slide-out drawer; clicking the dimmed backdrop closes it. */}
      {open && (
        <div className="fixed inset-0 z-50">
          <div
            aria-hidden="true"
            onClick={() => setOpen(false)}
            className="absolute inset-0 bg-maroon/30"
          />
          <nav
            aria-label="Main menu"
            className="absolute inset-y-0 left-0 flex w-64 flex-col gap-3 overflow-y-auto bg-cream-light p-4 shadow-lg"
          >
            <button
              type="button"
              aria-label="Close menu"
              onClick={() => setOpen(false)}
              className="self-end rounded px-2 py-1 text-sm text-maroon-soft hover:bg-cream"
            >
              ✕
            </button>
            {DRAWER_SECTIONS.map((section, index) => {
              const key = sectionKey(section, index);
              const isExpanded = section.heading ? Boolean(expanded[key]) : true;
              return (
                <div key={key} className="flex flex-col gap-1">
                  {section.heading && (
                    <button
                      type="button"
                      aria-expanded={isExpanded}
                      aria-controls={`drawer-${key}`}
                      onClick={() => toggleSection(key)}
                      className="flex items-center justify-between rounded px-3 py-1 text-xs font-medium tracking-wide text-maroon-soft uppercase hover:bg-cream"
                    >
                      <span>{section.heading}</span>
                      <span aria-hidden="true">{isExpanded ? "▾" : "▸"}</span>
                    </button>
                  )}
                  <div
                    id={`drawer-${key}`}
                    hidden={!isExpanded}
                    className="flex flex-col gap-1"
                  >
                    {section.items.map((item) => (
                      <NavLink
                        key={item.to}
                        to={item.to}
                        end={item.end}
                        onClick={() => setOpen(false)}
                        className={({ isActive }) =>
                          `rounded px-3 py-2 text-sm ${
                            isActive
                              ? "bg-lime font-medium text-maroon"
                              : "text-maroon hover:bg-cream"
                          }`
                        }
                      >
                        {item.label}
                      </NavLink>
                    ))}
                  </div>
                </div>
              );
            })}
          </nav>
        </div>
      )}

      <main className="mx-auto max-w-5xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}
