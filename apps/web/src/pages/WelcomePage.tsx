import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "@/api/client";
import type { Run } from "@/api/types";

/**
 * 3.8i welcome page: big centered headline, one prominent name input that
 * starts the wizard (name rides to the wizard's welcome step via location
 * state), and the "old searches" list of recent discovery runs.
 */

function runLabel(run: Run): string {
  const params = run.params ?? {};
  const country = typeof params.country === "string" ? params.country : null;
  const role = typeof params.role === "string" ? params.role : null;
  if (country && role) return `${role} jobs in ${country}`;
  if (country) return `Jobs in ${country}`;
  if (role) return `${role} jobs`;
  return "Discovery search";
}

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  const days = Math.floor(ms / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  const weeks = Math.floor(days / 7);
  return weeks === 1 ? "1 week ago" : `${weeks} weeks ago`;
}

function OldSearchRow({ run }: { run: Run }) {
  return (
    <Link
      to={`/search/results/${run.id}`}
      className="flex items-center justify-between gap-4 rounded-xl border border-maroon/10 bg-cream-light px-4 py-3 hover:border-maroon/30"
    >
      <div className="min-w-0">
        <p className="truncate text-sm font-medium text-maroon">{runLabel(run)}</p>
        <p className="text-xs text-maroon-soft">
          Finished {timeAgo(run.finished_at) || run.status}
        </p>
      </div>
      <span aria-hidden="true" className="text-maroon-soft">
        →
      </span>
    </Link>
  );
}

export function WelcomePage() {
  const navigate = useNavigate();
  const [name, setName] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["runs", "completed"],
    queryFn: () => api.listRuns("completed"),
  });

  const start = () => {
    navigate("/profile", { state: { name: name.trim() } });
  };

  const runs = (data ?? []).slice(0, 5);

  return (
    <div className="py-12 text-center">
      <h1 className="text-4xl font-bold tracking-tight text-maroon sm:text-5xl">
        Welcome to Job Search
      </h1>
      <p className="mt-3 text-base text-maroon-soft">
        Find your next role — start with your name
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          start();
        }}
        className="mx-auto mt-10 max-w-xl"
      >
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Let's start with your name here"
          aria-label="Your name"
          className="w-full rounded-full border border-maroon bg-cream-light px-6 py-3.5 text-center text-base text-maroon placeholder:text-maroon-soft focus:outline-none focus:ring-2 focus:ring-lime"
        />
      </form>

      <div className="mx-auto mt-14 max-w-2xl text-left">
        <div className="flex items-baseline justify-between">
          <h2 className="text-lg font-semibold text-maroon">Old searches</h2>
          {runs.length > 0 && (
            <span className="text-xs text-maroon-soft">{runs.length} recent</span>
          )}
        </div>
        {isLoading && <p className="mt-3 text-sm text-maroon-soft">Loading…</p>}
        {error && (
          <p className="mt-3 text-sm text-red-800">Failed to load recent searches.</p>
        )}
        {!isLoading && !error && runs.length === 0 && (
          <p className="mt-3 text-sm text-maroon-soft">
            No searches yet — start your first one above.
          </p>
        )}
        <div className="mt-3 space-y-2">
          {runs.map((run) => (
            <OldSearchRow key={run.id} run={run} />
          ))}
        </div>
      </div>
    </div>
  );
}