from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def enrich_paper_row(row: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(row)
    parsed_meta = load_parsed_note_metadata(row.get("parsed_json_path"), row.get("parsed_md_path"))
    analysis_meta = load_analysis_metadata(row.get("analysis_json_path"))

    enriched["parsed_note_cost"] = parsed_meta.get("note_cost")
    enriched["parsed_note_currency"] = parsed_meta.get("note_currency")
    enriched["parsed_note_generation_mode"] = parsed_meta.get("generation_mode")
    enriched["parsed_note_prompt_tokens"] = parsed_meta.get("note_prompt_tokens")
    enriched["parsed_note_completion_tokens"] = parsed_meta.get("note_completion_tokens")
    enriched["parsed_note_total_tokens"] = parsed_meta.get("note_total_tokens")

    enriched["analysis_cost"] = analysis_meta.get("estimated_cost")
    enriched["analysis_currency"] = analysis_meta.get("currency")
    enriched["analysis_prompt_tokens"] = analysis_meta.get("prompt_tokens")
    enriched["analysis_completion_tokens"] = analysis_meta.get("completion_tokens")
    enriched["analysis_total_tokens"] = analysis_meta.get("total_tokens")

    total_cost = sum_known_costs(
        [
            parsed_meta.get("note_cost"),
            analysis_meta.get("estimated_cost"),
        ]
    )
    enriched["total_ai_cost"] = total_cost
    enriched["total_ai_cost_currency"] = (
        analysis_meta.get("currency") or parsed_meta.get("note_currency")
    )
    return enriched


def load_parsed_note_metadata(
    parsed_json_path: str | None,
    parsed_md_path: str | None = None,
) -> dict[str, Any]:
    if parsed_json_path:
        path = Path(parsed_json_path)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                usage = payload.get("usage") or {}
                return {
                    "note_cost": usage.get("estimated_cost"),
                    "note_currency": usage.get("currency"),
                    "generation_mode": payload.get("generation_mode"),
                    "note_prompt_tokens": usage.get("prompt_tokens"),
                    "note_completion_tokens": usage.get("completion_tokens"),
                    "note_total_tokens": usage.get("total_tokens"),
                }
    if not parsed_md_path:
        return {}
    path = Path(parsed_md_path)
    if not path.exists():
        return {}
    return {}


def load_analysis_metadata(analysis_json_path: str | None) -> dict[str, Any]:
    if not analysis_json_path:
        return {}
    path = Path(analysis_json_path)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    usage = payload.get("usage") or {}
    return usage if isinstance(usage, dict) else {}


def sum_known_costs(values: list[Any]) -> float | None:
    numeric: list[float] = []
    for value in values:
        if value in (None, "", "unknown"):
            continue
        try:
            numeric.append(float(value))
        except (TypeError, ValueError):
            continue
    if not numeric:
        return None
    return sum(numeric)
