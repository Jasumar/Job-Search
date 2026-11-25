from __future__ import annotations

from pathlib import Path

import pytest
from jobsearch_core.profile_evidence import CandidateEvidence
from jobsearch_core.tailor import VALID_VARIANTS, load_template
from jobsearch_core.tailor.workflow import (
    _content_cache_key,
    _replace_project_bullets,
    _replace_section,
    apply_edits,
    compute_diff,
    load_tailor_cache,
    save_tailor_cache,
)

TEMPLATE_DIR = Path("data/cv_templates")


class TestLoadTemplate:
    def test_load_valid_variants(self):
        # The templates are the user's own CV files under the git-ignored data/.
        if not all((TEMPLATE_DIR / f"{v}.typst").exists() for v in VALID_VARIANTS):
            pytest.skip("CV templates not present in data/cv_templates/")
        for variant in VALID_VARIANTS:
            text = load_template(variant)
            assert len(text) > 100
            assert "paper: \"a4\"" in text, f"{variant} should use A4 paper"

    def test_load_invalid_variant(self):
        with pytest.raises(ValueError, match="Invalid variant"):
            load_template("nonexistent")


class TestReplaceSection:
    def test_replace_summary(self):
        template = "== Summary\n\nOld summary text.\n\n== Education\n\nSome education."
        result = _replace_section(template, "Summary", "New summary.")
        assert "New summary." in result
        assert "Old summary" not in result
        assert "== Education" in result

    def test_replace_nonexistent_section(self):
        template = "== Summary\n\nText.\n\n== Education\n\nMore."
        result = _replace_section(template, "Work Experience", "New.")
        assert result == template

    def test_replace_last_section(self):
        template = "== Summary\n\nText.\n\n== Skills\n\nSkill list."
        result = _replace_section(template, "Skills", "New skills.")
        assert "New skills." in result
        assert "Skill list." not in result


class TestReplaceProjectBullets:
    def test_replace_bullets(self):
        template = (
            "#project(\n  name: \"My Project\",\n  role: \"Dev\",\n  url: \"\",\n  dates: \"\"\n)\n"
            "- Original bullet\n- Another bullet\n\n"
            "#project(\n  name: \"Other Project\",\n  role: \"Dev\",\n  url: \"\",\n  dates: \"\"\n)\n"
            "- Other bullet\n"
        )
        result = _replace_project_bullets(template, "My Project", ["- New bullet 1", "- New bullet 2"])
        assert "- New bullet 1" in result
        assert "- New bullet 2" in result
        assert "Original bullet" not in result
        assert "Other bullet" in result  # other project unchanged

    def test_replace_empty_bullets(self):
        template = (
            "#project(\n  name: \"My Project\",\n  role: \"Dev\",\n  url: \"\",\n  dates: \"\"\n)\n"
            "- Original bullet\n"
        )
        result = _replace_project_bullets(template, "My Project", [])
        assert "Original bullet" not in result
        assert "#project" in result

    def test_replace_nonexistent_project(self):
        template = (
            "#project(\n  name: \"Real\",\n  role: \"Dev\",\n  url: \"\",\n  dates: \"\"\n)\n"
            "- Bullet\n"
        )
        result = _replace_project_bullets(template, "Fake", ["- New"])
        assert result == template


class TestApplyEdits:
    def test_summary_only(self):
        template = "== Summary\n\nOld summary.\n\n== Education\n\nEdu."
        edits = {"summary": "New summary.", "projects": {}}
        result = apply_edits(template, edits)
        assert "New summary." in result
        assert "Old summary." not in result

    def test_summary_and_projects(self):
        template = (
            "== Summary\n\nOld summary.\n\n== Projects\n\n"
            "#project(\n  name: \"Proj\",\n  role: \"Dev\",\n  url: \"\",\n  dates: \"\"\n)\n"
            "- Old bullet\n"
        )
        edits = {
            "summary": "New summary.",
            "projects": {
                "Proj": {"bullets": ["- New bullet"]}
            }
        }
        result = apply_edits(template, edits)
        assert "New summary." in result
        assert "- New bullet" in result
        assert "Old bullet" not in result

    def test_bullets_formatting(self):
        template = (
            "== Projects\n\n"
            "#project(\n  name: \"Proj\",\n  role: \"Dev\",\n  url: \"\",\n  dates: \"\"\n)\n"
            "- Old\n"
        )
        edits = {
            "summary": None,
            "projects": {
                "Proj": {"bullets": ["New bullet without dash"]}
            }
        }
        result = apply_edits(template, edits)
        assert "- New bullet without dash" in result


class TestComputeDiff:
    def test_diff_shows_changes(self):
        diff = compute_diff("line1\nline2\n", "line1\nmodified\n")
        assert "line2" in diff  # context line
        assert "modified" in diff  # added line
        assert "-line2" in diff  # removed line

    def test_diff_no_changes(self):
        diff = compute_diff("same\n", "same\n")
        assert diff == "" or len(diff) == 0  # no diff


class TestTailorCache:
    def test_cache_roundtrip(self, tmp_path):
        evidence = CandidateEvidence(name="Test", location="FI", skills=["Python"])
        job_desc = "A Python job"
        edits = {"summary": "Test.", "projects": {}}

        key = save_tailor_cache(tmp_path, evidence, job_desc, edits)
        assert key is not None

        loaded = load_tailor_cache(tmp_path, evidence, job_desc)
        assert loaded == edits

    def test_cache_miss(self, tmp_path):
        evidence = CandidateEvidence(name="Test", location="FI")
        job_desc = "Different job"
        loaded = load_tailor_cache(tmp_path, evidence, job_desc)
        assert loaded is None

    def test_cache_key_changes_on_different_input(self):
        ev1 = CandidateEvidence(name="A", location="FI")
        ev2 = CandidateEvidence(name="B", location="FI")
        key1 = _content_cache_key(ev1, "job desc")
        key2 = _content_cache_key(ev2, "job desc")
        assert key1 != key2

        key3 = _content_cache_key(ev1, "job desc")
        key4 = _content_cache_key(ev1, "different job")
        assert key3 != key4

    def test_cache_key_same_input(self):
        ev = CandidateEvidence(name="Test", location="FI")
        key1 = _content_cache_key(ev, "same job")
        key2 = _content_cache_key(ev, "same job")
        assert key1 == key2
