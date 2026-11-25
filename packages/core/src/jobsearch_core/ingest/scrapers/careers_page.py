from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from jobsearch_core.ingest.scrapers.base import ScrapedJob
from jobsearch_core.ingest.scrapers.pagination import is_pagination_link
from jobsearch_core.log import get_logger

logger = get_logger(__name__)

_JOB_LINK_PATTERNS = re.compile(
    r"/job[s]?/|/posting[s]?/|/position[s]?/|/opening[s]?/|/vacanc(y|ies)/|/career[s]?/"
    r"|/details/\d",
    re.IGNORECASE,
)

# A bare section root (/jobs, /careers/, /jobs/remote) is a listing surface,
# not a posting. Live DB junk rows: builtin.com/jobs/remote ("Remote"),
# employers.builtin.com/careers/ ("Careers"). A posting URL has something
# after the section segment; a section root does not.
_BARE_SECTION_URL = re.compile(
    r"^https?://[^/]+/[^/?#]*$",
    re.IGNORECASE,
)

# A link to a document (privacy policy PDF, etc.) is never a posting URL.
_DOCUMENT_EXTENSION = re.compile(
    r"\.(pdf|docx?|xlsx?|pptx?|zip|png|jpe?g|gif|svg)([?#]|$)",
    re.IGNORECASE,
)

# Known non-posting section sub-pages reached through job-shaped URL paths.
# Live DB junk rows were titled exactly these words ("Remote", "Français",
# "FAQs & Support"); a real posting title is not a bare section word.
_SECTION_WORDS = re.compile(
    r"^(remote|hybrid|onsite|faq|faqs|benefits|culture|life|about|team|teams|"
    r"contact|events|locations|students|graduates|search|jobs|careers)$",
    re.IGNORECASE,
)

_TITLE_KEYWORDS = re.compile(
    r"\b(engineer|developer|data|backend|frontend|fullstack|devops|sre|"
    r"analyst|scientist|architect|manager|lead|intern|associate|specialist|"
    r"python|java|golang|rust|cloud|security|platform|infrastructure|"
    r"machine learning|ai)\b",
    re.IGNORECASE,
)


_GENERIC_LINK_TEXT = re.compile(
    r"^(click here|apply|apply now|learn more|view|see more|"
    r"read more|details|more info|open positions|view jobs)$",
    re.IGNORECASE,
)

# Words a slug carries that are not part of a role: path furniture, locale
# segments, and the file extensions some boards still use.
_SLUG_FURNITURE = frozenset(
    {
        "job", "jobs", "job-detail", "detail", "details", "apply", "application",
        "vacancy", "vacancies", "position", "positions", "career", "careers",
        "opening", "openings", "search", "results", "en", "fi", "sv", "de", "fr",
        "es", "nl", "pl", "et", "us", "uk", "eu", "www", "index", "html", "htm",
        "php", "aspx",
    }
)

# Navigation and account links that match a job-shaped URL but are not
# postings (benchmark A3 defect, 2026-10-11: "Careers at Apple", "Sign in" to
# accounts.google.com, "Vacancies" on kesko.fi). Matched on link text.
_NAV_LINK_TEXT = re.compile(
    r"^(careers?( at \S+)?|vacancies|(explore )?work at \S+|life at \S+|sign in|log ?in|"
    r"saved jobs|job alerts|recommended jobs|how we hire|skip to .*|"
    r"main menu|faqs?|frequently asked questions|open jobs as rss feed|"
    r"locations|teams|students|jobs|your career|dashboard)$",
    re.IGNORECASE,
)
# Material-icon ligature text glued to a label ("work_outlinework_outlineJobs",
# "handymanhandymanHow we hire") is UI chrome, never a posting title.
_ICON_LIGATURE = re.compile(r"(_outline|handyman|person_outline)", re.IGNORECASE)

# Account/login hosts: a link there is never a posting.
# A leading field label inside a location element ("Location", "Locations:").
_LOCATION_LABEL = re.compile(r"^\s*locations?\b\s*:?\s*", re.IGNORECASE)

_AUTH_HOST_PREFIXES = ("accounts.", "login.", "auth.", "signin.")


def _title_from_url(url: str) -> str | None:
    """A role title read off the posting URL's slug, or None.

    ``/careers/jobs/senior-backend-engineer-10422`` reads as "Senior Backend
    Engineer": the trailing id, locale segments, path furniture and anything
    hash-shaped are dropped, and the rest is title-cased. Fewer than two words is
    never a title — ``/results/73697523738256070-prod`` would otherwise become
    "Prod", which is a locale marker dressed as a role, and the funnel's
    posting-shape gate would then accept it as a posting.
    """
    path = urlsplit(url).path
    segment = path.rstrip("/").rsplit("/", 1)[-1].split(";", 1)[0]
    if "." in segment:
        segment = segment.rsplit(".", 1)[0]
    words: list[str] = []
    for token in re.split(r"[-_+.~%]+", segment):
        low = token.lower()
        if not token or low in _SLUG_FURNITURE:
            continue
        if token.isdigit():
            continue
        if not re.search(r"[A-Za-z]", token):
            continue
        # A long token mixing digits and letters is an id, not a word.
        if len(token) > 6 and any(ch.isdigit() for ch in token):
            continue
        if len(token) < 2:
            continue
        words.append(token if token.isupper() else token[0].upper() + token[1:])
    if len(words) < 2:
        return None
    title = " ".join(words)
    return title if len(title) >= 8 else None


class CareersPageScraper:
    def matches(self, url: str, html: str | None = None) -> bool:
        return True

    def scrape_listings(self, html: str, base_url: str) -> list[ScrapedJob]:
        soup = BeautifulSoup(html, "html.parser")
        seen_urls: set[str] = set()
        jobs: list[ScrapedJob] = []

        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            if not href:
                continue

            # Pager links match the job-link patterns below (/jobs?page=2), so
            # they are excluded here rather than scraped as jobs titled "Next".
            if is_pagination_link(a_tag):
                continue

            abs_url = urljoin(base_url, href)
            link_text = a_tag.get_text(strip=True)

            if not self._looks_like_job_link(abs_url, link_text):
                continue
            if self._is_section_or_document(abs_url):
                continue
            if self._is_nav_or_self_link(abs_url, base_url, link_text):
                continue

            norm_url = abs_url.rstrip("/")
            if norm_url in seen_urls:
                continue
            seen_urls.add(norm_url)

            title = self._extract_title(a_tag, link_text, abs_url)
            location = self._extract_location(a_tag)

            jobs.append(ScrapedJob(
                url=abs_url,
                title=title,
                company="",
                location=location,
                authority_url=base_url,
                verified=True,
            ))

        logger.info("CareersPageScraper found %d job links", len(jobs))
        return jobs

    def _looks_like_job_link(self, url: str, text: str) -> bool:
        if _JOB_LINK_PATTERNS.search(url):
            return True
        if text and _TITLE_KEYWORDS.search(text) and len(text) > 5:
            return True
        return False

    def _is_section_or_document(self, url: str) -> bool:
        """Structural guards (4.1d defect 3): a bare section root is a listing
        surface and a document link is not a posting, no matter how the link
        was matched."""
        path = url.split("?", 1)[0].split("#", 1)[0]
        segments = [s for s in path.split("/") if s]
        if len(segments) <= 1:
            return True
        if _DOCUMENT_EXTENSION.search(path):
            return True
        # The last path segment (the slug) as a bare section word: /jobs/remote
        # is a section page, /jobs/senior-backend-engineer is a posting.
        slug = segments[-1]
        if _SECTION_WORDS.match(slug):
            return True
        return False

    def _is_nav_or_self_link(self, url: str, base_url: str, text: str) -> bool:
        """Guards for links that match a job-shaped URL but are not postings:
        the listing page itself (same URL, query ignored), an account/login
        host, or a navigation label such as "Careers at Apple"."""
        path = url.split("#", 1)[0].rstrip("/")
        if path.split("?", 1)[0] == base_url.split("#", 1)[0].rstrip("/").split("?", 1)[0]:
            return True
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith(_AUTH_HOST_PREFIXES):
            return True
        if text and (_NAV_LINK_TEXT.match(text.strip()) or _ICON_LIGATURE.search(text)):
            return True
        return False

    def _extract_title(self, a_tag: Tag, link_text: str, url: str = "") -> str:
        """The posting's title from the markup, falling back to the URL slug.

        The order is evidence strength: an explicit ``title`` attribute, the link
        text, an inner heading, then the slug. The slug is the last resort
        because it is weakest — Google's card links carry no text at all, and
        titling every posting "Unknown" made the funnel discard the lot as
        postings without a title (plan-scraping.md §Known gaps).
        """
        title_attr = a_tag.get("title", "")
        if title_attr and (not link_text or _GENERIC_LINK_TEXT.match(link_text)):
            return str(title_attr)
        if link_text and len(link_text) > 3 and not _GENERIC_LINK_TEXT.match(link_text):
            return link_text
        if title_attr:
            return str(title_attr)
        h_tag = a_tag.find(["h1", "h2", "h3", "h4", "h5", "h6"])
        if h_tag:
            heading = h_tag.get_text(strip=True)
            if heading and not _GENERIC_LINK_TEXT.match(heading):
                return heading
        slug_title = _title_from_url(url) if url else None
        if slug_title:
            return slug_title
        return link_text or "Unknown"

    def _extract_location(self, a_tag: Tag) -> str | None:
        parent = a_tag.parent
        if parent is None:
            return None
        loc_el = parent.find(class_=re.compile(r"location|city|place", re.IGNORECASE))
        if loc_el:
            # Join child texts with a space: a card with a "Location" label span
            # next to the value span would otherwise read "LocationEspoo, Finland".
            text = _LOCATION_LABEL.sub("", loc_el.get_text(" ", strip=True)).strip()
            return text or None
        sibling = a_tag.find_next_sibling(string=True)
        if sibling:
            text = sibling.strip()
            if text and len(text) < 100:
                return text
        return None
