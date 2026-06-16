from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from powerlit.models import PaperRecord
from powerlit.services.directory_audit import DirectoryAuditService
from powerlit.services.note_review import NoteQualityReviewService, NoteReviewArtifacts
from powerlit.services.pdf_parser import (
    PDFParseError,
    PDFParserService,
    build_output_paths,
)
from powerlit.settings import Settings


@dataclass(slots=True)
class ExistingNoteAuditArtifacts:
    note_path: Path | None
    proofread_markdown_path: Path
    proofread_json_path: Path
    parsed_json_path: Path | None = None
    directory_audit_path: Path | None = None
    initial_issue_count: int = 0
    initial_severe_issue_count: int = 0
    final_issue_count: int = 0
    final_severe_issue_count: int = 0
    final_direct_error_count: int = 0
    final_consistency_review_count: int = 0
    retranscribed: bool = False
    note_generation_mode: str = "existing-note-audit"


class ExistingNoteAuditService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.parser = PDFParserService(settings)
        self.reviewer = NoteQualityReviewService(settings)
        self.directory_audit = DirectoryAuditService(settings)

    def audit_record(
        self,
        record: PaperRecord,
        *,
        auto_retranscribe: bool = True,
        force_retranscribe: bool = False,
    ) -> ExistingNoteAuditArtifacts:
        paths = build_output_paths(self.settings, record)
        note_path = Path(record.parsed_md_path) if record.parsed_md_path else paths.markdown_path

        initial_review: NoteReviewArtifacts | None = None
        note_exists = note_path.exists()
        if note_exists:
            initial_review = self.reviewer.review_note(
                record,
                note_markdown=note_path.read_text(encoding="utf-8"),
                markdown_path=paths.proofread_markdown_path,
                json_path=paths.proofread_json_path,
            )

        should_retranscribe = auto_retranscribe and (
            force_retranscribe
            or not note_exists
            or (initial_review is not None and initial_review.issue_count > 0)
        )
        if should_retranscribe:
            pdf_path = resolve_existing_pdf_path(record.local_pdf_path)
            if pdf_path is None or not pdf_path.exists():
                raise PDFParseError(
                    "Cannot retranscribe: local PDF path is missing or the file does not exist."
                )
            transcribed = self.parser.transcribe_record(record, pdf_path=pdf_path)
            return ExistingNoteAuditArtifacts(
                note_path=transcribed.markdown_path,
                parsed_json_path=transcribed.json_path,
                proofread_markdown_path=transcribed.proofread_markdown_path
                or paths.proofread_markdown_path,
                proofread_json_path=transcribed.proofread_json_path or paths.proofread_json_path,
                directory_audit_path=transcribed.directory_audit_path,
                initial_issue_count=initial_review.issue_count if initial_review else 0,
                initial_severe_issue_count=(
                    initial_review.severe_issue_count if initial_review else 0
                ),
                final_issue_count=transcribed.review_issue_count,
                final_severe_issue_count=transcribed.review_severe_issue_count,
                final_direct_error_count=transcribed.review_derivation_direct_error_count,
                final_consistency_review_count=transcribed.review_derivation_consistency_count,
                retranscribed=True,
                note_generation_mode=transcribed.note_generation_mode,
            )

        if not note_exists:
            raise PDFParseError(
                "No existing markdown note was found and auto retranscription is disabled."
            )

        directory_audit_path = self.directory_audit.update_directory_summary(
            paths.output_base.parent
        )
        return ExistingNoteAuditArtifacts(
            note_path=note_path,
            proofread_markdown_path=paths.proofread_markdown_path,
            proofread_json_path=paths.proofread_json_path,
            directory_audit_path=directory_audit_path,
            initial_issue_count=initial_review.issue_count if initial_review else 0,
            initial_severe_issue_count=initial_review.severe_issue_count if initial_review else 0,
            final_issue_count=initial_review.issue_count if initial_review else 0,
            final_severe_issue_count=initial_review.severe_issue_count if initial_review else 0,
            final_direct_error_count=(
                initial_review.derivation_direct_error_count if initial_review else 0
            ),
            final_consistency_review_count=(
                initial_review.derivation_consistency_review_count if initial_review else 0
            ),
            retranscribed=False,
        )


def resolve_existing_pdf_path(local_pdf_path: str | None) -> Path | None:
    if not local_pdf_path:
        return None
    path = Path(local_pdf_path)
    if path.exists():
        return path
    return None
