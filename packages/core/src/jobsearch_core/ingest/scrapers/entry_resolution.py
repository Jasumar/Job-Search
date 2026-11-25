"""Careers-page entry resolution: landing page -> open-positions page.

Phase 4.0d (plan-scraping.md §Careers-page work §Entry resolution). The
landing-page hole: a verified ``careers_url`` is often a marketing page whose
nav points at the real jobs section. Scraping the landing page yields nothing
or a handful of culture links, so the company looks empty and every later
scrape starts in the wrong place.

Deterministic, no LLM. A fixed vocabulary (``config/entry_vocabulary.yaml``)
ranks links that *look* like a jobs nav; the extractor, re-run per hop,
decides which page is actually the listing.

Invariants — each has a named enforcement point:

* **Bounded.** ``MAX_HOPS`` harvest rounds, ``MAX_FETCHES_PER_HOP`` fetches per
  round and ``MAX_FETCHES_PER_COMPANY`` for the whole descent, counted against
  attempts (failures included), so a page with hundreds of matching nav links
  cannot turn one company into a crawl. The one rendered retry and the one
  sitemap read are extra single requests, never loops.
* **The extractor decides, not the vocabulary.** A candidate is the entry only
  when ``clears_fill_checks`` sees at least one posting-shaped item on it.
  Under-threshold postings mean "try the next hop", never "empty board".
* **The landing page competes, it does not short-circuit.** It is scored first
  (JobPosting JSON-LD, an ATS adapter and a plain list all count), but the
  harvest always runs. A careers page that carries one or two featured postings
  is exactly the false positive the descent exists to correct, so it keeps its
  place only if no fetched page beats it. A page replaces the entry only on a
  *strictly* larger posting count — a tie never moves it.
* **The start page is an entry only on evidence.** It comes back as a hop-0
  result only when it yields postings and no fetched page beats it; a descent
  that reaches nothing reports no resolution, and the entry is never recorded
  because a link merely looked right.
* **Stays on the employer's site.** Same host, or a subdomain relation with the
  start host (``acme.com`` <-> ``careers.acme.com``); everything else is
  refused by ``_followable``. The harvest is same-origin by design: a
  delegated ATS board is not an entry this descent can produce, because
  ``scrape_company``'s 4.1e origin gate refuses a bare board as a scrape
  source — boards are entered from the employer page at scrape time, which
  already works (``_scrape_via_ats_links``).
* **A posting is not a listing.** A candidate whose last path segment is
  posting-shaped (an id, or a slug plus an id) is never an entry, and a
  candidate whose last segment is neither a listing term nor a section
  modifier is a section page, not an entry (``_ends_on_listing_surface``).
* **A miss is evidence.** The report carries every fetched candidate with its
  score and outcome plus the top ranked links of the last page harvested, so a
  run can show why a company resolved to nothing instead of leaving it in the
  log (``EntryReport``).
* **A sitemap is a listing only when it classifies as one.** A file of
  posting URLs is first a hint at the listing directory; only after every page
  fails does it become the entry itself (``kind="sitemap"``), and only when
  same-site, posting-shaped locs with a job token in the path are the majority
  and number at least ``_MIN_SITEMAP_LISTING``. Anything else is refused by
  name (``sitemap_not_job_postings``) in the trace. The file is read bounded:
  one index level, ``MAX_CHILDREN`` children, ``MAX_LOCS`` URLs, and a cut is
  reported as ``sitemap_truncated``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, urlsplit

from bs4 import BeautifulSoup

from jobsearch_core.config import EntryVocabulary
from jobsearch_core.ingest.listing import _cluster
from jobsearch_core.ingest.page_chrome import chrome_urls, content_links, page_urls
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pagination import is_pagination_link, normalize_url
from jobsearch_core.ingest.scrapers.posting_check import NO_POSTING_SIGNAL, posting_obstacle
from jobsearch_core.ingest.scrapers.sitemap import (
    SitemapDocument,
    read_sitemap,
)
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

#: Hard cap on harvest rounds: start page -> hop 1 -> hop 2, then stop.
MAX_HOPS = 2
#: Hard cap on fetches per round (the best-ranked candidates).
MAX_FETCHES_PER_HOP = 3
#: Hard cap on candidate fetches for one company, across every round. Two hops
#: of three would otherwise be six requests plus the retries; five keeps the
#: second round possible while a nav full of jobs-shaped links still cannot
#: turn into a crawl.
MAX_FETCHES_PER_COMPANY = 5

#: A rendered retry is a browser launch: worth it while the descent has found
#: little (a client-rendered nav over a marketing page), not worth it once the
#: page already lists a handful of postings and only the next round came up
#: empty. Cheap sitemap reads carry no such gate.
RENDER_BELOW_POSTINGS = 3
#: How many ranked links the report keeps, so the UI can show the shortlist
#: that was considered rather than only the ones the budget reached.
MAX_TRACE_CANDIDATES = 5
#: A title shorter than this is UI chrome ("›", "Job"), not a posting.
_MIN_TITLE = 3

#: Hosts whose links are never an entry (accounts walls, not pages).
_AUTH_HOST_PREFIXES = ("accounts.", "login.", "auth.", "signin.")
_DOCUMENT_EXTENSION = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|png|jpe?g|gif|svg)([?#]|$)", re.IGNORECASE
)
_NON_PAGE_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")

# Suffixes of a label that carry no meaning for matching: a count badge
# ("Avoimet työpaikat (12)"), an arrow, a material-icon ligature prefix.
_WORD = re.compile(r"[a-z0-9]+")

#: An id-shaped path segment: a whole segment or a trailing token of three or
#: more digits (".../jobs/893398197391", "/senior-engineer-123456").
_ID_TOKEN = re.compile(r"(?:^|-)(\d{3,})$")
_SITEMAP_CANDIDATES = "/sitemap.xml"
#: A sitemap is only treated as a listing hint when it carries at least this
#: many posting URLs under one prefix — one stray URL is not a listing.
_MIN_SITEMAP_POSTINGS = 3
#: A sitemap *is* the listing only above this many posting-shaped URLs; below
#: it, it is a hint at best (plan-scraping.md §Sitemap listings).
_MIN_SITEMAP_LISTING = 5
#: Path-token stems that name a job-posting surface. Stems (not whole words)
#: so ``careers``/``vacancies``/``tyopaikat`` match one entry. This is the
#: classification rule for a sitemap in its own right, deliberately separate
#: from the nav vocabulary: an employer's sitemap path is site structure, not
#: a language choice.
_POSTING_PATH_STEMS = (
    "job",
    "career",
    "vacanc",
    "stellen",
    "tyopaikka",
    "avoimet",
    "rekry",
    "position",
    "posting",
    "requisition",
    "opening",
)

#: Outcomes of classifying a read sitemap (``SitemapFinding.status``).
SITEMAP_ABSENT = "absent"
SITEMAP_LISTING = "sitemap_listing"
#: Read, but refused by name: its URLs are not job postings. This is the value
#: a miss reason and the trace carry, so a refused sitemap is never a guess.
SITEMAP_NOT_JOB_POSTINGS = "sitemap_not_job_postings"



@dataclass(frozen=True)
class EntryCandidate:
    """One ranked link: where it points, how it was labelled, and its score."""

    url: str
    link_text: str
    score: int


@dataclass(frozen=True)
class EntryResolution:
    """The page that actually yields postings, plus how it was reached."""

    url: str
    #: The page the entry link was found on (the company's landing page for a
    #: one-hop descent, an intermediate section page for a two-hop one, the
    #: page itself for a hop-0 hit).
    landing_url: str
    link_text: str
    hops: int
    #: Postings the extractor saw on the entry page — evidence for the gate.
    postings: int
    #: ``page`` for a listing page, ``sitemap`` when the sitemap *is* the
    #: listing (no page carried it); ``postings`` is then the loc count.
    kind: str = "page"


@dataclass(frozen=True)
class EntryTried:
    """One candidate the descent actually spent a fetch on."""

    url: str
    score: int
    #: ``postings`` (cleared the fill checks), ``no_postings`` (fetched, judged
    #: empty) or ``unreachable`` (the fetch failed; counted against the budget).
    outcome: str
    #: Postings the extractor emitted, and how many survive chrome removal and
    #: the shared posting check (ES1-ES3). ``real`` is what the page competes
    #: on; a gap between the two is why a nav-heavy page lost.
    raw: int = 0
    real: int = 0


@dataclass(frozen=True)
class EntryReport:
    """The outcome of one descent, hit or miss, with the evidence behind it.

    ``resolution`` is ``None`` for a miss, and ``reason`` then says why in one
    sentence. ``tried`` and ``candidates`` are what makes the miss reviewable:
    the former is what was fetched, the latter is the shortlist that was
    ranked on the last page harvested. Both are JSON-safe for the run source.
    """

    resolution: EntryResolution | None
    reason: str
    tried: list[EntryTried] = field(default_factory=list)
    candidates: list[EntryCandidate] = field(default_factory=list)
    #: True once the rendered DOM was used for one harvest round (item 3).
    rendered: bool = False
    #: The sitemap consulted for a shortcut or a listing, when one was read.
    sitemap: str | None = None
    #: How the sitemap classified: ``sitemap_listing``, ``sitemap_not_job_postings``
    #: or ``absent`` when there was nothing readable. None = never read.
    sitemap_status: str | None = None
    #: Child sitemaps read from the index (0 for a urlset).
    sitemap_children: int = 0
    #: True when a cap cut the sitemap read short.
    sitemap_truncated: bool = False
    #: Postings the extractor saw on the careers page itself, before any hop.
    #: Larger than the entry's count means the landing page was kept; smaller
    #: means a deeper page won on posting count.
    landing_postings: int = 0
    #: Raw extracted count on the landing page, before ES1-ES3 filtering.
    landing_raw: int = 0

    @property
    def ok(self) -> bool:
        return self.resolution is not None

    def as_trace(self) -> dict[str, object]:
        """The JSON payload written to ``run_sources.entry_trace``."""
        resolution = self.resolution
        return {
            "reason": self.reason,
            "rendered": self.rendered,
            "sitemap": self.sitemap,
            "sitemap_status": self.sitemap_status,
            "sitemap_children": self.sitemap_children,
            "sitemap_truncated": self.sitemap_truncated,
            "landing_postings": self.landing_postings,
            "landing_raw": self.landing_raw,
            "tried": [
                {
                    "url": tried.url,
                    "score": tried.score,
                    "outcome": tried.outcome,
                    "raw": tried.raw,
                    "real": tried.real,
                }
                for tried in self.tried
            ],
            "candidates": [
                {"url": candidate.url, "score": candidate.score, "text": candidate.link_text}
                for candidate in self.candidates
            ],
            "entry": (
                None
                if resolution is None
                else {
                    "url": resolution.url,
                    "landing_url": resolution.landing_url,
                    "link_text": resolution.link_text,
                    "hops": resolution.hops,
                    "postings": resolution.postings,
                    "kind": resolution.kind,
                }
            ),
        }


@dataclass(frozen=True)
class SitemapFinding:
    """What one sitemap read means for a descent.

    ``status`` is the classification, never a guess:

    * ``absent`` — no sitemap, or one that listed nothing readable.
    * ``sitemap_listing`` — the sitemap *is* the listing: ``locs`` are the
      posting URLs it lists (the job subtree only, document order).
    * ``sitemap_not_job_postings`` — read, but refused by name; ``locs`` is
      empty and the miss reason carries the status.

    ``derived`` is the *hint* the file still gives (a directory that may be a
    listing page), independent of the classification: a real listing page
    always wins over a sitemap slice.
    """

    url: str | None
    status: str
    locs: list[str] = field(default_factory=list)
    derived: list[str] = field(default_factory=list)
    children: int = 0
    truncated: bool = False

    @property
    def is_listing(self) -> bool:
        return self.status == SITEMAP_LISTING


def fold(text: str) -> str:
    """Lowercase, accent-folded form used for every vocabulary comparison.

    Folding diacritics (not just case) is what makes one vocabulary entry cover
    ``Työpaikat``/``tyopaikat`` and ``Stellenangebote``/``stellenangebote``.
    Non-alphanumeric characters become spaces, so ``avoimet-tyopaikat`` and
    ``Avoimet työpaikat`` compare equal.
    """
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(_WORD.findall(stripped.casefold()))


def _tokens(text: str) -> list[str]:
    """Fold, drop pure-digit tokens (count badges), keep order."""
    return [token for token in fold(text).split() if not token.isdigit()]


class _Scorer:
    """Vocabulary matching for one ``EntryVocabulary``.

    Folded once at construction so a resolution pass does no repeated work.
    """

    def __init__(self, vocabulary: EntryVocabulary) -> None:
        terms: set[str] = set()
        for language_terms in vocabulary.link_terms.values():
            for term in language_terms:
                folded = fold(term)
                if folded:
                    terms.add(folded)
        self.link_terms = terms
        self.path_terms = {folded for term in vocabulary.path_terms if (folded := fold(term))}
        self.section_modifiers = {
            folded for term in vocabulary.section_modifiers if (folded := fold(term))
        }
        self.subset_segments = {
            folded for term in vocabulary.subset_segments if (folded := fold(term))
        }

    def is_subset_surface(self, url: str) -> bool:
        """True when the URL's last segment names one slice of the postings.

        ``/jobs/engineering``, ``/careers/locations/helsinki``,
        ``/careers/internships``: a real listing of *some* jobs. It may rescue a
        descent that found nothing, but it is never the entry while another page
        already lists postings.
        """
        segments = [segment for segment in urlsplit(url).path.split("/") if segment]
        if not segments:
            return False
        return bool(set(fold(segments[-1]).split()) & self.subset_segments)

    def label_score(self, label: str) -> int:
        """4 = the label *is* a jobs term, 3 = it starts with one, 2 = contains one."""
        tokens = _tokens(label)
        if not tokens:
            return 0
        normalised = " ".join(tokens)
        if normalised in self.link_terms:
            return 4
        if any(normalised.startswith(f"{term} ") for term in self.link_terms):
            return 3
        padded = f" {normalised} "
        if any(f" {term} " in padded for term in self.link_terms):
            return 2
        return 0

    def path_score(self, url: str) -> int:
        """One point per listing token on the path, capped at 2."""
        tokens = set(fold(urlparse(url).path).split()) | set(
            word for word in fold(urlparse(url).hostname or "").split() if word != "www"
        )
        return min(2, len(tokens & self.path_terms))


def _label(link_text: str, aria: str) -> str:
    """Human-readable label for provenance: text, else aria-label."""
    text = " ".join(link_text.split()) or " ".join(aria.split())
    return text[:200]


def _last_segment(url: str) -> str:
    segments = [segment for segment in urlparse(url).path.split("/") if segment]
    return segments[-1] if segments else ""


def _looks_like_posting(url: str) -> bool:
    """True when the URL's last segment is an id, or a slug ending in an id.

    ``/jobs/893398197391`` and ``/careers/senior-engineer-123456`` are postings;
    ``/jobs`` and ``/avoimet-tyopaikat/kaikki`` are not. The rule is
    deliberately shallow: only the last segment is judged, because that is the
    only part of the path that distinguishes a listing from one posting in it.
    """
    return bool(_ID_TOKEN.search(fold(_last_segment(url)).replace(" ", "-")))


#: Section modifiers that name a jobs section only under a jobs parent segment.
_PARENT_BOUND_MODIFIERS = frozenset({"join us"})


def _ends_on_listing_surface(url: str, scorer: _Scorer, *, page_url: str = "") -> bool:
    """True when the URL names a jobs section, by its last segment or its host.

    ``/careers/jobs`` is a listing; ``/careers/jobs/senior-engineer`` is a
    posting (the listing token is not last); ``/about`` is neither. A section
    modifier may close the path instead of a listing term:
    ``/avoimet-tyopaikat/kaikki`` is a listing, ``/avoimet-tyopaikat/12345`` is
    a posting. A multi-word term ("join us") matches the whole folded segment.

    The host counts too (RF11): ``careers.evermade.fi/`` and
    ``tyopaikat.netum.fi/en`` are listing surfaces although no path segment
    says so. Only a host that differs from the page the link sits on carries
    that evidence; a link inside ``careers.acme.fi`` to ``careers.acme.fi/about``
    gains nothing from the host the page already has.
    """
    if _looks_like_posting(url):
        return False
    if page_url and _host_names_jobs(url, page_url, scorer):
        return True
    last = _last_segment(url)
    if not last:
        return False
    folded = fold(last)
    if folded in _PARENT_BOUND_MODIFIERS and folded in scorer.section_modifiers:
        # "/careers/join-us/" is a listing section; "/join-us" is a culture page.
        segments = [segment for segment in urlsplit(url).path.split("/") if segment]
        parent = set(fold(segments[-2]).split()) if len(segments) > 1 else set()
        return bool(parent & scorer.path_terms)
    return bool(
        (set(folded.split()) | {folded}) & (scorer.path_terms | scorer.section_modifiers)
    )


def _host_names_jobs(url: str, page_url: str, scorer: _Scorer) -> bool:
    """True when *url*'s host is a different host with a jobs token in its labels."""
    host = _site_host(url)
    if not host or host == _site_host(page_url):
        return False
    labels = {word for word in fold(host).split() if word != "www"}
    return bool(labels & scorer.path_terms)


#: A locale path segment: ``en``, ``fi``, ``en-us``, ``pt_BR``.
_LOCALE_SEGMENT = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2,4})?$", re.IGNORECASE)
_LOCALE_PARAMS = frozenset({"lang", "locale", "hslang", "hl", "language"})


def _descent_key(url: str) -> str:
    """Equality key for the descent: ``normalize_url`` minus the locale (RF12).

    ``/en/job-categories`` and ``/fi/job-categories`` are one page in two
    languages; spending a fetch on each starves the descent (Amazon). The key
    drops a leading locale path segment and the locale query parameters. It is
    local to the descent: pagination keeps its own, stricter equality.
    """
    parsed = urlsplit(normalize_url(url))
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and _LOCALE_SEGMENT.match(segments[0]):
        segments = segments[1:]
    query = "&".join(
        pair
        for pair in parsed.query.split("&")
        if pair and pair.split("=", 1)[0].lower() not in _LOCALE_PARAMS
    )
    path = "/" + "/".join(segments)
    return f"{parsed.scheme}://{parsed.netloc}{path}" + (f"?{query}" if query else "")


def _site_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def _same_site(start_url: str, candidate: str) -> bool:
    """Same site, or a subdomain relation either way.

    ``www.`` is ignored because it is a naming habit, not a site boundary:
    ``www.acme.fi`` and ``careers.acme.fi`` are the same employer.
    """
    start_host = _site_host(start_url)
    host = _site_host(candidate)
    if not start_host or not host:
        return False
    if start_host == host:
        return True
    return host.endswith(f".{start_host}") or start_host.endswith(f".{host}")


def _followable(start_url: str, candidate: str) -> bool:
    """The single gate every candidate passes before it can be fetched."""
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if _DOCUMENT_EXTENSION.search(parsed.path):
        return False
    host = (parsed.hostname or "").lower()
    if host.startswith(_AUTH_HOST_PREFIXES):
        return False
    return _same_site(start_url, candidate)


def rank_entry_links(
    html: str, page_url: str, vocabulary: EntryVocabulary
) -> list[EntryCandidate]:
    """Rank the links on *html* that could lead to a jobs listing.

    Deterministic order: score descending, document order for ties (a nav is
    written in priority order, so the first equally-scored link is the better
    guess). Same-URL duplicates keep their highest score.
    """
    scorer = _Scorer(vocabulary)
    start_page = _descent_key(page_url)
    best: dict[str, EntryCandidate] = {}

    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.lower().startswith(_NON_PAGE_SCHEMES) or href.startswith("#"):
            continue
        # A pager link is a page *within* a listing, never the entry itself.
        if is_pagination_link(anchor):
            continue

        url = urljoin(page_url, href)
        if not _followable(page_url, url):
            continue
        if _descent_key(url) == start_page:
            continue
        if not _ends_on_listing_surface(url, scorer, page_url=page_url):
            continue

        text = anchor.get_text(" ", strip=True)
        aria = (anchor.get("aria-label") or "").strip()
        score = max(scorer.label_score(text), scorer.label_score(aria)) + scorer.path_score(url)
        if score <= 0:
            continue

        candidate = EntryCandidate(url=url, link_text=_label(text, aria), score=score)
        key = _descent_key(url)
        current = best.get(key)
        if current is None or candidate.score > current.score:
            best[key] = candidate

    return sorted(best.values(), key=lambda candidate: -candidate.score)


def clears_fill_checks(jobs: list[ScrapedJob], page_url: str) -> bool:
    """The cheapest structural check: does this page carry a posting at all?

    Not the R3 posting-existence gate, which judges content; this only asks
    whether the extractor found something shaped like a posting — a title worth
    reading and a URL that is not the page itself. It is deliberately the
    cheapest check so the descent never pays for a content judgement it only
    uses to pick a URL.
    """
    page = normalize_url(page_url)
    for job in jobs:
        title = (job.title or "").strip()
        if len(title) < _MIN_TITLE:
            continue
        url = (job.url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if normalize_url(url) == page:
            continue
        return True
    return False


def real_postings(
    jobs: list[ScrapedJob],
    page_url: str,
    html: str,
    *,
    shared: frozenset[str] | set[str] = frozenset(),
) -> list[ScrapedJob]:
    """The extracted jobs that are real postings on this page (ES1-ES3).

    * ES2: a job whose URL is a link inside header, nav, footer or aside, or a
      link the page shares with the landing page (``shared``), is site frame.
    * ES3: the links left over are grouped by ``listing._cluster``. A job inside
      a posting-shaped cluster (repeated siblings, one URL pattern) counts even
      when its title carries no role words.
    * ES1: every other job must pass the shared ``posting_obstacle`` check.
    """
    kept, _dropped = filter_postings(jobs, page_url, html, shared=shared, judge_ats=True)
    return kept


#: Reason recorded for a job whose URL is page chrome (ES2), not a posting_check
#: obstacle name.
CHROME = "chrome"


def filter_postings(
    jobs: list[ScrapedJob],
    page_url: str,
    html: str,
    *,
    shared: frozenset[str] | set[str] = frozenset(),
    judge_ats: bool = False,
) -> tuple[list[ScrapedJob], list[tuple[ScrapedJob, str]]]:
    """Split ``jobs`` into ``(kept, dropped)`` by the ES1-ES3 rule.

    ``dropped`` pairs each job with its reason: a ``posting_check`` obstacle name
    or ``chrome``. Judged with the job's own origin, so ``off_domain`` cannot
    fire: an employer page legitimately links out to an ATS board. A job from an
    ATS board adapter (``ats_platform`` set) is a structured posting and is kept
    unjudged unless ``judge_ats`` is set (the entry resolver judges every job).
    """
    frame = chrome_urls(html, page_url) | set(shared)
    cluster, _ = _cluster(content_links(html, page_url, exclude=frame))
    in_cluster = {normalize_url(url) for url, _text in cluster}
    kept: list[ScrapedJob] = []
    dropped: list[tuple[ScrapedJob, str]] = []
    for job in jobs:
        if job.ats_platform and not judge_ats:
            kept.append(job)
            continue
        key = normalize_url(job.url or "")
        if key in frame:
            dropped.append((job, CHROME))
            continue
        obstacle = posting_obstacle(job, _own_origin(job, page_url, judge_ats))
        if obstacle is None or (obstacle == NO_POSTING_SIGNAL and key in in_cluster):
            kept.append(job)
        else:
            dropped.append((job, obstacle))
    return kept, dropped


def _own_origin(job: ScrapedJob, page_url: str, judge_ats: bool) -> str:
    """The listing URL ``posting_obstacle`` compares a job against.

    The entry resolver compares against the page itself (an off-domain link is
    evidence the page is not the listing). The scrape filter uses the job's own
    origin, which switches ``off_domain`` off but keeps ``self_link`` for a
    bare-origin URL, as ``suspect_reason`` does.
    """
    if judge_ats:
        return page_url
    parts = urlsplit(job.url or "")
    if not parts.scheme or not parts.netloc:
        return page_url
    return f"{parts.scheme}://{parts.netloc}/"


def _fetch(client: object, url: str) -> str | None:
    """Fetch *url*, or ``None`` when the page is unreachable.

    A dead link during a descent is expected (stale nav, 404), so it is a
    reason to try the next candidate, never an exception for the caller.
    """
    try:
        return client.fetch(url)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - any transport failure is a miss
        logger.info("Entry candidate unreachable %s: %s", url, exc)
        return None


def _render(render: Callable[[str], str] | None, url: str) -> str | None:
    """Fetch *url* through the browser tier, or ``None`` when that fails.

    The rendered retry is one extra request on a page whose static HTML had no
    jobs links; a client-rendered nav is the case it exists for. A failure is a
    miss, not an error: the descent still has its static HTML.
    """
    if render is None:
        return None
    try:
        return render(url)
    except Exception as exc:  # noqa: BLE001 - no renderer, no retry
        logger.info("Entry render failed for %s: %s", url, exc)
        return None


def _has_job_path_token(url: str) -> bool:
    """True when a path token names a job-posting surface.

    ``/job/<slug>/<id>``, ``/careers/vacancies/...``, ``/avoimet-tyopaikat/...``
    all qualify; ``/products/<id>`` does not. Only the path is judged — the
    host may carry a jobs-shaped name and say nothing about the page.

    The token set is a stem list, not the nav vocabulary: this decides whether
    a *sitemap* lists postings, which is site structure rather than the
    employer's choice of language (plan-scraping.md §Sitemap listings).
    """
    for token in fold(urlparse(url).path).split():
        if token.isdigit():
            continue
        if any(token.startswith(stem) for stem in _POSTING_PATH_STEMS):
            return True
    return False


def classify_sitemap(start_url: str, doc: SitemapDocument) -> SitemapFinding:
    """Decide whether a read sitemap is the listing, is a hint, or is refused.

    Classification, in order:

    * posting-shaped, same-site locs are collected (``_looks_like_posting``);
    * a listing needs at least ``_MIN_SITEMAP_LISTING`` of them *and* a job
      token in the path for the majority — that is what separates an
      employer's job sitemap from a product catalogue or a site-wide file;
    * the kept locs are the job subtree only, so a site-wide sitemap with a
      jobs branch contributes its jobs, not the whole site.

    The hint directory is derived from every posting-shaped loc regardless of
    the token check: a file of ``/jobs/123`` postings still says the listing
    lives at ``/jobs``, and that is a candidate page like any other.
    """
    postings = [
        loc for loc in doc.locs if _followable(start_url, loc) and _looks_like_posting(loc)
    ]
    job_postings = [loc for loc in postings if _has_job_path_token(loc)]
    is_listing = (
        len(job_postings) >= _MIN_SITEMAP_LISTING
        and len(job_postings) * 2 >= len(postings)
    )
    return SitemapFinding(
        url=doc.url,
        status=SITEMAP_LISTING if is_listing else SITEMAP_NOT_JOB_POSTINGS,
        locs=job_postings if is_listing else [],
        derived=_derived_directory(start_url, postings),
        children=doc.children,
        truncated=doc.truncated,
    )


def read_sitemap_finding(client: object, start_url: str) -> SitemapFinding:
    """Read ``<start>/sitemap.xml`` and classify it (one request, bounded read)."""
    sitemap_url = urljoin(start_url, _SITEMAP_CANDIDATES)
    if not _same_site(start_url, sitemap_url):
        return SitemapFinding(url=None, status=SITEMAP_ABSENT)
    doc = read_sitemap(client, sitemap_url)
    if not doc.locs:
        # The URL is still reported when readable: "there was no sitemap here"
        # is evidence too, and it differs from an unreadable file.
        status = SITEMAP_ABSENT if not doc.truncated else SITEMAP_NOT_JOB_POSTINGS
        return SitemapFinding(
            url=sitemap_url,
            status=status,
            children=doc.children,
            truncated=doc.truncated,
        )
    return classify_sitemap(start_url, doc)


def sitemap_entry_candidates(
    client: object, start_url: str
) -> tuple[str | None, list[str]]:
    """Derive listing URLs from the site's sitemap, without ranking links.

    Returns the sitemap URL read (``None`` when there was none) and the
    candidate listing URLs it implies, best first. The candidates are only
    *candidates*: they still have to clear ``clears_fill_checks`` like any
    ranked link, so a sitemap of blog posts cannot become an entry.
    """
    finding = read_sitemap_finding(client, start_url)
    if finding.derived:
        logger.info(
            "Entry shortcut from %s: listing directory %s",
            finding.url,
            finding.derived[0],
        )
    return finding.url, finding.derived


def _derived_directory(start_url: str, postings: list[str]) -> list[str]:
    """The listing-directory candidate a sitemap's posting URLs imply."""
    if len(postings) < _MIN_SITEMAP_POSTINGS:
        return []
    prefix = _common_directory(postings)
    if prefix is None:
        return []
    derived = urljoin(start_url, f"{prefix}/")
    if not _followable(start_url, derived) or normalize_url(derived) == normalize_url(start_url):
        return []
    return [derived]


def _common_directory(urls: list[str]) -> str | None:
    """The deepest directory prefix shared by *urls* ("/jobs/"), or ``None``.

    A sitemap that lists ``/jobs/123`` and ``/jobs/456`` says the listing lives
    at ``/jobs``. The root and a bare host are not answers — they are the whole
    site, which is what the descent started from.
    """
    directories = [urlparse(url).path.rsplit("/", 1)[0] for url in urls]
    if not directories:
        return None
    prefix = directories[0]
    for directory in directories[1:]:
        while prefix and not (directory == prefix or directory.startswith(f"{prefix}/")):
            prefix = prefix.rsplit("/", 1)[0]
        if not prefix:
            return None
    prefix = prefix.rstrip("/")
    return prefix or None


def resolve_entry(
    careers_url: str,
    *,
    client: object,
    scrape: Callable[[str, str], list[ScrapedJob]],
    vocabulary: EntryVocabulary,
    start_html: str | None = None,
    render: Callable[[str], str] | None = None,
    max_hops: int = MAX_HOPS,
    max_fetches_per_hop: int = MAX_FETCHES_PER_HOP,
    max_fetches: int = MAX_FETCHES_PER_COMPANY,
) -> EntryReport:
    """Find the page behind *careers_url* that actually lists jobs.

    ``scrape(url, html)`` is the extractor re-run per hop; the caller injects
    it so this module stays free of the scrape pipeline (no import cycle) and
    the tests can serve a stub. ``render(url)`` is the browser-tier fetch used
    once when a page yields no ranked links at all — the page may be
    client-rendered rather than empty.

    Always returns a report: ``report.resolution`` is ``None`` when no page
    within the bounds clears the fill checks, and ``report.reason`` then says
    what was tried. Callers keep ``careers_url`` on a miss.
    """
    tried: list[EntryTried] = []
    candidates: list[EntryCandidate] = []
    rendered = False
    sitemap_url: str | None = None
    sitemap_finding: SitemapFinding | None = None
    #: The harvest round that read the sitemap; a sitemap entry records it as
    #: its ``hops`` (there is no page-link hop for it).
    sitemap_hop = 0
    landing_postings = 0
    landing_raw = 0
    #: Every link on the landing page: a candidate link that is also here is
    #: site frame, not a posting (ES2).
    landing_links: frozenset[str] = frozenset()
    #: (raw, real) counts of the page ``consider`` scored last, for the trace.
    last_counts = (0, 0)
    #: The best page seen so far: postings, the resolution that describes it,
    #: and whether it was reached by a fetch (only a fetched page can be a
    #: frontier for the next hop).
    best: tuple[int, EntryResolution, bool] | None = None
    #: What ``best`` won on: (real postings, raw fallback count). Compared
    #: tuple-wise, so any real posting beats every raw-only page.
    best_rank: tuple[int, int] = (0, 0)

    def miss(detail: str) -> EntryReport:
        if sitemap_finding is not None and sitemap_finding.status == SITEMAP_NOT_JOB_POSTINGS:
            # Refused by name, never guessed at: the trace says why the file
            # was read and thrown away instead of leaving a generic miss.
            detail = f"{detail}; {SITEMAP_NOT_JOB_POSTINGS}"
        return EntryReport(
            resolution=None,
            reason=f"no page under {careers_url} yields postings ({detail})",
            tried=list(tried),
            candidates=candidates[:MAX_TRACE_CANDIDATES],
            rendered=rendered,
            sitemap=sitemap_url,
            sitemap_status=sitemap_finding.status if sitemap_finding is not None else None,
            sitemap_children=sitemap_finding.children if sitemap_finding is not None else 0,
            sitemap_truncated=sitemap_finding.truncated if sitemap_finding is not None else False,
            landing_postings=landing_postings,
            landing_raw=landing_raw,
        )

    scorer = _Scorer(vocabulary)

    def consider(
        url: str, html: str, *, link_text: str, landing_url: str, hop: int, fetched: bool
    ) -> int:
        """Score one page; it replaces the entry only when it has more postings.

        A page replaces the current best only on a *strictly* larger posting
        count, so a tie never moves the entry: the landing page keeps its place
        against an equally-sized candidate, and the first page to reach a count
        is the one recorded. That is what stops a featured-jobs block on a
        marketing page from beating the listing (it has fewer postings), and
        what stops a descent from wandering (it must gain).
        """
        nonlocal best, best_rank, last_counts
        jobs = scrape(url, html)
        last_counts = (len(jobs), 0)
        if not clears_fill_checks(jobs, url):
            return 0
        # The page competes on real postings, not links (ES1-ES3). The landing
        # page has nothing to share links with, so only its chrome is removed.
        real = real_postings(
            jobs, url, html, shared=landing_links if hop > 0 else frozenset()
        )
        last_counts = (len(jobs), len(real))
        # Fail-open: while no page has a real posting, pages compete on the raw
        # count as before, so a descent that finds nothing real still resolves
        # to the best-looking page instead of turning a hit into a miss. One
        # real posting outranks any raw count.
        rank = (len(real), 0) if real else (0, len(jobs))
        count = len(real) or len(jobs)
        promoted = best is None or rank > best_rank
        if promoted and scorer.is_subset_surface(url) and best is not None and best[0] > 0:
            # A subset page never beats a page that already lists postings: the
            # /job_categories trap is a slice of the board, not the board.
            promoted = False
        if promoted:
            best_rank = rank
            best = (
                count,
                EntryResolution(
                    url=url,
                    landing_url=landing_url,
                    link_text=link_text,
                    hops=hop,
                    postings=count,
                ),
                fetched,
            )
        logger.info("Entry candidate %s: %d posting(s), hop %d", url, count, hop)
        return count

    if max_hops < 1 or max_fetches_per_hop < 1 or max_fetches < 1:
        return miss("the descent is disabled")

    if start_html is None:
        start_html = _fetch(client, careers_url)
        if start_html is None:
            return EntryReport(
                resolution=None,
                reason=f"{careers_url} is unreachable",
                tried=list(tried),
            )

    # Hop 0 is a competitor, not a short-circuit. The landing page is scored
    # (JSON-LD, an ATS adapter and a plain list all count), but the harvest
    # below still runs: a careers page with one or two featured postings must
    # lose to the listing behind it, which is the whole point of the descent.
    landing_links = frozenset(page_urls(start_html, careers_url))
    landing_postings = consider(
        careers_url, start_html, link_text="", landing_url=careers_url, hop=0, fetched=False
    )
    landing_raw = last_counts[0]

    visited = {_descent_key(careers_url)}
    #: Where each fetched candidate came from (hop, page it was linked on), so a
    #: rendered retry (RF13) records the same provenance as the static fetch.
    origin: dict[str, tuple[int, str]] = {}
    #: Rendered HTML by page, so the RF13 retry never renders one twice.
    rendered_pages: dict[str, str] = {}
    page_url, page_html = careers_url, start_html
    fetches = 0

    # A vocabulary with no terms is an operator error, not a site that has no
    # jobs: the descent would rank nothing and only spend requests. The
    # landing-page judgement above still applies — it needs no vocabulary.
    if not (
        vocabulary.link_terms or vocabulary.path_terms or vocabulary.section_modifiers
    ):
        return miss("the vocabulary is empty")

    for hop in range(1, max_hops + 1):
        ranked = rank_entry_links(page_html, page_url, vocabulary)

        # Item 4: no links at all, but the site publishes a sitemap of posting
        # URLs. One read, once per descent, only when ranking produced nothing;
        # it is a plain GET, so it is tried before paying for a browser. The
        # file is two things: a hint at the listing directory, and — when it
        # classifies as a job-postings list — a listing in its own right. The
        # hint is ranked now; the listing waits until every page has failed,
        # because a real listing page always wins (it paginates).
        if not ranked and sitemap_finding is None:
            sitemap_finding = read_sitemap_finding(client, careers_url)
            sitemap_url = sitemap_finding.url
            sitemap_hop = hop
            if sitemap_finding.derived:
                ranked = [
                    EntryCandidate(url=url, link_text="sitemap", score=0)
                    for url in sitemap_finding.derived
                ]

        # Item 3: no ranked link on a page can mean a client-rendered nav
        # rather than no jobs section. One rendered retry, once per descent.
        ahead = best_rank[0]
        if not ranked and not rendered and render is not None and ahead < RENDER_BELOW_POSTINGS:
            rendered_html = _render(render, page_url)
            if isinstance(rendered_html, str) and rendered_html.strip():
                rendered = True
                rendered_pages[_descent_key(page_url)] = rendered_html
                html_ranked = rank_entry_links(rendered_html, page_url, vocabulary)
                if html_ranked:
                    page_html = rendered_html
                    ranked = html_ranked

        # Keep the last shortlist that had anything in it: on a later round
        # with no links at all, the empty round is not the interesting one.
        if ranked:
            candidates = ranked[:MAX_TRACE_CANDIDATES]
        if not ranked:
            break

        # The page to descend from next round: the best-ranked candidate whose
        # page we managed to fetch. The descent continues even when the landing
        # page already looked like a listing, so a section page that lists
        # everything ("/jobs/all") can still beat it.
        descent: tuple[str, str] | None = None
        attempts = 0

        for candidate in ranked:
            if attempts >= max_fetches_per_hop or fetches >= max_fetches:
                break
            key = _descent_key(candidate.url)
            if key in visited:
                continue
            visited.add(key)
            attempts += 1
            fetches += 1

            origin[candidate.url] = (hop, page_url)
            html = _fetch(client, candidate.url)
            if html is None:
                tried.append(
                    EntryTried(url=candidate.url, score=candidate.score, outcome="unreachable")
                )
                continue
            if descent is None:
                descent = (candidate.url, html)
            count = consider(
                candidate.url,
                html,
                link_text=candidate.link_text,
                landing_url=page_url,
                hop=hop,
                fetched=True,
            )
            tried.append(
                EntryTried(
                    url=candidate.url,
                    score=candidate.score,
                    outcome="postings" if count else "no_postings",
                    raw=last_counts[0],
                    real=last_counts[1],
                )
            )

        if descent is None:
            break
        page_url, page_html = descent

    # RF13: a high-scoring candidate whose static HTML is a JS shell reads as
    # ``no_postings`` and is never rendered. One rendered retry of the
    # best-scoring such candidate, counted against the same fetch cap.
    ahead = best_rank[0]
    empties = [index for index, item in enumerate(tried) if item.outcome == "no_postings"]
    if render is not None and empties and ahead < RENDER_BELOW_POSTINGS:
        index = max(empties, key=lambda i: (tried[i].score, -i))
        target = tried[index]
        key = _descent_key(target.url)
        # The in-loop retry may already have rendered this page (it found no
        # links on it); reuse that HTML instead of rendering it a second time.
        rendered_html = rendered_pages.get(key)
        if rendered_html is None and fetches < max_fetches:
            fetches += 1
            rendered_html = _render(render, target.url)
        if isinstance(rendered_html, str) and rendered_html.strip():
            rendered = True
            count = consider(
                target.url,
                rendered_html,
                link_text=next((c.link_text for c in candidates if c.url == target.url), ""),
                landing_url=origin[target.url][1],
                hop=origin[target.url][0],
                fetched=True,
            )
            if count:
                tried[index] = EntryTried(
                    url=target.url,
                    score=target.score,
                    outcome="postings",
                    raw=last_counts[0],
                    real=last_counts[1],
                )

    if best is None and sitemap_finding is not None and sitemap_finding.is_listing:
        # Every page failed and the sitemap *is* the listing: entry kind
        # ``sitemap``, the file itself recorded as the entry URL (no new
        # column), with the count of posting URLs it lists.
        resolution = EntryResolution(
            url=sitemap_finding.url or careers_url,
            landing_url=careers_url,
            link_text="sitemap",
            hops=sitemap_hop,
            postings=len(sitemap_finding.locs),
            kind="sitemap",
        )
        return EntryReport(
            resolution=resolution,
            reason="the sitemap is the listing",
            tried=list(tried),
            candidates=candidates[:MAX_TRACE_CANDIDATES],
            rendered=rendered,
            sitemap=sitemap_url,
            sitemap_status=sitemap_finding.status,
            sitemap_children=sitemap_finding.children,
            sitemap_truncated=sitemap_finding.truncated,
            landing_postings=landing_postings,
            landing_raw=landing_raw,
        )

    if best is None:
        return miss("no candidate yielded postings")

    _count, resolution, _fetched = best
    return EntryReport(
        resolution=resolution,
        reason="resolved" if resolution.hops else "the landing page out-lists every page tried",
        tried=list(tried),
        candidates=candidates[:MAX_TRACE_CANDIDATES],
        rendered=rendered,
        sitemap=sitemap_url,
        sitemap_status=sitemap_finding.status if sitemap_finding is not None else None,
        sitemap_children=sitemap_finding.children if sitemap_finding is not None else 0,
        sitemap_truncated=sitemap_finding.truncated if sitemap_finding is not None else False,
        landing_postings=landing_postings,
        landing_raw=landing_raw,
    )
