"""Phase 3.8g/3.8h backend tests (decided 2026-09-12).

3.8g: profile-parse summary carries ``suggested_role_families`` inferred from
CV/description, validated against the tech.yaml role_families vocabulary.
3.8h: ``POST /profile/cv/file`` extracts text from PDF/docx in core and
enqueues the same profile_parse run; the binary is never retained.
"""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from jobsearch_api.app import create_app
from jobsearch_core.cv.extract import CvExtractionError, extract_cv_text
from jobsearch_core.cv_parse import filter_role_families, known_role_families
from jobsearch_core.db.session import get_session, init_db
from jobsearch_worker import consumer

# --- 3.8g: role-family inference --------------------------------------------


def test_filter_role_families_keeps_taxonomy_ids_and_translates_old_ids():
    # MF3: taxonomy ids pass, old chip ids are translated, unknown ids dropped.
    assert filter_role_families(
        ["legal/counsel", "Backend", "nonsense", "legal/counsel", "sales"]
    ) == ["legal/counsel", "technology/backend", "sales"]
    assert filter_role_families([]) == []
    assert filter_role_families(["nonsense"]) == []


def test_known_role_families_are_the_taxonomy():
    known = set(known_role_families())
    assert {"technology", "legal", "technology/backend", "sales/pre-sales"} <= known


def test_profile_parse_summary_includes_suggested_role_families(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'infer.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    init_db(database_url)

    def fake_propose(client, cache_dir, cv_text, description=None):
        return {
            "name": "T", "skills": ["Python"], "job_preferences": [],
            "notes": [], "education": "", "experience": "",
            "suggested_role_families": ["backend", "ai"],
        }

    monkeypatch.setattr("jobsearch_core.run.propose_profile_from_cv", fake_propose)
    session = get_session()
    try:
        run = consumer.enqueue_run(session, "profile_parse", {"cv_text": "CV"})
        consumer.execute_run(session, run)
        session.refresh(run)
        summary = json.loads(run.summary)
        assert summary["proposal"]["suggested_role_families"] == ["backend", "ai"]
    finally:
        session.close()


# --- 3.8h: CV file extraction ------------------------------------------------


def test_extract_rejects_unsupported_type():
    with pytest.raises(CvExtractionError):
        extract_cv_text(b"whatever", "cv.txt")


def test_extract_rejects_empty_and_oversized():
    with pytest.raises(CvExtractionError):
        extract_cv_text(b"", "cv.pdf")
    with pytest.raises(CvExtractionError):
        extract_cv_text(b"x" * (10 * 1024 * 1024 + 1), "cv.pdf")


def _minimal_pdf(text: str) -> bytes:
    # Real PDF object stream so PyPDF2 has text to extract.
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF".encode()
    )
    return out.getvalue()


def test_extract_pdf_text():
    text = extract_cv_text(_minimal_pdf("Backend Engineer CV"), "cv.pdf")
    assert "Backend Engineer CV" in text


def test_extract_docx_text(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "cv.docx"
    document = docx.Document()
    document.add_paragraph("Data Engineer with Python")
    document.save(path)
    text = extract_cv_text(path.read_bytes(), "cv.docx")
    assert "Data Engineer with Python" in text


# --- 3.8h: API endpoint ------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'file.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    init_db(database_url)
    app = create_app(database_url, start_worker=False)
    with TestClient(app) as test_client:
        yield test_client


def test_cv_file_endpoint_enqueues_parse_run(client):
    response = client.post(
        "/profile/cv/file",
        files={"file": ("cv.pdf", _minimal_pdf("Python backend developer"), "application/pdf")},
        data={"description": "backend roles"},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()
    assert run["run_type"] == "profile_parse"
    assert "Python backend developer" in run["params"]["cv_text"]


def test_cv_file_endpoint_rejects_bad_file(client):
    response = client.post(
        "/profile/cv/file",
        files={"file": ("cv.txt", b"plain text", "text/plain")},
    )
    assert response.status_code == 422


def test_cv_file_endpoint_rejects_missing_file(client):
    response = client.post("/profile/cv/file")
    assert response.status_code == 422
