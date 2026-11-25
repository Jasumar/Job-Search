from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from jobsearch_core.config import FiltersConfig, RejectRule


@dataclass(frozen=True)
class IncludeMatch:
    family: str
    matched_patterns: list[str]


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str | None = None
    detail: str | None = None
    included_families: list[str] = field(default_factory=list)
    include_signals: list[str] = field(default_factory=list)


def apply_filters(
    title: str,
    company: str,
    location: str,
    description: str,
    config: FiltersConfig,
) -> FilterResult:
    matches = match_positive_families(title, description, config)
    families = [m.family for m in matches]
    signals = [pattern for m in matches for pattern in m.matched_patterns]

    if not matches:
        return FilterResult(
            accepted=False,
            reason="no_positive_family",
            detail="job matched no target role family in title or description",
            included_families=families,
            include_signals=signals,
        )

    rejected, reason, detail = apply_reject_rules(title, company, location, description, config)
    if rejected:
        return FilterResult(False, reason, detail, families, signals)
    return FilterResult(True, None, None, families, signals)


def fit_flags_json(result: FilterResult) -> str | None:
    """The filter hit as the row's ``fit_flags`` (3.8c M0): a fact, not a verdict."""
    if result.accepted:
        return None
    return json.dumps([{"reason": result.reason, "detail": result.detail}], ensure_ascii=False)


def match_positive_families(
    title: str,
    description: str,
    config: FiltersConfig,
) -> list[IncludeMatch]:
    text = f"{title}\n{description}".lower()
    matches: list[IncludeMatch] = []
    for rule in config.include_rules:
        matched_patterns = [p for p in rule.patterns if re.search(p, text)]
        if matched_patterns:
            matches.append(IncludeMatch(family=rule.family, matched_patterns=matched_patterns))
    return matches


def apply_reject_rules(
    title: str,
    company: str,
    location: str,
    description: str,
    config: FiltersConfig,
) -> tuple[bool, str | None, str | None]:
    for rule in config.reject_rules:
        text = _get_field(rule, title, company, location, description)
        if text and re.search(rule.pattern, text):
            return True, rule.reason, f"matched pattern in {rule.field}"
    return False, None, None


def _get_field(
    rule: RejectRule, title: str, company: str, location: str, description: str
) -> str | None:
    field_map = {
        "title": title,
        "company": company,
        "location": location,
        "description": description,
    }
    return field_map.get(rule.field)
