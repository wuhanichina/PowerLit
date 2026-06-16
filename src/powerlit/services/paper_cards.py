from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powerlit.models import PaperAnalysis, PaperRecord
from powerlit.services.library_layout import build_card_output_base
from powerlit.settings import Settings

CARD_VERSION = 1


@dataclass(slots=True)
class PaperCardArtifacts:
    markdown_path: Path | None
    json_path: Path
    payload: dict[str, Any]


class PaperCardService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_card(self, record: PaperRecord) -> PaperCardArtifacts:
        payload = build_card_payload(record)
        output_base = build_card_output_base(self.settings.analysis_output_dir, record)
        output_base.parent.mkdir(parents=True, exist_ok=True)
        json_path = output_base.with_suffix(".json")

        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return PaperCardArtifacts(
            markdown_path=None,
            json_path=json_path,
            payload=payload,
        )


def build_card_payload(record: PaperRecord) -> dict[str, Any]:
    analysis = load_analysis_from_record(record)
    return {
        "card_version": CARD_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "record": {
            "dedupe_key": record.dedupe_key,
            "title": record.title,
            "doi": record.doi,
            "year": record.year,
            "document_type": record.document_type.value,
            "source_title": record.source_title,
            "publisher": record.publisher,
            "volume": record.volume,
            "issue": record.issue,
            "query_pack": record.query_pack,
        },
        "workflow": {
            "acquisition_method": (
                record.acquisition_method.value if record.acquisition_method else None
            ),
            "acquisition_stage": record.acquisition_stage.value,
            "download_status": record.download_status,
        },
        "artifact_paths": {
            "local_pdf_path": record.local_pdf_path,
            "parsed_json_path": record.parsed_json_path,
            "parsed_md_path": record.parsed_md_path,
            "analysis_md_path": record.analysis_md_path,
            "analysis_json_path": record.analysis_json_path,
        },
        "analysis": analysis.model_dump(mode="json"),
    }


def load_analysis_from_record(record: PaperRecord) -> PaperAnalysis:
    if record.analysis_json_path:
        path = Path(record.analysis_json_path)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            analysis_payload = payload.get("analysis")
            if isinstance(analysis_payload, dict):
                return PaperAnalysis.model_validate(analysis_payload)

    return PaperAnalysis.model_validate(
        {
            "title": record.title,
            "source_basis": infer_source_basis(record),
            "research_problem": "unknown",
            "power_system_context": "unknown",
            "methods": ["unknown"],
            "data_and_case_studies": ["unknown"],
            "key_findings": ["unknown"],
            "limitations": ["unknown"],
            "relevance": "unknown",
            "keywords": ["unknown"],
            "evidence_items": [{"claim": "unknown", "support": "unknown"}],
            "caution": "unknown",
        }
    )


def infer_source_basis(record: PaperRecord) -> str:
    if record.analysis_json_path:
        return "analysis_json"
    if record.parsed_json_path:
        return "parsed_json"
    if record.parsed_md_path:
        return "parsed_markdown"
    if record.abstract:
        return "metadata+abstract"
    return "metadata_only"


def render_card_markdown(payload: dict[str, Any]) -> str:
    record = payload["record"]
    workflow = payload["workflow"]
    artifacts = payload["artifact_paths"]
    analysis = payload["analysis"]

    lines = [
        f"# {record['title']}",
        "",
        "## Metadata",
        "",
        f"- DOI: {record.get('doi') or 'unknown'}",
        f"- Year: {record.get('year') or 'unknown'}",
        f"- Type: {record.get('document_type') or 'unknown'}",
        f"- Source: {record.get('source_title') or 'unknown'}",
        f"- Publisher: {record.get('publisher') or 'unknown'}",
        f"- Volume / Issue: {format_volume_issue(record.get('volume'), record.get('issue'))}",
        f"- Query Pack: {record.get('query_pack') or 'unknown'}",
        f"- Dedupe Key: {record.get('dedupe_key') or 'unknown'}",
        "",
        "## Workflow",
        "",
        f"- Acquisition Method: {workflow.get('acquisition_method') or 'unknown'}",
        f"- Acquisition Stage: {workflow.get('acquisition_stage') or 'unknown'}",
        f"- Download Status: {workflow.get('download_status') or 'unknown'}",
        f"- Card Version: {payload.get('card_version') or CARD_VERSION}",
        f"- Generated At: {payload.get('generated_at') or 'unknown'}",
        "",
        "## Research Problem",
        "",
        analysis.get("research_problem") or "unknown",
        "",
        "## Power System Context",
        "",
        analysis.get("power_system_context") or "unknown",
        "",
        "## Methods",
        "",
    ]
    lines.extend(render_bullet_list(analysis.get("methods")))
    lines.extend(["", "## Data And Case Studies", ""])
    lines.extend(render_bullet_list(analysis.get("data_and_case_studies")))
    lines.extend(["", "## Key Findings", ""])
    lines.extend(render_bullet_list(analysis.get("key_findings")))
    lines.extend(["", "## Limitations", ""])
    lines.extend(render_bullet_list(analysis.get("limitations")))
    lines.extend(["", "## Relevance", "", analysis.get("relevance") or "unknown", "", "## Keywords", ""])
    lines.extend(render_bullet_list(analysis.get("keywords")))
    lines.extend(["", "## Evidence", ""])
    for item in analysis.get("evidence_items") or [{"claim": "unknown", "support": "unknown"}]:
        lines.append(f"- Claim: {item.get('claim') or 'unknown'}")
        lines.append(f"  Support: {item.get('support') or 'unknown'}")
    lines.extend(["", "## Caution", "", analysis.get("caution") or "unknown", "", "## Files", ""])
    lines.extend(render_file_links(artifacts))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_bullet_list(items: Any) -> list[str]:
    if not isinstance(items, list) or not items:
        return ["- unknown"]
    values = [str(item).strip() for item in items if str(item).strip()]
    return [f"- {item}" for item in values] or ["- unknown"]


def render_file_links(artifacts: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for label, key in (
        ("PDF", "local_pdf_path"),
        ("Parsed Markdown", "parsed_md_path"),
        ("Analysis Markdown", "analysis_md_path"),
        ("Analysis JSON", "analysis_json_path"),
    ):
        lines.append(f"- {label}: {artifacts.get(key) or 'missing'}")
    return lines


def format_volume_issue(volume: Any, issue: Any) -> str:
    volume_text = str(volume).strip() if volume else ""
    issue_text = str(issue).strip() if issue else ""
    if volume_text and issue_text:
        return f"{volume_text} / {issue_text}"
    if volume_text:
        return volume_text
    if issue_text:
        return issue_text
    return "unknown"
