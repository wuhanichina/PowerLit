from __future__ import annotations

import csv
import json
from pathlib import Path

from powerlit.citations import format_gbt_7714
from powerlit.models import PaperRecord

MARKDOWN_ENCODING = "utf-8"
MARKDOWN_NEWLINE = "\n"


def export_records(records: list[PaperRecord], output_base: Path) -> dict[str, Path]:
    output_base.parent.mkdir(parents=True, exist_ok=True)
    payload = [build_export_payload(record) for record in records]

    json_path = output_base.with_suffix(".json")
    csv_path = output_base.with_suffix(".csv")

    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(csv_path, payload)
    return {"json": json_path, "csv": csv_path}


def export_download_queue(rows: list[dict[str, str]], output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "title",
        "gbt7714_citation",
        "doi",
        "journal_short_name",
        "volume_folder",
        "issue_folder",
        "target_reference_dir",
        "publisher_url",
        "researchgate_url",
        "researchgate_lookup_url",
        "source_title",
        "year",
        "query_pack",
        "acquisition_method",
        "acquisition_stage",
        "acquisition_source_url",
        "download_status",
        "local_pdf_path",
        "suggested_filename",
        "target_pdf_path",
    ]
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def write_markdown(path: Path, content: str) -> None:
    with path.open("w", encoding=MARKDOWN_ENCODING, newline=MARKDOWN_NEWLINE) as handle:
        handle.write(content)


def build_export_payload(record: PaperRecord) -> dict[str, str]:
    citation = format_gbt_7714(record)
    return record.export_row(citation)


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "title",
        "gbt7714_citation",
        "doi",
        "publisher_url",
        "researchgate_url",
        "researchgate_lookup_url",
        "researchgate_match_status",
        "acquisition_method",
        "acquisition_stage",
        "acquisition_source_url",
        "download_status",
        "local_pdf_path",
        "parsed_json_path",
        "parsed_md_path",
        "analysis_md_path",
        "analysis_json_path",
        "providers",
        "year",
        "document_type",
        "source_title",
        "query_pack",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
