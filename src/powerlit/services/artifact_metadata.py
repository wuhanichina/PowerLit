from __future__ import annotations

from typing import Any


ARTIFACT_STATUS_FIELDS = (
    "local_pdf_path",
    "parsed_json_path",
    "parsed_md_path",
    "analysis_json_path",
    "analysis_md_path",
    "paper_card_json_path",
    "paper_card_md_path",
)


def enrich_paper_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return UI-safe artifact metadata without token or cost accounting."""
    enriched = dict(row)
    for field in ARTIFACT_STATUS_FIELDS:
        enriched[f"has_{field.removesuffix('_path')}"] = bool(enriched.get(field))
    return enriched
