"""Scheduler/worker service (Phase M1) — consumes job_runs and executes pipeline runs."""

from jobsearch_worker.consumer import RunWorker

__all__ = ["RunWorker"]
