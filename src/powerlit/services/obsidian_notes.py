from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from re import IGNORECASE, compile
from time import sleep

from pypdf import PdfReader

from powerlit.models import PaperRecord
from powerlit.services.ai_analysis import (
    AIChatResult,
    AIServiceError,
    OpenAICompatibleAIClient,
    strip_code_fence,
)
from powerlit.services.ai_config import resolve_ai_profile
from powerlit.services.ai_pricing import AIUsageMetrics, merge_usage_metrics
from powerlit.services.mineru_runtime import MineruRuntimeError, MineruTranscriptionService
from powerlit.services.pdf_content_cleaner import clean_direct_pdf_markdown, clean_mineru_markdown
from powerlit.services.pdf_parse_text import extract_pdf_text_pdf_parse, extract_pdf_text_per_page
from powerlit.settings import Settings

CHERRY_STYLE_PDF_PROMPT = "Transcribe the PDF faithfully into Markdown."
ProgressCallback = Callable[[str, dict[str, object] | None], None]
PAGE_HEADING_RE = compile(r"^###\s+Page\s+\d+\s*$", IGNORECASE)


@dataclass(slots=True)
class FormattedNote:
    markdown: str
    usage: AIUsageMetrics | None = None
    generation_mode: str = "ai_direct_pdf_transcription"  # or ai_direct_pdf_page_transcription


GENERATION_MODE_PAGE_BY_PAGE = "ai_direct_pdf_page_transcription"
GENERATION_MODE_SINGLE_SHOT = "ai_direct_pdf_transcription"
GENERATION_MODE_MINERU = "mineru_hybrid_auto_engine"
AI_DIRECT_TRANSCRIPTION_ATTEMPTS = 3


def _transcribe_single_pdf_page_job(
    *,
    settings: Settings,
    profile,
    pdf_filename: str,
    page_index: int,
    total_pages: int,
    page_plain: str,
) -> tuple[int, AIChatResult]:
    """Worker for concurrent page transcription (separate client per thread)."""
    client = OpenAICompatibleAIClient(settings, profile=profile)
    messages = build_per_page_cherry_messages(
        pdf_filename,
        page_index,
        total_pages,
        page_plain,
    )
    for attempt in range(1, AI_DIRECT_TRANSCRIPTION_ATTEMPTS + 1):
        try:
            result = client.chat_text(
                messages,
                timeout=profile.effective_note_timeout,
            )
            return page_index, result
        except AIServiceError:
            if attempt >= AI_DIRECT_TRANSCRIPTION_ATTEMPTS:
                raise
            sleep(float(attempt))
    raise RuntimeError("unreachable")


class ObsidianNoteFormatterService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile = resolve_ai_profile(settings, "note")
        self.client = OpenAICompatibleAIClient(settings, profile=self.profile)
        self.mineru = MineruTranscriptionService(settings)

    def format_note(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path | None = None,
        output_path: Path | None = None,
        proofread_markdown_path: Path | None = None,
        backend_override: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> FormattedNote:
        if pdf_path is None:
            raise AIServiceError("A local PDF path is required for PDF note transcription.")
        backend = (backend_override or self.settings.pdf_transcription_backend).strip().lower()
        if backend == "mineru":
            return self._format_note_with_mineru(
                record,
                pdf_path=pdf_path,
                output_path=output_path,
                progress_callback=progress_callback,
            )
        return self._format_note_from_pdf(
            record,
            pdf_path=pdf_path,
            output_path=output_path,
            proofread_markdown_path=proofread_markdown_path,
            progress_callback=progress_callback,
        )

    def _format_note_with_mineru(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        output_path: Path | None,
        progress_callback: ProgressCallback | None,
    ) -> FormattedNote:
        emit_progress(
            progress_callback,
            "pdf_text_extraction_started",
            {"pdf_path": str(pdf_path), "backend": "mineru"},
        )
        try:
            result = self.mineru.transcribe_pdf(
                record,
                pdf_path=pdf_path,
                output_path=output_path,
            )
        except MineruRuntimeError as exc:
            raise AIServiceError(str(exc)) from exc
        finally:
            emit_progress(
                progress_callback,
                "pdf_text_extraction_finished",
                {"pdf_path": str(pdf_path), "backend": "mineru"},
            )
        body = clean_mineru_markdown(
            result.markdown,
            source_title=record.source_title,
        )

        return FormattedNote(
            markdown=render_obsidian_note(
                record,
                body=body,
            ),
            usage=None,
            generation_mode=result.generation_mode or GENERATION_MODE_MINERU,
        )

    def _format_note_from_pdf(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        output_path: Path | None,
        proofread_markdown_path: Path | None,
        progress_callback: ProgressCallback | None,
    ) -> FormattedNote:
        # Cherry Studio–style: pdf-parse 2.x plain text, then chat (no native PDF upload).
        reader = PdfReader(str(pdf_path))
        page_count = len(reader.pages)
        if page_count == 0:
            raise AIServiceError(f"PDF has no pages: {pdf_path}")

        use_page_mode = (
            self.settings.ai_direct_pdf_page_by_page
            and page_count >= self.settings.ai_direct_pdf_min_pages_for_page_mode
        )

        if not use_page_mode:
            return self._format_note_from_pdf_single_shot(
                record,
                pdf_path=pdf_path,
                output_path=output_path,
                proofread_markdown_path=proofread_markdown_path,
                progress_callback=progress_callback,
            )

        emit_progress(
            progress_callback,
            "pdf_text_extraction_started",
            {"pdf_path": str(pdf_path), "backend": "pdf-parse"},
        )
        try:
            page_texts = extract_pdf_text_per_page(pdf_path, settings=self.settings)
        finally:
            emit_progress(
                progress_callback,
                "pdf_text_extraction_finished",
                {"pdf_path": str(pdf_path), "backend": "pdf-parse"},
            )

        return self._format_note_from_pdf_page_by_page(
            record,
            pdf_path=pdf_path,
            page_texts=page_texts,
            output_path=output_path,
            proofread_markdown_path=proofread_markdown_path,
            progress_callback=progress_callback,
        )

    def _format_note_from_pdf_single_shot(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        output_path: Path | None,
        proofread_markdown_path: Path | None,
        progress_callback: ProgressCallback | None,
    ) -> FormattedNote:
        emit_progress(
            progress_callback,
            "pdf_text_extraction_started",
            {"pdf_path": str(pdf_path), "backend": "pdf-parse"},
        )
        try:
            pdf_plain = extract_pdf_text_pdf_parse(pdf_path, settings=self.settings)
        finally:
            emit_progress(
                progress_callback,
                "pdf_text_extraction_finished",
                {"pdf_path": str(pdf_path), "backend": "pdf-parse"},
            )
        if not pdf_plain.strip():
            raise AIServiceError(
                "pdf-parse returned no text for this PDF; cannot transcribe without extractable text."
            )
        result = self._chat_direct_pdf_plain_text(
            build_cherry_style_pdf_chat_messages(pdf_path.name, pdf_plain),
            progress_callback=progress_callback,
        )

        body = normalize_direct_pdf_note_body(result.text, record.title)
        return FormattedNote(
            markdown=render_obsidian_note(
                record,
                body=body,
            ),
            usage=result.usage,
            generation_mode=GENERATION_MODE_SINGLE_SHOT,
        )

    def _format_note_from_pdf_page_by_page(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        page_texts: list[str],
        output_path: Path | None,
        proofread_markdown_path: Path | None,
        progress_callback: ProgressCallback | None,
    ) -> FormattedNote:
        total = len(page_texts)
        emit_progress(
            progress_callback,
            "pdf_page_transcription_plan",
            {"pdf_path": str(pdf_path), "total_pages": total},
        )
        non_empty: list[tuple[int, str]] = []
        for page_index, page_plain in enumerate(page_texts, start=1):
            if not page_plain.strip():
                emit_progress(
                    progress_callback,
                    "pdf_page_transcription_skipped",
                    {"page": page_index, "total_pages": total, "reason": "empty_text"},
                )
                continue
            non_empty.append((page_index, page_plain))

        if not non_empty:
            raise AIServiceError(
                "No extractable text on any PDF page; cannot transcribe this document."
            )

        max_workers = max(
            1,
            min(self.settings.ai_direct_pdf_page_max_concurrency, len(non_empty)),
        )
        emit_progress(
            progress_callback,
            "pdf_page_transcription_concurrent_start",
            {
                "pdf_path": str(pdf_path),
                "total_pages": total,
                "pages_to_transcribe": len(non_empty),
                "max_workers": max_workers,
            },
        )

        page_results: list[tuple[int, AIChatResult]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_page = {
                executor.submit(
                    _transcribe_single_pdf_page_job,
                    settings=self.settings,
                    profile=self.profile,
                    pdf_filename=pdf_path.name,
                    page_index=page_index,
                    total_pages=total,
                    page_plain=page_plain,
                ): page_index
                for page_index, page_plain in non_empty
            }
            for future in as_completed(future_to_page):
                try:
                    idx, result = future.result()
                except AIServiceError:
                    emit_progress(
                        progress_callback,
                        "ai_transcription_finished",
                        {
                            "mode": "pdf-parse-text-page",
                            "page": future_to_page[future],
                            "total_pages": total,
                            "status": "error",
                        },
                    )
                    raise
                emit_progress(
                    progress_callback,
                    "ai_transcription_finished",
                    {
                        "mode": "pdf-parse-text-page",
                        "page": idx,
                        "total_pages": total,
                        "status": "completed",
                    },
                )
                page_results.append((idx, result))

        page_results.sort(key=lambda item: item[0])
        raw_pages = [r.text for _, r in page_results]
        usages = [r.usage for _, r in page_results]

        merged = merge_page_transcription_markdown(raw_pages, record.title)
        combined_usage = merge_usage_metrics(*usages)

        return FormattedNote(
            markdown=render_obsidian_note(
                record,
                body=merged,
            ),
            usage=combined_usage,
            generation_mode=GENERATION_MODE_PAGE_BY_PAGE,
        )

    def _chat_direct_pdf_plain_text(
        self,
        messages: list[dict[str, str]],
        *,
        progress_callback: ProgressCallback | None,
        page_index: int | None = None,
        page_total: int | None = None,
    ):
        payload: dict[str, object] = {"mode": "pdf-parse-text"}
        if page_index is not None and page_total is not None:
            payload["mode"] = "pdf-parse-text-page"
            payload["page"] = page_index
            payload["total_pages"] = page_total
        emit_progress(
            progress_callback,
            "ai_transcription_started",
            payload,
        )
        for attempt in range(1, AI_DIRECT_TRANSCRIPTION_ATTEMPTS + 1):
            try:
                result = self.client.chat_text(
                    messages,
                    timeout=self.profile.effective_note_timeout,
                )
                break
            except AIServiceError:
                if attempt >= AI_DIRECT_TRANSCRIPTION_ATTEMPTS:
                    emit_progress(
                        progress_callback,
                        "ai_transcription_finished",
                        {**payload, "status": "error"},
                    )
                    raise
                sleep(float(attempt))
        emit_progress(
            progress_callback,
            "ai_transcription_finished",
            {**payload, "status": "completed"},
        )
        return result


def render_obsidian_note(
    record: PaperRecord,
    *,
    body: str,
) -> str:
    return normalize_direct_pdf_note_body(body, record.title)


def build_cherry_style_pdf_chat_messages(filename: str, pdf_plain_text: str) -> list[dict[str, str]]:
    body = f"{filename}\n{pdf_plain_text.strip()}"
    return [{"role": "user", "content": f"{body}\n\n{CHERRY_STYLE_PDF_PROMPT}"}]


def build_per_page_cherry_messages(
    filename: str,
    page_num: int,
    page_total: int,
    page_plain: str,
) -> list[dict[str, str]]:
    if page_num == 1:
        user = (
            f"File: {filename}\nPage {page_num} of {page_total}.\n\n"
            f"{page_plain.strip()}\n\n"
            "Transcribe this page only into Markdown. Include the paper title if it appears on this page. "
            "Preserve structure, equations in LaTeX, tables/figures captions. "
            "Omit running headers, footers, page numbers, and publication boilerplate. "
            "Output only this page's content."
        )
    else:
        user = (
            f"File: {filename}\nPage {page_num} of {page_total}.\n\n"
            f"{page_plain.strip()}\n\n"
            "Transcribe this page only into Markdown. Do not repeat the title or abstract from earlier pages "
            "unless they appear verbatim on this page. Preserve structure, equations, captions. "
            "Omit running headers, footers, page numbers. Output only this page."
        )
    return [{"role": "user", "content": user}]


def merge_page_transcription_markdown(raw_pages: list[str], title: str) -> str:
    if not raw_pages:
        return f"# {title}\n"
    if len(raw_pages) == 1:
        return normalize_direct_pdf_note_body(raw_pages[0], title)
    chunks: list[str] = [normalize_direct_pdf_note_body(raw_pages[0], title).rstrip()]
    for raw in raw_pages[1:]:
        piece = normalize_continuation_page_markdown(raw, title)
        if piece.strip():
            chunks.append(piece.rstrip())
    return "\n\n".join(chunks).rstrip() + "\n"


def normalize_continuation_page_markdown(value: str, title: str) -> str:
    body = clean_direct_pdf_markdown(strip_code_fence(value)).strip()
    if body.startswith("---"):
        parts = body.split("\n---", 1)
        if len(parts) == 2:
            body = parts[1].strip()
    lines = [line for line in body.splitlines() if not PAGE_HEADING_RE.match(line.strip())]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines:
        first_line = strip_heading_markup(lines[0])
        if first_line.casefold() == title.strip().casefold():
            lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
    return "\n".join(lines).strip()


def normalize_direct_pdf_note_body(value: str, title: str) -> str:
    body = clean_direct_pdf_markdown(strip_code_fence(value)).strip()
    if body.startswith("---"):
        parts = body.split("\n---", 1)
        if len(parts) == 2:
            body = parts[1].strip()
    if not body:
        body = f"# {title}"
    if not body.startswith("# "):
        body = f"# {title}\n\n{body}"
    return body.rstrip() + "\n"


def strip_heading_markup(line: str) -> str:
    stripped = line.strip()
    while stripped.startswith("#"):
        stripped = stripped[1:].lstrip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) >= 4:
        stripped = stripped[2:-2].strip()
    return stripped


def emit_progress(
    progress_callback: ProgressCallback | None,
    event: str,
    payload: dict[str, object] | None = None,
) -> None:
    if progress_callback is None:
        return
    progress_callback(event, payload)


def obsidian_path(path: Path | None) -> str:
    if path is None:
        return "unknown"
    root = Path.cwd().resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def escape_frontmatter(value: str) -> str:
    return value.replace('"', "'")
