"""PDF plain-text extraction via pdf-parse 2.x (Cherry Studio–aligned)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pypdf import PdfReader

from powerlit.services.ai_analysis import AIServiceError
from powerlit.settings import PROJECT_ROOT, Settings


def extract_pdf_text_pdf_parse(pdf_path: Path, *, settings: Settings) -> str:
    """Mirror Cherry Studio ``packages/shared/utils/pdf.ts`` buffer path using ``PDFParse``."""
    node_path = shutil.which("node")
    if node_path is None:
        raise AIServiceError("Node.js is required for pdf-parse PDF text extraction.")

    helper_path = PROJECT_ROOT / "tools" / "pdf_parse_extract.mjs"
    if not helper_path.exists():
        raise AIServiceError(f"pdf-parse helper script is missing: {helper_path}")

    timeout = settings.ai_local_pdf_text_timeout
    try:
        result = subprocess.run(
            [node_path, str(helper_path), str(pdf_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(PROJECT_ROOT),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AIServiceError(
            "pdf-parse timed out while extracting PDF text "
            f"after {timeout:.1f}s."
        ) from exc
    except OSError as exc:
        raise AIServiceError(f"Unable to run pdf-parse extraction: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise AIServiceError("pdf-parse extraction failed" + (f": {stderr}" if stderr else "."))

    return result.stdout


def extract_pdf_text_per_page(pdf_path: Path, *, settings: Settings) -> list[str]:
    """
    One plain-text segment per PDF page. Prefer pdf-parse full text split on form-feed (\\f)
    when segment count matches page count; otherwise fall back to pypdf per-page extraction.
    """
    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    if page_count == 0:
        return []

    full = extract_pdf_text_pdf_parse(pdf_path, settings=settings)
    if "\f" in full:
        segments = [p.strip() for p in full.split("\f")]
        if len(segments) == page_count:
            return segments
        if len(segments) > page_count:
            tail = "\f".join(segments[page_count - 1 :]).strip()
            head = segments[: page_count - 1]
            if len(head) + 1 == page_count:
                return head + [tail]

    return [((reader.pages[i].extract_text() or "").strip()) for i in range(page_count)]
