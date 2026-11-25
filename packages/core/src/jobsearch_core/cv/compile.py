from __future__ import annotations

import subprocess
from pathlib import Path

from jobsearch_core.log import get_logger

logger = get_logger(__name__)

_MAX_PAGE_COUNT = 1
_TYPST_CMD = "typst"


def is_typst_available() -> bool:
    try:
        subprocess.run([_TYPST_CMD, "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def compile_typst(source_path: Path, output_path: Path | None = None) -> Path:
    if output_path is None:
        output_path = source_path.with_suffix(".pdf")

    logger.info("Compiling %s -> %s", source_path, output_path)
    result = subprocess.run(
        [_TYPST_CMD, "compile", str(source_path), str(output_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Typst compilation failed:\n{result.stderr}")

    if not output_path.exists():
        raise RuntimeError(f"Typst compiled but output not found at {output_path}")

    page_count = _count_pages(output_path)
    if page_count > _MAX_PAGE_COUNT:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"CV overflow: {page_count} pages (max {_MAX_PAGE_COUNT}) — rejected"
        )

    logger.info("Compiled successfully: %s (%d page(s))", output_path, page_count)
    return output_path


def _count_pages(pdf_path: Path) -> int:
    try:
        import pypdf
        with open(pdf_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            return len(reader.pages)
    except ImportError:
        logger.warning("pypdf not installed; skipping page count validation")
        return 1
    except Exception as exc:
        logger.warning("Failed to count pages: %s; assuming 1 page", exc)
        return 1
