"""One posting check: is an extracted job a real posting?

ES1 (master_plan §ES). The entry resolver and the position_extraction funnel
judge extracted jobs by this one rule, so a page the resolver prefers is a page
the funnel scores well. Shape checks only: title, URL placement and a posting
signal. It reads no body text; the funnel adds its description check on top.

Pure: no network, no model.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

#: Obstacle names are the funnel report's contract; keep them stable.
TOO_THIN = "too_thin"
NO_TITLE = "no_title"
NAV_LINK = "nav_link"
OFF_DOMAIN = "off_domain"
SELF_LINK = "self_link"
NO_POSTING_SIGNAL = "no_posting_signal"

_AUTH_HOSTS = ("accounts.", "login.", "auth.", "signin.")
_PLACEHOLDER_TITLES = frozenset({"unknown"})

# Navigation / account links the generic extractor sometimes returns as
# postings.
_NAV_TITLE = re.compile(
    r"^(careers?|vacancies|work at|life at|sign in|log ?in|saved jobs|job alerts|"
    r"recommended jobs|how we hire|skip to|main menu|share data|faq|"
    r"frequently asked|open jobs as rss|data protection|.*\bfor students\b)",
    re.IGNORECASE,
)
# A numeric posting identifier in the URL path (``/details/200688368/…``).
_POSTING_ID = re.compile(r"\d{3,}")
# The same id in a query parameter (``?jid=216``, ``?jobId=1234``): some
# applicant systems carry the posting id only there.
_QUERY_POSTING_ID = re.compile(r"(?:^|&)(?:[a-z_]*id|job|req|requisition)=\d{3,}", re.IGNORECASE)
# A job-shaped path segment, as the production extractor's link patterns see it.
_JOB_PATH = re.compile(
    r"/(jobs?|postings?|positions?|openings?|vacanc(y|ies)|careers?)/", re.IGNORECASE
)
_TITLE_KEYWORD = re.compile(
    r"\b(engineer|developer|data|backend|frontend|fullstack|devops|sre|analyst|scientist|"
    r"architect|manager|lead|intern|associate|specialist|python|java|golang|rust|cloud|"
    r"security|platform|infrastructure|machine learning|ai)\b",
    re.IGNORECASE,
)
# "AI at Google", "Work at Apple": a company-section label, not a role.
_AT_COMPANY_TITLE = re.compile(r"\bat \S+$", re.IGNORECASE)


def _host_domain(url: str) -> str:
    """Last two labels of the host: ``jobs.apple.com`` -> ``apple.com``."""
    host = urlsplit(url).hostname or ""
    return ".".join(host.split(".")[-2:])


def has_posting_signal(title: str, url: str) -> bool:
    """A posting carries a numeric id in its path, or a role-like title of
    three or more words under a job-shaped path. Section labels ("Benefits",
    "Google Cloud", "Analyst coverage") carry neither."""
    parts = urlsplit(url)
    path = parts.path
    if _POSTING_ID.search(path) or _QUERY_POSTING_ID.search(parts.query):
        return True
    return (
        len(title.split()) >= 3
        and bool(_TITLE_KEYWORD.search(title))
        and bool(_JOB_PATH.search(path))
        and not _AT_COMPANY_TITLE.search(title)
    )


def posting_obstacle(job: Any, listing: str) -> str | None:
    """Why ``job`` is not a real posting on ``listing``, or ``None``."""
    title = (job.title or "").strip()
    if len(title) < 3:
        return TOO_THIN
    # The careers extractor falls back to the literal "Unknown" when a card link
    # carries no text. That is a missing title, not a posting.
    if title.lower() in _PLACEHOLDER_TITLES:
        return NO_TITLE
    url = job.url or ""
    if _NAV_TITLE.match(title):
        return NAV_LINK
    host = urlsplit(url).hostname
    if host and host.startswith(_AUTH_HOSTS):
        return NAV_LINK
    if _host_domain(url) != _host_domain(listing):
        return OFF_DOMAIN
    if url.rstrip("/").split("?")[0] == listing.rstrip("/").split("?")[0]:
        return SELF_LINK
    if not has_posting_signal(title, url):
        return NO_POSTING_SIGNAL
    return None


def is_real_posting(job: Any, listing: str) -> bool:
    return posting_obstacle(job, listing) is None


def suspect_reason(title: str | None, url: str | None) -> str | None:
    """Why a stored job looks like a non-posting (an article, a category page),
    or ``None`` (MQ, master_plan).

    Judges a stored row without its listing page: the URL's own origin stands in
    for the listing, so ``off_domain`` cannot fire (an employer page legitimately
    links out to an ATS board) and only a bare-origin URL is a ``self_link``. A
    suspect is a hint for ranking and display, never a status and never a reason
    to delete.
    """
    parts = urlsplit(url or "")
    if not parts.scheme or not parts.netloc:
        return None
    job = SimpleNamespace(title=title or "", url=url)
    return posting_obstacle(job, f"{parts.scheme}://{parts.netloc}/")
