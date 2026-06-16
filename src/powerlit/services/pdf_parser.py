from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path

from pypdf import PdfReader

from powerlit.models import PaperRecord
from powerlit.services.ai_pricing import AIUsageMetrics, merge_usage_metrics
from powerlit.services.note_review import NoteQualityReviewService
from powerlit.services.library_layout import build_parsed_output_base
from powerlit.services.obsidian_notes import ObsidianNoteFormatterService
from powerlit.services.pdf_parse_text import extract_pdf_text_pdf_parse
from powerlit.settings import Settings


class PDFParseError(RuntimeError):
    """Raised when a local PDF cannot be parsed into text."""


@dataclass(slots=True)
class OutputPaths:
    output_base: Path
    json_path: Path
    markdown_path: Path
    proofread_markdown_path: Path
    proofread_json_path: Path


@dataclass(slots=True)
class TranscribedArtifacts:
    json_path: Path
    markdown_path: Path
    proofread_markdown_path: Path | None = None
    proofread_json_path: Path | None = None
    directory_audit_path: Path | None = None
    note_usage: AIUsageMetrics | None = None
    review_usage: AIUsageMetrics | None = None
    note_generation_mode: str = "ai_direct_pdf_transcription"
    review_passed: bool | None = None
    review_issue_count: int = 0
    review_severe_issue_count: int = 0
    review_derivation_direct_error_count: int = 0
    review_derivation_consistency_count: int = 0


@dataclass(slots=True)
class ParsedArtifacts:
    page_count: int
    text: str
    json_path: Path
    markdown_path: Path | None = None
    proofread_markdown_path: Path | None = None
    proofread_json_path: Path | None = None
    directory_audit_path: Path | None = None
    note_usage: AIUsageMetrics | None = None
    review_usage: AIUsageMetrics | None = None
    note_generation_mode: str = "ai_direct_pdf_transcription"
    review_passed: bool | None = None
    review_issue_count: int = 0
    review_severe_issue_count: int = 0
    review_derivation_direct_error_count: int = 0
    review_derivation_consistency_count: int = 0
    extraction_method_counts: dict[str, int] | None = None
    debug_artifacts: dict[str, Path] | None = None


class PDFParserService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.note_formatter = ObsidianNoteFormatterService(settings)
        self.reviewer = NoteQualityReviewService(settings)

    def parse_record(
        self,
        record: PaperRecord,
        pdf_path: Path,
        *,
        progress_callback: Callable[[str, dict[str, object] | None], None] | None = None,
    ) -> ParsedArtifacts:
        return self._parse_record_direct(
            record,
            pdf_path,
            progress_callback=progress_callback,
        )

    def _parse_record_direct(
        self,
        record: PaperRecord,
        pdf_path: Path,
        *,
        progress_callback: Callable[[str, dict[str, object] | None], None] | None = None,
    ) -> ParsedArtifacts:
        paths = build_output_paths(self.settings, record)
        paths.output_base.parent.mkdir(parents=True, exist_ok=True)
        transcribed = self.transcribe_record(
            record,
            pdf_path=pdf_path,
            progress_callback=progress_callback,
        )
        return ParsedArtifacts(
            page_count=get_pdf_page_count(pdf_path),
            text=load_parsed_content(transcribed.json_path),
            json_path=transcribed.json_path,
            markdown_path=transcribed.markdown_path,
            proofread_markdown_path=transcribed.proofread_markdown_path,
            proofread_json_path=transcribed.proofread_json_path,
            directory_audit_path=transcribed.directory_audit_path,
            note_usage=transcribed.note_usage,
            review_usage=transcribed.review_usage,
            note_generation_mode=transcribed.note_generation_mode,
            review_passed=transcribed.review_passed,
            review_issue_count=transcribed.review_issue_count,
            review_severe_issue_count=transcribed.review_severe_issue_count,
            review_derivation_direct_error_count=(
                transcribed.review_derivation_direct_error_count
            ),
            review_derivation_consistency_count=(
                transcribed.review_derivation_consistency_count
            ),
            extraction_method_counts=None,
            debug_artifacts=None,
        )

    def transcribe_record(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path | None = None,
        progress_callback: Callable[[str, dict[str, object] | None], None] | None = None,
    ) -> TranscribedArtifacts:
        if pdf_path is None:
            raise PDFParseError("A local PDF path is required for transcription.")
        paths = build_output_paths(self.settings, record)
        paths.output_base.parent.mkdir(parents=True, exist_ok=True)

        note_result = self._format_note(
            record,
            pdf_path=pdf_path,
            output_path=paths.markdown_path,
            progress_callback=progress_callback,
        )
        write_parsed_json(
            paths.json_path,
            build_parsed_payload(
                record,
                note_content=note_result.markdown,
                generation_mode=note_result.generation_mode,
                page_count=get_pdf_page_count(pdf_path),
                note_usage=note_result.usage,
            ),
        )
        cleanup_review_artifacts(paths)

        review_result = self._review_transcribed_note(
            record,
            pdf_path=pdf_path,
            note_markdown=note_result.markdown,
        )
        total_note_usage = note_result.usage
        total_review_usage = review_result.usage
        if self._should_retry_with_ai_direct(review_result):
            fallback = self._retry_with_ai_direct(
                record,
                pdf_path=pdf_path,
                output_path=paths.markdown_path,
                progress_callback=progress_callback,
            )
            if fallback is not None:
                fallback_note_result, fallback_review_result = fallback
                total_note_usage = merge_usage_metrics(note_result.usage, fallback_note_result.usage)
                total_review_usage = merge_usage_metrics(review_result.usage, fallback_review_result.usage)
                note_result = fallback_note_result
                review_result = fallback_review_result
                write_parsed_json(
                    paths.json_path,
                    build_parsed_payload(
                        record,
                        note_content=note_result.markdown,
                        generation_mode=note_result.generation_mode,
                        page_count=get_pdf_page_count(pdf_path),
                        note_usage=total_note_usage,
                    ),
                )
        write_parsed_json(
            paths.json_path,
            build_parsed_payload(
                record,
                note_content=note_result.markdown,
                generation_mode=note_result.generation_mode,
                page_count=get_pdf_page_count(pdf_path),
                note_usage=total_note_usage,
                review_result=review_result,
            ),
        )
        return TranscribedArtifacts(
            json_path=paths.json_path,
            markdown_path=None,
            proofread_markdown_path=None,
            proofread_json_path=None,
            directory_audit_path=None,
            note_usage=total_note_usage,
            review_usage=total_review_usage,
            note_generation_mode=note_result.generation_mode,
            review_passed=review_result.passed,
            review_issue_count=review_result.issue_count,
            review_severe_issue_count=review_result.severe_issue_count,
            review_derivation_direct_error_count=(
                review_result.derivation_direct_error_count
            ),
            review_derivation_consistency_count=(
                review_result.derivation_consistency_review_count
            ),
        )

    def _review_transcribed_note(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        note_markdown: str,
    ):
        if not self.settings.note_review_enabled or not self.reviewer.profile.api_key:
            return build_empty_review_result()

        source_text: str | None = None
        try:
            source_text = extract_pdf_text_pdf_parse(pdf_path, settings=self.settings)
        except Exception:
            source_text = None

        try:
            return self.reviewer.review_note(
                record,
                note_markdown=note_markdown,
                source_text=source_text,
            )
        except Exception:
            return build_empty_review_result()

    def _format_note(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        output_path: Path,
        progress_callback: Callable[[str, dict[str, object] | None], None] | None,
        backend_override: str | None = None,
    ):
        return self.note_formatter.format_note(
            record,
            pdf_path=pdf_path,
            output_path=output_path,
            proofread_markdown_path=None,
            backend_override=backend_override,
            progress_callback=progress_callback,
        )

    def _should_retry_with_ai_direct(self, review_result) -> bool:  # noqa: ANN001
        return (
            self.settings.pdf_transcription_backend == "mineru"
            and review_result.passed is False
        )

    def _retry_with_ai_direct(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        output_path: Path,
        progress_callback: Callable[[str, dict[str, object] | None], None] | None,
    ):
        try:
            note_result = self._format_note(
                record,
                pdf_path=pdf_path,
                output_path=output_path,
                progress_callback=progress_callback,
                backend_override="ai_direct",
            )
            review_result = self._review_transcribed_note(
                record,
                pdf_path=pdf_path,
                note_markdown=note_result.markdown,
            )
        except Exception:
            return None
        return note_result, review_result


def build_output_paths(settings: Settings, record: PaperRecord) -> OutputPaths:
    output_base = build_parsed_output_base(settings.parsed_output_dir, record)
    return OutputPaths(
        output_base=output_base,
        json_path=output_base.with_suffix(".json"),
        markdown_path=output_base.with_suffix(".md"),
        proofread_markdown_path=output_base.parent / f"{output_base.name}.proofread.md",
        proofread_json_path=output_base.parent / f"{output_base.name}.proofread.json",
    )


def build_empty_review_result():  # noqa: ANN201
    return type(
        "EmptyReviewResult",
        (),
        {
            "passed": None,
            "markdown_path": None,
            "json_path": None,
            "issue_count": 0,
            "severe_issue_count": 0,
            "derivation_direct_error_count": 0,
            "derivation_consistency_review_count": 0,
            "usage": None,
        },
    )()


def cleanup_review_artifacts(paths: OutputPaths) -> None:
    for path in (paths.proofread_markdown_path, paths.proofread_json_path):
        try:
            path.unlink(missing_ok=True)
        except Exception:
            continue


def build_parsed_payload(
    record: PaperRecord,
    *,
    note_content: str,
    generation_mode: str,
    page_count: int,
    note_usage: AIUsageMetrics | None = None,
    review_result=None,  # noqa: ANN001
) -> dict[str, object]:
    payload: dict[str, object] = {
        "doi": record.doi,
        "title": record.title,
        "source_title": record.source_title,
        "page_count": page_count,
        "generation_mode": generation_mode,
        "content_format": "markdown_transcription",
        "content": note_content,
    }
    if note_usage is not None:
        payload["usage"] = note_usage.to_dict()
    if review_result is not None:
        payload["review"] = {
            "passed": review_result.passed,
            "issue_count": review_result.issue_count,
            "severe_issue_count": review_result.severe_issue_count,
            "derivation_direct_error_count": review_result.derivation_direct_error_count,
            "derivation_consistency_review_count": (
                review_result.derivation_consistency_review_count
            ),
        }
    return payload


def write_parsed_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_parsed_content(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    content = payload.get("content")
    return content if isinstance(content, str) else ""


def get_pdf_page_count(pdf_path: Path) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception as exc:  # pragma: no cover - defensive fallback
        raise PDFParseError(f"Unable to read PDF page count: {pdf_path}") from exc
