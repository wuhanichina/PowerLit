from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powerlit.services.export import write_markdown
from powerlit.settings import Settings

DIRECTORY_AUDIT_FILENAME = "_directory_audit.md"
PROOFREAD_MARKDOWN_SUFFIX = ".proofread.md"
PROOFREAD_JSON_SUFFIX = ".proofread.json"
ANALYSIS_MARKDOWN_SUFFIX = "-analysis.md"
NOTE_SUFFIX = ".md"


@dataclass(slots=True)
class DirectoryAuditEntry:
    stem: str
    note_path: Path | None
    proofread_markdown_path: Path | None
    proofread_json_path: Path | None
    analysis_markdown_path: Path | None
    issue_count: int
    severe_issue_count: int
    derivation_direct_error_count: int
    derivation_consistency_review_count: int


class DirectoryAuditService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def update_directory_summary(self, directory: Path) -> Path:
        entries = collect_directory_entries(directory)
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "directory": workspace_path(self.settings, directory),
            "paper_count": len(entries),
            "issue_count": sum(entry.issue_count for entry in entries),
            "severe_issue_count": sum(entry.severe_issue_count for entry in entries),
            "source_derivation_direct_error_count": sum(
                entry.derivation_direct_error_count for entry in entries
            ),
            "source_derivation_consistency_review_count": sum(
                entry.derivation_consistency_review_count for entry in entries
            ),
            "entries": [entry_to_payload(self.settings, entry) for entry in entries],
        }
        output_path = directory / DIRECTORY_AUDIT_FILENAME
        write_markdown(output_path, render_directory_audit_markdown(payload))
        return output_path


def collect_directory_entries(directory: Path) -> list[DirectoryAuditEntry]:
    if not directory.exists():
        return []
    entries: list[DirectoryAuditEntry] = []
    for stem in collect_entry_stems(directory):
        proofread_json_path = directory / f"{stem}.proofread.json"
        proofread_payload = load_json_file(proofread_json_path)
        derivation_counts = proofread_payload.get("source_derivation_review_counts") or {}
        entries.append(
            DirectoryAuditEntry(
                stem=stem,
                note_path=first_existing_path(directory / f"{stem}.md"),
                proofread_markdown_path=first_existing_path(directory / f"{stem}.proofread.md"),
                proofread_json_path=first_existing_path(proofread_json_path),
                analysis_markdown_path=first_existing_path(directory / f"{stem}-analysis.md"),
                issue_count=int(proofread_payload.get("issue_count") or 0),
                severe_issue_count=int(proofread_payload.get("severe_issue_count") or 0),
                derivation_direct_error_count=int(
                    derivation_counts.get("direct_error_detection") or 0
                ),
                derivation_consistency_review_count=int(
                    derivation_counts.get("consistency_review") or 0
                ),
            )
        )
    return entries


def collect_entry_stems(directory: Path) -> list[str]:
    stems: set[str] = set()
    for path in directory.iterdir():
        if not path.is_file() or path.name == DIRECTORY_AUDIT_FILENAME:
            continue
        stem = stem_from_artifact_name(path.name)
        if stem is not None:
            stems.add(stem)
    return sorted(stems)


def stem_from_artifact_name(name: str) -> str | None:
    suffix_map = (
        (PROOFREAD_JSON_SUFFIX, PROOFREAD_JSON_SUFFIX),
        (PROOFREAD_MARKDOWN_SUFFIX, PROOFREAD_MARKDOWN_SUFFIX),
        (ANALYSIS_MARKDOWN_SUFFIX, ANALYSIS_MARKDOWN_SUFFIX),
        (NOTE_SUFFIX, NOTE_SUFFIX),
    )
    for marker, trimmed_suffix in suffix_map:
        if not name.endswith(marker):
            continue
        stem = name.removesuffix(trimmed_suffix)
        if stem:
            return stem
    return None


def first_existing_path(path: Path) -> Path | None:
    if path.exists():
        return path
    return None


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def entry_to_payload(settings: Settings, entry: DirectoryAuditEntry) -> dict[str, Any]:
    return {
        "stem": entry.stem,
        "note_path": workspace_path(settings, entry.note_path),
        "proofread_markdown_path": workspace_path(settings, entry.proofread_markdown_path),
        "proofread_json_path": workspace_path(settings, entry.proofread_json_path),
        "analysis_markdown_path": workspace_path(settings, entry.analysis_markdown_path),
        "issue_count": entry.issue_count,
        "severe_issue_count": entry.severe_issue_count,
        "derivation_direct_error_count": entry.derivation_direct_error_count,
        "derivation_consistency_review_count": entry.derivation_consistency_review_count,
    }


def render_directory_audit_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Directory Audit",
        "",
        f"- Directory: {payload['directory']}",
        f"- Papers: {payload['paper_count']}",
        f"- Total Issues: {payload['issue_count']}",
        f"- Severe Issues: {payload['severe_issue_count']}",
        (
            "- Source Derivation Direct Errors: "
            f"{payload['source_derivation_direct_error_count']}"
        ),
        (
            "- Source Derivation Consistency Reviews: "
            f"{payload['source_derivation_consistency_review_count']}"
        ),
        f"- Generated At: {payload['generated_at']}",
        "",
    ]
    if not payload["entries"]:
        lines.extend(
            [
                "## Entries",
                "",
                "No note or analysis artifacts were found in this directory.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(["## Entries", ""])
    for entry in payload["entries"]:
        status = resolve_entry_status(entry)
        lines.extend(
            [
                f"### {entry['stem']}",
                "",
                f"- Status: {status}",
                f"- Note: {render_link(entry['note_path'])}",
                f"- Proofread Report: {render_link(entry['proofread_markdown_path'])}",
                f"- Proofread JSON: {render_link(entry['proofread_json_path'])}",
                f"- Analysis: {render_link(entry['analysis_markdown_path'])}",
                f"- Issue Count: {entry['issue_count']}",
                f"- Severe Issue Count: {entry['severe_issue_count']}",
                (
                    "- Source Derivation Direct Errors: "
                    f"{entry['derivation_direct_error_count']}"
                ),
                (
                    "- Source Derivation Consistency Reviews: "
                    f"{entry['derivation_consistency_review_count']}"
                ),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_link(path_value: str | None) -> str:
    if not path_value:
        return "missing"
    return f"[[{path_value}]]"


def resolve_entry_status(entry: dict[str, Any]) -> str:
    note_ready = bool(entry["note_path"])
    proofread_ready = bool(entry["proofread_markdown_path"])
    if note_ready and not proofread_ready:
        return "note-without-proofread"
    if entry["severe_issue_count"] > 0:
        return "severe-issues"
    if entry["issue_count"] > 0:
        return "issues"
    if note_ready:
        return "ok"
    return "incomplete"


def workspace_path(settings: Settings, path: Path | None) -> str | None:
    if path is None:
        return None
    root = settings.literature_root.parent.resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())
