from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jobsearch_core.llm.client import LLMClient
from jobsearch_core.profile_evidence import CandidateEvidence
from jobsearch_core.tailor.prompts import TAILOR_SYSTEM_PROMPT, build_tailor_user_prompt

TEMPLATE_DIR = Path("data/cv_templates")
VALID_VARIANTS = {"backend", "data", "ai"}

SECTION_HEADER_RE = re.compile(r"^==\s+(.+)$", re.MULTILINE)


class TailorResult:
    def __init__(
        self,
        original: str,
        tailored: str,
        diff: str,
        edits: dict,
        cache_hit: bool = False,
    ):
        self.original = original
        self.tailored = tailored
        self.diff = diff
        self.edits = edits
        self.cache_hit = cache_hit


class ProjectEdits(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bullets: list[str] = Field(default_factory=list)


class TailorEdits(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str | None = None
    projects: dict[str, ProjectEdits] = Field(default_factory=dict)


def load_template(variant: str) -> str:
    if variant not in VALID_VARIANTS:
        msg = f"Invalid variant '{variant}'. Must be one of {sorted(VALID_VARIANTS)}"
        raise ValueError(msg)
    path = TEMPLATE_DIR / f"{variant}.typst"
    return path.read_text(encoding="utf-8")


def _extract_section(text: str, section_name: str) -> str | None:
    lines = text.splitlines(keepends=True)
    start = None
    end = None
    for i, line in enumerate(lines):
        m = SECTION_HEADER_RE.match(line)
        if m:
            if m.group(1).strip() == section_name:
                start = i + 1
            elif start is not None:
                end = i
                break
    if start is None:
        return None
    content = "".join(lines[start:end]).strip()
    return content


def _replace_section(text: str, section_name: str, new_content: str) -> str:
    lines = text.splitlines(keepends=True)
    start = None
    end = None
    for i, line in enumerate(lines):
        m = SECTION_HEADER_RE.match(line)
        if m:
            if m.group(1).strip() == section_name:
                start = i + 1
            elif start is not None:
                end = i
                break
    if start is None:
        return text
    if end is None:
        end = len(lines)
    new_lines = new_content.splitlines(keepends=True)
    return "".join(lines[:start] + new_lines + lines[end:])


_PROJECT_TAIL = r"[^\"]*\"[^)]*\)\s*\n"


def _replace_project_bullets(text: str, project_name: str, new_bullets: list[str]) -> str:
    escaped_name = re.escape(project_name)
    pattern = (
        r"(#project\(\s*name:\s*\"[^\"]*"
        + escaped_name
        + _PROJECT_TAIL
        + r")(.*?)(?=\n#project|\n==\s|$)"
    )
    replacement = r"\1" + "\n".join(new_bullets) + "\n" if new_bullets else r"\1"
    return re.sub(pattern, replacement, text, count=1, flags=re.DOTALL)


def compute_diff(original: str, modified: str) -> str:
    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile="original",
            tofile="tailored",
            n=3,
        )
    )
    return "".join(diff_lines)


def _content_cache_key(evidence: CandidateEvidence, job_description: str) -> str:
    ev_json = json.dumps(evidence.model_dump(), sort_keys=True, default=str)
    ev_hash = hashlib.sha256(ev_json.encode()).hexdigest()[:16]
    job_hash = hashlib.sha256(job_description.encode()).hexdigest()[:16]
    return f"{ev_hash}_{job_hash}"


def load_tailor_cache(
    cache_dir: Path, evidence: CandidateEvidence, job_description: str
) -> dict | None:
    key = _content_cache_key(evidence, job_description)
    cache_file = cache_dir / "tailor" / f"{key}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return TailorEdits.model_validate(data["edits"]).model_dump()
    return None


def save_tailor_cache(
    cache_dir: Path, evidence: CandidateEvidence, job_description: str, edits: dict
) -> str:
    key = _content_cache_key(evidence, job_description)
    cache_file = cache_dir / "tailor" / f"{key}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps({"edits": edits, "key": key}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return key


def apply_edits(template: str, edits: dict) -> str:
    result = template

    summary = edits.get("summary")
    if summary:
        result = _replace_section(result, "Summary", summary.strip())

    projects = edits.get("projects", {})
    for project_name, project_data in projects.items():
        bullets = project_data.get("bullets")
        if bullets is not None:
            formatted = [b if b.startswith("- ") else f"- {b}" for b in bullets]
            result = _replace_project_bullets(result, project_name, formatted)

    return result


def tailor_cv(
    evidence: CandidateEvidence,
    job_description: str,
    base_variant: str,
    client: LLMClient,
    cache_dir: Path,
) -> TailorResult:
    template = load_template(base_variant)

    cached = load_tailor_cache(cache_dir, evidence, job_description)
    if cached is not None:
        tailored = apply_edits(template, cached)
        diff = compute_diff(template, tailored)
        return TailorResult(
            original=template,
            tailored=tailored,
            diff=diff,
            edits=cached,
            cache_hit=True,
        )

    evidence_yaml_lines = []
    # The tailor prompt is the one prompt that legitimately carries the
    # candidate's own contact header (it fills the CV template), so unlike
    # the scoring projection this path reads the fields it needs directly.
    evidence_yaml_lines.append(f"Name: {evidence.name}")
    evidence_yaml_lines.append(f"Location: {evidence.location}")
    evidence_yaml_lines.append(f"Education: {', '.join(e.degree + ' ' + e.field for e in evidence.education)}")
    evidence_yaml_lines.append(f"Skills: {', '.join(evidence.skills)}")
    evidence_yaml_lines.append("Experience:")
    for exp in evidence.experience:
        evidence_yaml_lines.append(f"  - {exp.title} @ {exp.company} ({exp.start} - {exp.end})")
        for resp in exp.responsibilities:
            evidence_yaml_lines.append(f"    - {resp}")
        if exp.technologies:
            evidence_yaml_lines.append(f"    Technologies: {', '.join(exp.technologies)}")
    evidence_yaml_lines.append("Projects:")
    for proj in evidence.projects:
        evidence_yaml_lines.append(f"  - {proj.name} ({proj.role})")
        evidence_yaml_lines.append(f"    Description: {proj.description}")
        if proj.technologies:
            evidence_yaml_lines.append(f"    Technologies: {', '.join(proj.technologies)}")
    if evidence.additional_experience.strip():
        evidence_yaml_lines.append("Additional experience (use verbatim):")
        evidence_yaml_lines.append(evidence.additional_experience)

    evidence_text = "\n".join(evidence_yaml_lines)
    user_prompt = build_tailor_user_prompt(evidence_text, job_description, template)

    response = client.complete_json(TAILOR_SYSTEM_PROMPT, user_prompt)
    edits = TailorEdits.model_validate_json(response).model_dump()

    save_tailor_cache(cache_dir, evidence, job_description, edits)

    tailored = apply_edits(template, edits)
    diff = compute_diff(template, tailored)

    return TailorResult(
        original=template,
        tailored=tailored,
        diff=diff,
        edits=edits,
    )
