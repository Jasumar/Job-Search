"""Listing pages for M1 ``name_collect`` (B3): capture and a name reference.

A ``name_collect`` source is one page listing many companies. This module turns
one page load into the two things the recipe author needs, and nothing else:

- :func:`capture_page` — the page three ways, in the shape the author reads
  (``Artifact``): the plain GET, the rendered DOM, and every JSON/XHR response
  the rendered page itself received. Nothing is requested in addition to the
  page load.
- :func:`name_reference` — the company names the page's own markup carries, as
  the same ``ScrapedJob`` rows a recipe replay produces (shapes and choice:
  ``name_shapes``). It is measurement, not
  input: the author is told which artifact carried the list, never what the
  names are (recipe_author.build_prompt). Where the benchmark derives postings
  (``benchmarks/recipe_author/reference.py``), this derives *names*: the largest
  group of same-shaped links, no job-shaped requirement — a company directory's
  links are not job links, and the jobishness test would reject every real
  source.

A page whose markup carries no such group is a capture gap: there is no
reference to verify an authored recipe against, and that is a fact about the
page, not a recipe failure.

Nothing here writes to the database and nothing here calls a model.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from jobsearch_core.ingest.name_shapes import (
    MIN_NAME_SHARE,
    NameCandidate,
    looks_like_name,
    name_share,
    rank,
    text_candidates,
)
from jobsearch_core.ingest.page_chrome import content_links
from jobsearch_core.ingest.recipe import name_identity
from jobsearch_core.ingest.recipe_author import Artifact, AuthorInput, FetchMode
from jobsearch_core.ingest.scrapers.base import ScrapedJob

# A directory cluster needs more same-shaped links than a nav menu has entries.
MIN_CLUSTER = 8
# Share of a cluster's links that must carry text before it counts as a list.
MIN_LABEL_SHARE = 0.8
_NAME_MIN_CHARS = 2
# Anchor text that is a control, not a company: a listing that only says
# "read more" carries no names even though it has the links.
_GENERIC_LABELS = frozenset(
    {
        "read more",
        "read more...",
        "more",
        "show more",
        "load more",
        "next",
        "previous",
        "next page",
        "previous page",
        "home",
        "back",
        "lue lisää",
        "näytä lisää",
        "lisää",
        "seuraava",
        "edellinen",
        "etusivu",
        "takaisin",
    }
)
_VARIABLE_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-f]{8,}|[0-9a-f][0-9a-f-]{15,})$", re.IGNORECASE
)


class CaptureClient(Protocol):
    """The reads and recovery a capture needs; ``PoliteClient`` satisfies it."""

    def fetch(self, url: str) -> str: ...

    def fetch_rendered_with_xhr(self, url: str) -> tuple[str, list]: ...

    def reset_browser(self) -> None: ...


@dataclass(frozen=True)
class Capture:
    """Everything one page load produced: bodies, payloads, and what happened."""

    artifacts: list[Artifact]
    payloads: list[tuple[str, str, FetchMode]]
    notes: dict
    static: str
    rendered: str | None


@dataclass(frozen=True)
class NameReference:
    """The names the page's markup carries, and which artifact carried them."""

    names: tuple[ScrapedJob, ...]
    carried_by: str | None
    note: str
    shape: str | None = None

    @property
    def count(self) -> int:
        return len(self.names)


def _is_json(text: str) -> bool:
    return text.lstrip()[:1] in ("{", "[")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def capture_page(page_url: str, client: CaptureClient) -> Capture:
    """Load the page three ways: plain GET, rendered DOM, and the JSON it fetched."""
    notes: dict = {"page_url": page_url}
    static = client.fetch(page_url)
    notes["plain_bytes"] = len(static)
    artifacts: list[Artifact] = [Artifact("static_html", "static", page_url, static)]

    rendered: str | None = None
    captures: list = []
    try:
        try:
            dom, captures = client.fetch_rendered_with_xhr(page_url)
        except Exception as first:  # noqa: BLE001 — one relaunch usually clears it
            notes["render_retry"] = str(first)[:200]
            client.reset_browser()
            dom, captures = client.fetch_rendered_with_xhr(page_url)
        rendered = dom
        notes["rendered_bytes"] = len(dom)
        artifacts.append(Artifact("rendered_html", "rendered", page_url, dom))
    except Exception as exc:  # noqa: BLE001 — a failed render still leaves the static page
        notes["render_error"] = str(exc)[:200]

    payloads: list[tuple[str, str, FetchMode]] = []
    for capture in captures:
        body = capture.response_body
        if capture.status >= 400 or not body or not _is_json(body):
            continue
        payloads.append((capture.url, body, "xhr"))
    # Stable names: XHR responses arrive in whatever order the page fired them.
    payloads.sort(key=lambda item: item[0])
    for number, (url, body, mode) in enumerate(payloads, start=1):
        artifacts.append(Artifact(f"json_{number}", mode, url, body))
    notes["json_payloads"] = len(payloads)
    return Capture(artifacts, payloads, notes, static, rendered)


def _links(html: str, page_url: str) -> list[tuple[str, str]]:
    """(url, anchor text) for every distinct http(s) link on the page."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    self_url = page_url.split("#")[0]
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(page_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        url = url.split("#")[0]
        if url == self_url or url in seen:
            continue
        seen.add(url)
        links.append((url, " ".join(anchor.get_text(" ").split())))
    return links


def _shape(url: str) -> str:
    """Path with id/ slug-shaped segments replaced, so one list has one shape."""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return "/"
    segments = [
        "{var}" if _VARIABLE_SEGMENT.match(segment) else segment for segment in path.split("/")
    ]
    return "/".join(segments)


def _prefix_shape(url: str) -> tuple[int, str] | None:
    """(depth, prefix) for a link deeper than one segment.

    The fallback grouping for a directory whose slugs carry no separator: the
    links differ only in their last segment, so the shared prefix is the list.
    A flat top-level menu has prefix ``/`` and is rejected by the caller.
    """
    segments = [segment for segment in urlparse(url).path.split("/") if segment]
    if len(segments) < 2:
        return None
    return len(segments), "/" + "/".join(segments[:-1])


def _labelled(name: str) -> bool:
    text = name.strip()
    return len(text) >= _NAME_MIN_CHARS and text.casefold() not in _GENERIC_LABELS


def _largest_rule_group(
    links: list[tuple[str, str]], key: Callable[[str], object], rule: str
) -> tuple[list[tuple[str, str]], str]:
    groups: dict = {}
    for url, text in links:
        shape = key(url)
        if shape is None:
            continue
        groups.setdefault(shape, []).append((url, text))
    if not groups:
        return [], "the page has no usable links"
    shape, group = max(groups.items(), key=lambda item: len(item[1]))
    if len(group) < MIN_CLUSTER:
        return [], f"largest link group has {len(group)} entries (needs {MIN_CLUSTER})"
    labelled = [item for item in group if _labelled(item[1])]
    share = len(labelled) / len(group)
    if share < MIN_LABEL_SHARE:
        return [], f"largest link group ({len(group)}) carries names on only {share:.0%} of its links"
    return labelled, f"largest link group has {len(group)} entries ({rule})"


def _cluster(links: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str]:
    """The largest group of same-shaped links, if it looks like a name list.

    Two rules, strongest first: the id/slug shape (a row per posting, real ids
    in the path) and then the shared-prefix shape (a directory whose slugs
    carry no separator). The prefix rule needs a non-empty prefix, so a flat
    top-level menu cannot become a company list.
    """
    if not links:
        return [], "the page has no links"
    group, why = _largest_rule_group(links, _shape, "same id/slug shape")
    if group:
        return group, why
    prefix_group, prefix_why = _largest_rule_group(
        links, _prefix_shape, "shared path prefix"
    )
    if prefix_group:
        return prefix_group, prefix_why
    return [], f"{why}; {prefix_why}"


def _anchor_candidate(page_url: str, html: str) -> tuple[NameCandidate | None, str]:
    """The legacy shape: the largest same-shaped link group, chrome removed."""
    cluster, why = _cluster(content_links(html, page_url, drop_menu_duplicates=False))
    if not cluster:
        return None, why
    items = tuple((url, text) for url, text in cluster)
    return NameCandidate("anchors", items, why), why


def name_reference(artifacts: list[Artifact]) -> NameReference:
    """Derive the name list one capture carries. Pure: reads artifacts, writes nothing.

    Builds one candidate per shape (links, table columns, lists, headings) with
    page chrome removed, and keeps the one whose items look most like
    organisation names (``name_shapes.rank``). ``note`` names the chosen
    shape and why each other shape lost, so a zero-yield page says why.
    """
    rendered = next((a for a in artifacts if a.fetch_mode == "rendered"), None)
    static = next((a for a in artifacts if a.fetch_mode == "static"), None)
    page = rendered or static
    if page is None:
        return NameReference((), None, "the capture has no page body")
    anchors, anchor_why = _anchor_candidate(page.url, page.source)
    eligible: list[tuple[NameCandidate, float]] = []
    lost: list[str] = []
    if anchors is None:
        lost.append(f"anchors: {anchor_why}")
    else:
        # The link rule already required labelled text; the share only ranks it.
        eligible.append((anchors, name_share(anchors.items)))
    for candidate in text_candidates(page.source, page.url):
        share = name_share(candidate.items)
        if share < MIN_NAME_SHARE:
            lost.append(f"{candidate.label}: {len(candidate.items)} items, {share:.0%} name-like")
            continue
        eligible.append((candidate, share))
    if not eligible:
        reasons = "; ".join(lost) or "no list-shaped content on the page"
        return NameReference((), None, f"{page.name}: {reasons}")
    ranked = rank(eligible)
    chosen, share = ranked[0]
    for other, other_share in ranked[1:]:
        lost.append(f"{other.label}: {len(other.items)} items, {other_share:.0%} name-like")
    items = chosen.items
    if chosen.shape != "anchors":
        items = tuple(item for item in items if looks_like_name(item[1]))
    host = _host(page.url)
    names = tuple(
        ScrapedJob(url=url or name_identity(text), title=text, company=host)
        for url, text in items
    )
    note = f"{page.name}: {chosen.label}, {len(names)} items, {share:.0%} name-like"
    if lost:
        note += "; not chosen: " + "; ".join(lost)
    return NameReference(names, page.name, note, chosen.shape)


def author_input(
    page_url: str, capture: Capture, reference: NameReference, fetch, fetch_html=None
) -> AuthorInput:
    """``AuthorInput`` for one listing page, built from the capture and its reference.

    ``reference`` is attached to the artifact that carried it, so a ``dom``
    replay reads that same artifact. ``allowed_hosts`` is the page plus every
    host the capture or the reference touched; ``url_pattern`` is the reference
    hosts, because an item's URL is what identifies one company on this page.
    """
    if not reference.names:
        raise ValueError("a name reference is required to author a listing recipe")
    carried_by = reference.carried_by or capture.artifacts[0].name
    known = {artifact.name for artifact in capture.artifacts}
    if carried_by not in known:
        carried_by = capture.artifacts[0].name
    artifacts = [
        Artifact(a.name, a.fetch_mode, a.url, a.source, reference.names) if a.name == carried_by else a
        for a in capture.artifacts
    ]
    origins = sorted(
        {
            f"{urlparse(job.url).scheme}://{urlparse(job.url).netloc}"
            for job in reference.names
            if urlparse(job.url).scheme in {"http", "https"}
        }
    )
    # Text-only items are identified by name (``name:``); the pattern is only measured.
    parts = [re.escape(origin + "/") for origin in origins] + ["name:"]
    url_pattern = "^(?:" + "|".join(parts) + ")."
    hosts = {_host(page_url)} | {_host(job.url) for job in reference.names}
    hosts |= {_host(artifact.url) for artifact in capture.artifacts}
    return AuthorInput(
        target_url=page_url,
        allowed_hosts=sorted(host for host in hosts if host),
        url_pattern=url_pattern,
        page_url=page_url,
        reference=list(reference.names),
        artifacts=artifacts,
        carried_by=carried_by,
        fetch=fetch,
        fetch_html=fetch_html,
    )


__all__ = [
    "Capture",
    "CaptureClient",
    "MIN_CLUSTER",
    "NameReference",
    "author_input",
    "capture_page",
    "name_reference",
]
