from __future__ import annotations

from urllib.parse import urlparse

from jobsearch_core.config import SearchScope, load_scope
from jobsearch_core.countries import canonical_country, country_table

# MF6: every ISO 3166-1 country from config/countries.yaml (lower-case alpha-2
# code -> display name). Discovery queries DEFAULT_COUNTRIES when the profile
# names none; the full list is the vocabulary, not the default search.
COUNTRIES: dict[str, str] = country_table().by_code()
DEFAULT_COUNTRIES: dict[str, str] = country_table().default_by_code()

KNOWN_AGGREGATORS: set[str] = {
    "jobgether",
    "mindrift",
}

KNOWN_AGGREGATOR_HOSTS: set[str] = {
    "antal.com",
    "careerkarma.com",
    "freelancer.com",
    "h2kinfosys.com",
    "indeed.com",
    "jobgether.com",
    "duunitori.fi",
    "englishjobs.fi",
    "jobly.fi",
    "jooble.org",
    "linkedin.com",
    "meetfrank.com",
    "oikotie.fi",
    "simplyhired.com",
    "te-palvelut.fi",
    "weworkremotely.com",
}

# Third-party job boards and aggregators seen in live careers searches that the
# exact host set above cannot cover: one brand serves many TLDs
# (glassdoor.com, glassdoor.co.uk) or gives every customer a tenant subdomain
# (acme.eightfold.ai). A brand matches any label in the host; a domain entry
# matches itself and its subdomains. P3 of demo polish: a search result on any
# of these is refused before the fetch and the run row says so — a board is
# never a job source (4.1e) and an aggregator page is a guaranteed miss.
KNOWN_BOARD_BRANDS: set[str] = {
    "eightfold",
    "glassdoor",
    "jobcrawls",
}

KNOWN_BOARD_DOMAINS: set[str] = {
    "alion.io",
}


def is_known_board_host(url: str) -> bool:
    """True when *url*'s host is one of the third-party boards above."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if any(label in KNOWN_BOARD_BRANDS for label in host.split(".")):
        return True
    return any(host == domain or host.endswith(f".{domain}") for domain in KNOWN_BOARD_DOMAINS)


KNOWN_RECRUITER_HOSTS: set[str] = {
    "adecco.com",
    "experis.com",
    "futureheads.com",
    "hays.com",
    "honeypot.io",
    "landing.jobs",
    "manpower.com",
    "randstad.com",
    "roberthalf.com",
    "talent.com",
    "toptal.com",
    "upwork.com",
    "workingnomads.com",
}

RECRUITER_PATH_PARTS: set[str] = {
    "agency",
    "marketplace",
    "recruiters",
    "staffing",
}

NOISY_PATH_PARTS: set[str] = {
    "article",
    "blog",
    "forum",
    "freelance",
    "job-search",
    "news",
    "tutorial",
    "wiki",
}

SEARCH_EXCLUSIONS = "-blog -article -news -freelance -job-search -tutorial -course"

# Stage A company-identity queries deliberately keep article/blog/news content
# signals (a page may name a credible employer) and reject only the genuinely
# unusable sources. The strict hiring-source filter applies later at Stage B.
SEARCH_EXCLUSIONS_IDENTITY = "-freelance -job-search -tutorial -course"


def build_queries(
    country_code: str | None = None,
    role_family: str | None = None,
    scope: SearchScope | None = None,
) -> list[str]:
    scope = scope or load_scope()
    role_families = scope.role_families
    queries: list[str] = []

    target_countries: list[tuple[str, str]] = []
    if country_code and country_code in COUNTRIES:
        target_countries.append((country_code, COUNTRIES[country_code]))
    else:
        target_countries.extend(DEFAULT_COUNTRIES.items())

    target_families: list[str] = []
    if role_family and role_family in role_families:
        target_families.append(role_family)
    else:
        target_families.extend(role_families.keys())

    for cc, country_name in target_countries:
        for family in target_families:
            role_terms = role_families[family]
            role_query = " OR ".join(f'"{t}"' if " " in t else t for t in role_terms)
            # Phase 4.1e: no ATS-board site: queries. A search must reach
            # employer careers pages, never third-party ATS boards (§Employer
            # Pages Over ATS Boards).
            queries.append(
                f"(\"careers\" OR \"join our team\" OR \"open positions\") "
                f"({role_query}) \"{country_name}\" site:.{cc} {SEARCH_EXCLUSIONS}"
            )

    return queries


# Company-segment categories. Company identity should be found independently
# of whether the company currently has open target roles; role relevance is judged
# later at the job-listing include gate (Phase 3.5e). The segment templates come
# from the resolved scope (Phase 3.6a).
def build_company_segment_queries(
    country_code: str | None = None,
    scope: SearchScope | None = None,
) -> list[str]:
    """Deterministic Stage A baselines by company segment (no role-family terms).

    Emits one query per segment per country, naming the country, without any
    ``site:.{cc}`` TLD restriction (so international employers whose careers
    pages live on other domains can surface). ``build_queries`` and classic
    discovery are unchanged.
    """
    scope = scope or load_scope()
    templates = scope.company_segment_templates
    queries: list[str] = []

    target_countries: list[tuple[str, str]] = []
    if country_code and country_code in COUNTRIES:
        target_countries.append((country_code, COUNTRIES[country_code]))
    else:
        target_countries.extend(DEFAULT_COUNTRIES.items())

    for _cc, country_name in target_countries:
        queries.extend(
            template.format(country=country_name) + f" {SEARCH_EXCLUSIONS_IDENTITY}"
            for template in templates
        )

    return queries


def select_templates_for_segments(
    scope: SearchScope,
    segments: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Pair up (segment, template) honoring the pre-wizard priority rule.

    When ``segments`` is given, only templates whose segment name is selected
    are kept, in tech.yaml order (the pre-wizard narrowing rule: template list
    order is the priority order). A missing/misaligned
    ``company_segment_names`` list disables filtering — all templates pass
    with the segment name ``""``.
    """
    templates = scope.company_segment_templates
    names = scope.company_segment_names
    aligned = len(names) == len(templates)
    if segments is None or not aligned:
        return [
            (names[i] if aligned and i < len(names) else "", template)
            for i, template in enumerate(templates)
        ]
    wanted = set(segments)
    return [
        (names[i], template)
        for i, template in enumerate(templates)
        if names[i] in wanted
    ]


def select_baseline_countries(
    country_code: str | None = None,
    target_country_names: list[str] | None = None,
) -> list[str]:
    """Resolve which country names the Stage A baselines cover (Phase 4.3).

    Precedence: an explicit ``country_code`` wins (single-country run);
    otherwise the profile/evidence ``target_country_names`` list (validated
    against ``config/countries.yaml`` through its aliases; unknown names are
    ignored, an empty result falls back to ``DEFAULT_COUNTRIES``); otherwise
    ``DEFAULT_COUNTRIES``.
    """
    if country_code and country_code in COUNTRIES:
        return [COUNTRIES[country_code]]

    if target_country_names:
        selected: list[str] = []
        for name in target_country_names:
            resolved = canonical_country(name)
            if resolved and resolved not in selected:
                selected.append(resolved)
        if selected:
            return selected

    return list(DEFAULT_COUNTRIES.values())


def build_company_segment_queries_for_selection(
    country_names: list[str],
    scope: SearchScope | None = None,
    max_queries: int | None = None,
    segments: list[str] | None = None,
) -> list[str]:
    """Narrow Stage A baselines for an explicit country selection (Phase 4.3).

    Emits one narrow query per segment per selected country (templates in the
    scope carry no OR chains across segments). ``max_queries`` caps the output
    (segments x countries is truncated to fit) — callers pass the run's
    ``max_baseline_queries`` ceiling; no cap is applied when it is ``None``.
    ``segments`` optionally restricts which templates run (Phase 3.8f wizard
    role-family selection); ``None`` keeps every template.
    """
    scope = scope or load_scope()
    queries: list[str] = []
    for country_name in country_names:
        for _segment, template in select_templates_for_segments(scope, segments):
            queries.append(
                template.format(country=country_name) + f" {SEARCH_EXCLUSIONS_IDENTITY}"
            )
            if max_queries is not None and len(queries) >= max_queries:
                return queries
    return queries
