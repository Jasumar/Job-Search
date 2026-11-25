"""Recipe author (Phase F3, plan-scraping.md §Recipe authoring).

An LLM proposes one recipe body per trial; code decides everything that makes a
recipe trusted. Per trial: the LLM proposes a body; code overwrites
``allowed_hosts`` and ``verification`` with values from the caller and the
original fetch; ``parse_body`` admits the body (before any request); the replay
runs; ``verify_postings`` rejects only an empty replay and measures the rest.
A pass stores the recipe as ``verified``. A
failure is recorded on the company (``recipe_state``) and its reason is fed back
into the next trial's prompt (owner decision, 2026-10-18).

The LLM never sets trust fields, never makes a request, and never writes to the
database. A transport error from the LLM is not a trial: it propagates.

Scope: company-careers recipes (``scope='company_careers'``, owner a company,
trial state on its row) and M1 listing recipes (``scope='m1_url'``, owner a
source URL, no trial state, one attempt).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from sqlalchemy.orm import Session

from jobsearch_core.db.models import Company, CompanyRecipe
from jobsearch_core.db.recipe_state import RECIPE_FAIL, RECIPE_TRIAL_CAP, ensure_trials_allowed, record_trial
from jobsearch_core.ingest.llm_protocol import JsonCompleter
from jobsearch_core.ingest.recipe import (
    ApiRecipe,
    RecipeError,
    Verification,
    parse_body,
    run_api_recipe,
    run_dom_recipe,
    run_dom_recipe_report,
    verify_postings,
)
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

SCOPE_COMPANY = "company_careers"
SCOPE_URL = "m1_url"
STATUS_VERIFIED = "verified"

_DROP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "template",
        "iframe",
        "link",
        "meta",
        "head",
        "img",
        "picture",
        "source",
        "path",
    }
)
# ``datetime`` and ``itemprop`` stay on <time>: a posted date is often only in the
# attribute, never in the display text.
_KEEP_ATTRS = frozenset({"href", "id", "role", "aria-label", "itemprop", "datetime"})
_ANCESTOR_LEVELS = 5
_CANDIDATE_MIN_COUNT = 3
_CANDIDATE_MAX_COUNT = 200
# Candidate lines are cheap and a wrong pick costs a trial; example subtrees are not.
_MAX_CANDIDATES = 20
_MAX_TEXT = 120
_MAX_HREF = 200
_STABLE_CLASS = re.compile(r"^[A-Za-z][\w-]{2,30}$")
# Build-generated class names: a digit run of 3+, or letter-digit-letter mixing.
_GENERATED = re.compile(r"\d{3,}|[a-z]+\d[a-z\d]{4,}", re.IGNORECASE)
_EXAMPLES = 2
_EXAMPLE_CHARS = 4000
_JSON_DEPTH = 6
_JSON_LINES = 400
_JSON_SAMPLE = 80
_SAMPLE_TITLES = 3


# --- Evidence reduction (no LLM) -------------------------------------------------


def _stable_classes(tag: Tag) -> list[str]:
    classes = tag.get("class") or []
    if isinstance(classes, str):
        classes = [classes]
    return [c for c in classes if _STABLE_CLASS.match(c) and not _GENERATED.search(c)]


def _attr_selector(tag: Tag, attr: str) -> str | None:
    """``tag[attr="value"]`` when the value is a plain, quotable token.

    A value with a quote, a backslash, or markup in it cannot go inside a CSS
    attribute string, and a selector that fails to parse would abort candidate
    collection for the whole page.
    """
    value = tag.get(attr)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 60 or any(ch in value for ch in '"\\<>'):
        return None
    return f'{tag.name}[{attr}="{value}"]'


def _selector_for(tag: Tag) -> str:
    """CSS selector for one element, most stable anchor first.

    Order (plan-scraping.md §Evidence surface): ``data-*``, ``id``, stable
    class, ``role``/``aria-label``, bare tag. A per-row id cannot become a
    container: the candidate count floor drops selectors that match one element.
    """
    data_attr = next((name for name in tag.attrs if name.startswith("data-")), None)
    if data_attr is not None:
        return f"{tag.name}[{data_attr}]"
    id_ = tag.get("id")
    if isinstance(id_, str) and _STABLE_CLASS.match(id_) and not _GENERATED.search(id_):
        return f"#{id_}"
    classes = _stable_classes(tag)
    if classes:
        return f"{tag.name}.{classes[0]}"
    for attr in ("role", "aria-label"):
        selector = _attr_selector(tag, attr)
        if selector is not None:
            return selector
    return tag.name


def _shape(tag: Tag) -> tuple:
    """Tag name, stable classes, and child tag names: equal shapes mean a uniform row."""
    return (tag.name, tuple(_stable_classes(tag)), tuple(c.name for c in tag.find_all(recursive=False)))


def _candidates(soup: BeautifulSoup) -> list[tuple[str, int, bool]]:
    """Selectors of elements that enclose links: (selector, real count, uniform).

    Only ancestors of ``a[href]`` are considered, so a candidate always wraps a
    link. Counts come from ``soup.select`` so the LLM sees what it will match.
    ``uniform`` is true when every match has the same shape, which is what a
    list of postings looks like; ranking puts uniform candidates first.
    """
    selectors: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        node = anchor.parent
        for _ in range(_ANCESTOR_LEVELS):
            if not isinstance(node, Tag) or node.name in _DROP_TAGS or node.name in {"html", "body"}:
                break
            selectors.add(_selector_for(node))
            node = node.parent
    counted: list[tuple[str, int, bool]] = []
    for selector in selectors:
        matches = soup.select(selector)
        if _CANDIDATE_MIN_COUNT <= len(matches) <= _CANDIDATE_MAX_COUNT:
            uniform = len({_shape(m) for m in matches}) == 1
            counted.append((selector, len(matches), uniform))
    counted.sort(key=lambda item: (not item[2], -item[1], item[0]))
    return counted[:_MAX_CANDIDATES]


def _direct_text(tag: Tag) -> str:
    parts = [str(child) for child in tag.children if type(child) is NavigableString]
    return " ".join(" ".join(parts).split())


def _attr_text(tag: Tag) -> str:
    out: list[str] = []
    for name, value in tag.attrs.items():
        if name == "class":
            classes = _stable_classes(tag)
            if classes:
                out.append(f'class="{" ".join(classes)}"')
        elif name in _KEEP_ATTRS or name.startswith("data-"):
            text = " ".join(value) if isinstance(value, list) else str(value)
            if name == "href":
                text = text[:_MAX_HREF]
            out.append(f'{name}="{text}"')
    return " ".join(out)


def _emit(tag: Tag, depth: int, out: list[str], limit: int, used: list[int]) -> None:
    if used[0] > limit:
        return
    if tag.name in _DROP_TAGS:
        return
    attrs = _attr_text(tag)
    text = _direct_text(tag)[:_MAX_TEXT]
    child_depth = depth
    if attrs or text:
        line = f"{'  ' * depth}<{tag.name}{(' ' + attrs) if attrs else ''}>{(' ' + text) if text else ''}"
        out.append(line)
        used[0] += len(line) + 1
        child_depth = depth + 1
    for child in tag.children:
        if isinstance(child, Tag) and not isinstance(child, Comment):
            _emit(child, child_depth, out, limit, used)


def reduce_html(html: str, max_chars: int = 12000) -> str:
    """Prune a page to job-relevant structure for the LLM.

    Output: a candidate block (selectors that wrap links, with real match counts)
    then an outline of tags with stable attributes and direct text. Scripts,
    styles, images, and generated class names are dropped. The outline is cut at
    ``max_chars``; the candidate list is capped at ``_MAX_CANDIDATES`` (bounded
    prompt, and the ranked head is the part worth reading).
    """
    soup = BeautifulSoup(html, "html.parser")
    candidates = _candidates(soup)
    lines = ["CANDIDATE CONTAINERS (selector: match count on this page, uniform rows):"]
    for selector, count, uniform in candidates:
        lines.append(f"  {selector}: {count}{' uniform' if uniform else ''}")
    # Examples: the first match of the top uniform candidates, so the posting
    # structure is visible even when it sits deep in the page.
    examples = [sel for sel, _, uniform in candidates if uniform][:_EXAMPLES]
    for selector in examples:
        lines.append("")
        lines.append(f"EXAMPLE {selector}:")
        sample: list[str] = []
        used = [0]
        _emit(soup.select(selector)[0], 0, sample, _EXAMPLE_CHARS, used)
        lines.extend(sample)
    lines.append("")
    lines.append("OUTLINE (document head):")
    root = soup.body or soup
    outline: list[str] = []
    used = [0]
    _emit(root, 0, outline, max_chars, used)
    lines.extend(outline)
    if used[0] > max_chars:
        lines.append("[outline truncated]")
    return "\n".join(lines)


def _json_sample(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= _JSON_SAMPLE else text[:_JSON_SAMPLE] + "..."


def _walk_json(node: object, path: list[str | int], out: list[str], depth: int) -> None:
    if depth > _JSON_DEPTH or len(out) >= _JSON_LINES:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            _walk_json(value, [*path, key], out, depth + 1)
    elif isinstance(node, list):
        out.append(f"{json.dumps(path)} is a list of {len(node)}")
        if node:
            _walk_json(node[0], [*path, 0], out, depth + 1)
    else:
        out.append(f"{json.dumps(path)} = {_json_sample(node)}")


def reduce_json(payload: str) -> str:
    """List pointer paths (the recipe ``Pointer`` format) with sample values.

    Raises ``ValueError`` if the payload is not JSON.
    """
    data = json.loads(payload)
    lines: list[str] = []
    _walk_json(data, [], lines, 0)
    return "\n".join(lines)


_JSON_LD_TYPE = "application/ld+json"


def script_artifacts(url: str, html: str) -> list[Artifact]:
    """The page's JSON-LD as its own artifact; every other script stays cut.

    The one exception to "script bodies are never fed" (plan-scraping.md
    §Evidence surface). A JSON-LD block is a structured description the site
    publishes for machines — the cheapest evidence a ``dom`` recipe can be
    authored from. Inline page state (``__NEXT_DATA__`` and friends) is *not*
    taken from here: when a page fetches its postings, the XHR capture already
    holds the real response, and a guessed inline blob is as often an analytics
    config as a listing.

    The artifact carries the page URL, not a request URL: it sits inside the
    page, so it is evidence for choosing a selector and never an ``api`` source.
    All valid blocks are handed over as one artifact (a site may publish one
    block per posting).
    """
    soup = BeautifulSoup(html, "html.parser")
    blocks: list[object] = []
    for script in soup.find_all("script"):
        if (script.get("type") or "").split(";")[0].strip().lower() != _JSON_LD_TYPE:
            continue
        body = (script.string if script.string is not None else script.get_text()).strip()
        if not body:
            continue
        try:
            parsed = json.loads(body)
        except ValueError:
            continue
        if isinstance(parsed, (dict, list)):
            blocks.append(parsed)
    if not blocks:
        return []
    return [Artifact("json_ld", "json_ld", url, json.dumps(blocks, ensure_ascii=False))]


# --- Prompt -----------------------------------------------------------------------

FetchMode = Literal["static", "rendered", "xhr", "ats_board", "json_ld"]
# Artifacts whose evidence is JSON, so they are reduced as JSON for the author.
_JSON_EVIDENCE = ("xhr", "ats_board", "json_ld")
# Artifacts a recipe can replay: a request the page made. JSON-LD sits inside
# the page body, so it is evidence only, never an api source.
_REPLAYABLE_JSON = ("xhr", "ats_board")

_SCHEMA_API = """\
{"kind": "api", "method": "GET",
 "url_template": "https://<host>/...?page={page}",      (literal https host; placeholders only in the path/query)
 "vars": {},                                            (string values only, e.g. {"site": "CX_1"}, never a number)
 "pagination": {"kind": "none|page|offset|cursor", "param": "page", "start": 1, "step": 1,
                "cursor_path": [...], "max_pages": 20},
 "list_path": [...],                                    (pointer to the job array in each response)
 "field_map": {"url": {"path": [...]},                   (a direct child of field_map; use url OR url_template)
               "url_template": "https://.../{id}",       (a direct child of field_map, never nested inside "url")
               "title": {"path": [...]},
               "location": {"path": [...], "fallback": {"path": [...]}},
               "posted_date": {"path": [...], "transform": "iso_date"},
               "description": {"path": [...], "transform": "html_text"}},
 "detail": null}
Pointers are JSON arrays of keys and integer indexes, copied from the evidence.
A url_template in field_map may use the item's top-level scalar keys as bare
placeholders, e.g. {id}. {item.id} is the detail-recipe syntax and is refused.
Transforms: "text" (default), "html_text", "iso_date"."""

_SCHEMA_DOM = """\
{"kind": "dom",
 "container": "<css selector matching one posting row>",
 "link": {"selector": "<css relative to the row; empty = the row>", "attr": "href"},
 "title": {"selector": "<css relative to the row>"},
 "location": {"selector": "<css relative to the row>"},
 "pagination": {"kind": "none|page|offset|next", "param": "page", "start": 1, "step": 1,
                "selector": "<css for the next-page anchor>", "max_pages": 10}}
"none" reads the listing once. "page"/"offset" rewrite one query parameter of the
listing URL (page starts at 1, offset at 0 and needs "step" = the page size).
"next" follows the href of the anchor "selector" matches. max_pages is a document
budget capped at 20; page through the site's own pagination instead of raising it.
Selectors must match rows that are real postings, not navigation links.
When the items carry no link (an unlinked table cell, a list item or a heading
holding only a name), omit "link": each item is then identified by its title."""

_SYSTEM = """\
You author one deterministic scrape recipe for a company's job listing.
Reply with one JSON object and nothing else.
Choose the kind yourself: "api" when a captured JSON response already carries
the postings, "dom" when the postings exist only in the page markup.
Do not include "verification" or "allowed_hosts"; the caller sets them.
Use only the evidence given: every selector, pointer, and URL must come from it.
Every posting URL the recipe yields must match the job-URL pattern.
If a previous attempt failed, the reason is listed; correct that specific fault."""


def build_prompt(
    *,
    url_pattern: str,
    page_url: str,
    artifacts: list[Artifact],
    feedback: list[str],
) -> tuple[str, str]:
    """One prompt holding every artifact of the capture; the kind is the model's call.

    Before F3 parity the caller picked the kind and the model never saw the other
    artifacts, so a page whose postings sat in an XHR response could only ever be
    authored as the caller guessed (and vice versa).

    The reference is measurement, never input (owner, 2026-10-26): the author is
    told which artifact carried the postings, never how many or what they are
    called. A rigid example pulls the model toward reproducing it, and when the
    reference is a nav-link crawl that pull is toward wrong postings.
    """
    parts = [
        "Decide the kind from the evidence below.",
        f"Source URL: {page_url}",
        f"Job-URL pattern (regex): {url_pattern}",
        "",
        "Captured artifacts:",
    ]
    for artifact in artifacts:
        kind_hint = "JSON" if artifact.is_json else "HTML"
        carried = ", carried the postings" if artifact.reference else ""
        parts.append(
            f"- {artifact.name}: {kind_hint}, fetch_mode={artifact.fetch_mode},"
            f" url={artifact.url}{carried}"
        )
    json_artifacts = [a for a in artifacts if a.fetch_mode in _REPLAYABLE_JSON]
    if json_artifacts:
        parts.append("")
        parts.append(
            "A captured JSON response is usable as an api recipe only if its url is"
            " the request the recipe would keep making (the author sees the exact URLs above)."
        )
    parts.extend(["", "Schemas:", _SCHEMA_API, "", _SCHEMA_DOM])
    if feedback:
        parts.append("")
        parts.append("Previous attempts failed:")
        parts.extend(f"- attempt {i}: {reason}" for i, reason in enumerate(feedback, start=1))
    parts.append("")
    parts.append("Evidence:")
    for artifact in artifacts:
        parts.append("")
        parts.append(
            f"--- artifact {artifact.name} (fetch_mode={artifact.fetch_mode}, url={artifact.url}) ---"
        )
        parts.append(artifact.evidence())
    return _SYSTEM, "\n".join(parts)


# --- Authoring --------------------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    """One thing a single capture of a page produced, with where it came from.

    ``fetch_mode`` is how the text arrived: ``static`` (plain GET of the page),
    ``rendered`` (the same page's DOM after the browser ran), ``xhr`` (a JSON
    response the rendered page itself received), ``ats_board`` (JSON an ATS
    adapter fetched from a board link), ``json_ld`` (the page's
    ``application/ld+json`` blocks) and ``inline`` (the largest JSON-shaped
    inline script body). ``url`` is the request that produced it, or the page URL
    for the two bodies that were never a request.
    ``reference`` is the posting list the existing scraper built from *this*
    artifact, so the author is never told a verdict — only what each artifact
    holds.
    """

    name: str
    fetch_mode: FetchMode
    url: str
    source: str
    reference: tuple[ScrapedJob, ...] = ()

    @property
    def is_json(self) -> bool:
        """True when the evidence is JSON, so it is reduced as JSON."""
        return self.fetch_mode in _JSON_EVIDENCE

    @property
    def is_replayable(self) -> bool:
        """True when the artifact is a request a recipe can make again."""
        return self.fetch_mode in _REPLAYABLE_JSON

    def evidence(self) -> str:
        """The artifact reduced to what the LLM may read.

        A capture that is not valid JSON (truncated, HTML error body) is reported
        as such instead of failing the whole authoring run: the other artifacts
        may still hold the postings.
        """
        if not self.is_json:
            return reduce_html(self.source)
        try:
            return reduce_json(self.source)
        except ValueError as exc:
            return f"(artifact is not usable JSON: {exc})"


@dataclass(frozen=True)
class AuthorInput:
    """What one authoring run is given.

    ``artifacts`` is the whole capture: static HTML, the rendered DOM, every
    JSON response, and any ATS board JSON. ``reference`` is the posting list the
    original fetch produced and ``carried_by`` names the artifact that produced
    it, so a dom replay reads that same artifact (rendered if it was rendered).
    ``fetch`` performs one plain GET for ``api`` replays and ``fetch_html`` one
    for the pages of a paginated ``dom`` replay (page one comes from the
    capture, pages 2+ are live). ``allowed_hosts`` comes from the company, never
    from the LLM.

    The owner is exactly one of ``company_id`` (a company careers page) or
    ``target_url`` (an M1 listing page, which is not a company). Only a company
    carries trial state; a listing URL has none, so its authoring is one attempt.
    """

    allowed_hosts: list[str]
    url_pattern: str
    page_url: str
    reference: list[ScrapedJob]
    artifacts: list[Artifact]
    carried_by: str
    fetch: Callable[[str], str] | None = None
    fetch_html: Callable[[str], str] | None = None
    company_id: int | None = None
    target_url: str | None = None

    def __post_init__(self) -> None:
        if (self.company_id is None) == (self.target_url is None):
            raise ValueError("AuthorInput needs exactly one owner: company_id or target_url")

    @property
    def is_company(self) -> bool:
        return self.company_id is not None

    def artifact(self, name: str) -> Artifact:
        for artifact in self.artifacts:
            if artifact.name == name:
                return artifact
        raise ValueError(f"carried_by names no artifact: {name!r}")

    @property
    def json_artifacts(self) -> list[Artifact]:
        """The JSON artifacts a recipe can replay (never JSON-LD or an inline blob)."""
        return [a for a in self.artifacts if a.is_replayable]

    def dom_source(self) -> tuple[str, str]:
        """The HTML a dom recipe is replayed on, and the artifact it came from.

        Order: the artifact that carried the reference, else the rendered DOM,
        else the static page. Never the static page when a render exists: the
        postings are what the recipe is written against.
        """
        carried = self.artifact(self.carried_by)
        if carried.fetch_mode in ("static", "rendered"):
            return carried.source, carried.name
        for mode in ("rendered", "static"):
            for artifact in self.artifacts:
                if artifact.fetch_mode == mode:
                    return artifact.source, artifact.name
        raise ValueError("a dom replay needs an HTML artifact in the capture")


@dataclass(frozen=True)
class Trial:
    """One authoring attempt.

    ``body`` is the model's raw response, kept even when the attempt failed: the
    reason alone cannot be re-diagnosed after the run, and a failed trial's body
    is the only record of what the model actually wrote (2026-11-02).
    """

    number: int
    passed: bool
    reason: str | None
    body: str | None = None


@dataclass(frozen=True)
class AuthorResult:
    status: Literal["verified", "failed"]
    trials: list[Trial]
    recipe_id: int | None
    # ``None`` for a listing URL: only a company carries trial state.
    recipe_state: str | None
    kind: Literal["api", "dom"] | None = None
    # Artifact the replay read, so a later run can replay the same source. The
    # company_recipes table has no column for it yet (F3 note): it lives on the
    # result and in the benchmark snapshot.
    source_artifact: str | None = None


def _verification_for(inp: AuthorInput) -> Verification:
    return Verification.model_validate(
        {
            "expected_count": len(inp.reference),
            "sample_titles": [job.title for job in inp.reference[:_SAMPLE_TITLES]],
            "url_pattern": inp.url_pattern,
        }
    )


def _attempt(
    raw: str, inp: AuthorInput, verification: Verification
) -> tuple[dict | None, str | None, str | None]:
    """Judge one LLM reply. Returns (body, None, source artifact) on pass.

    The reply's own ``kind`` selects the interpreter; a kind outside ``api`` /
    ``dom`` is a trial failure, not a caller error.
    """
    try:
        body = json.loads(raw)
    except ValueError as exc:
        return None, f"reply is not JSON: {exc}", None
    if not isinstance(body, dict):
        return None, "reply is not a JSON object", None
    kind = body.get("kind")
    if kind not in ("api", "dom"):
        return None, f"reply kind must be 'api' or 'dom', got {kind!r}", None
    body["allowed_hosts"] = list(inp.allowed_hosts)
    body["verification"] = verification.model_dump()
    try:
        recipe = parse_body(body, kind)
    except ValueError as exc:
        return None, f"admission rejected: {exc}", None
    try:
        if isinstance(recipe, ApiRecipe):
            assert inp.fetch is not None  # checked in author_recipe
            postings = run_api_recipe(recipe, inp.fetch, inp.page_url)
            source_artifact = _artifact_for_url(inp, recipe.url_template)
        else:
            html, source_artifact = inp.dom_source()
            if recipe.pagination.kind == "none":
                postings = run_dom_recipe(recipe, html, inp.page_url)
            elif inp.fetch_html is None:
                # Page one is in the capture; pages 2+ are not. Without a live
                # HTML fetch a paginated body cannot be judged, and a trial that
                # only read page one would pass a recipe that cannot page.
                return None, "replay failed: a paginated dom recipe needs an html fetch, none available", None
            else:
                postings = list(
                    run_dom_recipe_report(recipe, inp.fetch_html, inp.page_url, first_html=html).jobs
                )
    except RecipeError as exc:
        return None, f"replay failed: {exc}", None
    except ValueError as exc:
        return None, f"replay failed: {exc}", None
    result = verify_postings(postings, recipe.verification)
    if not result.ok:
        return None, f"verification: {result.reason}", None
    return body, None, source_artifact


def _artifact_for_url(inp: AuthorInput, template: str) -> str | None:
    """Name of the JSON artifact whose URL the recipe template replays.

    The host is literal (``literal_host`` rejects templates there), so the match
    is host plus the longest shared leading path. Unmatched returns ``None``: the
    model is allowed to author an endpoint the capture never saw.
    """
    literal = re.split(r"[{?]", template, maxsplit=1)[0]
    host = re.split(r"[/?#]", literal[len("https://") :], maxsplit=1)[0].lower()
    prefix = literal[len("https://") + len(host) :].rstrip("/")
    best: tuple[int, str] | None = None
    for artifact in inp.json_artifacts:
        parts = re.split(r"[/?#]", artifact.url[len("https://") :], maxsplit=1)
        if not artifact.url.startswith("https://") or parts[0].lower() != host:
            continue
        shared = 0
        while shared < min(len(prefix), len(parts[1])) and prefix[shared] == parts[1][shared]:
            shared += 1
        if best is None or shared > best[0]:
            best = (shared, artifact.name)
    return best[1] if best else None


def author_recipe(
    session: Session, company: Company | None, inp: AuthorInput, llm: JsonCompleter
) -> AuthorResult:
    """Run up to ``RECIPE_TRIAL_CAP`` trials for one company; store the recipe on a pass.

    The LLM chooses the kind (``api`` or ``dom``) from the artifact set; code
    overwrites the trust fields and replays whatever it chose.

    ``company`` is ``None`` for an M1 listing URL (``inp.target_url``): a listing
    page is not a company, so the recipe is stored with ``scope='m1_url'`` and
    there is no ``recipe_state``/trial counter to read or write — the owner
    decided one call per source per recipe version, so the URL path gets exactly
    one trial.

    Raises ``RecipeBlockedError`` before any LLM call if the company is already in
    ``recipe_fail``. Raises ``ValueError`` only for a caller error: a mismatched
    company, an empty reference, an empty artifact set, a ``carried_by`` naming
    no artifact, or a capture with a JSON artifact but no ``fetch`` to replay
    through. A reference that misses the URL pattern or a sample title is
    evidence about the source, not a reason to skip the company (owner,
    2026-10-19). The caller commits the session.
    """
    if company is None:
        if inp.target_url is None or inp.company_id is not None:
            raise ValueError("authoring a listing needs target_url and no company")
        scope, owner = SCOPE_URL, {"target_url": inp.target_url}
        max_trials, state = 1, None
    else:
        if inp.company_id != company.id:
            raise ValueError("AuthorInput.company_id does not match the company")
        scope, owner = SCOPE_COMPANY, {"company_id": company.id}
        max_trials, state = RECIPE_TRIAL_CAP, company.recipe_state
        ensure_trials_allowed(company)
    if not inp.reference:
        raise ValueError("authoring needs a non-empty reference from the original fetch")
    if not inp.artifacts:
        raise ValueError("authoring needs at least one captured artifact")
    inp.artifact(inp.carried_by)  # raises when the name is unknown
    if inp.json_artifacts and inp.fetch is None:
        raise ValueError("a capture with a JSON artifact needs a fetch: the author may choose api")
    # No reference-versus-pattern gate: it removed eight companies from the
    # benchmark for mixed-host boards and nav links in the reference (owner,
    # 2026-10-19). The verification block still carries the pattern and titles,
    # but replay only has to return something; the benchmark scores the rest.
    verification = _verification_for(inp)

    feedback: list[str] = []
    trials: list[Trial] = []
    for number in range(1, max_trials + 1):
        system, user = build_prompt(
            url_pattern=inp.url_pattern,
            page_url=inp.page_url,
            artifacts=inp.artifacts,
            feedback=feedback,
        )
        raw = llm.complete_json(system, user)
        body, reason, source_artifact = _attempt(raw, inp, verification)
        if reason is None and body is not None:
            kind = body["kind"]
            if company is not None:
                state = record_trial(session, company, passed=True)
            now = datetime.now(UTC)
            row = CompanyRecipe(
                scope=scope,
                kind=kind,
                # Written as verified only here, after the verifier passed.
                status=STATUS_VERIFIED,
                body=json.dumps(body),
                model_id=llm.model,
                verification=verification.model_dump_json(),
                created_at=now,
                updated_at=now,
                **owner,
            )
            session.add(row)
            session.flush()
            trials.append(Trial(number, True, None, raw))
            logger.info(
                "recipe verified %s kind=%s trial=%d",
                owner,
                kind,
                number,
            )
            return AuthorResult("verified", trials, row.id, state, kind, source_artifact)
        if company is not None:
            state = record_trial(session, company, passed=False)
        trials.append(Trial(number, False, reason, raw))
        feedback.append(reason or "unknown")
        logger.info("recipe trial failed %s trial=%d: %s", owner, number, reason)
        if state == RECIPE_FAIL:
            break
    return AuthorResult("failed", trials, None, state)
