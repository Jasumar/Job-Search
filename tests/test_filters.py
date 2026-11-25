from __future__ import annotations

from pathlib import Path

from jobsearch_core.config import load_filters_config
from jobsearch_core.filters import apply_filters, apply_reject_rules, match_positive_families


def test_rejects_senior_title():
    config = load_filters_config(Path("config/filters.yaml"))
    rejected, reason, _ = apply_reject_rules(
        "Senior Staff Engineer", "Corp", "Helsinki", "some description", config
    )
    assert rejected
    assert "senior" in reason or "staff" in reason


def test_does_not_reject_junior_title():
    config = load_filters_config(Path("config/filters.yaml"))
    rejected, _, _ = apply_reject_rules(
        "Junior Python Developer", "Corp", "Helsinki", "Python experience required", config
    )
    assert not rejected


def test_rejects_high_experience_requirement():
    config = load_filters_config(Path("config/filters.yaml"))
    rejected, reason, _ = apply_reject_rules(
        "Python Developer", "Corp", "Helsinki", "Must have 10+ years of experience", config
    )
    assert rejected
    assert "high_experience" in reason


def test_rejects_non_target_country():
    config = load_filters_config(Path("config/filters.yaml"))
    rejected, reason, _ = apply_reject_rules(
        "Python Developer", "Corp", "United States", "great job in the US", config
    )
    assert rejected
    assert "outside_target_region" in reason


def test_rejects_frontend_role():
    config = load_filters_config(Path("config/filters.yaml"))
    rejected, reason, _ = apply_reject_rules(
        "Developer", "Corp", "Helsinki", "React frontend developer needed", config
    )
    assert rejected
    assert "wrong_role_family" in reason


def test_include_gate_rejects_no_family_match():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Accountant", "Corp", "Helsinki", "Manage ledgers, invoices, and reporting", config
    )
    assert not result.accepted
    assert result.reason == "no_positive_family"


def test_include_gate_accepts_python_backend():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Python Developer", "Corp", "Helsinki", "Build backend services with Django", config
    )
    assert result.accepted
    assert "backend_python" in result.included_families
    assert result.include_signals


def test_include_gate_matches_description_when_title_vague():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Software Developer", "Corp", "Helsinki",
        "We ship LLM-powered products built on PyTorch", config
    )
    assert result.accepted
    assert "ai_llm" in result.included_families


def test_include_gate_accepts_generic_software_engineer():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Software Engineer", "Corp", "Helsinki",
        "We are looking for a developer to build and maintain products", config
    )
    assert result.accepted
    assert "software_engineering" in result.included_families


def test_include_gate_accepts_junior_software_engineer():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Junior Software Engineer", "Corp", "Helsinki",
        "Join our team and grow into a full-time engineer", config
    )
    assert result.accepted
    assert "software_engineering" in result.included_families


def test_include_gate_accepts_junior_developer_without_specific_stack():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Junior Developer", "Corp", "Helsinki",
        "Start your career here; we mentor juniors", config
    )
    assert result.accepted
    assert "software_engineering" in result.included_families


def test_include_gate_accepts_fullstack_software_engineer():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Fullstack Software Engineer", "Corp", "Helsinki",
        "Work across the whole product", config
    )
    assert result.accepted
    assert "backend_python" in result.included_families


def test_include_gate_senior_software_engineer_still_rejected():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Senior Software Engineer", "Corp", "Helsinki", "Python backend", config
    )
    assert not result.accepted
    assert "senior" in result.reason
    assert "software_engineering" in result.included_families


def test_include_gate_rejects_pure_hr_consultant():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "HR Consultant", "Corp", "Helsinki", "Advise hiring managers on people processes", config
    )
    assert not result.accepted


def test_reject_rules_apply_after_include_gate():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Python Developer", "Corp", "United States", "Python backend, great job in the US", config
    )
    assert not result.accepted
    assert result.reason == "outside_target_region"
    assert "backend_python" in result.included_families


def test_reject_rules_apply_after_include_gate_experience():
    config = load_filters_config(Path("config/filters.yaml"))
    result = apply_filters(
        "Python Developer", "Corp", "Helsinki", "Python backend, must have 10+ years", config
    )
    assert not result.accepted
    assert "high_experience" in result.reason
    assert "backend_python" in result.included_families


def test_match_positive_families_reports_family_and_signals():
    config = load_filters_config(Path("config/filters.yaml"))
    matches = match_positive_families("Python Developer", "Backend services", config)
    assert matches
    assert matches[0].family == "backend_python"
    assert any("python" in pattern for pattern in matches[0].matched_patterns)
