from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from powerlit.models import PaperRecord
from powerlit.services.index import IndexStore
from powerlit.services.pdf_parser import PDFParseError, PDFParserService
from powerlit.settings import Settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run MinerU parsing for all indexed PDFs that do not have parsed artifacts yet."
    )
    parser.add_argument("--status-path", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10000)
    return parser.parse_args()


def count_pending(db_path: Path) -> int:
    with sqlite3.connect(db_path, timeout=30) as conn:
        return int(
            conn.execute(
                """
                select count(*)
                from papers
                where local_pdf_path is not null and trim(local_pdf_path) != ''
                  and doi is not null and trim(doi) != ''
                  and (parsed_json_path is null or trim(parsed_json_path) = '')
                  and (parsed_md_path is null or trim(parsed_md_path) = '')
                """
            ).fetchone()[0]
        )


def write_status(status_path: Path, payload: dict) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    args = parse_args()
    settings = Settings()
    store = IndexStore(settings)
    parser = PDFParserService(settings)
    status_path: Path = args.status_path.resolve()
    started_at = datetime.now(UTC).isoformat()
    pending_before = count_pending(settings.db_path)

    records: list[PaperRecord] = store.load_paper_records(
        limit=args.limit,
        unresolved_only=False,
    )
    pending_records = [
        record
        for record in records
        if record.doi
        and record.local_pdf_path
        and not record.parsed_json_path
        and not record.parsed_md_path
    ]

    state: dict[str, object] = {
        "state": "running",
        "started_at": started_at,
        "settings": {
            "pdf_transcription_backend": settings.pdf_transcription_backend,
            "note_review_enabled": settings.note_review_enabled,
            "parsed_output_dir": str(settings.parsed_output_dir),
            "db_path": str(settings.db_path),
        },
        "pending_before": pending_before,
        "loaded_records": len(records),
        "eligible_records": len(pending_records),
        "parsed": 0,
        "failed": 0,
        "skipped": len(records) - len(pending_records),
        "failures": [],
        "last_success": None,
        "totals": {
            "note_cost_cny": 0.0,
            "review_issues": 0,
            "review_severe_issues": 0,
            "direct_errors": 0,
            "consistency_reviews": 0,
        },
    }
    write_status(status_path, state)

    print(f"[start] {started_at}", flush=True)
    print(
        "[config] "
        f"backend={settings.pdf_transcription_backend} "
        f"note_review_enabled={settings.note_review_enabled}",
        flush=True,
    )
    print(
        f"[queue] loaded={len(records)} eligible={len(pending_records)} "
        f"pending_before={pending_before}",
        flush=True,
    )

    try:
        total = len(pending_records)
        for idx, record in enumerate(pending_records, start=1):
            doi = record.doi or "<no-doi>"
            print(
                f"[item {idx}/{total}] start doi={doi} pdf={record.local_pdf_path}",
                flush=True,
            )
            try:
                artifacts = parser.parse_record(record, Path(record.local_pdf_path))
                store.attach_parsed_artifacts(
                    doi=doi,
                    json_path=artifacts.json_path,
                    markdown_path=artifacts.markdown_path,
                )
                state["parsed"] = int(state["parsed"]) + 1
                totals = dict(state["totals"])
                if artifacts.note_usage and artifacts.note_usage.estimated_cost is not None:
                    totals["note_cost_cny"] = float(totals["note_cost_cny"]) + float(
                        artifacts.note_usage.estimated_cost
                    )
                totals["review_issues"] = int(totals["review_issues"]) + int(
                    artifacts.review_issue_count
                )
                totals["review_severe_issues"] = int(
                    totals["review_severe_issues"]
                ) + int(artifacts.review_severe_issue_count)
                totals["direct_errors"] = int(totals["direct_errors"]) + int(
                    artifacts.review_derivation_direct_error_count
                )
                totals["consistency_reviews"] = int(
                    totals["consistency_reviews"]
                ) + int(artifacts.review_derivation_consistency_count)
                state["totals"] = totals
                state["last_success"] = {
                    "doi": doi,
                    "json_path": str(artifacts.json_path),
                    "markdown_path": str(artifacts.markdown_path),
                    "page_count": int(artifacts.page_count),
                    "finished_at": datetime.now(UTC).isoformat(),
                }
                write_status(status_path, state)
                print(
                    f"[item {idx}/{total}] done doi={doi} pages={artifacts.page_count} "
                    f"json={artifacts.json_path}",
                    flush=True,
                )
            except PDFParseError as exc:
                state["failed"] = int(state["failed"]) + 1
                failures = list(state["failures"])
                failures.append(
                    {
                        "doi": doi,
                        "error": str(exc),
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                state["failures"] = failures
                write_status(status_path, state)
                print(f"[item {idx}/{total}] failed doi={doi} error={exc}", flush=True)
            except Exception as exc:  # noqa: BLE001
                state["failed"] = int(state["failed"]) + 1
                failures = list(state["failures"])
                failures.append(
                    {
                        "doi": doi,
                        "error": f"unexpected: {exc}",
                        "traceback": traceback.format_exc(),
                        "at": datetime.now(UTC).isoformat(),
                    }
                )
                state["failures"] = failures
                write_status(status_path, state)
                print(f"[item {idx}/{total}] failed doi={doi} unexpected={exc}", flush=True)
                traceback.print_exc()

        pending_after = count_pending(settings.db_path)
        state["state"] = "completed"
        state["completed_at"] = datetime.now(UTC).isoformat()
        state["pending_after"] = pending_after
        write_status(status_path, state)
        print(
            f"[done] pending_after={pending_after} parsed={state['parsed']} "
            f"failed={state['failed']}",
            flush=True,
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        pending_after = count_pending(settings.db_path)
        state["state"] = "failed"
        state["failed_at"] = datetime.now(UTC).isoformat()
        state["pending_after"] = pending_after
        state["fatal_error"] = str(exc)
        state["fatal_traceback"] = traceback.format_exc()
        write_status(status_path, state)
        print(f"[fatal] {exc}", flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
