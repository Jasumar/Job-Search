"""CV file text extraction (Phase 3.8h backend).

Decided 2026-09-12: the uploaded CV file is transient — text is extracted
in core and only the extracted text is persisted (it becomes the stored raw
CV). The original binary is never retained by the caller.
"""

from __future__ import annotations

import io
from pathlib import Path

# pypdf (PyPDF2's maintained successor) with layout extraction preserves word
# spacing and column structure far better than the default mode (live-validation
# finding 2026-09-22: PyPDF2 mangled spaces badly). Imported at module level so
# the import never happens inside a request thread (see _extract_pdf comment).
from pypdf import PdfReader  # noqa: E402  (pypdf ships with the core deps)

from jobsearch_core.log import get_logger

logger = get_logger(__name__)

SUPPORTED_CV_SUFFIXES = {".pdf", ".docx"}
# Defensive server-side cap; the client validates first (3.8h).
MAX_CV_FILE_BYTES = 10 * 1024 * 1024


class CvExtractionError(ValueError):
    """Raised when a CV file cannot be read or yields no text."""


def _extract_pdf(data: bytes) -> str:
    # pypdf is imported at module level (not lazily here): a lazy import inside
    # a FastAPI/anyio worker thread can deadlock on the interpreter import lock
    # when another thread holds it (observed as a full-suite pytest hang).
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [
            (page.extract_text(extraction_mode="layout") or "")
            for page in reader.pages
        ]
    except Exception as exc:  # corrupt / encrypted / not a PDF
        raise CvExtractionError(f"Unreadable PDF: {exc}") from exc
    return "\n".join(parts for parts in pages if parts).strip()


def _extract_docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as exc:
        raise CvExtractionError("docx support not installed") from exc
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise CvExtractionError(f"Unreadable docx: {exc}") from exc
    paragraphs = [p.text for p in document.paragraphs if p.text and p.text.strip()]
    tables = [
        cell.text.strip()
        for table in document.tables
        for row in table.rows
        for cell in row.cells
        if cell.text and cell.text.strip()
    ]
    return "\n".join(paragraphs + tables).strip()


def extract_cv_text(data: bytes, filename: str) -> str:
    """Extract plain text from a PDF or docx CV file.

    Raises :class:`CvExtractionError` for unsupported extensions, oversized
    files, unreadable content, or empty extractions (no silent empty text).
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_CV_SUFFIXES:
        raise CvExtractionError(
            f"Unsupported CV file type '{suffix or filename}'; expected one of: "
            + ", ".join(sorted(SUPPORTED_CV_SUFFIXES))
        )
    if len(data) > MAX_CV_FILE_BYTES:
        raise CvExtractionError("CV file exceeds the size cap")
    if not data:
        raise CvExtractionError("CV file is empty")

    if suffix == ".pdf":
        text = _extract_pdf(data)
    else:
        text = _extract_docx(data)

    if not text:
        raise CvExtractionError(
            "No text could be extracted (the file may be scanned images or corrupt)"
        )
    logger.info("Extracted %d characters from CV file (%s)", len(text), suffix)
    return text
