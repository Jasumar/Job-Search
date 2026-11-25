from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlparse

from jobsearch_core.config import EntryVocabulary, load_entry_vocabulary, load_filters_config
from jobsearch_core.db.company_link import CompanyLinkResolver
from jobsearch_core.db.models import Company, Job, JobExclusion, RunSource
from jobsearch_core.db.session import get_session
from jobsearch_core.db.source_evidence import clear_verified_urls
from jobsearch_core.filters import apply_filters, fit_flags_json
from jobsearch_core.ingest import site_class
from jobsearch_core.ingest.http import PoliteClient
from jobsearch_core.ingest.llm_protocol import JsonCompleter
from jobsearch_core.ingest.recipe_replay import (
    ReplayOutcome,
    load_verified_recipe,
    mark_stale,
    replay_stored_recipe,
)
from jobsearch_core.ingest.scrapers.ats_json import (
    AshbyScraper,
    GreenhouseScraper,
    LeverScraper,
    SmartRecruitersScraper,
    TeamtailorScraper,
)
from jobsearch_core.ingest.scrapers.ats_links import (
    _classify_ats_url,
    _find_ats_board_links,
)
from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.careers_page import CareersPageScraper
from jobsearch_core.ingest.scrapers.detail import (
    extract_detail_description,
    extract_posting_title,
    fetch_detail,
)
from jobsearch_core.ingest.scrapers.entry_resolution import (
    EntryReport,
    EntryResolution,
    classify_sitemap,
    filter_postings,
    resolve_entry,
)
from jobsearch_core.ingest.scrapers.json_ld import JsonLdJobPostingScraper
from jobsearch_core.ingest.scrapers.oracle_hcm import OracleHcmScraper
from jobsearch_core.ingest.scrapers.pagination import crawl_careers_pages
from jobsearch_core.ingest.scrapers.sitemap import is_sitemap_document, read_sitemap
from jobsearch_core.log import get_logger
from jobsearch_core.normalize import dedup_key, normalize

logger = get_logger(__name__)

def _scrape_ats_board(platform: str, ats_url: str, html: str, client: PoliteClient) -> list[ScrapedJob]:
    """Dispatch an ATS board URL to its JSON adapter, bypassing the generic scraper."""
    for scraper in _SCRAPERS:
        if not getattr(scraper, "requires_client", False):
            continue
        if isinstance(scraper, CareersPageScraper):
            continue
        try:
            if scraper.matches(ats_url, html):
                jobs = scraper.scrape_listings_with_client(html, ats_url, client)
                if jobs:
                    logger.info("%s found %d listings via %s", type(scraper).__name__, len(jobs), ats_url)
                    for job in jobs:
                        job.ats_platform = platform
                    return jobs
        except Exception as exc:
            logger.warning("%s failed for %s: %s", type(scraper).__name__, ats_url, exc)
    return []


def _scrape_via_ats_links(careers_url: str, html: str, client: PoliteClient) -> list[ScrapedJob]:
    """Follow ATS board links found on an employer careers page.

    Only boards explicitly linked from the employer's own page are followed,
    in document order, and the first platform that yields listings wins.
    """
    links = _find_ats_board_links(html, careers_url)
    for platform, ats_url in links:
        board_html = client.fetch(ats_url)
        jobs = _scrape_ats_board(platform, ats_url, board_html, client)
        if jobs:
            logger.info("Followed %s board %s from %s", platform, ats_url, careers_url)
            return jobs
    if links:
        logger.warning("ATS links on %s yielded no listings", careers_url)
    return []

_SCRAPERS = [
    GreenhouseScraper(),
    LeverScraper(),
    AshbyScraper(),
    SmartRecruitersScraper(),
    TeamtailorScraper(),
    OracleHcmScraper(),
    JsonLdJobPostingScraper(),
    CareersPageScraper(),
]

# Phase 4.3 S5: the site class each scraper serves. Scrapers are tried in the
# classifier's ranked order; every scraper stays in the list, so the ranking
# only changes the order, never removes a fallback.
_SCRAPER_CLASS: dict[type, str] = {
    GreenhouseScraper: "A",
    LeverScraper: "A",
    AshbyScraper: "A",
    SmartRecruitersScraper: "A",
    TeamtailorScraper: "A",
    OracleHcmScraper: "A",
    JsonLdJobPostingScraper: "B",
    CareersPageScraper: "C",
}


def _dispatch_order(url: str, html: str) -> list:
    """Return ``_SCRAPERS`` reordered by the site classifier's ranking.

    An unknown or ambiguous profile keeps the original order: a tie between
    classes (e.g. JSON-LD B vs. generic C links) must not hand the page to a
    weaker scraper that happens to return something. Scrapers with the same
    class keep their relative order (stable sort). Classification never does
    I/O and a failure falls back to the original order.
    """
    try:
        profile = site_class.classify_site(url, html)
    except Exception as exc:  # noqa: BLE001 — classification must not block scraping
        logger.warning("Site classification failed for %s; using default order: %s", url, exc)
        return list(_SCRAPERS)
    if profile.unknown or profile.ambiguous:
        return list(_SCRAPERS)
    rank = {cs.site_class: i for i, cs in enumerate(profile.ranked)}
    fallback_rank = len(rank)
    return sorted(
        _SCRAPERS,
        key=lambda scraper: rank.get(_SCRAPER_CLASS.get(type(scraper), ""), fallback_rank),
    )


def scrape_company(
    company_name: str,
    careers_url: str,
    *,
    client: PoliteClient | None = None,
    fetch_descriptions: bool = True,
    verified: bool = False,
    company_id: int | None = None,
    page_html_out: list[str] | None = None,
) -> list[ScrapedJob]:
    """Scrape an employer careers page and the boards it delegates to.

    ``page_html_out``, when given, receives the entry page HTML the jobs were
    scraped from (after the Class D render), so the caller can judge the jobs
    against the page's chrome and clusters.

    Phase 4.1e origin gate: the scrape source must be the employer's own
    careers page. An unverified call or a careers URL that is itself an ATS
    board is refused before any fetch — a board is reachable only through a
    link on the employer's page (§Employer Pages Over ATS Boards), and
    ``verified`` must gate scraping, not merely annotate its results.

    Phase 4.3 S3/S4: a Class D page (static HTML says "needs render", or is
    unknown) is rendered and its DOM replaces the static HTML. When a render
    ran and ``company_id`` is given, its XHR capture verdicts are persisted
    (``record_capture_rows``). Without a company id nothing is persisted.
    """
    if not verified:
        logger.warning(
            "Refusing unverified scrape of %s (%s) — verified origin required", company_name, careers_url
        )
        return []
    if _classify_ats_url(careers_url):
        logger.warning(
            "Refusing direct ATS board URL %s (%s) — scrape the employer careers page instead",
            careers_url,
            company_name,
        )
        return []
    close_client = client is None
    if client is None:
        client = PoliteClient()
    try:
        logger.info("Scraping careers page: %s (%s)", company_name, careers_url)
        html = client.fetch(careers_url)
        html = _rendered_html_if_class_d(careers_url, html, client, company_id)
        if page_html_out is not None:
            page_html_out.append(html)
        jobs = _scrape_all_pages(careers_url, html, client)
        if not jobs:
            # Phase 4.1e: the employer page itself yielded nothing; follow an
            # explicit ATS board link/redirect found on it. The board URL is
            # reached via the employer page, so it stays within the
            # "employer pages over ATS boards" rule.
            jobs = _scrape_via_ats_links(careers_url, html, client)
        for job in jobs:
            if not job.company:
                job.company = company_name
            job.authority_url = careers_url if verified else None
            job.verified = verified
        if fetch_descriptions:
            _fill_new_descriptions(jobs, client, careers_url)
        return jobs
    except RuntimeError as exc:
        logger.error("Failed to scrape %s: %s", careers_url, exc)
        return []
    finally:
        if close_client:
            client.close()


def _rendered_html_if_class_d(
    url: str, html: str, client: PoliteClient, company_id: int | None
) -> str:
    """Return the rendered DOM for a Class D page, else the static HTML unchanged.

    Phase 4.3 S3/S4. ``classify_with_render`` renders only when the static profile
    ranks D first or is unknown, so a plain page never starts a browser. When a
    render ran, its XHR capture rows are replaced (S4) if ``company_id`` is known;
    a call without a company id persists nothing. Classification failures keep
    the static HTML (the scrape must not be blocked by the gate).
    """
    try:
        profile = site_class.classify_with_render(
            url,
            html,
            client.fetch_rendered,
            fetch_rendered_with_xhr=client.fetch_rendered_with_xhr,
        )
    except Exception as exc:  # noqa: BLE001 — the gate must not block scraping
        logger.warning("Class D render gate failed for %s; using static HTML: %s", url, exc)
        return html
    if profile.rendered_html is None:
        return html
    if company_id is not None:
        site_class.record_capture_rows(
            company_id, profile, captured_at=datetime.now(UTC)
        )
    return profile.rendered_html


def _scrape_all_pages(url: str, html: str, client: PoliteClient) -> list[ScrapedJob]:
    """Scrape page 1 and follow its next-page links (RF3).

    ``crawl_careers_pages`` owns the stop rules: the page ceiling, the repeat and
    no-new-jobs guards, and the same-scope check. Fetches go through ``client``,
    so the per-host interval applies. A crawl failure keeps the jobs found so far.
    """
    result = crawl_careers_pages(
        url,
        lambda page_html, page_url: _select_and_scrape(page_url, page_html, client),
        client=client,
        start_html=html,
    )
    return result.jobs


def _select_and_scrape(url: str, html: str, client: PoliteClient) -> list[ScrapedJob]:
    # Phase 4.1e: a URL that is itself an ATS board is never dispatched —
    # boards are entered only via _scrape_via_ats_links from the employer's
    # careers page, which records ats_platform provenance itself.
    # Phase 4.3 S5: scrapers are tried in the classifier's ranked class order.
    for scraper in _dispatch_order(url, html):
        try:
            if scraper.matches(url, html):
                if getattr(scraper, "requires_client", False):
                    jobs = scraper.scrape_listings_with_client(html, url, client)
                else:
                    jobs = scraper.scrape_listings(html, url)
                if jobs:
                    logger.info("%s found %d listings", type(scraper).__name__, len(jobs))
                    return jobs
        except Exception as exc:
            logger.warning("%s failed: %s", type(scraper).__name__, exc)
    logger.warning("No scraper produced results for %s", url)
    return []


def _fill_descriptions(jobs: list[ScrapedJob], client: PoliteClient, source_url: str) -> None:
    for job in jobs:
        if job.description:
            continue
        if job.url == source_url:
            logger.debug("Skipping detail fetch for same-URL job: %s", job.url)
            continue
        logger.debug("Fetching detail: %s", job.url)
        try:
            desc = fetch_detail(client, job.url)
        except (RuntimeError, ValueError) as exc:
            # 4.1d defect 1: a transport failure is not the same outcome as a
            # page without content. Warn so "the site is down / the URL is
            # malformed" is visible instead of sharing the no-content debug.
            logger.warning("Detail fetch failed for %s: %s", job.url, exc)
            continue
        if desc:
            job.description = desc
        else:
            logger.debug("No description extracted from %s", job.url)


def _known_fingerprints(jobs: list[ScrapedJob]) -> set[str]:
    """Dedup fingerprints of ``jobs`` already stored or excluded by a human."""
    keys = {
        dedup_key(*normalize(job.title, job.company, job.location or "")[:3]) for job in jobs
    }
    if not keys:
        return set()
    session = get_session()
    try:
        stored = session.query(Job.dedup_hash).filter(Job.dedup_hash.in_(keys)).all()
        excluded = (
            session.query(JobExclusion.dedup_hash).filter(JobExclusion.dedup_hash.in_(keys)).all()
        )
        return {row[0] for row in stored} | {row[0] for row in excluded}
    finally:
        session.close()


def _stored_urls(urls: list[str]) -> set[str]:
    """The subset of ``urls`` that already exist as ``jobs.url``."""
    if not urls:
        return set()
    session = get_session()
    try:
        rows = session.query(Job.url).filter(Job.url.in_(urls)).all()
        return {row[0] for row in rows}
    finally:
        session.close()


def _fill_new_descriptions(jobs: list[ScrapedJob], client: PoliteClient, source_url: str) -> None:
    """Fill descriptions for postings the store does not know yet (RF1).

    A known or excluded posting is dropped by ``ingest_scraped`` anyway, so its
    detail page is never fetched.
    """
    known = _known_fingerprints(jobs)
    fresh = [
        job
        for job in jobs
        if dedup_key(*normalize(job.title, job.company, job.location or "")[:3]) not in known
    ]
    _fill_descriptions(fresh, client, source_url)


#: Postings fetched between two commits. A stopped or crashed run keeps every
#: finished batch instead of losing the whole company.
INGEST_BATCH = 20


def _fill_and_ingest(
    jobs: list[ScrapedJob],
    client: PoliteClient,
    source_url: str,
    *,
    fetch_descriptions: bool,
    dry_run: bool,
    company_id: int | None,
) -> dict[str, int]:
    """Fetch details and store the postings in batches of ``INGEST_BATCH``.

    Same result as fill-all-then-ingest-all, but each batch is committed before
    the next detail fetch starts. Known postings are ingested first without a
    fetch, as before (RF1).
    """
    if not fetch_descriptions:
        return ingest_scraped(jobs, source_url=source_url, dry_run=dry_run, company_id=company_id)
    known = _known_fingerprints(jobs)
    fresh: list[ScrapedJob] = []
    rest: list[ScrapedJob] = []
    for job in jobs:
        key = dedup_key(*normalize(job.title, job.company, job.location or "")[:3])
        (rest if key in known else fresh).append(job)
    total = dict(
        ingest_scraped(rest, source_url=source_url, dry_run=dry_run, company_id=company_id)
    )
    for start in range(0, len(fresh), INGEST_BATCH):
        chunk = fresh[start : start + INGEST_BATCH]
        _fill_descriptions(chunk, client, source_url)
        stats = ingest_scraped(chunk, source_url=source_url, dry_run=dry_run, company_id=company_id)
        for key, value in stats.items():
            total[key] = total.get(key, 0) + value
        logger.info("Stored %d/%d new postings", min(start + INGEST_BATCH, len(fresh)), len(fresh))
    return total


def ingest_scraped(
    scraped_jobs: list[ScrapedJob],
    source_url: str | None = None,
    *,
    dry_run: bool = False,
    company_id: int | None = None,
) -> dict[str, int]:
    """Persist scraped jobs, resolving each to its owning company (gap 12).

    ``company_id`` is passed by :func:`scrape_company_entries`, which already
    knows the row it scraped. When it is ``None`` (the bare-URL scrape path)
    the owner is resolved from ``source_url`` or the job's company name by
    :class:`~jobsearch_core.db.company_link.CompanyLinkResolver`.

    ``linked`` in the returned stats counts jobs whose ``company_id`` was set
    by this call, so a scrape that cannot attribute its jobs is visible rather
    than silent.
    """
    config = load_filters_config()
    session = get_session()

    stats = {
        "added": 0,
        "skipped_dup": 0,
        "flagged": 0,
        "excluded": 0,
        "no_description": 0,
        "unverified": 0,
        "linked": 0,
    }
    resolver = CompanyLinkResolver(session)

    try:
        for job in scraped_jobs:
            authority_url = job.authority_url or source_url
            if not job.verified or not authority_url:
                stats["unverified"] += 1
                logger.warning("Dropping unverified job: %s", job.title)
                continue
            if not _is_http_url(job.url):
                stats["unverified"] += 1
                logger.warning("Dropping job with invalid URL: %s (%s)", job.title, job.url)
                continue
            if not job.description:
                stats["no_description"] += 1
                logger.debug("Dropping %s — no description", job.title)
                continue

            norm_title, norm_company, norm_location, country = normalize(
                job.title, job.company, job.location or ""
            )
            dkey = dedup_key(norm_title, norm_company, norm_location)

            # Gap 12: attribute the job to its company. The explicit
            # company_id from the company-scrape path wins; otherwise resolve
            # from the careers URL, then the job's own company name.
            owner_id = company_id
            if owner_id is None:
                owner_id = resolver.resolve(
                    careers_url=source_url, company_name=job.company
                )

            existing = session.query(Job).filter(Job.dedup_hash == dkey).first()
            if existing:
                stats["skipped_dup"] += 1
                logger.debug("Duplicate: %s @ %s (existing #%d)", job.title, job.company, existing.id)
                continue

            # A human deleted this posting. The fingerprint outlives the row, so
            # the decision is not undone by the next scrape.
            if session.query(JobExclusion).filter(JobExclusion.dedup_hash == dkey).first():
                stats["excluded"] += 1
                logger.debug("Excluded by a human: %s @ %s", job.title, job.company)
                continue

            result = apply_filters(
                job.title, job.company, job.location or "", job.description, config
            )

            if dry_run:
                status = "would_flag" if not result.accepted else "would_add"
                logger.info(
                    "[dry-run] %s: %s @ %s (%s)", status, job.title, job.company, result.reason or "ok"
                )
                stats["added"] += 1
                if not result.accepted:
                    stats["flagged"] += 1
                continue

            db_job = Job(
                company_id=owner_id,
                url=job.url,
                authority_url=authority_url,
                extraction_url=job.url,
                source_url=source_url,
                raw_title=job.title,
                raw_company=job.company,
                raw_location=job.location or "",
                normalized_title=norm_title,
                normalized_company=norm_company,
                normalized_location=norm_location,
                country=country,
                description=job.description,
                posted_date=job.posted_date,
                closing_date=job.closing_date,
                remote_policy=job.remote_policy,
                employment_type=job.employment_type,
                seniority=job.seniority,
                languages_required=json.dumps(job.languages_required) if job.languages_required else None,
                work_authorization=job.work_authorization,
                sponsorship=job.sponsorship,
                salary=job.salary,
                extracted_requirements=(
                    json.dumps(job.extracted_requirements) if job.extracted_requirements else None
                ),
                dedup_hash=dkey,
                status="new",
                verification_status="verified",
                verified_at=datetime.now(UTC),
                fit_flags=fit_flags_json(result),
                included_families=(
                    json.dumps(result.included_families) if result.included_families else None
                ),
                include_signals=(
                    json.dumps(result.include_signals) if result.include_signals else None
                ),
            )
            session.add(db_job)

            # A filter hit is a flag on the stored row, never a rejection.
            stats["added"] += 1
            if not result.accepted:
                stats["flagged"] += 1
            if owner_id is not None:
                stats["linked"] += 1

        if not dry_run:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return stats


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


#: A sitemap listing is expanded as a deterministic prefix of its posting URLs
#: (document order), so a re-run reads the same pages. Truncation is counted in
#: the run summary, never silent.
SITEMAP_PAGE_CEILING = 200


def _sitemap_entry_url(url: str) -> bool:
    """Cheap pre-check before paying for the entry fetch.

    A resolved sitemap entry is recorded as its ``.xml`` URL; the content
    check in :func:`_scrape_sitemap_entry` is what actually decides. This only
    keeps a normal careers page from being fetched twice.
    """
    return urlparse(url).path.lower().endswith(".xml")


def _scrape_sitemap_entry(
    entry_url: str, client: PoliteClient, company_name: str
) -> tuple[list[ScrapedJob] | None, dict[str, int]]:
    """Expand a sitemap entry into postings, one fetch per posting page.

    Returns ``None`` as the job list when the response is not a sitemap: the
    caller then scrapes it as the page it is, so an entry URL that happens to
    end in ``.xml`` and answers with HTML is never lost. XML is recognised
    before any HTML parse, so a sitemap never reaches an HTML extractor.

    Counters are the run's evidence (``sitemap_listed`` / ``sitemap_scraped``,
    truncation and the per-page drop reasons), because an expanded sitemap is
    a slice of a file and the loss must stay visible.
    """
    counters = {
        "sitemap_listed": 0,
        "sitemap_scraped": 0,
        "sitemap_truncated": 0,
        "sitemap_unreachable": 0,
        "sitemap_no_title": 0,
        "sitemap_no_description": 0,
        "sitemap_known": 0,
    }
    try:
        body = client.fetch(entry_url)
    except Exception as exc:  # noqa: BLE001 - an unreachable entry is a zero-yield row
        logger.warning("Sitemap entry unreachable %s: %s", entry_url, exc)
        return [], counters
    if not is_sitemap_document(body):
        return None, counters

    doc = read_sitemap(client, entry_url, body=body)
    finding = classify_sitemap(entry_url, doc)
    if not finding.is_listing:
        logger.warning(
            "Sitemap entry %s no longer classifies as postings (%s)",
            entry_url,
            finding.status,
        )
        return [], counters

    counters["sitemap_listed"] = len(finding.locs)
    page_urls = finding.locs[:SITEMAP_PAGE_CEILING]
    counters["sitemap_truncated"] = int(len(finding.locs) > len(page_urls))

    # RF5: a posting page already stored is not fetched again. The title is
    # only known after the fetch, so the stored URL is the check available here.
    known_urls = _stored_urls(page_urls)
    jobs: list[ScrapedJob] = []
    for page_url in page_urls:
        if page_url in known_urls:
            counters["sitemap_known"] += 1
            continue
        try:
            html = client.fetch(page_url)
        except Exception as exc:  # noqa: BLE001 - one dead posting, not the run
            counters["sitemap_unreachable"] += 1
            logger.info("Sitemap posting unreachable %s: %s", page_url, exc)
            continue
        title = extract_posting_title(html)
        if not title:
            counters["sitemap_no_title"] += 1
            continue
        description = extract_detail_description(html)
        if not description:
            counters["sitemap_no_description"] += 1
            continue
        jobs.append(
            ScrapedJob(
                url=page_url,
                title=title,
                company=company_name,
                description=description,
            )
        )
    counters["sitemap_scraped"] = len(jobs)
    logger.info(
        "Sitemap %s: %d listed, %d scraped, truncated=%s",
        entry_url,
        counters["sitemap_listed"],
        len(jobs),
        bool(counters["sitemap_truncated"]),
    )
    return jobs, counters


def scrape_company_entries(
    *,
    dry_run: bool = False,
    fetch_descriptions: bool = True,
    accepted_batch: int | None = None,
    refresh_known: bool = False,
    company_ids: list[int] | None = None,
    limit: int | None = None,
    run_id: int | None = None,
    llm: JsonCompleter | None = None,
) -> dict[str, int]:
    """Scrape human-approved company rows from the ``companies`` table.

    Scrapes ``accepted`` rows (optionally limited to ``accepted_batch``) and,
    when ``refresh_known`` is set, additionally re-scrapes all ``known`` rows to
    catch fresh postings. ``limit`` caps the total number of sources scraped
    across both sets.

    An ``accepted`` company is promoted to ``known`` only when its scrape added
    at least one job (a durable state change that moves it out of the default
    accepted set, so it is not scraped again unless ``refresh_known`` or
    ``--all-companies`` is used). ``known`` rows are never re-promoted.
    ``pending`` rows are never scraped here; review is the gate.

    Each row is scraped at its resolved entry (``open_positions_url``) when one
    is recorded, else at ``careers_url`` (Phase 4.0d, fail-open).

    ``run_id`` makes this a real run (R5a): each company gets a ``run_sources``
    row, and a company with a verified recipe is replayed instead of scraped
    generically. A replay that fails retires the recipe and falls back to the
    generic scraper for this run. Without ``run_id`` (the CLI path) there is no
    evidence row, so no replay happens.

    ``company_ids`` replaces the status selection: exactly those rows are
    scraped, whatever their status (the owner's tick on Find jobs is the
    decision). ``accepted_batch`` and ``refresh_known`` are ignored then;
    ``limit`` still caps the count. Promotion is unchanged: only an
    ``accepted`` row becomes ``known``.
    """
    session = get_session()
    try:
        accepted = list(
            session.query(Company)
            .filter(
                Company.id.in_(company_ids)
                if company_ids is not None
                else Company.status == "accepted"
            )
            .order_by(Company.id)
            .all()
        )
        if accepted_batch is not None and company_ids is None:
            accepted = accepted[:accepted_batch]
        known: list[Company] = []
        if refresh_known and company_ids is None:
            known = list(
                session.query(Company)
                .filter(Company.status == "known")
                .order_by(Company.id)
                .all()
            )
    finally:
        session.close()

    if limit is not None:
        accepted = accepted[:limit]
        remaining = max(0, limit - len(accepted))
        known = known[:remaining]

    rows = accepted + known
    if not rows:
        logger.info("No accepted%s companies to scrape", " or known" if refresh_known else "")
        return {
            "companies": 0,
            "total_added": 0,
            "total_flagged": 0,
            "total_dup": 0,
            "total_no_desc": 0,
            "total_linked": 0,
            "promoted": 0,
            "board_url_rejected": 0,
            "sitemap_listed": 0,
            "sitemap_scraped": 0,
            "sitemap_truncated": 0,
            "sitemap_unreachable": 0,
            "sitemap_no_title": 0,
            "sitemap_no_description": 0,
            "sitemap_known": 0,
        }

    client = PoliteClient()
    total_stats: dict[str, int] = {
        "companies": 0, "total_added": 0, "total_flagged": 0,
        "total_dup": 0, "total_no_desc": 0, "total_linked": 0, "promoted": 0,
        "board_url_rejected": 0,
        "sitemap_listed": 0, "sitemap_scraped": 0, "sitemap_truncated": 0,
        "sitemap_unreachable": 0, "sitemap_no_title": 0,
        "sitemap_no_description": 0, "sitemap_known": 0,
    }

    try:
        for company in rows:
            # Phase 4.0d, entry-first: the entry is the page that lists jobs, so
            # an accepted company whose entry was resolved is scrapable even
            # with no careers_url. Only a row with neither URL has nothing to
            # fetch. Fail-open is deliberate: no entry means "fall back", not
            # "skip".
            entry_url = company.open_positions_url or company.careers_url
            if not entry_url:
                logger.warning("Skipping %s — no entry or careers_url", company.name)
                _record_skipped(run_id, dry_run, company.id, "no careers_url or entry")
                continue
            # Phase 4.1e: a board URL is not a verified employer origin — skip
            # loudly instead of scraping it. The check applies to whichever URL
            # the scrape would use, the entry included. Fixing the row (finding
            # the real employer careers page) is a human action.
            if _classify_ats_url(entry_url):
                logger.warning(
                    "Skipping %s — %s is an ATS board URL, not the employer careers page",
                    company.name,
                    entry_url,
                )
                total_stats["board_url_rejected"] += 1
                _record_skipped(
                    run_id,
                    dry_run,
                    company.id,
                    f"{entry_url} is an ATS board URL",
                )
                continue
            logger.info("Scraping %s (%s) [%s]", company.name, entry_url, company.status)
            run_source_id = (
                _open_run_source(run_id, company.id)
                if run_id is not None and not dry_run
                else None
            )
            jobs: list[ScrapedJob] | None = None
            if _sitemap_entry_url(entry_url):
                # A resolved sitemap entry: the file *is* the listing, so it
                # is expanded into posting pages instead of being scraped or
                # replayed. None means the response was not a sitemap after
                # all, and the ordinary path below takes over.
                jobs, sitemap_counts = _scrape_sitemap_entry(
                    entry_url, client, company.name
                )
                for key, value in sitemap_counts.items():
                    total_stats[key] += value
                if jobs is not None:
                    for job in jobs:
                        if not job.company:
                            job.company = company.name
                        job.authority_url = entry_url
                        job.verified = True
                    if run_source_id is not None:
                        _finish_run_source(
                            run_source_id,
                            status="completed" if jobs else "zero_yield",
                            identities=len(jobs),
                        )
            if jobs is None:
                if run_source_id is not None:
                    outcome = _replay_company(
                        run_source_id, company.id, entry_url, client, llm
                    )
                    if outcome is not None and outcome.gate_rejected:
                        logger.warning(
                            "Replay for %s judged not postings by the gate; "
                            "falling back to the generic scraper this run",
                            company.name,
                        )
                    elif outcome is not None and outcome.ok:
                        jobs = list(outcome.postings)
                        for job in jobs:
                            if not job.company:
                                job.company = company.name
                            job.authority_url = entry_url
                            job.verified = True
                        _finish_run_source(
                            run_source_id,
                            status="completed",
                            identities=len(jobs),
                            extraction="recipe",
                            recipe_id=outcome.recipe_id,
                        )
                    elif outcome is not None:
                        _finish_run_source(
                            run_source_id,
                            status="failed",
                            extraction="recipe",
                            error=outcome.error,
                        )
            if jobs is None:
                page_html: list[str] = []
                scraped = scrape_company(
                    company.name,
                    entry_url,
                    client=client,
                    fetch_descriptions=False,  # fetched per batch by _fill_and_ingest
                    verified=True,
                    company_id=company.id,
                    page_html_out=page_html,
                )
                # Generic branch only: links the extractor returned that are not
                # postings are dropped before any detail fetch (master_plan §Drop non-postings at scrape).
                jobs, dropped = filter_postings(
                    scraped, entry_url, page_html[0] if page_html else ""
                )
                summary = _drop_summary(len(scraped), dropped)
                if summary is not None:
                    logger.info(
                        "Dropped %d of %d links for %s: %s",
                        summary["dropped"],
                        summary["total"],
                        company.name,
                        summary["reasons"],
                    )
                if run_source_id is not None:
                    _finish_run_source(
                        run_source_id,
                        status="completed" if jobs else "zero_yield",
                        identities=len(jobs),
                        dropped=summary,
                        error=(
                            f"no postings kept: {_drop_text(summary)}"
                            if not jobs and summary is not None
                            else None
                        ),
                    )
            # Phase 4.1e: ats_platform is scrape-time provenance — derived
            # from the jobs actually scraped (direct board URL or a board
            # linked from the careers page), never from search results.
            # Written through its own short session: the listing session above
            # is already closed, so mutating the detached row would be lost.
            ats_platform = next((job.ats_platform for job in jobs if job.ats_platform), None)
            if ats_platform:
                _record_ats_platform(company.id, ats_platform)
            stats = _fill_and_ingest(
                jobs,
                client,
                entry_url,
                fetch_descriptions=fetch_descriptions,
                dry_run=dry_run,
                company_id=company.id,
            )
            total_stats["companies"] += 1
            total_stats["total_added"] += stats["added"]
            total_stats["total_flagged"] += stats["flagged"]
            total_stats["total_dup"] += stats["skipped_dup"]
            total_stats["total_no_desc"] += stats["no_description"]
            total_stats["total_linked"] += stats["linked"]
            if (
                company.status == "accepted"
                and not dry_run
                and stats["added"] > 0
                and _promote_to_known(company.id)
            ):
                total_stats["promoted"] += 1
    finally:
        client.close()

    return total_stats


def _record_skipped(run_id: int | None, dry_run: bool, company_id: int, reason: str) -> None:
    """Record a company the run refused to scrape, so it is never silently dropped."""
    if run_id is None or dry_run:
        return
    run_source_id = _open_run_source(run_id, company_id)
    _finish_run_source(run_source_id, status="failed", error=reason)


_DROP_EXAMPLES = 5


def _drop_summary(total: int, dropped: list[tuple[ScrapedJob, str]]) -> dict | None:
    """What the scrape filter dropped, as stored on ``run_sources.dropped``.

    ``None`` when nothing was dropped. Every drop stays visible: the counts, the
    reasons and up to ``_DROP_EXAMPLES`` titles with their URLs.
    """
    if not dropped:
        return None
    reasons: dict[str, int] = {}
    for _job, reason in dropped:
        reasons[reason] = reasons.get(reason, 0) + 1
    return {
        "total": total,
        "dropped": len(dropped),
        "reasons": reasons,
        "examples": [
            {"title": job.title, "url": job.url} for job, _reason in dropped[:_DROP_EXAMPLES]
        ],
    }


def _drop_text(summary: dict | None) -> str:
    if summary is None:
        return "nothing found"
    reasons = ", ".join(f"{name} {count}" for name, count in summary["reasons"].items())
    return f"{summary['dropped']} of {summary['total']} links dropped ({reasons})"


def _open_run_source(run_id: int, company_id: int) -> int:
    """Create this run's per-company progress row and return its id."""
    session = get_session()
    try:
        row = RunSource(
            run_id=run_id,
            company_id=company_id,
            status="running",
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def _finish_run_source(
    run_source_id: int,
    *,
    status: str,
    identities: int = 0,
    extraction: str | None = None,
    recipe_id: int | None = None,
    error: str | None = None,
    dropped: dict | None = None,
) -> None:
    """Close a run source row with what this run observed."""
    session = get_session()
    try:
        row = session.get(RunSource, run_source_id)
        if row is None:
            return
        row.status = status
        row.identities_found = identities
        row.dropped = json.dumps(dropped) if dropped else None
        row.extraction = extraction
        row.recipe_id = recipe_id
        row.error = error
        row.finished_at = datetime.now(UTC).replace(tzinfo=None)
        session.commit()
    finally:
        session.close()


def _replay_company(
    run_source_id: int,
    company_id: int,
    page_url: str,
    client: PoliteClient,
    llm: JsonCompleter | None,
) -> ReplayOutcome | None:
    """Replay the company's verified recipe, or None when it has none.

    A replay that fails retires the recipe (``stale``) so the next run re-authors
    instead of retrying a broken body; this run falls back to the generic
    scraper. The verified URL set and the gate's flag are written by
    ``replay_stored_recipe`` through this session.
    """
    session = get_session()
    try:
        recipe = load_verified_recipe(session, company_id=company_id)
        if recipe is None:
            return None
        outcome = replay_stored_recipe(
            session,
            run_source_id=run_source_id,
            recipe_row=recipe,
            page_url=page_url,
            client=client,
            llm=llm,
            drift_checks=True,
        )
        if not outcome.ok:
            mark_stale(session, recipe)
        elif outcome.gate_rejected:
            # RF2: the gate judged the items junk. The flag is stored; the
            # recipe stays verified (the stop rule owns retiring it) and this
            # run's junk URL set must not become the next drift baseline.
            clear_verified_urls(session, run_source_id)
        session.commit()
        return outcome
    finally:
        session.close()


def _record_ats_platform(company_id: int, ats_platform: str) -> None:
    """Persist ``ats_platform`` provenance on a company row (Phase 4.1e).

    Idempotent: re-writing the same platform is a no-op. Never clears an
    existing value — provenance is only ever added.
    """
    session = get_session()
    try:
        company = session.query(Company).filter(Company.id == company_id).first()
        if not company or company.ats_platform == ats_platform:
            return
        company.ats_platform = ats_platform
        session.commit()
        logger.info("Recorded ats_platform=%s for company #%s", ats_platform, company_id)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def resolve_scraping_entry(
    careers_url: str,
    *,
    client: PoliteClient | None = None,
    vocabulary: EntryVocabulary | None = None,
    page_html: str | None = None,
) -> EntryReport:
    """Resolve the open-positions page behind a verified careers page (4.0d).

    The single entry point ``careers_search``'s per-company step calls before
    recording a company. Always returns a report: ``report.resolution`` is the
    entry, or ``None`` when nothing within the bounds lists postings — the
    caller then keeps ``careers_url`` — with ``report.tried`` and
    ``report.candidates`` as the evidence for a miss.

    ``page_html`` is the landing page the caller already has (a search-surfaced
    candidate), so the descent reuses it instead of fetching it twice. The
    landing page is judged as the entry itself first — a careers page that is
    already the listing must not be descended past — which also covers a page
    carrying JobPosting JSON-LD.

    The extractor injected here is the production dispatcher, so the hop judges
    a candidate with the same scrapers (JSON-LD, ATS adapters, generic links)
    that will run on it later, and the browser tier is available for the one
    rendered retry. A resolution failure is logged and reported, never raised:
    the descent is an optimisation, not a blocker for the caller.
    """
    owns_client = client is None
    if client is None:
        client = PoliteClient()
    try:
        return resolve_entry(
            careers_url,
            client=client,
            scrape=lambda url, html: _select_and_scrape(url, html, client),
            vocabulary=vocabulary if vocabulary is not None else load_entry_vocabulary(),
            start_html=page_html,
            render=getattr(client, "fetch_rendered", None),
        )
    except Exception as exc:  # noqa: BLE001 - never let a descent break discovery
        logger.warning("Entry resolution failed for %s: %s", careers_url, exc)
        return EntryReport(
            resolution=None, reason=f"entry resolution failed for {careers_url}: {exc}"
        )
    finally:
        if owns_client:
            client.close()


def record_open_positions_entry(company_id: int, entry: EntryResolution) -> None:
    """Persist a resolved entry with the provenance of the descent (4.0d).

    Written as one unit — URL, landing page, link text — so a review can never
    see a half-recorded hop. Never touches ``careers_url``: the entry is a
    separate field precisely so 4.1e provenance survives (plan-scraping.md
    §Entry resolution).
    """
    session = get_session()
    try:
        company = session.query(Company).filter(Company.id == company_id).first()
        if company is None:
            logger.warning("Cannot record entry — company #%s is gone", company_id)
            return
        company.open_positions_url = entry.url
        company.open_positions_landing_url = entry.landing_url
        company.open_positions_link_text = entry.link_text
        session.commit()
        logger.info(
            "Recorded open_positions_url=%s for company #%s (from %s, %d hop(s))",
            entry.url,
            company_id,
            entry.landing_url,
            entry.hops,
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _promote_to_known(company_id: int) -> bool:
    """Promote an accepted company row to ``known`` after a successful scrape.

    Returns True when the row existed and was actually promoted. A no-op (and
    False) when the row is already gone or no longer accepted, so the caller
    does not double-count.
    """
    session = get_session()
    try:
        company = session.query(Company).filter(Company.id == company_id).first()
        if not company or company.status != "accepted":
            return False
        company.status = "known"
        session.commit()
        logger.info("Promoted company #%s to known", company_id)
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
