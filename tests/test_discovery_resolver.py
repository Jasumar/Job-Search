from __future__ import annotations

from unittest.mock import MagicMock

from jobsearch_core.search.resolver import resolve_url


def test_resolver_drops_ats_board_job_urls_to_path_parents():
    """Phase 4.1e: no ATS canonicalization — a job URL degrades to path parents.

    The careers URL stored from an ATS job link is the deepest live parent path,
    never an ATS board root (which would violate employer-pages-over-ATS).
    Phase 4.1e enforcement completion: board candidates are not even fetched —
    resolution never fetches, scores, or selects an ATS board URL.
    """
    source = "https://jobs.ashbyhq.com/anyone-ai/123"
    client = MagicMock()
    client.fetch.return_value = "<html><title>Dead</title></html>"

    result = resolve_url(source, client)

    assert result.status == "unresolved"
    fetched = [call.args[0] for call in client.fetch.call_args_list]
    assert "https://jobs.ashbyhq.com/anyone-ai" not in fetched
    assert "https://jobs.ashbyhq.com/anyone-ai/123" not in fetched


def test_resolver_never_selects_ats_board_candidate():
    """A board path parent that looks like a careers page is not selectable.

    Even if the board page scores as a careers page, the resolver refuses it
    as the resolved URL — the employer's own page must win or stay unresolved.
    """
    source = "https://acme.example/jobs/123?board=greenhouse"
    client = MagicMock()
    client.fetch.side_effect = RuntimeError("not found")

    result = resolve_url(source, client)

    assert result.status == "unresolved"


def test_resolver_prefers_live_parent_careers_page():
    source = "https://example.com/jobs/python-developer-in-helsinki"
    parent = "https://example.com/jobs"
    client = MagicMock()
    client.fetch.side_effect = lambda url: {
        source: "<html><title>Python Developer</title><p>Apply for this job</p></html>",
        parent: "<html><title>Careers</title><h1>Join our team</h1><a>Open positions</a></html>",
        "https://example.com": "<html><title>Example</title></html>",
    }[url]

    result = resolve_url(source, client)

    assert result.resolved_url == parent
    assert result.status == "resolved"
    assert result.page_type == "careers"


def test_resolver_preserves_unresolved_dead_url():
    client = MagicMock()
    client.fetch.side_effect = RuntimeError("not found")

    result = resolve_url("https://dead.example/jobs/old-role", client)

    assert result.resolved_url == "https://dead.example/jobs/old-role"
    assert result.status == "unresolved"
    assert result.page_type == "dead_or_unrelated"
