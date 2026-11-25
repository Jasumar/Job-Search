"""Role taxonomy (MF3) and role fit (MF4).

``config/role_taxonomy.yaml`` holds broad groups with common roles. A role id is
``"<group>/<role>"``; a group id alone selects the whole group. The profile's
``role_families`` holds these ids, and ``custom_roles`` holds roles the owner
typed with the ids they were mapped to.

The job side is a fact extracted once per job (``job_group``, ``job_role`` in the
job profile), so a profile edit never needs a new extraction. The comparison
below is code only: the score stays deterministic (SYSTEM_MAP seam 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jobsearch_core.config import load_scope, load_yaml
from jobsearch_core.countries import fold

TAXONOMY_PATH = Path("config/role_taxonomy.yaml")
OTHER = "other"

# Old role-family ids (config/scopes/tech.yaml chips) to taxonomy ids. Used by
# migration 0023 and when an old id still arrives (a stale browser tab).
LEGACY_ROLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "backend": ("technology/backend",),
    "data": ("technology/data-engineer", "technology/data-analyst"),
    "ai": ("technology/ml-engineer",),
    "automation": ("technology/devops",),
}


@dataclass(frozen=True)
class Role:
    id: str  # "<group>/<role>"
    group: str
    label: str
    keywords: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Group:
    id: str
    label: str
    roles: tuple[Role, ...]


@dataclass(frozen=True)
class Taxonomy:
    groups: tuple[Group, ...]

    @property
    def roles(self) -> dict[str, Role]:
        return {role.id: role for group in self.groups for role in group.roles}

    @property
    def group_ids(self) -> dict[str, Group]:
        return {group.id: group for group in self.groups}

    def is_id(self, value: str) -> bool:
        return value in self.roles or value in self.group_ids

    def group_of(self, value: str) -> str | None:
        if value in self.group_ids:
            return value
        role = self.roles.get(value)
        return role.group if role else None

    def label(self, value: str) -> str:
        """``Legal › Counsel`` for a role, ``Legal`` for a group."""
        role = self.roles.get(value)
        if role:
            return f"{self.group_ids[role.group].label} › {role.label}"
        group = self.group_ids.get(value)
        return group.label if group else value

    def roles_in(self, selection: set[str]) -> list[Role]:
        """Every role a selection covers (a group id covers all its roles)."""
        out: list[Role] = []
        for role in self.roles.values():
            if role.id in selection or role.group in selection:
                out.append(role)
        return out


def _default_path() -> Path:
    if TAXONOMY_PATH.exists():
        return TAXONOMY_PATH
    return Path(__file__).resolve().parents[4] / TAXONOMY_PATH


@lru_cache(maxsize=4)
def _load(path: str) -> Taxonomy:
    data = load_yaml(Path(path))
    groups: list[Group] = []
    for raw_group in data.get("groups") or []:
        group_id = str(raw_group["id"])
        roles = tuple(
            Role(
                id=f"{group_id}/{raw_role['id']}",
                group=group_id,
                label=str(raw_role.get("label") or raw_role["id"]),
                keywords=tuple(str(k) for k in raw_role.get("keywords") or ()),
            )
            for raw_role in raw_group.get("roles") or []
        )
        groups.append(Group(id=group_id, label=str(raw_group.get("label") or group_id), roles=roles))
    return Taxonomy(groups=tuple(groups))


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    return _load(str(path or _default_path()))


def normalize_selection(ids: list[str], taxonomy: Taxonomy | None = None) -> list[str]:
    """Known taxonomy ids in input order, deduped; legacy ids are translated."""
    taxonomy = taxonomy or load_taxonomy()
    out: list[str] = []
    for raw in ids:
        value = str(raw).strip()
        for candidate in LEGACY_ROLE_FAMILIES.get(value, (value,)):
            if taxonomy.is_id(candidate) and candidate not in out:
                out.append(candidate)
    return out


def discovery_families(selection: list[str], scope_name: str = "tech") -> list[str]:
    """tech.yaml discovery families the selected taxonomy ids point to.

    Discovery stays tech-scoped (MF3 boundary): the mapping lives in the scope
    file, in its family order. A group id selects a family when any of the
    family's ids is in that group.
    """
    taxonomy = load_taxonomy()
    chosen = set(normalize_selection(selection, taxonomy))
    groups = {value for value in chosen if value in taxonomy.group_ids}
    families: list[str] = []
    for family, ids in load_scope(scope_name).role_family_taxonomy.items():
        if any(i in chosen or taxonomy.group_of(i) in groups for i in ids):
            families.append(family)
    return families


def prompt_vocabulary(taxonomy: Taxonomy | None = None) -> str:
    """One line per group: ``group: group/role (Label), ...`` for a prompt."""
    taxonomy = taxonomy or load_taxonomy()
    lines = []
    for group in taxonomy.groups:
        roles = ", ".join(f"{role.id} ({role.label})" for role in group.roles)
        lines.append(f"{group.id} [{group.label}]: {roles}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# title words
# --------------------------------------------------------------------------


def _phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    folded = fold(phrase)
    if not folded:
        return None
    return re.compile(r"(?<![0-9a-z])" + re.escape(folded) + r"(?![0-9a-z])")


@lru_cache(maxsize=2048)
def _cached_pattern(phrase: str) -> re.Pattern[str] | None:
    return _phrase_pattern(phrase)


def phrase_in_title(phrase: str, title: str) -> bool:
    pattern = _cached_pattern(phrase)
    return bool(pattern and pattern.search(fold(title)))


def title_keyword_hits(
    title: str | None,
    selection: list[str],
    custom_texts: list[str] | None = None,
    taxonomy: Taxonomy | None = None,
) -> int:
    """How many selected roles (or typed roles) the job title names."""
    if not title:
        return 0
    taxonomy = taxonomy or load_taxonomy()
    folded = fold(title)
    hits = 0
    for role in taxonomy.roles_in(set(selection)):
        if any(
            (pattern := _cached_pattern(keyword)) is not None and pattern.search(folded)
            for keyword in role.keywords
        ):
            hits += 1
    for text in custom_texts or []:
        if custom_role_in_title(text, title):
            hits += 1
    return hits


def custom_role_in_title(text: str, title: str) -> bool:
    """A typed role matches a title when every word of it is in the title."""
    words = fold(text).split()
    if not words:
        return False
    title_words = set(fold(title).split())
    return all(word in title_words for word in words)


# --------------------------------------------------------------------------
# custom roles
# --------------------------------------------------------------------------


def custom_role_ids(custom_roles: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for entry in custom_roles:
        for value in entry.get("ids") or []:
            if value not in out:
                out.append(str(value))
    return out


def custom_role_texts(custom_roles: list[dict[str, Any]]) -> list[str]:
    return [str(entry.get("text") or "").strip() for entry in custom_roles if str(entry.get("text") or "").strip()]


def clean_custom_roles(raw: list[Any], taxonomy: Taxonomy | None = None) -> list[dict[str, Any]]:
    """``[{text, ids}]`` with empty texts dropped and unknown ids removed."""
    taxonomy = taxonomy or load_taxonomy()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw or []:
        if isinstance(entry, str):
            entry = {"text": entry, "ids": []}
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text or fold(text) in seen:
            continue
        seen.add(fold(text))
        ids = normalize_selection([str(v) for v in entry.get("ids") or []], taxonomy)
        out.append({"text": text, "ids": ids})
    return out


# --------------------------------------------------------------------------
# role fit (MF4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleFit:
    value: float | None  # None: nothing to compare
    strength: str | None = None
    gap: str | None = None
    hide_reason: str | None = None


def role_fit(
    job_profile: dict[str, Any],
    title: str | None,
    selection: list[str],
    custom_roles: list[dict[str, Any]] | None = None,
    taxonomy: Taxonomy | None = None,
) -> RoleFit:
    """Compare the job's extracted function with the owner's target roles.

    1.0: the job's role (or its whole group) is selected, or its title names a
    typed role. 0.5: same group as a selected role. 0.0: outside the selection.
    ``None``: no selection, or the job has no extracted function yet.

    Hidden from Matches only when both the group and the role are outside the
    selection, the role is a taxonomy role (``other`` is never hidden on role
    alone), and the title names no selected role keyword.
    """
    taxonomy = taxonomy or load_taxonomy()
    custom_roles = custom_roles or []
    selected = set(normalize_selection(selection, taxonomy)) | set(custom_role_ids(custom_roles))
    texts = custom_role_texts(custom_roles)
    if not selected and not texts:
        return RoleFit(None)

    job_role = str(job_profile.get("job_role") or "").strip()
    job_group = str(job_profile.get("job_group") or "").strip()
    if not job_role and not job_group:
        return RoleFit(None)  # profile extracted before MF3
    if job_role in taxonomy.roles:
        job_group = taxonomy.roles[job_role].group
    known_role = job_role in taxonomy.roles
    known_group = job_group in taxonomy.group_ids

    if title and any(custom_role_in_title(text, title) for text in texts):
        matched = next(text for text in texts if custom_role_in_title(text, title))
        return RoleFit(1.0, strength=f"Title matches your role \"{matched}\"")
    if (known_role and job_role in selected) or (known_group and job_group in selected):
        return RoleFit(1.0, strength=f"Role {taxonomy.label(job_role if known_role else job_group)} is a target role")
    if not known_group:
        return RoleFit(None)  # group "other": nothing to compare

    selected_groups = {taxonomy.group_of(value) for value in selected} - {None}
    label = taxonomy.label(job_role if known_role else job_group)
    if job_group in selected_groups:
        return RoleFit(0.5, gap=f"Role {label} is near your target roles (same group)")

    title_hit = title_keyword_hits(title, sorted(selected), texts, taxonomy) > 0
    gap = f"Role {label} is not in your target roles"
    if known_role and not title_hit:
        return RoleFit(0.0, gap=gap, hide_reason=f"Role is {label}, not in your target roles")
    return RoleFit(0.0, gap=gap)
