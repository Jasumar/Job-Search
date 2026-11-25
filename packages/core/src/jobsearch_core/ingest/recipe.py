"""Recipe schema, generic interpreter, and replay verifier (Phase F1, plan-scraping.md §Recipe authoring).

A recipe is a recorded path to a company's job list: either a JSON request
(``api``) or a CSS-selected listing in a page (``dom``). This module is pure:
it makes no LLM call and writes nothing. The caller supplies ``fetch`` (for
``api``) or the page HTML (for ``dom``), so tests run without network.

Invariants enforced here:

- A request is made only to a host in ``allowed_hosts`` (exact hostname match).
- A pagination loop stops on an empty page, a page that adds no new posting
  URL, a repeated URL, or ``max_pages``. The first page is always requested.
  A ``dom`` page after the first that cannot be read stops the loop with what
  was already collected; only the first page can fail a replay.
- A replay that yields nothing is a failure, never an empty success
  (``verify_postings``).
- Any request failure or unresolvable ``list_path`` raises ``RecipeError``.
  It is never turned into an empty list.

F1 scope: ``dom`` selectors are CSS only (BeautifulSoup has no XPath); ``api``
requests are GET only, with literal headers; pagination is single-level.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from jobsearch_core.ingest.scrapers.ats_json import _html_to_text, _iso_date, _text
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pagination import normalize_url

Pointer = list[str | int]
Transform = Literal["text", "html_text", "iso_date"]

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_BARE_HOST = re.compile(r"^[a-z0-9.-]+$")
# An item's top-level key, as used by field_map.url_template ({id}, not {item.id}).
_ITEM_KEY = re.compile(r"^[A-Za-z_][\w-]*$")


class RecipeError(Exception):
    """A recipe could not be replayed (request failed, bad shape, host refused)."""


class _Strict(BaseModel):
    """Recipe bodies are admitted only if they carry no unknown keys and no coerced types."""

    model_config = ConfigDict(extra="forbid", strict=True)


def literal_host(url: str) -> str:
    """Return the literal hostname of an https URL; raise ValueError otherwise.

    A templated host (``{...}``), userinfo, or a port is refused: the host a
    recipe contacts must be fixed in the body, so the allowlist can check it.
    """
    if not url.startswith("https://"):
        raise ValueError(f"URL must be https: {url!r}")
    authority = re.split(r"[/?#]", url[len("https://") :], maxsplit=1)[0]
    if any(ch in authority for ch in "{}@:"):
        raise ValueError(f"URL host must be a literal hostname: {url!r}")
    return authority.lower()


def _check_placeholders(template: str, allowed_roots: tuple[str, ...]) -> None:
    """Refuse a placeholder the interpreter cannot substitute.

    ``allowed_roots`` must be the interpreter's own resolvable set, never a guess
    at what sites use: a placeholder that passes here and cannot be substituted
    reaches the request URL as a literal ``{name}``.
    """
    for name in _PLACEHOLDER.findall(template):
        if name.split(".", 1)[0] not in allowed_roots:
            allowed = ", ".join(sorted(set(allowed_roots))) or "nothing"
            raise ValueError(
                f"unknown placeholder {{{name}}}: this template resolves only {allowed}; "
                f"declare the value in vars (or use it as pagination.param) to use {{{name}}}"
            )


def _check_allowed_hosts(hosts: list[str]) -> list[str]:
    for host in hosts:
        if not _BARE_HOST.match(host):
            raise ValueError(f"allowed host must be a lowercase bare hostname: {host!r}")
    return hosts


class Verification(_Strict):
    expected_count: int = Field(ge=1)
    sample_titles: list[str] = Field(min_length=1)
    url_pattern: str

    @model_validator(mode="after")
    def _pattern_compiles(self) -> Verification:
        try:
            re.compile(self.url_pattern)
        except re.error as exc:
            raise ValueError(f"url_pattern does not compile: {exc}") from exc
        return self


class Extract(_Strict):
    path: Pointer = Field(min_length=1)
    transform: Transform = "text"
    # Used when ``path`` yields nothing, mirroring adapter fallbacks (e.g. city).
    fallback: Extract | None = None


class FieldMap(_Strict):
    """Maps one list item to a ScrapedJob. Exactly one of ``url`` / ``url_template``.

    ``url_template`` is formatted with the item's top-level scalar keys, for
    example ``https://jobs.example.com/acme/{id}``. Placeholders are bare item
    keys: ``{item.id}`` is the detail-recipe syntax, resolves nothing here, and
    would silently drop every posting at replay time, so it is refused at
    admission instead.
    """

    url: Extract | None = None
    url_template: str | None = None
    title: Extract
    location: Extract | None = None
    posted_date: Extract | None = None
    description: Extract | None = None
    employment_type: Extract | None = None
    seniority: Extract | None = None

    @field_validator("url_template")
    @classmethod
    def _template_is_replayable(cls, value: str | None) -> str | None:
        """Refuse a listing URL template that cannot format into a real URL.

        The host is checked as literal (https, no ``{...}``, no port) but is not
        required to be on ``allowed_hosts``: the template names where postings
        live, and that is often a different host from the API being replayed
        (``jobs.smartrecruiters.com`` postings from ``api.smartrecruiters.com``).
        """
        if value is None:
            return None
        literal_host(value)
        for name in _PLACEHOLDER.findall(value):
            if not _ITEM_KEY.match(name):
                raise ValueError(
                    "field_map.url_template takes bare item keys as placeholders"
                    f" (e.g. {{id}}), got {{{name}}}"
                )
        return value

    @model_validator(mode="after")
    def _exactly_one_url(self) -> FieldMap:
        if (self.url is None) == (self.url_template is None):
            raise ValueError("field_map needs exactly one of url or url_template")
        return self


class Pagination(_Strict):
    kind: Literal["none", "page", "offset", "cursor"] = "none"
    # Name of the URL-template placeholder carrying the page/offset/cursor value.
    param: str | None = None
    # Initial value: page 1, offset 0, cursor "" when None.
    start: int | str | None = None
    # Offset step (the page size). Required for offset; page defaults to 1.
    step: int | None = None
    # Pointer into the response to the next cursor (cursor kind only).
    cursor_path: Pointer | None = None
    max_pages: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def _kind_requirements(self) -> Pagination:
        if self.kind == "none":
            if self.param is not None or self.step is not None:
                raise ValueError("pagination kind none takes no param or step")
            return self
        if not self.param:
            raise ValueError(f"pagination kind {self.kind} needs a param")
        if self.kind == "offset" and (self.step is None or self.step < 1):
            raise ValueError("pagination kind offset needs a positive step")
        if self.kind == "cursor" and not self.cursor_path:
            raise ValueError("pagination kind cursor needs cursor_path")
        return self


class Detail(_Strict):
    """Per-item second request (SmartRecruiters-style). Failure leaves description None."""

    url: str
    description_paths: list[Pointer] = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _url_ok(cls, url: str) -> str:
        literal_host(url)
        _check_placeholders(url, ("vars", "item"))
        return url


class ApiRecipe(_Strict):
    kind: Literal["api"]
    allowed_hosts: list[str] = Field(min_length=1, max_length=8)
    verification: Verification
    method: Literal["GET"] = "GET"
    url_template: str
    # Literal values substituted into url_template and url_template fields
    # (e.g. a company slug that the list items do not carry).
    vars: dict[str, str] = Field(default_factory=dict)
    pagination: Pagination = Field(default_factory=Pagination)
    list_path: Pointer = Field(default_factory=list)
    field_map: FieldMap
    detail: Detail | None = None

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts_ok(cls, hosts: list[str]) -> list[str]:
        return _check_allowed_hosts(hosts)

    @model_validator(mode="after")
    def _template_uses_param(self) -> ApiRecipe:
        if self.pagination.kind != "none" and f"{{{self.pagination.param}}}" not in self.url_template:
            raise ValueError(f"url_template must contain {{{self.pagination.param}}}")
        # The placeholders a body can resolve are exactly the ones the interpreter
        # substitutes: the vars keys and pagination.param (run_api_recipe_report).
        # A fixed page/offset/cursor list made every other param name unsatisfiable
        # — it demanded the template carry {param} and then rejected it
        # (2026-11-02: a real API using param="start" could not be authored at all).
        param = self.pagination.param if self.pagination.kind != "none" else None
        _check_placeholders(self.url_template, (*self.vars, *((param,) if param else ())))
        if literal_host(self.url_template) not in set(self.allowed_hosts):
            raise ValueError("url_template host is not in allowed_hosts")
        if self.detail is not None and literal_host(self.detail.url) not in set(self.allowed_hosts):
            raise ValueError("detail url host is not in allowed_hosts")
        return self


class DomExtract(_Strict):
    """A CSS selector relative to the job container. Empty selector = the container."""

    selector: str = ""
    attr: str | None = None


class DomPagination(_Strict):
    """How a ``dom`` replay reaches the pages after the first.

    ``page`` / ``offset`` rewrite one query parameter of the listing URL
    (``param``): the value replaces ``{param}`` when the URL carries it and is
    appended otherwise, so a recipe can be authored against a plain listing URL
    that the pager later parameterizes. ``next`` follows the ``href`` of the
    anchor ``selector`` matches on the page just read — the recipe's own
    next-page signal, never a guess from the markup.

    ``max_pages`` counts documents fetched, not steps taken, and is capped
    lower than an ``api`` recipe's budget: every page is a full HTML document,
    so ten documents is already a heavy replay.
    """

    kind: Literal["none", "page", "offset", "next"] = "none"
    # Query parameter carrying the page/offset value (page/offset kinds only).
    param: str | None = None
    # First value: page 1, offset 0 when None.
    start: int | None = None
    # Offset step (the page size). Required for offset; page defaults to 1.
    step: int | None = None
    # CSS selector for the next-page anchor, resolved against the page (next kind).
    selector: str | None = None
    max_pages: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def _kind_requirements(self) -> DomPagination:
        if self.kind == "none":
            if any(v is not None for v in (self.param, self.start, self.step, self.selector)):
                raise ValueError("pagination kind none takes no param, start, step, or selector")
            return self
        if self.kind in ("page", "offset"):
            if not self.param:
                raise ValueError(
                    f"pagination kind {self.kind} needs a param (the query parameter carrying the page)"
                )
            if self.selector is not None:
                raise ValueError(f"pagination kind {self.kind} takes a param, not a selector")
            if self.step is not None and self.step < 1:
                raise ValueError("pagination step must be >= 1")
            if self.kind == "offset" and self.step is None:
                raise ValueError("pagination kind offset needs a positive step")
            return self
        if not self.selector:
            raise ValueError("pagination kind next needs a selector for the next-page anchor")
        if self.param is not None or self.start is not None or self.step is not None:
            raise ValueError("pagination kind next takes a selector, not a param, start, or step")
        return self


class DomRecipe(_Strict):
    kind: Literal["dom"]
    allowed_hosts: list[str] = Field(min_length=1, max_length=8)
    verification: Verification
    container: str = Field(min_length=1)
    # None = text-only items (an unlinked table cell, a prose list, a heading):
    # an item's identity is then its name, see ``name_identity``.
    link: DomExtract | None = None
    title: DomExtract
    location: DomExtract | None = None
    pagination: DomPagination = Field(default_factory=DomPagination)

    @field_validator("allowed_hosts")
    @classmethod
    def _hosts_ok(cls, hosts: list[str]) -> list[str]:
        return _check_allowed_hosts(hosts)

    @model_validator(mode="after")
    def _link_has_attr(self) -> DomRecipe:
        if self.link is not None and not self.link.attr:
            raise ValueError("dom link needs an attr (e.g. href)")
        return self


Recipe = Annotated[ApiRecipe | DomRecipe, Field(discriminator="kind")]


# (location suffix, pydantic error type) -> the sentence that says what was meant.
# The schema itself stays strict; the rejection explains the intent, so an author
# that guessed a plausible-but-wrong shape learns the right one from the error
# instead of spending trials on it (2026-10-25: Amazon burned all three trials on
# url_template nested inside url plus a max_pages above the cap).
_ADMISSION_HINTS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("url_template",),
        "extra_forbidden",
        'url_template is a direct child of field_map, beside "url": '
        'field_map.url_template (never inside field_map.url)',
    ),
    (
        ("max_pages",),
        "less_than_equal",
        "max_pages is a fetch budget: api caps at 50, dom at 20 (a page is a "
        "whole document). Page through the site's own pagination instead "
        "(kind/param/step, or kind=next with a selector) rather than raising it",
    ),
    (
        ("field_map",),
        "value_error",
        'field_map takes exactly one of "url": {"path": [...]} or a top-level '
        '"url_template": "https://<host>/.../{id}"',
    ),
    (
        ("verification",),
        "value_error",
        "verification takes url_pattern plus expected_count; sample_titles stay "
        "outside the regex",
    ),
)


def _explain_validation(exc: ValidationError) -> str:
    """Render a ``ValidationError`` as located errors plus the intended shape."""
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        lines.append(f"  {loc or '<body>'}: {err.get('msg', '')}")
        for suffix, err_type, hint in _ADMISSION_HINTS:
            if err.get("type") != err_type:
                continue
            tail = tuple(str(part) for part in err.get("loc", ()))[-len(suffix) :]
            if tail == suffix:
                lines.append(f"    -> {hint}")
                break
    return "\n".join(lines) if lines else str(exc)


def parse_body(body: dict | str, column_kind: str) -> ApiRecipe | DomRecipe:
    """Admit a recipe body for storage, or raise ``ValueError``.

    Shape and allowlist are checked here, before any replay HTTP request. The
    body's ``kind`` must equal the ``company_recipes.kind`` column the caller
    wants to write; a mismatch is rejected rather than coerced.
    """
    data = json.loads(body) if isinstance(body, str) else body
    if not isinstance(data, dict):
        raise ValueError("recipe body must be a JSON object")
    if data.get("kind") != column_kind:
        raise ValueError(f"body kind {data.get('kind')!r} does not match column kind {column_kind!r}")
    try:
        return TypeAdapter(Recipe).validate_python(data)
    except ValidationError as exc:
        raise ValueError(f"recipe body rejected:\n{_explain_validation(exc)}") from exc



@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a replay: one gate, plus the signals a report can score.

    Only an empty replay fails. Every other field is measured, never enforced:
    a nav link or a button label is evidence about the recipe, not a reason to
    discard postings the site really has.
    """

    ok: bool
    reason: str | None = None
    # Postings the replay produced. Compare with ``Verification.expected_count``
    # to spot drift; never compare for equality.
    count: int = 0
    # Postings whose URL does not match ``verification.url_pattern``.
    off_pattern: int = 0
    # Sample titles the replay did not produce (casefolded, whitespace-folded match).
    missing_titles: tuple[str, ...] = ()
    # Postings not in ``reference_urls``, when the caller supplied a reference set.
    outside_reference: int = 0


def _check_host(url: str, allowed_hosts: Iterable[str]) -> None:
    host = (urlparse(url).hostname or "").lower()
    allowed = {h.lower() for h in allowed_hosts}
    if host not in allowed:
        raise RecipeError(f"host {host or '(none)'} is not in allowed_hosts {sorted(allowed)}: {url}")


def _pointer(node: object, path: Pointer) -> object:
    for key in path:
        if isinstance(node, dict) and isinstance(key, str):
            node = node.get(key)
        elif isinstance(node, list) and isinstance(key, int) and 0 <= key < len(node):
            node = node[key]
        else:
            return None
        if node is None:
            return None
    return node


def _describe(node: object) -> str:
    """Name what a pointer resolved to, so a wrong path can be corrected.

    "does not resolve to a list" alone leaves the author guessing whether the
    path was wrong or the response was different from the capture; the observed
    keys are the evidence that separates the two (2026-11-02).
    """
    if isinstance(node, dict):
        keys = list(node)[:8]
        rest = f", +{len(node) - 8} more" if len(node) > 8 else ""
        return f"an object with keys {keys}{rest}"
    if isinstance(node, list):
        return f"a list of {len(node)}"
    return f"{type(node).__name__} {node!r}"[:120]


def _apply(value: object, transform: Transform) -> str | None:
    if value is None:
        return None
    if transform == "text":
        return _text(value)
    if transform == "html_text":
        return _html_to_text(value)
    return _iso_date(value)


def _extract(item: dict, spec: Extract | None) -> str | None:
    if spec is None:
        return None
    value = _apply(_pointer(item, spec.path), spec.transform)
    if not value and spec.fallback is not None:
        return _extract(item, spec.fallback)
    return value


@dataclass
class DetailTally:
    """How many detail requests a replay made, and how many were answered.

    ``attempted`` counts every per-item request the recipe asked for;
    ``succeeded`` counts the ones whose body arrived as JSON. A body with no
    text at the description paths is a success: the request worked, the page
    simply says nothing there. R3's detail-success router reads this.

    ``last_error`` keeps the reason the most recent request failed. Without it a
    replay can report "detail 3/12" with no cause, which is not something an
    author can act on (2026-11-02).
    """

    attempted: int = 0
    succeeded: int = 0
    last_error: str | None = None


def _detail_description(
    item: dict,
    recipe: ApiRecipe,
    fetch: Callable[[str], str],
    tally: DetailTally | None = None,
) -> str | None:
    """Fetch the per-item detail and join its description paths.

    A detail request that fails (host refused, transport error, non-JSON)
    returns None and keeps the job. The list replay is not failed by it, but the
    reason is kept on the tally so the failure is diagnosable.
    """
    detail = recipe.detail
    if detail is None:
        return None
    if tally is not None:
        tally.attempted += 1
    try:
        url = _render_template(detail.url, item, recipe.vars)
        _check_host(url, recipe.allowed_hosts)
        payload = json.loads(fetch(url))
    except (RecipeError, RuntimeError, ValueError, KeyError) as exc:
        if tally is not None:
            tally.last_error = f"{type(exc).__name__}: {exc}"
        return None
    if tally is not None:
        tally.succeeded += 1
    parts = []
    for path in detail.description_paths:
        value = _html_to_text(_pointer(payload, path))
        if value:
            parts.append(value)
    return "\n\n".join(parts) or None


def _render_template(template: str, item: dict, literals: dict[str, str]) -> str:
    scalars = {k: str(v) for k, v in item.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    context = {"item": scalars, "vars": literals}

    def lookup(match: re.Match[str]) -> str:
        root, _, key = match.group(1).partition(".")
        if root not in context or key not in context[root]:
            raise KeyError(match.group(1))
        return str(context[root][key])

    return re.sub(r"\{([^{}]+)\}", lookup, template)


def _job_from_item(
    item: dict,
    field_map: FieldMap,
    base_url: str,
    literals: dict[str, str],
    recipe: ApiRecipe | None = None,
    fetch: Callable[[str], str] | None = None,
    tally: DetailTally | None = None,
) -> tuple[ScrapedJob | None, str | None]:
    """Build one posting, or return the reason this item was skipped.

    The reason is evidence, not decoration: when every item on a page is skipped
    the replay yields nothing, and "empty list is never success" cannot say
    whether the list path or the field map was wrong. The caller aggregates the
    reasons into one error (2026-11-02).
    """
    title = _extract(item, field_map.title)
    if field_map.url is not None:
        raw_url = _extract(item, field_map.url)
    else:
        scalars = {k: str(v) for k, v in item.items() if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
        scalars.update(literals)
        try:
            raw_url = field_map.url_template.format_map(scalars)  # type: ignore[union-attr]
        except KeyError as exc:
            return None, (
                f"url_template placeholder {{{exc.args[0]}}} is neither an item key nor a var "
                f"(item keys: {sorted(scalars)[:10]})"
            )
    if not title:
        return None, f"no title at {field_map.title.path}"
    if not raw_url:
        where = field_map.url.path if field_map.url is not None else "url_template"
        return None, f"no url at {where}"
    description = _extract(item, field_map.description)
    if recipe is not None and recipe.detail is not None and fetch is not None:
        description = _detail_description(item, recipe, fetch, tally)
    return (
        ScrapedJob(
            url=urljoin(base_url, raw_url),
            title=title,
            company="",
            location=_extract(item, field_map.location),
            posted_date=_extract(item, field_map.posted_date),
            employment_type=_extract(item, field_map.employment_type),
            seniority=_extract(item, field_map.seniority),
            description=description,
        ),
        None,
    )


@dataclass(frozen=True)
class ApiReplay:
    """What one ``api`` replay did, beyond the postings it produced.

    ``page_counts`` holds the postings each request added, in request order, so
    ``page_advance`` can say whether a second page contributed anything.
    ``detail_attempted`` / ``detail_succeeded`` come from the per-item requests,
    and ``detail_error`` carries the reason the last of them failed.
    R3's statistical routers read these; nothing here gates a replay.
    """

    jobs: list[ScrapedJob]
    page_counts: tuple[int, ...] = ()
    detail_attempted: int = 0
    detail_succeeded: int = 0
    detail_error: str | None = None

    @property
    def page_advance(self) -> bool:
        """True when a page after the first added at least one new posting."""
        return any(count > 0 for count in self.page_counts[1:])


def run_api_recipe(recipe: ApiRecipe, fetch: Callable[[str], str], base_url: str) -> list[ScrapedJob]:
    """Replay an ``api`` recipe. ``fetch`` performs one plain GET and returns JSON text.

    Callers pass ``PoliteClient.fetch_json``: it refuses a body that is not JSON
    and reports the tier/status/content-type it got, so a failure here is
    attributed to the transport rather than surfacing as a parse error.
    ``base_url`` resolves relative posting URLs. Raises ``RecipeError`` on any
    request failure, non-JSON body, host refusal, or ``list_path`` that does not
    resolve to a list.
    """
    return run_api_recipe_report(recipe, fetch, base_url).jobs


def run_api_recipe_report(
    recipe: ApiRecipe, fetch: Callable[[str], str], base_url: str
) -> ApiReplay:
    """``run_api_recipe`` plus the per-page and detail counts R3 reports."""
    pagination = recipe.pagination
    cursor: object = pagination.start if pagination.start is not None else ("" if pagination.kind == "cursor" else None)
    if pagination.kind == "page" and cursor is None:
        cursor = 1
    if pagination.kind == "offset" and cursor is None:
        cursor = 0

    found: dict[str, ScrapedJob] = {}
    page_counts: list[int] = []
    tally = DetailTally()
    max_requests = pagination.max_pages if pagination.kind != "none" else 1
    for _ in range(max_requests):
        url = recipe.url_template
        for name, value in recipe.vars.items():
            url = url.replace(f"{{{name}}}", value)
        if pagination.kind != "none":
            url = url.replace(f"{{{pagination.param}}}", str(cursor))
        leftover = _PLACEHOLDER.findall(url)
        if leftover:
            # Admission derives its allowed roots from vars + param, so this is a
            # value that itself carried braces. Fail here rather than request a
            # URL with a literal placeholder in it.
            raise RecipeError(f"placeholder {{{leftover[0]}}} could not be substituted in {url}")
        _check_host(url, recipe.allowed_hosts)
        try:
            payload = json.loads(fetch(url))
        except (RuntimeError, ValueError) as exc:
            raise RecipeError(f"request failed for {url}: {exc}") from exc

        items = _pointer(payload, recipe.list_path)
        if not isinstance(items, list):
            raise RecipeError(
                f"list_path {recipe.list_path} does not resolve to a list: {_explain_path(payload, recipe.list_path)}"
                f" (request: {url})"
            )
        if not items:
            if not page_counts:
                # An empty first page is a failure, not an empty success: say which
                # request came back empty instead of letting it surface as
                # "empty list is never success" with no URL (2026-11-02).
                raise RecipeError(f"list_path {recipe.list_path} resolved to an empty list: {url}")
            # Record the request that returned nothing: a recipe whose pager
            # walks into an empty page is a different fault from one that loops
            # on the first page, and R3 reports both as "no advance".
            page_counts.append(0)
            break

        new = 0
        drops: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                drops["item is not an object"] = drops.get("item is not an object", 0) + 1
                continue
            job, drop = _job_from_item(item, recipe.field_map, base_url, recipe.vars, recipe, fetch, tally)
            if job is None:
                reason = drop or "skipped"
                drops[reason] = drops.get(reason, 0) + 1
                continue
            if job.url in found:
                continue
            found[job.url] = job
            new += 1
        if not found and drops:
            # The list path resolved but no item became a posting: name the field
            # that dropped them, so the next trial changes that field instead of
            # guessing (2026-11-02).
            reasons = "; ".join(f"{n}x {reason}" for reason, n in sorted(drops.items(), key=lambda kv: -kv[1])[:3])
            noun = "item" if len(items) == 1 else "items"
            raise RecipeError(
                f"list_path {recipe.list_path} returned {len(items)} {noun} at {url} "
                f"but none became a posting: {reasons}"
            )
        page_counts.append(new)
        if new == 0 or pagination.kind == "none":
            break

        if pagination.kind == "page":
            cursor = int(cursor) + (pagination.step or 1)  # type: ignore[arg-type]
        elif pagination.kind == "offset":
            cursor = int(cursor) + (pagination.step or 0)  # type: ignore[arg-type]
        else:
            next_cursor = _pointer(payload, pagination.cursor_path or [])
            if not next_cursor:
                break
            cursor = str(next_cursor)
    return ApiReplay(
        jobs=list(found.values()),
        page_counts=tuple(page_counts),
        detail_attempted=tally.attempted,
        detail_succeeded=tally.succeeded,
        detail_error=tally.last_error,
    )


def _select(node, selector: str):
    return node if selector == "" else node.select_one(selector)


def _explain_path(node: object, path: Pointer) -> str:
    """Say where a pointer stopped and what it found there.

    "does not resolve to a list" cannot tell a wrong path from an empty list at
    the right path, and the author has to change one or the other.
    """
    walked: list[str] = []
    for key in path:
        here = f"[{', '.join(walked)}]" if walked else "the response"
        if isinstance(node, dict) and isinstance(key, str):
            if key not in node:
                return f"{here} has no key {key!r} (keys: {list(node)[:8]})"
            node = node[key]
        elif isinstance(node, list) and isinstance(key, int):
            if not 0 <= key < len(node):
                return f"{here} is a list of {len(node)}, so index {key} is out of range"
            node = node[key]
        else:
            return f"{here} is {_describe(node)}, so {key!r} cannot be applied"
        walked.append(repr(key))
    here = f"[{', '.join(walked)}]" if walked else "the response"
    return f"{here} is {_describe(node)}"


def _node_value(node, spec: DomExtract) -> str | None:
    if node is None:
        return None
    if spec.attr is None:
        return _text(node.get_text(" ", strip=True))
    return _text(node.get(spec.attr))


def name_identity(title: str) -> str:
    """The identity of a text-only item: its folded name, as a ``name:`` URI.

    A listing of company names may carry no link per item. Every URL-keyed
    mechanism (dedupe, verified-URL baselines, drift statistics) then keys on
    the name instead. The scheme is not http(s), so nothing fetches it, and it
    has no ``#`` fragment for ``_norm_url`` to strip.
    """
    return "name:" + " ".join(title.casefold().split())


def run_dom_recipe(recipe: DomRecipe, html: str, page_url: str) -> list[ScrapedJob]:
    """Replay a ``dom`` recipe against ``html`` fetched from ``page_url``.

    Containers without a link or title are skipped, but a selector that matches
    nothing — or matches containers that never yield both — raises ``RecipeError``
    naming the selector. An empty list cannot distinguish a wrong selector from a
    board with no postings, and the author needs that difference (2026-11-02).
    Links are resolved against ``page_url``.

    This reads exactly one document. Paginated replays go through
    ``run_dom_recipe_report``, which calls this once per page.
    """
    _check_host(page_url, recipe.allowed_hosts)
    soup = BeautifulSoup(html, "html.parser")
    containers = soup.select(recipe.container)
    if not containers:
        raise RecipeError(f"container selector {recipe.container!r} matched no elements on {page_url}")
    jobs: list[ScrapedJob] = []
    seen: set[str] = set()
    no_link = 0
    no_title = 0
    for container in containers:
        href = (
            _node_value(_select(container, recipe.link.selector), recipe.link)
            if recipe.link is not None
            else None
        )
        title = _node_value(_select(container, recipe.title.selector), recipe.title)
        if recipe.link is not None and not href:
            no_link += 1
            continue
        if not title:
            no_title += 1
            continue
        url = urljoin(page_url, href) if href else name_identity(title)
        if url in seen:
            continue
        seen.add(url)
        location = None
        if recipe.location is not None:
            location = _node_value(_select(container, recipe.location.selector), recipe.location)
        jobs.append(ScrapedJob(url=url, title=title, company="", location=location))
    if not jobs:
        raise RecipeError(
            f"container {recipe.container!r} matched {len(containers)} elements on {page_url} but none became a "
            f"posting: {no_link} had no link at {recipe.link.selector if recipe.link else None!r}, "
            f"{no_title} had no title at {recipe.title.selector!r}"
        )
    return jobs


# How a paginated ``dom`` replay ended. ``single_page`` is the ``none`` kind;
# every other value names the guard that stopped the loop, so a short listing is
# diagnosable from the run instead of looking like the site had one page.
DOM_STOP_REASONS = frozenset(
    {
        "single_page",
        "no_signal",
        "repeat",
        "no_new_jobs",
        "page_cap",
        "fetch_error",
        "empty_page",
        "offsite",
    }
)


@dataclass(frozen=True)
class DomReplay:
    """What one ``dom`` replay did, beyond the postings it produced.

    ``page_counts`` holds the postings each document added, in fetch order, so
    ``page_advance`` answers whether a second page contributed anything.
    ``first_page_jobs`` is what the first document produced: the DOM-agreement
    check compares against one rendered page, and postings from page 2+ are not
    on it.
    """

    jobs: list[ScrapedJob]
    page_counts: tuple[int, ...] = ()
    pages: tuple[str, ...] = ()
    stop_reason: str = "single_page"
    stop_detail: str | None = None

    @property
    def page_advance(self) -> bool:
        """True when a page after the first added at least one new posting."""
        return any(count > 0 for count in self.page_counts[1:])

    @property
    def first_page_jobs(self) -> tuple[ScrapedJob, ...]:
        """The postings the first document produced, in fetch order."""
        if not self.page_counts:
            return ()
        return tuple(self.jobs[: self.page_counts[0]])


def _page_url(page_url: str, param: str, value: int) -> str:
    """The listing URL for one page value.

    A literal ``{param}`` in the URL is replaced (so ``?page={page}`` and
    ``/jobs/{page}`` both work); otherwise the parameter is appended to the
    query. An existing value for it is replaced, never duplicated — two
    ``page=`` parameters is a request the site reads as page one.
    """
    if f"{{{param}}}" in page_url:
        return page_url.replace(f"{{{param}}}", str(value))
    parts = urlsplit(page_url)
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    pairs.append((param, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def _next_page_url(html: str, selector: str, current_url: str) -> str | None:
    """The href the next-page anchor carries, resolved against ``current_url``.

    ``None`` when the selector matches nothing or the anchor has no usable href
    (empty, a fragment, or a non-http scheme such as ``javascript:``): the site
    is saying there is no next document, which is a clean stop.
    """
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(selector)
    if node is None:
        return None
    href = node.get("href")
    if not isinstance(href, str):
        return None
    href = href.strip()
    if not href or href.startswith("#") or re.match(r"^(javascript|mailto|tel):", href, re.IGNORECASE):
        return None
    target = urljoin(current_url, href)
    if urlsplit(target).scheme not in ("http", "https"):
        return None
    return target


def run_dom_recipe_report(
    recipe: DomRecipe,
    fetch: Callable[[str], str],
    page_url: str,
    *,
    first_html: str | None = None,
) -> DomReplay:
    """``run_dom_recipe`` over every page the recipe's pagination reaches.

    ``first_html`` is the document already fetched for ``page_url`` by a caller
    that needed it for another reason (the replay keeps it for DOM agreement);
    when it is absent the first page is fetched like any other.

    Only the first document can fail the replay: a later page that cannot be
    fetched, or that the recipe cannot read, stops the loop with the postings
    already found and records why. Losing a page one already produced because
    page four timed out would turn a transport fault into an empty board.

    Stops are the four guards from the module contract plus two that only a
    paginated ``dom`` replay can hit: ``empty_page`` (the next document matched
    no container — often the pager landed on a filtered or empty view) and
    ``offsite`` (the next anchor pointed off ``allowed_hosts``, which is
    reported rather than fetched).
    """
    pagination = recipe.pagination
    allowed = {host.lower() for host in recipe.allowed_hosts}
    found: dict[str, ScrapedJob] = {}
    pages: list[str] = []
    page_counts: list[int] = []
    stop_reason = "single_page"
    stop_detail: str | None = None

    max_pages = pagination.max_pages if pagination.kind != "none" else 1
    cursor = pagination.start
    if cursor is None:
        cursor = 1 if pagination.kind == "page" else 0

    # The first request is the listing URL the recipe was validated on, never a
    # rewritten one: page one of a site is not always ``?page=1`` (some start at
    # 0, some at no parameter at all), and the recipe was authored against that
    # exact document. Parameter paging therefore begins at ``start + step`` for
    # the second request. A literal ``{param}`` is the exception — that URL does
    # not exist unsubstituted, so the first request carries ``start``.
    url = page_url
    if pagination.kind in ("page", "offset") and pagination.param:
        if f"{{{pagination.param}}}" in page_url:
            url = _page_url(page_url, pagination.param, cursor)
    html = first_html
    visited = {normalize_url(url), normalize_url(page_url)}

    for index in range(max_pages):
        _check_host(url, recipe.allowed_hosts)
        if html is None:
            try:
                html = fetch(url)
            except (RuntimeError, ValueError) as exc:
                if index == 0:
                    raise RecipeError(f"request failed for {url}: {exc}") from exc
                stop_reason, stop_detail = "fetch_error", f"{url}: {exc}"
                break
        try:
            postings = run_dom_recipe(recipe, html, url)
        except RecipeError as exc:
            if index == 0:
                raise
            stop_reason, stop_detail = "empty_page", str(exc)
            break

        pages.append(url)
        new = 0
        for job in postings:
            if job.url in found:
                continue
            found[job.url] = job
            new += 1
        page_counts.append(new)
        if new == 0:
            stop_reason, stop_detail = "no_new_jobs", f"{url} contributed no unseen postings"
            break
        if pagination.kind == "none":
            break

        if pagination.kind == "next":
            target = _next_page_url(html, pagination.selector or "", url)
            if target is None:
                stop_reason = "no_signal"
                break
        else:
            step = pagination.step if pagination.step is not None else (1 if pagination.kind == "page" else 0)
            cursor += step
            target = _page_url(page_url, pagination.param or "", cursor)

        if (urlsplit(target).hostname or "").lower() not in allowed:
            stop_reason, stop_detail = "offsite", target
            break
        key = normalize_url(target)
        if key in visited:
            stop_reason, stop_detail = "repeat", target
            break
        visited.add(key)
        url, html = target, None
    else:
        if pagination.kind != "none":
            stop_reason, stop_detail = "page_cap", f"reached max_pages={max_pages}"

    if stop_reason not in DOM_STOP_REASONS:  # pragma: no cover - guard
        raise AssertionError(f"unknown dom stop reason: {stop_reason}")

    return DomReplay(
        jobs=list(found.values()),
        page_counts=tuple(page_counts),
        pages=tuple(pages),
        stop_reason=stop_reason,
        stop_detail=stop_detail,
    )


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def verify_postings(
    postings: list[ScrapedJob],
    verification: Verification,
    *,
    reference_urls: frozenset[str] | None = None,
) -> VerifyResult:
    """Judge a replay by one rule: it must produce postings.

    Everything else is measured and returned, never enforced (owner, 2026-10-19).
    The recipe-authoring benchmark showed every deterministic filter rejecting
    correct work: count equality failed recipes whenever the reference was
    inflated by nav links, the URL pattern failed mixed-host boards, and the
    sample titles failed companies whose links carry a button label (24 real
    postings, every title "Apply"). Filters also drop real postings: "legal" and
    "privacy" are words that appear in genuine titles. Whether a replay is good
    is decided by comparing it with an independent extraction (hand labels, or
    the XHR/ATS JSON the page itself fetched), not by a regex over its output.

    ``off_pattern`` counts URLs that do not match ``verification.url_pattern``;
    ``missing_titles`` lists sample titles absent from the replay;
    ``outside_reference`` counts postings outside ``reference_urls`` when the
    caller supplied a reference set; ``count`` is the observed total.
    """
    if not postings:
        return VerifyResult(False, "empty list is never success")
    pattern = re.compile(verification.url_pattern)
    off_pattern = sum(1 for job in postings if not pattern.search(job.url))
    titles = {_fold(job.title) for job in postings}
    missing = tuple(sample for sample in verification.sample_titles if _fold(sample) not in titles)
    outside = 0
    if reference_urls is not None:
        outside = sum(1 for job in postings if job.url not in reference_urls)
    return VerifyResult(
        True,
        None,
        len(postings),
        off_pattern=off_pattern,
        missing_titles=missing,
        outside_reference=outside,
    )
