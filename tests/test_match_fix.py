"""Match fix MF1–MF6 (master_plan §Match fix): evidence proposal shape, empty
components, role taxonomy and role fit, Matches status, full country list."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from jobsearch_core.countries import canonical_country, country_matches, country_table
from jobsearch_core.cv_parse import _validated_evidence
from jobsearch_core.profile_evidence import CandidateEvidence, EvidenceExperience
from jobsearch_core.role_taxonomy import (
    discovery_families,
    load_taxonomy,
    normalize_selection,
    role_fit,
)
from jobsearch_core.scoring.match import apply_result_to_job, score_job_against_evidence
from jobsearch_core.scoring.profile import extract_job_profile, normalize_job_role, profile_is_current
from sqlalchemy import inspect

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _evidence(**overrides) -> CandidateEvidence:
    base = dict(
        skills=["python", "fastapi"],
        experience=[
            EvidenceExperience(
                title="Backend Developer", start="2019", end="present", technologies=["python"]
            )
        ],
        target_countries=["Finland"],
        target_roles=["technology/backend"],
    )
    base.update(overrides)
    return CandidateEvidence(**base)


def _job(title: str = "Backend Developer", **kwargs):
    return SimpleNamespace(
        id=kwargs.pop("id", 1),
        raw_title=title,
        description=kwargs.pop("description", "A role."),
        country=kwargs.pop("country", "Finland"),
        raw_location=kwargs.pop("raw_location", None),
        normalized_location=kwargs.pop("normalized_location", None),
    )


# ---------------------------------------------------------------------------
# MF2: empty is not perfect
# ---------------------------------------------------------------------------


def test_a_job_with_no_stated_requirements_no_longer_outranks_a_matching_one():
    empty = {"remote_policy": "onsite"}
    matching = {"remote_policy": "onsite", "required_technologies": ["Python", "FastAPI"]}
    evidence = _evidence(target_roles=[])
    low = score_job_against_evidence(_job(id=1), empty, evidence)
    high = score_job_against_evidence(_job(id=2), matching, evidence)
    assert set(low.components) == {"location"}
    assert {"location", "required_technologies"} == set(high.components)
    assert high.score >= low.score
    assert "Compared: location." in low.summary
    assert "Compared: required technologies, location." in high.summary


def test_missing_technologies_score_zero_on_that_component_only():
    profile = {"remote_policy": "onsite", "required_technologies": ["Rust"]}
    result = score_job_against_evidence(_job(), profile, _evidence(target_roles=[]))
    assert result.components["required_technologies"] == 0.0
    assert "experience" not in result.components  # no stated years
    assert "languages" not in result.components
    assert "nice_to_have" not in result.components


# ---------------------------------------------------------------------------
# MF3/MF4: role taxonomy and role fit
# ---------------------------------------------------------------------------


def test_taxonomy_ids_and_legacy_translation():
    taxonomy = load_taxonomy()
    assert "technology/backend" in taxonomy.roles
    assert taxonomy.group_of("legal/licensing") == "legal"
    assert normalize_selection(["data", "ai", "legal", "bogus"]) == [
        "technology/data-engineer",
        "technology/data-analyst",
        "technology/ml-engineer",
        "legal",
    ]


def test_discovery_families_come_from_technology_roles_only():
    assert discovery_families(["technology/backend", "legal/counsel"]) == ["backend"]
    assert discovery_families(["legal"]) == []
    assert discovery_families(["technology"]) == ["backend", "data", "ai", "automation"]
    assert discovery_families(["backend", "data", "ai", "automation"]) == [
        "backend",
        "data",
        "ai",
        "automation",
    ]


def _fit(job_role: str, title: str = "Some title", selection=("technology/backend",), custom=None):
    group = job_role.split("/")[0] if "/" in job_role else job_role
    return role_fit({"job_group": group, "job_role": job_role}, title, list(selection), custom)


def test_role_fit_values():
    assert _fit("technology/backend").value == 1.0
    assert _fit("technology/frontend").value == 0.5
    legal = _fit("legal/licensing", title="Licensing Manager")
    assert legal.value == 0.0
    assert legal.hide_reason == "Role is Legal & compliance › Licensing & IP, not in your target roles"
    # A selected group covers each of its roles.
    assert _fit("legal/counsel", selection=("legal",)).value == 1.0


def test_role_other_is_never_hidden_on_role_alone():
    fit = role_fit({"job_group": "legal", "job_role": "other"}, "Something", ["technology/backend"])
    assert fit.value == 0.0 and fit.hide_reason is None
    assert role_fit({"job_group": "other", "job_role": "other"}, "x", ["technology/backend"]).value is None


def test_a_title_with_a_selected_role_keyword_is_never_hidden():
    # Extraction says legal, but the title names "backend": keep it visible.
    fit = _fit("legal/compliance", title="Backend Developer, compliance tooling")
    assert fit.value == 0.0
    assert fit.hide_reason is None


def test_a_typed_role_matches_the_title_by_words():
    custom = [{"text": "solutions consultant", "ids": []}]
    fit = _fit("sales/account-manager", title="Senior Solutions Consultant", selection=(), custom=custom)
    assert fit.value == 1.0
    mapped = [{"text": "pre-sales lead", "ids": ["sales/pre-sales"]}]
    assert _fit("sales/pre-sales", selection=(), custom=mapped).value == 1.0


def test_no_selection_or_no_extracted_role_leaves_the_component_out():
    assert _fit("legal/counsel", selection=()).value is None
    assert role_fit({}, "Backend Developer", ["technology/backend"]).value is None


def test_score_carries_the_hide_reason_and_persists_it():
    profile = {"remote_policy": "onsite", "job_group": "legal", "job_role": "legal/licensing"}
    result = score_job_against_evidence(_job(title="Licensing Manager"), profile, _evidence())
    assert result.components["role_fit"] == 0.0
    assert result.hidden_reason and result.hidden_reason.startswith("Role is Legal")
    row = SimpleNamespace()
    apply_result_to_job(row, result)
    assert row.fit_hidden_reason == result.hidden_reason
    assert "role_fit" in json.loads(row.fit_components)


def test_target_city_is_a_strength_line_with_no_score_effect():
    profile = {"remote_policy": "onsite"}
    plain = score_job_against_evidence(_job(raw_location="Espoo"), profile, _evidence())
    city = score_job_against_evidence(
        _job(raw_location="Helsinki, Finland"), profile, _evidence(target_cities=["Helsinki"])
    )
    assert "In your target city Helsinki" in city.strengths
    assert city.score == plain.score


def test_job_role_is_forced_into_the_taxonomy():
    assert normalize_job_role({"job_role": "Legal/Counsel", "job_group": "x"}) == {
        "job_role": "legal/counsel",
        "job_group": "legal",
    }
    assert normalize_job_role({"job_role": "made/up", "job_group": "sales"}) == {
        "job_role": "other",
        "job_group": "sales",
    }
    assert normalize_job_role({"job_role": "made/up", "job_group": "nope"})["job_group"] == "other"


def test_extraction_records_the_role_and_old_profiles_are_not_current(tmp_path):
    class Client:
        def complete_json(self, system, user):
            assert "technology/backend" in system  # the role list is in the prompt
            return json.dumps({"job_group": "technology", "job_role": "technology/backend"})

    profile = extract_job_profile(Client(), tmp_path, "A backend role")
    assert profile["job_role"] == "technology/backend"
    assert profile_is_current(json.dumps(profile))
    assert not profile_is_current(json.dumps({"required_technologies": []}))
    assert not profile_is_current(None)


# ---------------------------------------------------------------------------
# MF1: the evidence proposal is in the EvidenceData shape
# ---------------------------------------------------------------------------


def test_evidence_proposal_is_normalised_into_the_stored_shape():
    response = json.dumps(
        {
            "skills": ["Python", "SQL"],
            "evidence": {
                "experience": [
                    {"title": "Dev", "company": "Acme", "start": 2019, "end": None,
                     "technologies": ["Python", "python"]},
                    {"role": "Analyst", "company": "Beta"},  # older prompt shape
                    {"company": "No title"},  # dropped
                ],
                "languages": ["Finnish", {"language": "English", "proficiency": "C1"}],
                "education": [{"degree": "MSc", "institution": "Aalto", "year": "2018"}],
            },
        }
    )
    evidence = _validated_evidence(response)
    assert evidence["skills"] == ["Python", "SQL"]  # fallback from the top level
    assert [e["title"] for e in evidence["experience"]] == ["Dev", "Analyst"]
    assert evidence["experience"][0]["start"] == "2019"
    assert evidence["experience"][0]["technologies"] == ["Python"]
    assert evidence["languages"] == [
        {"language": "Finnish", "proficiency": ""},
        {"language": "English", "proficiency": "C1"},
    ]
    assert evidence["education"][0]["year"] == 2018


def test_can_score_needs_skills_or_experience():
    assert not CandidateEvidence().can_score
    assert CandidateEvidence(skills=["x"]).can_score


# ---------------------------------------------------------------------------
# MF6: countries
# ---------------------------------------------------------------------------


def test_the_country_list_is_the_full_iso_list():
    table = country_table()
    assert len(table.countries) == 249
    assert len({c.code for c in table.countries}) == 249
    assert [c.lower() for c in table.discovery_default] == ["fi", "se", "ee", "dk", "de", "nl"]


def test_country_matching_is_whole_name_through_aliases():
    assert country_matches("Finland", ["Suomi"])
    assert country_matches("Finland", ["fi"])
    assert country_matches("United States", ["USA"])
    assert country_matches("Côte d'Ivoire", ["cote d'ivoire"])
    assert not country_matches("Indiana", ["India"])
    assert not country_matches("Unknown", ["Finland"])
    assert not country_matches("Finland", [""])
    assert not country_matches(None, ["Finland"])
    assert canonical_country("holland") == "Netherlands"
    assert canonical_country("Atlantis") is None


# ---------------------------------------------------------------------------
# migration 0023
# ---------------------------------------------------------------------------

_MIGRATION = Path(__file__).resolve().parents[1] / "packages/core/alembic/versions/0023_role_taxonomy.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0023", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0023_adds_columns_and_maps_old_role_ids(tmp_path):
    module = _load_migration()
    assert module.down_revision == "0022_run_source_dropped"
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE user_profile (id INTEGER PRIMARY KEY, role_families TEXT NOT NULL)"
        )
        conn.exec_driver_sql(
            "INSERT INTO user_profile VALUES (1, '[\"backend\", \"data\", \"ai\", \"automation\", \"x\"]')"
        )
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            module.upgrade()
            module.upgrade()  # idempotent
        assert "fit_hidden_reason" in {c["name"] for c in inspect(conn).get_columns("jobs")}
        assert "custom_roles" in {c["name"] for c in inspect(conn).get_columns("user_profile")}
        stored = json.loads(conn.exec_driver_sql("SELECT role_families FROM user_profile").scalar())
        assert stored == [
            "technology/backend",
            "technology/data-engineer",
            "technology/data-analyst",
            "technology/ml-engineer",
            "technology/devops",
            "x",
        ]
        with Operations.context(ctx):
            module.downgrade()
        stored = json.loads(conn.exec_driver_sql("SELECT role_families FROM user_profile").scalar())
        assert stored == ["backend", "data", "ai", "automation", "x"]
        assert "custom_roles" not in {c["name"] for c in inspect(conn).get_columns("user_profile")}


def test_discovery_country_mentions_are_whole_names():
    from jobsearch_core.search.discover import _mentioned_countries

    assert _mentioned_countries("woman-led startups in Indiana") == set()
    assert _mentioned_countries("software companies in Oman and Finland") == {"oman", "finland"}
    assert _mentioned_countries("Papua New Guinea careers") == {"papua new guinea"}
