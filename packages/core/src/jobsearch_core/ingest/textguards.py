"""Shared text-classification constants for posting-vs-obstacle decisions.

Single source of truth for the login-wall heuristic. The benchmark scorer
(``benchmarks/reach/score_reach.py``) imports these instead of carrying its
own copy, so the two layers cannot drift. The calibration lives here; see
``LOGIN_WALL_MAX_CHARS`` for the reasoning.
"""

# A login wall REPLACES the posting; an incidental login link (a "report this
# job" widget, an account upsell) sits inside a complete posting. Marker
# presence cannot tell those apart on its own — the real Built In postings in
# the live DB carry "Please log in or sign up to report this job" alongside
# 4-15KB of perfectly good description.
#
# Calibrated 2026-10-02 against the live DB: the two genuine walls
# (jobs.siemens.com) are 763 chars, the eight incidental-widget pages are
# 4844-15152 chars. 2000 sits in the empty gap with wide margin on both sides.
#
# The invariant: a page this short cannot also carry a real posting body, so
# if a login marker is present there is no posting behind it to reach. A page
# long enough to hold a posting reads as usable even when a login marker is
# inside it — the marker describes the widget, not the page.
LOGIN_MARKERS: tuple[str, ...] = (
    "already registered?",
    "log in with a code",
    "sign in to continue",
    "log in to view",
    "create an account to view",
    "this content is only available",
    "sign in to apply",
    "please log in or sign up",
)

LOGIN_WALL_MAX_CHARS = 2000


def is_login_wall(text: str) -> bool:
    """True when ``text`` is a login wall rather than a posting body.

    Marker presence alone is not sufficient — the length cap is what
    distinguishes a wall (which replaces the posting) from an incidental
    login widget inside a real posting.
    """
    lower = text.strip().lower()
    return len(text.strip()) < LOGIN_WALL_MAX_CHARS and any(m in lower for m in LOGIN_MARKERS)
