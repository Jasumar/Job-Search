"""Reference postings for the recipe benchmark, derived from the capture itself.

Why this exists
---------------
The reference used to be the existing scraper's output, so "verified" meant
"agrees with the scraper". That is circular where the scraper is wrong (Google
2026-10-25: 26 navigation links, zero postings) and useless where the scraper
path is exactly what a recipe is replacing. Hand labels were the other option,
but a frozen list of postings rots the next time the company posts a job, and 25
companies of monthly drift is not something anyone maintains by hand.

So every capture re-derives its own reference from the site's own data, with
detectors that never call the scraper path:

  json      a JSON body the page itself fetched, holding a consistent posting list
  joined    that list carries ids instead of URLs; the URLs come from the DOM's own
            links, joined on the id
  dom       no structured list at all: the largest cluster of same-shaped links
            (marked low confidence -- it cannot tell a job list from a nav menu)

Nothing is stored. Run 2 re-derives the reference from its own capture, so
day-1 -> day-2 movement shows up as drift between two references instead of as a
failed recipe, and no committed URL list has to be maintained.

A capture where all three detectors find nothing is a capture gap, not a recipe
failure: ``method == "none"``. The run records it as such.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# A JSON list is a posting list when it is at least this long, its items are all
# objects, and (almost) all of them share one URL shape. Nav menus and facet lists
# fail the shape test: their entries point at different kinds of page.
MIN_LIST = 3
# A DOM cluster needs more same-shaped links than a nav menu has entries.
MIN_DOM_CLUSTER = 8
# Share of a list's items that must agree before the list counts as one template.
CONSISTENT_SHARE = 0.8

TITLE_KEYS = (
    "title",
    "jobTitle",
    "job_title",
    "positionName",
    "position_title",
    "postingTitle",
    "name",
    "text",
)
URL_KEYS = (
    "url",
    "positionUrl",
    "absolute_url",
    "jobUrl",
    "job_url",
    "field_ad_url",
    "applyUrl",
    "link",
    "href",
    "job_path",
    "path",
)
ID_KEYS = (
    "id",
    "Id",
    "ID",
    "jobId",
    "job_id",
    "id_icims",
    "requisitionId",
    "requisition_id",
    "postingId",
    "slug",
)

_VARIABLE_SEGMENT = re.compile(r"\d|[-_]")

# A posting list is only believed when the evidence is job-shaped: the list's key
# path, or the shape its URLs share, carries one of these. Shape alone is not
# enough -- a footer menu or a product catalogue is every bit as consistent as a
# job list (2026-10-25: Apple's 52 product links and Danske's 5 investor links
# both came back as "postings" from a shape-only rule).
JOBISH = (
    "job",
    "career",
    "vacanc",
    "position",
    "opening",
    "requisition",
    "posting",
    "apply",
    "search",
)


def _jobish(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in JOBISH)


class ArtifactLike(Protocol):
    """What the reference needs from a captured artifact."""

    name: str
    fetch_mode: str
    url: str
    source: str


@dataclass(frozen=True)
class Posting:
    url: str
    title: str
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class Reference:
    postings: tuple[Posting, ...] = ()
    # "json" | "joined" | "dom" | "none"
    method: str = "none"
    # "structured" | "joined" | "dom-only" | "none"
    confidence: str = "none"
    # Artifact the postings came from; None when nothing was found.
    carried_by: str | None = None
    by_artifact: dict[str, int] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.postings)

    def urls(self) -> frozenset[str]:
        return frozenset(p.url for p in self.postings)

    def sample(self, size: int) -> list[dict[str, str]]:
        return [{"title": p.title, "url": p.url} for p in self.postings[:size]]


@dataclass(frozen=True)
class _Item:
    title: str
    url: str | None
    ident: str | None


def _text(value: object) -> str | None:
    if isinstance(value, str):
        return " ".join(value.split()) or None
    return None


def _shape(url: str) -> str:
    """The URL's shape: path segments that carry an id or a slug become ``*``."""
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    shaped = ["*" if _VARIABLE_SEGMENT.search(s) else s.lower() for s in segments]
    return f"{(parsed.hostname or '').lower()}/{'/'.join(shaped)}"


def _resolve(base_url: str, raw: str) -> str | None:
    url = urljoin(base_url, raw.strip())
    if not url.startswith(("http://", "https://")):
        return None
    return url


def _lowered(row: dict) -> dict:
    """Row keyed by lowercased name: payloads spell the same field Id/ID/id."""
    return {str(key).lower(): value for key, value in row.items()}


def _title_of(row: dict) -> str | None:
    lowered = _lowered(row)
    for key in TITLE_KEYS:
        text = _text(lowered.get(key.lower()))
        if text and len(text) <= 200:
            return text
    return None


def _url_of(row: dict) -> str | None:
    lowered = _lowered(row)
    for key in URL_KEYS:
        raw = _text(lowered.get(key.lower()))
        if not raw:
            continue
        if key == "path" and not raw.startswith("/"):
            continue  # a bare "path" is a JSON pointer, not a URL
        if raw.startswith(("http://", "https://", "/")) or "/" in raw:
            return raw
    return None


def _ident_of(row: dict) -> str | None:
    lowered = _lowered(row)
    for key in ID_KEYS:
        value = lowered.get(key.lower())
        if isinstance(value, int):
            return str(value)
        raw = _text(value)
        if raw:
            return raw
    return None


def _lists(node: object, out: list[tuple[str, list[dict]]], path: str = "") -> None:
    """Every list of objects in the document, with the key path that led to it."""
    if isinstance(node, dict):
        for key, value in node.items():
            _lists(value, out, f"{path}.{key}" if path else str(key))
        return
    if not isinstance(node, list):
        return
    if len(node) >= MIN_LIST and all(isinstance(item, dict) for item in node):
        out.append((path, node))
    for value in node:
        _lists(value, out, path)


def _modal(counter: Counter) -> tuple[Any, float]:
    if not counter:
        return None, 0.0
    value, hits = counter.most_common(1)[0]
    return value, hits / sum(counter.values())


def _posting_list(rows: list[dict], base_url: str, path: str) -> tuple[list[_Item], str]:
    """Read one JSON list as a posting list, or reject it with a reason."""
    titles = [_title_of(row) for row in rows]
    if sum(1 for t in titles if t) < CONSISTENT_SHARE * len(rows):
        return [], "items carry no title-like field"

    urls: list[str | None] = []
    for row in rows:
        raw = _url_of(row)
        urls.append(_resolve(base_url, raw) if raw else None)
    resolved = [u for u in urls if u]
    if len(resolved) >= CONSISTENT_SHARE * len(rows):
        shape, share = _modal(Counter(_shape(u) for u in resolved))
        if share < CONSISTENT_SHARE:
            return [], f"{len(resolved)} URLs spread over {len(set(map(_shape, resolved)))} shapes"
        if not (_jobish(shape) or _jobish(path)):
            return [], f"{len(resolved)} same-shaped URLs but nothing job-shaped about {path}"
        items = [
            _Item(title=t or "", url=u, ident=None)
            for t, u in zip(titles, urls, strict=True)
            if u
        ]
        return items, f"list of {len(items)} postings sharing {shape}"

    idents = [_ident_of(row) for row in rows]
    if sum(1 for i in idents if i) >= CONSISTENT_SHARE * len(rows):
        if not _jobish(path):
            return [], f"{len(idents)} ids but nothing job-shaped about {path}"
        items = [
            _Item(title=t or "", url=None, ident=i)
            for t, i in zip(titles, idents, strict=True)
            if i
        ]
        return items, f"list of {len(items)} titled items carrying ids, no URLs"

    return [], "items carry neither URLs nor ids"


def _json_items(body: str, base_url: str) -> tuple[list[_Item], str]:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return [], "not JSON"
    lists: list[tuple[str, list[dict]]] = []
    _lists(payload, lists)
    best: list[_Item] = []
    reason = "no list long enough to be a posting list"
    for path, rows in lists:
        items, why = _posting_list(rows, base_url, path)
        if items and len(items) > len(best):
            best, reason = items, why
        elif not best and why:
            reason = why
    return best, reason


def _dom_links(html: str, page_url: str) -> list[tuple[str, str]]:
    """(url, anchor text) for every distinct http(s) link on the page."""
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url = _resolve(page_url, anchor["href"])
        if url is None or url in seen or url.split("#")[0] == page_url.split("#")[0]:
            continue
        seen.add(url)
        links.append((url, " ".join(anchor.get_text(" ").split())))
    return links


def _id_index(links: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Map every id-shaped path segment of every link to that link's URL."""
    index: dict[str, str] = {}
    for url, _ in links:
        for segment in urlparse(url).path.split("/"):
            if segment and (_VARIABLE_SEGMENT.search(segment) or segment.isdigit()):
                index.setdefault(segment, url)
    return index


def _dom_cluster(links: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], str]:
    """The largest group of same-shaped links, if it is bigger than a nav menu."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for url, title in links:
        groups.setdefault(_shape(url), []).append((url, title))
    if not groups:
        return [], "no links"
    shape, group = max(groups.items(), key=lambda item: len(item[1]))
    if len(group) < MIN_DOM_CLUSTER:
        return [], f"largest link group has {len(group)} entries (needs {MIN_DOM_CLUSTER})"
    if not _jobish(shape):
        return [], f"largest link group ({len(group)}) is not job-shaped: {shape}"
    return group, f"largest link group has {len(group)} entries sharing {shape}"


def build_reference(artifacts: Iterable[ArtifactLike]) -> Reference:
    """Derive the posting reference for one capture. Pure: reads artifacts, writes nothing."""
    artifacts = list(artifacts)
    notes: list[str] = []
    pages = [a for a in artifacts if a.fetch_mode in ("static", "rendered")]
    # JSON-LD rides inside the page, so it is not a fetched payload: the page's
    # own detector already reads those links, and counting the block again would
    # score the same postings twice (or count an unrelated Organization block).
    payloads = [a for a in artifacts if a.fetch_mode not in ("static", "rendered", "json_ld")]
    # The rendered page is the one a person sees; fall back to the static body.
    page = next((a for a in pages if a.fetch_mode == "rendered"), pages[0] if pages else None)
    links = _dom_links(page.source, page.url) if page is not None else []
    ids = _id_index(links)

    found: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    by_artifact: dict[str, int] = {}
    carried_by: str | None = None
    unresolved = 0
    joined = 0

    for artifact in payloads:
        items, why = _json_items(artifact.source, artifact.url)
        notes.append(f"{artifact.name}: {why}")
        added = 0
        for item in items:
            url = item.url
            if url is None and item.ident is not None:
                url = ids.get(item.ident)
                if url is None:
                    unresolved += 1
                    continue
                joined += 1
            if url is None:
                continue
            found.setdefault(url, []).append(artifact.name)
            titles.setdefault(url, item.title)
            added += 1
        if added:
            carried_by = carried_by or artifact.name

    if unresolved:
        notes.append(f"{unresolved} id-only item(s) had no link on the page to resolve")
    method = "json" if found else "none"
    # A URL the payload carried is the strongest source; an id joined to a page
    # link is only as good as that join.
    confidence = "joined" if found and joined else "structured" if found else "none"
    if not found:
        cluster, why = _dom_cluster(links)
        notes.append(f"dom: {why}")
        if cluster:
            for url, title in cluster:
                found.setdefault(url, []).append(page.name)  # type: ignore[union-attr]
                titles.setdefault(url, title)
            carried_by = page.name  # type: ignore[union-attr]
            method, confidence = "dom", "dom-only"

    for url, sources in found.items():
        for name in set(sources):
            by_artifact[name] = by_artifact.get(name, 0) + 1
    postings = tuple(
        Posting(url=url, title=titles.get(url, ""), sources=tuple(sources))
        for url, sources in found.items()
    )
    if confidence == "none":
        notes.append("no detector found a posting list: capture gap, not a recipe failure")
    return Reference(
        postings=postings,
        method=method,
        confidence=confidence,
        carried_by=carried_by,
        by_artifact=by_artifact,
        notes=tuple(notes),
    )


__all__ = [
    "ArtifactLike",
    "Posting",
    "Reference",
    "build_reference",
]
