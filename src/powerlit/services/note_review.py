# ruff: noqa: E501

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from re import match
from typing import Any

from powerlit.models import PaperRecord
from powerlit.services.ai_analysis import (
    AIServiceError,
    OpenAICompatibleAIClient,
    parse_json_object,
)
from powerlit.services.ai_config import resolve_ai_profile
from powerlit.services.ai_pricing import AIUsageMetrics
from powerlit.services.export import write_markdown
from powerlit.settings import Settings

FILE_LINKS_HEADING = "## 文件链接"
STANDARD_REVIEW_LEVEL = "standard"
SOURCE_DERIVATION_CATEGORY = "source_derivation"
SOURCE_DERIVATION_DIRECT_ERROR_LEVEL = "direct_error_detection"
SOURCE_DERIVATION_CONSISTENCY_LEVEL = "consistency_review"
SOURCE_DERIVATION_LEVEL_LABELS = {
    SOURCE_DERIVATION_DIRECT_ERROR_LEVEL: "仅识别错误",
    SOURCE_DERIVATION_CONSISTENCY_LEVEL: "推导自洽性复核",
}


@dataclass(slots=True)
class NoteReviewIssue:
    category: str
    review_level: str
    severity: str
    title: str
    evidence: str
    explanation: str
    suggestion: str
    source: str
    is_source_paper_issue: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "review_level": self.review_level,
            "severity": self.severity,
            "title": self.title,
            "evidence": self.evidence,
            "explanation": self.explanation,
            "suggestion": self.suggestion,
            "source": self.source,
            "is_source_paper_issue": self.is_source_paper_issue,
        }


@dataclass(slots=True)
class ReviewedChunk:
    page_range: str
    issues: list[NoteReviewIssue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_range": self.page_range,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(slots=True)
class ReviewSegment:
    page_range: str
    raw_text: str
    note_text: str


@dataclass(slots=True)
class NoteReviewArtifacts:
    passed: bool | None
    issue_count: int
    severe_issue_count: int
    markdown_path: Path | None = None
    json_path: Path | None = None
    derivation_direct_error_count: int = 0
    derivation_consistency_review_count: int = 0
    usage: AIUsageMetrics | None = None


class NoteQualityReviewService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile = resolve_ai_profile(settings, "review")
        self.client = OpenAICompatibleAIClient(settings, profile=self.profile)

    def review_note(
        self,
        record: PaperRecord,
        *,
        note_markdown: str,
        markdown_path: Path | None = None,
        json_path: Path | None = None,
        source_text: str | None = None,
    ) -> NoteReviewArtifacts:
        reviewed_chunks: list[ReviewedChunk] = []
        usage_items: list[AIUsageMetrics] = []
        global_issues = collect_global_format_issues(note_markdown)

        for segment in build_review_segments(
            source_text=source_text,
            note_markdown=note_markdown,
            settings=self.settings,
        ):
            issues = collect_chunk_heuristic_issues(
                page_range=segment.page_range,
                raw_text=segment.raw_text,
                note_text=segment.note_text,
            )
            ai_issues, usage = self._review_chunk_with_ai(
                record,
                page_range=segment.page_range,
                raw_text=segment.raw_text,
                note_text=segment.note_text,
            )
            issues.extend(ai_issues)
            if issues:
                reviewed_chunks.append(
                    ReviewedChunk(
                        page_range=segment.page_range,
                        issues=issues,
                    )
                )
            if usage is not None:
                usage_items.append(usage)

        payload = build_review_payload(
            record=record,
            note_markdown_path=markdown_path,
            note_json_path=json_path,
            reviewed_chunks=reviewed_chunks,
            global_issues=global_issues,
            usage=merge_usage_metrics(usage_items),
            settings=self.settings,
        )
        if markdown_path is not None and json_path is not None:
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            write_markdown(markdown_path, render_review_markdown(payload))
        severe_issue_count = sum(
            1
            for issue in payload["global_issues"] + payload["issues"]
            if issue["severity"] == "error"
        )
        derivation_counts = payload["source_derivation_review_counts"]
        return NoteReviewArtifacts(
            passed=(len(payload["global_issues"]) + len(payload["issues"])) == 0,
            markdown_path=markdown_path,
            json_path=json_path,
            issue_count=len(payload["global_issues"]) + len(payload["issues"]),
            severe_issue_count=severe_issue_count,
            derivation_direct_error_count=derivation_counts[
                SOURCE_DERIVATION_DIRECT_ERROR_LEVEL
            ],
            derivation_consistency_review_count=derivation_counts[
                SOURCE_DERIVATION_CONSISTENCY_LEVEL
            ],
            usage=merge_usage_metrics(usage_items),
        )

    def _review_chunk_with_ai(
        self,
        record: PaperRecord,
        *,
        page_range: str,
        raw_text: str,
        note_text: str,
    ) -> tuple[list[NoteReviewIssue], AIUsageMetrics | None]:
        if not self.settings.note_review_enabled or not self.profile.api_key:
            return [], None
        if not note_text.strip():
            return [], None

        try:
            result = self.client.chat_text(
                build_review_messages(record, page_range=page_range, raw_text=raw_text, note_text=note_text),
                timeout=self.profile.effective_note_timeout,
            )
            payload = parse_json_object(result.text)
        except AIServiceError:
            return [], None
        return normalize_ai_review_issues(payload.get("issues")), result.usage


def extract_note_chunk_map(note_markdown: str) -> dict[str, str]:
    content = strip_frontmatter(note_markdown)
    if FILE_LINKS_HEADING in content:
        content = content.split(FILE_LINKS_HEADING, 1)[0].rstrip()

    mapping: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in content.splitlines():
        if line.startswith("### "):
            current_key = normalize_chunk_heading(line.removeprefix("### ").strip())
            mapping.setdefault(current_key, [])
            continue
        if current_key is None:
            continue
        mapping[current_key].append(line)
    return {
        key: "\n".join(lines).strip()
        for key, lines in mapping.items()
    }


def build_review_segments(
    *,
    source_text: str | None,
    note_markdown: str,
    settings: Settings,
) -> list[ReviewSegment]:
    del settings
    source_body = (source_text or "").strip()
    note_body = strip_frontmatter(note_markdown).strip()
    if not source_body or not note_body:
        return []
    return [
        ReviewSegment(
            page_range="full note",
            raw_text=source_body,
            note_text=note_body,
        )
    ]


def strip_frontmatter(note_markdown: str) -> str:
    lines = note_markdown.splitlines()
    if not lines or lines[0] != "---":
        return note_markdown
    for index in range(1, len(lines)):
        if lines[index] == "---":
            return "\n".join(lines[index + 1 :]).lstrip()
    return note_markdown
def collect_global_format_issues(note_markdown: str) -> list[NoteReviewIssue]:
    issues: list[NoteReviewIssue] = []
    if note_markdown.count("```") % 2 != 0:
        issues.append(
            NoteReviewIssue(
                category="format",
                review_level=STANDARD_REVIEW_LEVEL,
                severity="error",
                title="Unclosed fenced code block",
                evidence="The markdown contains an odd number of ``` fences.",
                explanation="A code fence is not closed, which can break the rendered note structure.",
                suggestion="Close or remove the unmatched fenced code block.",
                source="heuristic",
            )
        )
    issues.extend(check_math_delimiters(note_markdown))
    return issues


def check_math_delimiters(note_markdown: str) -> list[NoteReviewIssue]:
    issues: list[NoteReviewIssue] = []
    delimiter_pairs = [
        ("$$", "$$", "display math fence"),
        ("\\[", "\\]", "display math bracket"),
        ("\\(", "\\)", "inline math bracket"),
    ]
    for left, right, label in delimiter_pairs:
        left_count = note_markdown.count(left)
        right_count = note_markdown.count(right)
        if left == right:
            if left_count % 2 != 0:
                issues.append(
                    NoteReviewIssue(
                        category="formula_format",
                        review_level=STANDARD_REVIEW_LEVEL,
                        severity="error",
                        title=f"Unbalanced {label}",
                        evidence=f"{label} count is odd: {left_count}.",
                        explanation="Math delimiters are unbalanced, so formulas may render incorrectly.",
                        suggestion="Check the surrounding formula block and close the delimiter pair.",
                        source="heuristic",
                    )
                )
            continue
        if left_count != right_count:
            issues.append(
                NoteReviewIssue(
                    category="formula_format",
                    review_level=STANDARD_REVIEW_LEVEL,
                    severity="error",
                    title=f"Unbalanced {label}",
                    evidence=f"Left={left_count}, Right={right_count}.",
                    explanation="Math delimiters are mismatched, so formulas may render incorrectly.",
                    suggestion="Check the surrounding formula block and repair the delimiter pair.",
                    source="heuristic",
                )
            )
    return issues


def collect_chunk_heuristic_issues(
    *,
    page_range: str,
    raw_text: str,
    note_text: str,
) -> list[NoteReviewIssue]:
    issues: list[NoteReviewIssue] = []
    if not note_text.strip():
        issues.append(
            NoteReviewIssue(
                category="format",
                review_level=STANDARD_REVIEW_LEVEL,
                severity="error",
                title="Missing chunk section",
                evidence=f"No note section was found for {page_range}.",
                explanation="The generated markdown does not contain a matching chunk body.",
                suggestion="Regenerate the note chunk and make sure the heading is preserved.",
                source="heuristic",
            )
        )
        return issues

    raw_compact = compact_text(raw_text)
    note_compact = compact_text(note_text)
    if raw_compact and note_compact and len(note_compact) / len(raw_compact) < 0.20:
        issues.append(
            NoteReviewIssue(
                category="faithful_transcription",
                review_level=STANDARD_REVIEW_LEVEL,
                severity="warning",
                title="Chunk looks too short",
                evidence=f"{page_range}: note/source compact length ratio < 0.20.",
                explanation="The translated chunk may still be compressed instead of preserving full content.",
                suggestion="Inspect the chunk manually and consider regenerating it with a smaller chunk size.",
                source="heuristic",
            )
        )

    raw_formula_count = count_formula_like_lines(raw_text)
    note_formula_count = count_formula_like_lines(note_text)
    if raw_formula_count >= 2 and note_formula_count == 0:
        issues.append(
            NoteReviewIssue(
                category="formula_transcription",
                review_level=STANDARD_REVIEW_LEVEL,
                severity="warning",
                title="Formula coverage dropped",
                evidence=f"{page_range}: raw chunk has {raw_formula_count} formula-like lines, note has 0.",
                explanation="Formulas may have been omitted or rewritten into prose during note generation.",
                suggestion="Compare the chunk with the raw extraction and restore the missing formulas.",
                source="heuristic",
            )
        )
    return issues


def build_review_messages(
    record: PaperRecord,
    *,
    page_range: str,
    raw_text: str,
    note_text: str,
) -> list[dict[str, str]]:
    metadata = {
        "title": record.title,
        "doi": record.doi or "unknown",
        "source_title": record.source_title or "unknown",
        "page_range": page_range,
    }
    return [
        {
            "role": "system",
            "content": (
                "You audit a machine-generated scholarly markdown note against the source extraction. "
                "Be conservative and evidence-based. Check five categories only: "
                "format, ocr_recognition, faithful_transcription, formula_transcription, "
                "source_derivation. "
                "For faithful_transcription, check whether the note preserves paragraph-level order, "
                "coverage, and wording fidelity instead of summarizing or omitting content. "
                "For every issue, also set review_level. Use review_level=standard for non-derivation issues. "
                "For category=source_derivation, use one of two levels only: "
                "direct_error_detection when the source text itself explicitly shows a mathematical or logical error; "
                "consistency_review when the derivation is not obviously false but its self-consistency or missing "
                "steps require manual verification. "
                "Return one JSON object with key 'issues'. "
                "Each issue must include: category, review_level, severity, title, evidence, explanation, suggestion, "
                "is_source_paper_issue. "
                "Write issue explanations in zh-CN. "
                "If there is no issue, return {\"issues\": []}."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "metadata": metadata,
                    "raw_chunk": raw_text,
                    "note_chunk": note_text,
                },
                ensure_ascii=False,
                indent=2,
            ),
        },
    ]


def normalize_ai_review_issues(value: Any) -> list[NoteReviewIssue]:
    if not isinstance(value, list):
        return []
    issues: list[NoteReviewIssue] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        category, review_level = normalize_category_and_review_level(
            item.get("category"),
            item.get("review_level"),
        )
        issues.append(
            NoteReviewIssue(
                category=category,
                review_level=review_level,
                severity=str(item.get("severity") or "warning"),
                title=str(item.get("title") or "Untitled issue"),
                evidence=str(item.get("evidence") or "unknown"),
                explanation=str(item.get("explanation") or "unknown"),
                suggestion=str(item.get("suggestion") or "unknown"),
                source="ai_review",
                is_source_paper_issue=bool(item.get("is_source_paper_issue")),
            )
        )
    return issues


def build_review_payload(
    *,
    record: PaperRecord,
    note_markdown_path: Path | None,
    note_json_path: Path | None,
    reviewed_chunks: list[ReviewedChunk],
    global_issues: list[NoteReviewIssue],
    usage: AIUsageMetrics | None,
    settings: Settings,
) -> dict[str, Any]:
    flat_issues = [issue.to_dict() for chunk in reviewed_chunks for issue in chunk.issues]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "title": record.title,
        "doi": record.doi,
        "source_title": record.source_title,
        "note_markdown_path": workspace_path(settings, note_markdown_path),
        "note_review_json_path": workspace_path(settings, note_json_path),
        "global_issues": [issue.to_dict() for issue in global_issues],
        "issues": flat_issues,
        "chunk_reviews": [chunk.to_dict() for chunk in reviewed_chunks],
        "issue_count": len(flat_issues) + len(global_issues),
        "severe_issue_count": sum(
            1
            for issue in [*global_issues, *(item for chunk in reviewed_chunks for item in chunk.issues)]
            if issue.severity == "error"
        ),
        "source_derivation_review_counts": build_source_derivation_review_counts(
            [*global_issues, *(item for chunk in reviewed_chunks for item in chunk.issues)]
        ),
        "usage": usage.to_dict() if usage else None,
    }


def render_review_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Note Proofread Report",
        "",
        f"- Title: {payload['title']}",
        f"- DOI: {payload['doi'] or 'unknown'}",
        f"- Source Title: {payload['source_title'] or 'unknown'}",
        f"- Note: {payload['note_markdown_path']}",
        f"- Total Issues: {payload['issue_count']}",
        f"- Severe Issues: {payload['severe_issue_count']}",
        (
            "- Source Derivation Direct Errors: "
            f"{payload['source_derivation_review_counts'][SOURCE_DERIVATION_DIRECT_ERROR_LEVEL]}"
        ),
        (
            "- Source Derivation Consistency Reviews: "
            f"{payload['source_derivation_review_counts'][SOURCE_DERIVATION_CONSISTENCY_LEVEL]}"
        ),
        f"- Generated At: {payload['generated_at']}",
        "",
    ]
    if payload["global_issues"]:
        lines.extend(["## Global Issues", ""])
        for issue in payload["global_issues"]:
            lines.extend(render_issue_lines(issue))
    if payload["issues"]:
        lines.extend(["## Chunk Issues", ""])
        for chunk in payload["chunk_reviews"]:
            if not chunk["issues"]:
                continue
            lines.append(f"### {chunk['page_range']}")
            lines.append("")
            for issue in chunk["issues"]:
                lines.extend(render_issue_lines(issue))
    if not payload["global_issues"] and not payload["issues"]:
        lines.extend(
            [
                "## Result",
                "",
                "No issue was detected by the current heuristic and AI review pipeline.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_issue_lines(issue: dict[str, Any]) -> list[str]:
    lines = [
        f"- [{issue['severity']}] {issue['category']}: {issue['title']}",
        f"  Evidence: {issue['evidence']}",
        f"  Explanation: {issue['explanation']}",
        f"  Suggestion: {issue['suggestion']}",
    ]
    if issue.get("review_level") and issue["review_level"] != STANDARD_REVIEW_LEVEL:
        lines.append(f"  Review Level: {render_review_level_label(issue['review_level'])}")
    if issue.get("is_source_paper_issue"):
        lines.append("  Scope: source-paper derivation risk")
    return lines + [""]


def normalize_category_and_review_level(
    raw_category: Any,
    raw_review_level: Any,
) -> tuple[str, str]:
    category = str(raw_category or "unknown").strip() or "unknown"
    review_level = normalize_review_level(raw_review_level)
    if category in {"source_derivation_direct_error", "source_derivation_error"}:
        return SOURCE_DERIVATION_CATEGORY, SOURCE_DERIVATION_DIRECT_ERROR_LEVEL
    if category in {
        "source_derivation_consistency_review",
        "source_derivation_consistency",
    }:
        return SOURCE_DERIVATION_CATEGORY, SOURCE_DERIVATION_CONSISTENCY_LEVEL
    if category == SOURCE_DERIVATION_CATEGORY:
        if review_level == STANDARD_REVIEW_LEVEL:
            review_level = SOURCE_DERIVATION_DIRECT_ERROR_LEVEL
        return category, review_level
    return category, STANDARD_REVIEW_LEVEL


def normalize_review_level(value: Any) -> str:
    normalized = str(value or STANDARD_REVIEW_LEVEL).strip().lower()
    aliases = {
        STANDARD_REVIEW_LEVEL: STANDARD_REVIEW_LEVEL,
        "none": STANDARD_REVIEW_LEVEL,
        "normal": STANDARD_REVIEW_LEVEL,
        SOURCE_DERIVATION_DIRECT_ERROR_LEVEL: SOURCE_DERIVATION_DIRECT_ERROR_LEVEL,
        "direct_error": SOURCE_DERIVATION_DIRECT_ERROR_LEVEL,
        "error_identification": SOURCE_DERIVATION_DIRECT_ERROR_LEVEL,
        "only_identify_errors": SOURCE_DERIVATION_DIRECT_ERROR_LEVEL,
        "仅识别错误": SOURCE_DERIVATION_DIRECT_ERROR_LEVEL,
        SOURCE_DERIVATION_CONSISTENCY_LEVEL: SOURCE_DERIVATION_CONSISTENCY_LEVEL,
        "consistency_check": SOURCE_DERIVATION_CONSISTENCY_LEVEL,
        "self_consistency_review": SOURCE_DERIVATION_CONSISTENCY_LEVEL,
        "推导自洽性复核": SOURCE_DERIVATION_CONSISTENCY_LEVEL,
    }
    return aliases.get(normalized, STANDARD_REVIEW_LEVEL)


def build_source_derivation_review_counts(
    issues: list[NoteReviewIssue],
) -> dict[str, int]:
    counts = {
        SOURCE_DERIVATION_DIRECT_ERROR_LEVEL: 0,
        SOURCE_DERIVATION_CONSISTENCY_LEVEL: 0,
    }
    for issue in issues:
        if issue.category != SOURCE_DERIVATION_CATEGORY:
            continue
        if issue.review_level not in counts:
            continue
        counts[issue.review_level] += 1
    return counts


def render_review_level_label(review_level: str) -> str:
    if review_level in SOURCE_DERIVATION_LEVEL_LABELS:
        return SOURCE_DERIVATION_LEVEL_LABELS[review_level]
    return review_level


def count_formula_like_lines(value: str) -> int:
    count = 0
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if len(line) < 3:
            continue
        if match(r"^\(\d+\)", line):
            count += 1
            continue
        if any(token in line for token in ("=", "\\sum", "\\int", "\\max", "\\min", "^", "_")):
            count += 1
    return count


def compact_text(value: str) -> str:
    return "".join(char for char in value if not char.isspace())


def workspace_path(settings: Settings, path: Path | None) -> str:
    if path is None:
        return "not_written"
    root = settings.literature_root.parent.resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())


def merge_usage_metrics(items: list[AIUsageMetrics]) -> AIUsageMetrics | None:
    if not items:
        return None
    first = items[0]
    estimated_cost = None
    if all(item.estimated_cost is not None for item in items):
        estimated_cost = sum(float(item.estimated_cost) for item in items)
    return AIUsageMetrics(
        prompt_tokens=sum(item.prompt_tokens for item in items),
        completion_tokens=sum(item.completion_tokens for item in items),
        total_tokens=sum(item.total_tokens for item in items),
        currency=first.currency,
        input_price_per_mtokens=first.input_price_per_mtokens,
        output_price_per_mtokens=first.output_price_per_mtokens,
        estimated_cost=estimated_cost,
    )
