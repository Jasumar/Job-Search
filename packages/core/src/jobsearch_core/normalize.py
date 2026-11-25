from __future__ import annotations

import re
from difflib import SequenceMatcher


def normalize(title: str, company: str, location: str) -> tuple[str, str, str, str]:
    norm_title = _normalize_text(title)
    norm_company = _normalize_text(company)
    norm_location = _normalize_text(location)
    country = _guess_country(norm_location)
    return norm_title, norm_company, norm_location, country


def _normalize_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    text = text.lower()
    text = re.sub(r"[,.]$", "", text)
    return text


def _guess_country(location: str) -> str:
    country_map: dict[str, str] = {
        "finland": "Finland",
        "suomi": "Finland",
        "helsinki": "Finland",
        "espoo": "Finland",
        "tampere": "Finland",
        "oulu": "Finland",
        "turku": "Finland",
        "vantaa": "Finland",
        "sweden": "Sweden",
        "sverige": "Sweden",
        "stockholm": "Sweden",
        "gothenburg": "Sweden",
        "göteborg": "Sweden",
        "malmö": "Sweden",
        "malmo": "Sweden",
        "estonia": "Estonia",
        "eesti": "Estonia",
        "tallinn": "Estonia",
        "tartu": "Estonia",
        "denmark": "Denmark",
        "danmark": "Denmark",
        "copenhagen": "Denmark",
        "københavn": "Denmark",
        "aarhus": "Denmark",
        "århus": "Denmark",
        "germany": "Germany",
        "deutschland": "Germany",
        "berlin": "Germany",
        "munich": "Germany",
        "münchen": "Germany",
        "hamburg": "Germany",
        "cologne": "Germany",
        "köln": "Germany",
        "frankfurt": "Germany",
        "netherlands": "Netherlands",
        "nederland": "Netherlands",
        "amsterdam": "Netherlands",
        "rotterdam": "Netherlands",
        "utrecht": "Netherlands",
        "eindhoven": "Netherlands",
        "the hague": "Netherlands",
        "den haag": "Netherlands",
        "spain": "Spain",
        "españa": "Spain",
        "barcelona": "Spain",
        "madrid": "Spain",
        "remote": "Remote",
        "worldwide": "Remote",
    }

    for keyword, country in country_map.items():
        if keyword in location:
            return country
    return "Unknown"


def dedup_key(title: str, company: str, location: str) -> str:
    return f"{title}|{company}|{location}"


def is_duplicate(a: str, b: str, threshold: float = 0.85) -> bool:
    return SequenceMatcher(None, a, b).ratio() >= threshold
