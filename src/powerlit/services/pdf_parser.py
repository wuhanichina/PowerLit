from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path

from powerlit.models import PaperRecord
from powerlit.services.ai_pricing import AIUsageMetrics
from powerlit.services.library_layout import build_parsed_output_base
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
    markdown_path: Path | None = None
    proofread_markdown_path: Path | None = None
    proofread_json_path: Path | None = None
    directory_audit_path: Path | None = None
    note_usage: AIUsageMetrics | None = None
    review_usage: AIUsageMetrics | None = None
    note_generation_mode: str = "mineru_official_batch_api"
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
    note_generation_mode: str = "mineru_official_batch_api"
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
        emit_progress(
            progress_callback,
            "mineru_api_parse_started",
            {"pdf_path": str(pdf_path), "doi": record.doi},
        )
        try:
            from powerlit.services.mineru_official_api import (
                MineruOfficialAPIError,
                MineruOfficialBatchAPIService,
            )

            artifact = MineruOfficialBatchAPIService(self.settings).parse_single_record(
                record,
                pdf_path=pdf_path,
            )
        except MineruOfficialAPIError as exc:
            raise PDFParseError(str(exc)) from exc
        finally:
            emit_progress(
                progress_callback,
                "mineru_api_parse_finished",
                {"pdf_path": str(pdf_path), "doi": record.doi},
            )

        review_result = build_empty_review_result()
        return ParsedArtifacts(
            page_count=artifact.page_count,
            text=load_parsed_content(artifact.json_path),
            json_path=artifact.json_path,
            markdown_path=None,
            proofread_markdown_path=None,
            proofread_json_path=None,
            directory_audit_path=None,
            note_usage=None,
            review_usage=None,
            note_generation_mode=artifact.generation_mode,
            review_passed=review_result.passed,
            review_issue_count=review_result.issue_count,
            review_severe_issue_count=review_result.severe_issue_count,
            review_derivation_direct_error_count=review_result.derivation_direct_error_count,
            review_derivation_consistency_count=(
                review_result.derivation_consistency_review_count
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
        parsed = self.parse_record(
            record,
            pdf_path,
            progress_callback=progress_callback,
        )
        return TranscribedArtifacts(
            json_path=parsed.json_path,
            markdown_path=parsed.markdown_path,
            proofread_markdown_path=parsed.proofread_markdown_path,
            proofread_json_path=parsed.proofread_json_path,
            directory_audit_path=parsed.directory_audit_path,
            note_usage=parsed.note_usage,
            review_usage=parsed.review_usage,
            note_generation_mode=parsed.note_generation_mode,
            review_passed=parsed.review_passed,
            review_issue_count=parsed.review_issue_count,
            review_severe_issue_count=parsed.review_severe_issue_count,
            review_derivation_direct_error_count=parsed.review_derivation_direct_error_count,
            review_derivation_consistency_count=parsed.review_derivation_consistency_count,
        )

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
