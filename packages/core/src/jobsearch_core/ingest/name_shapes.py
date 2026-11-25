"""Candidate name lists on a pasted page, and the name-likeness that picks one.

PX2 (master_plan §PX). A page that lists companies does it in one of a few
shapes: a cluster of same-shaped links, a table column, a list, or a run of
headings. ``listing.name_reference`` builds one candidate per shape and keeps
the one whose items look most like organisation names, not the largest.

Page chrome (header, nav, footer, aside) is removed first through
``page_chrome``. Items need not be links: a text-only item is identified by its
name (``recipe.name_identity``).

Pure: reads HTML, writes nothing, never fetches, no model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from jobsearch_core.ingest.page_chrome import CHROME_TAGS

#: Fewest items a candidate needs; a nav menu has fewer entries than this.
MIN_ITEMS = 8
#: Share of a candidate's items that must look like names.
MIN_NAME_SHARE = 0.8
_MAX_NAME_CHARS = 60
_MAX_NAME_WORDS = 6
_SHARE_BAND = 0.05

# Column headers that name the organisation column of a table.
_NAME_HEADERS = frozenset(
    {
        "company",
        "company name",
        "name",
        "organisation",
        "organization",
        "firm",
        "business",
        "startup",
        "employer",
        "yritys",
        "yrityksen nimi",
        "nimi",
        "organisaatio",
        "yhtiö",
        "firma",
    }
)
# Words that are navigation or page furniture, never an organisation.
_NAV_WORDS = frozenset(
    {
        "home",
        "about",
        "about us",
        "contact",
        "contact us",
        "login",
        "log in",
        "sign in",
        "sign up",
        "register",
        "privacy",
        "privacy policy",
        "terms",
        "terms of use",
        "cookies",
        "cookie policy",
        "menu",
        "search",
        "careers",
        "jobs",
        "news",
        "blog",
        "pricing",
        "help",
        "support",
        "read more",
        "more",
        "next",
        "previous",
        "back",
        "etusivu",
        "yhteystiedot",
        "tietosuoja",
        "valikko",
        "haku",
        "uutiset",
        "kirjaudu",
        "tietoa meistä",
        "lue lisää",
    }
)
_NUMBERING = re.compile(r"^\s*(?:#|no\.?\s*)?\d{1,3}\s*[.):\-–]\s*|^\s*#\d{1,3}\s+", re.IGNORECASE)
_FOOTNOTE = re.compile(r"\[\s*(?:\d+|[a-z])\s*\]")
_NUMBERISH = re.compile(r"^[\d\s.,%$€£¥+\-–/]+$")
_SEPARATOR = re.compile(r"\s[–—-]\s|:\s")


@dataclass(frozen=True)
class NameCandidate:
    """One shape's list: ``(url or None, name)`` per item, and how it was built."""

    shape: str
    items: tuple[tuple[str | None, str], ...]
    detail: str = ""

    @property
    def label(self) -> str:
        return f"{self.shape}{f' ({self.detail})' if self.detail else ''}"


def clean_name(text: str) -> str:
    """Strip footnote marks and a leading rank number; collapse whitespace."""
    text = _FOOTNOTE.sub("", text)
    text = _NUMBERING.sub("", " ".join(text.split()))
    return text.strip(" \t-–—:|•·")


def looks_like_name(text: str) -> bool:
    """Short, capitalised, not a sentence, not navigation, not a bare number."""
    if not 2 <= len(text) <= _MAX_NAME_CHARS:
        return False
    if len(text.split()) > _MAX_NAME_WORDS:
        return False
    if text.casefold() in _NAV_WORDS or _NUMBERISH.match(text):
        return False
    if text.endswith((".", "?", "!", ",", ";")) and not re.search(r"\b(?:inc|ltd|co|corp|oyj?)\.$", text, re.I):
        return False
    first = next((ch for ch in text if ch.isalpha()), None)
    if first is None:
        return False
    return first.isupper() or any(ch.isupper() for ch in text[1:])


def name_share(items: tuple[tuple[str | None, str], ...]) -> float:
    return sum(1 for _, text in items if looks_like_name(text)) / len(items) if items else 0.0


def _in_chrome(node: Tag) -> bool:
    return node.find_parent(CHROME_TAGS) is not None


def _own_text(node: Tag) -> str:
    return " ".join(node.get_text(" ").split())


def _label_text(node: Tag) -> str:
    """The text of a cell or link; its first block when it stacks name over detail.

    ``<a><div>NVIDIA</div><div>NVDA</div></a>`` is a name over a ticker.
    """
    blocks = node.find_all(["div", "p"])
    if len(blocks) >= 2 and blocks[0].find(["div", "p"]) is None:
        first = _own_text(blocks[0])
        if first:
            return first
    return _own_text(node)


def _item(node: Tag, page_url: str) -> tuple[str | None, str]:
    """(url, name) for a cell, list item or heading: the first link's text, else the text."""
    anchor = node.find("a", href=True)
    if anchor is not None:
        href = str(anchor["href"]).strip()
        text = clean_name(_label_text(anchor))
        if text and href and not href.lower().startswith(("#", "mailto:", "tel:", "javascript:")):
            url = urljoin(page_url, href).split("#")[0]
            return url, text
    text = clean_name(_label_text(node))
    # "Company – what it does": the name is what comes before the separator.
    head = _SEPARATOR.split(text, maxsplit=1)[0]
    return None, clean_name(head)


def _dedupe(items: list[tuple[str | None, str]]) -> tuple[tuple[str | None, str], ...]:
    seen: set[str] = set()
    out: list[tuple[str | None, str]] = []
    for url, text in items:
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append((url, text))
    return tuple(out)


def table_candidates(soup: BeautifulSoup, page_url: str) -> list[NameCandidate]:
    """One candidate per table column; a header such as "Company" marks the name column."""
    found: list[NameCandidate] = []
    for index, table in enumerate(soup.find_all("table")):
        if _in_chrome(table):
            continue
        rows = [
            row
            for row in table.find_all("tr")
            if row.find_parent("table") is table
        ]
        grid = [row.find_all(["td", "th"], recursive=False) for row in rows]
        grid = [cells for cells in grid if cells]
        if len(grid) < MIN_ITEMS:
            continue
        header_cells = grid[0] if all(c.name == "th" for c in grid[0]) else None
        body = grid[1:] if header_cells else grid
        for column in range(max(len(cells) for cells in body)):
            header = _own_text(header_cells[column]).casefold() if header_cells and column < len(header_cells) else ""
            cells = [cells[column] for cells in body if column < len(cells)]
            items = _dedupe([_item(cell, page_url) for cell in cells])
            if len(items) < MIN_ITEMS:
                continue
            named = header in _NAME_HEADERS
            detail = f"table {index + 1}, column {column + 1}" + (f" '{header}'" if header else "")
            found.append(NameCandidate("table", items, detail + (", name header" if named else "")))
    return found


def list_candidates(soup: BeautifulSoup, page_url: str) -> list[NameCandidate]:
    found: list[NameCandidate] = []
    for index, lst in enumerate(soup.find_all(["ul", "ol"])):
        if _in_chrome(lst):
            continue
        entries = lst.find_all("li", recursive=False)
        if len(entries) < MIN_ITEMS:
            continue
        items = _dedupe([_item(entry, page_url) for entry in entries])
        if len(items) >= MIN_ITEMS:
            found.append(NameCandidate("list", items, f"{lst.name} {index + 1}"))
    return found


def heading_candidates(soup: BeautifulSoup, page_url: str) -> list[NameCandidate]:
    """Headings of one level under one parent: a numbered article's entries."""
    groups: dict[tuple[str, int], list[Tag]] = {}
    for heading in soup.find_all(["h2", "h3", "h4"]):
        if _in_chrome(heading) or heading.parent is None:
            continue
        groups.setdefault((heading.name, id(heading.parent)), []).append(heading)
    found: list[NameCandidate] = []
    for (tag, _), headings in groups.items():
        if len(headings) < MIN_ITEMS:
            continue
        items = _dedupe([_item(heading, page_url) for heading in headings])
        if len(items) >= MIN_ITEMS:
            found.append(NameCandidate("headings", items, f"{tag} x{len(headings)}"))
    return found


def text_candidates(html: str, page_url: str) -> list[NameCandidate]:
    """Every table-column, list and heading candidate on the page, chrome excluded."""
    soup = BeautifulSoup(html, "html.parser")
    return [
        *table_candidates(soup, page_url),
        *list_candidates(soup, page_url),
        *heading_candidates(soup, page_url),
    ]


#: A link cluster keeps a tie only when it is at least this share of the best other list.
_ANCHOR_KEEP_SHARE = 0.5


def rank(
    eligible: list[tuple[NameCandidate, float]],
) -> list[tuple[NameCandidate, float]]:
    """Best first: name-likeness in 5% bands, then size; a link cluster that is
    about as large as the leader of its band takes the lead.

    The link cluster is the shape ``name_collect`` read before PX2, so a page
    keeps the names it had. The size guard stops a small nav-like cluster from
    beating a real list that is much longer.
    """
    ordered = sorted(
        eligible, key=lambda pair: (round(pair[1] / _SHARE_BAND), len(pair[0].items)), reverse=True
    )
    if not ordered or ordered[0][0].shape == "anchors":
        return ordered
    lead, lead_share = ordered[0]
    for pair in ordered[1:]:
        candidate, share = pair
        same_band = round(share / _SHARE_BAND) == round(lead_share / _SHARE_BAND)
        if (
            candidate.shape == "anchors"
            and same_band
            and len(candidate.items) >= _ANCHOR_KEEP_SHARE * len(lead.items)
        ):
            ordered.remove(pair)
            ordered.insert(0, pair)
            break
    return ordered


__all__ = [
    "MIN_ITEMS",
    "MIN_NAME_SHARE",
    "NameCandidate",
    "clean_name",
    "looks_like_name",
    "name_share",
    "rank",
    "text_candidates",
]
