"""The country vocabulary (MF6): every ISO 3166-1 country, read from config.

``config/countries.yaml`` is the one list. Code reads it through this module:
the profile's country selection, discovery's country names, ``GET /segments``
and the matcher's country comparison.

Matching rule: a name is folded (case, accents, punctuation) and compared whole
against the names, aliases and alpha-2 codes. It is never a substring test, so
"india" does not match "indiana" and "fi" does not match "finland".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from jobsearch_core.config import load_yaml

COUNTRIES_PATH = Path("config/countries.yaml")


@dataclass(frozen=True)
class Country:
    code: str  # ISO alpha-2, upper case
    name: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CountryTable:
    countries: tuple[Country, ...]
    discovery_default: tuple[str, ...]  # alpha-2 codes
    by_key: dict[str, Country]  # folded name / alias / code -> country

    def names(self) -> list[str]:
        return [country.name for country in self.countries]

    def by_code(self) -> dict[str, str]:
        """Lower-case alpha-2 code -> display name."""
        return {country.code.lower(): country.name for country in self.countries}

    def default_by_code(self) -> dict[str, str]:
        codes = self.by_code()
        return {code.lower(): codes[code.lower()] for code in self.discovery_default if code.lower() in codes}


def fold(text: str) -> str:
    """Case- and accent-free form with single spaces between words."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", " ", stripped.casefold()).strip()


@lru_cache(maxsize=4)
def _load(path: str) -> CountryTable:
    data = load_yaml(Path(path))
    countries: list[Country] = []
    by_key: dict[str, Country] = {}
    for entry in data.get("countries") or []:
        country = Country(
            code=str(entry["code"]).upper(),
            name=str(entry["name"]),
            aliases=tuple(str(alias) for alias in entry.get("aliases") or ()),
        )
        countries.append(country)
        for key in (country.name, country.code, *country.aliases):
            folded = fold(key)
            if folded:
                by_key.setdefault(folded, country)
    default = tuple(str(code).upper() for code in data.get("discovery_default") or ())
    return CountryTable(countries=tuple(countries), discovery_default=default, by_key=by_key)


def _default_path() -> Path:
    # Config paths are relative to the repo root (like config/defaults.yaml).
    # Module-level readers (search/templates.COUNTRIES) load at import, so fall
    # back to the repo copy when the process runs from another directory.
    if COUNTRIES_PATH.exists():
        return COUNTRIES_PATH
    return Path(__file__).resolve().parents[4] / COUNTRIES_PATH


def country_table(path: Path | None = None) -> CountryTable:
    return _load(str(path or _default_path()))


def resolve_country(text: str | None, path: Path | None = None) -> Country | None:
    """The country a whole name, alias or alpha-2 code names, else ``None``."""
    if not text:
        return None
    return country_table(path).by_key.get(fold(text))


def canonical_country(text: str | None, path: Path | None = None) -> str | None:
    """Display name for ``text``, or ``None`` when it names no country."""
    country = resolve_country(text, path)
    return country.name if country else None


def country_matches(job_country: str | None, targets: list[str]) -> bool:
    """Whether the job's country is one of ``targets``.

    Both sides go through the alias table. A value that names no known country
    matches only an identical folded value (for example a stored "Remote").
    """
    if not job_country:
        return False
    job_folded = fold(job_country)
    if not job_folded:
        return False
    job = resolve_country(job_country)
    for target in targets:
        target_folded = fold(str(target))
        if not target_folded:
            continue
        wanted = resolve_country(str(target))
        if job is not None and wanted is not None:
            if job.code == wanted.code:
                return True
        elif job_folded == target_folded:
            return True
    return False
