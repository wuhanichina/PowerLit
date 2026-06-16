from __future__ import annotations

import argparse
import builtins
import json
import os
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
from powerlit.services.mineru_official_api import (
    MineruBatchFile,
    MineruOfficialAPIError,
    MineruOfficialBatchAPIService,
    MineruStorageUnavailableError,
)
from powerlit.settings import Settings


def print(*args, **kwargs) -> None:  # noqa: A001
    try:
        builtins.print(*args, **kwargs)
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the official MinerU batch API for all indexed PDFs without parsed artifacts."
    )
    parser.add_argument("--status-path", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--max-batches", type=int, default=0)
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


def chunked[T](items: list[T], size: int) -> list[list[T]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_pending_records(db_path: Path, *, limit: int) -> tuple[list[PaperRecord], list[dict[str, str]]]:
    # Keep startup snappy on NAS-backed SQLite: the generic record loader pulls
    # raw metadata JSON for many rows, but MinerU only needs routing fields here.
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select
                title,
                doi,
                local_pdf_path,
                source_title,
                volume,
                issue,
                year,
                document_type,
                parsed_json_path,
                parsed_md_path
            from papers
            where local_pdf_path is not null and trim(local_pdf_path) != ''
              and doi is not null and trim(doi) != ''
              and (parsed_json_path is null or trim(parsed_json_path) = '')
              and (parsed_md_path is null or trim(parsed_md_path) = '')
            limit ?
            """,
            (limit,),
        ).fetchall()

    records: list[PaperRecord] = []
    missing_files: list[dict[str, str]] = []
    for row in rows:
        pdf_path = optional_text(row["local_pdf_path"])
        doi = optional_text(row["doi"])
        if not pdf_path or not Path(pdf_path).is_file():
            missing_files.append(
                {
                    "doi": doi or "",
                    "local_pdf_path": pdf_path or "",
                    "error": "local_pdf_path does not exist or is not a file",
                }
            )
            continue
        title = optional_text(row["title"]) or doi or (
            Path(pdf_path).stem if pdf_path else "Untitled paper"
        )
        records.append(
            PaperRecord(
                title=title,
                doi=doi,
                local_pdf_path=pdf_path,
                source_title=optional_text(row["source_title"]),
                volume=optional_text(row["volume"]),
                issue=optional_text(row["issue"]),
                year=optional_int(row["year"]),
                document_type=optional_text(row["document_type"]),
                parsed_json_path=optional_text(row["parsed_json_path"]),
                parsed_md_path=optional_text(row["parsed_md_path"]),
            )
        )
    return records, missing_files


def main() -> int:
    args = parse_args()
    settings = Settings()
    store = IndexStore(settings)
    service = MineruOfficialBatchAPIService(settings)
    status_path = args.status_path.resolve()
    started_at = datetime.now(UTC).isoformat()
    pending_before = count_pending(settings.db_path)
    pending_records, missing_files = build_pending_records(settings.db_path, limit=args.limit)

    batch_size = max(1, min(args.batch_size, settings.mineru_api_batch_size))
    planned_batches = chunked(pending_records, batch_size)
    if args.max_batches > 0:
        planned_batches = planned_batches[: args.max_batches]

    state: dict[str, object] = {
        "state": "running",
        "started_at": started_at,
        "settings": {
            "mineru_api_base_url": settings.mineru_api_base_url,
            "mineru_api_model_version": settings.mineru_api_model_version,
            "mineru_api_language": settings.mineru_api_language,
            "mineru_api_batch_size": batch_size,
            "mineru_api_poll_interval": settings.mineru_api_poll_interval,
            "mineru_api_upload_timeout": settings.mineru_api_upload_timeout,
            "mineru_api_download_timeout": settings.mineru_api_download_timeout,
            "parsed_output_dir": str(settings.parsed_output_dir),
            "db_path": str(settings.db_path),
        },
        "pending_before": pending_before,
        "loaded_records": len(pending_records),
        "eligible_records": len(pending_records),
        "missing_files": len(missing_files),
        "skipped_missing_files": missing_files,
        "planned_batches": len(planned_batches),
        "completed_batches": 0,
        "parsed": 0,
        "failed": 0,
        "skipped": 0,
        "failures": [],
        "last_success": None,
        "current_batch": None,
    }
    write_status(status_path, state)

    print(f"[start] {started_at}", flush=True)
    print(
        "[config] "
        f"model={settings.mineru_api_model_version} "
        f"language={settings.mineru_api_language} "
        f"batch_size={batch_size}",
        flush=True,
    )
    print(
        f"[queue] loaded={len(pending_records) + len(missing_files)} eligible={len(pending_records)} "
        f"missing_files={len(missing_files)} pending_before={pending_before} planned_batches={len(planned_batches)}",
        flush=True,
    )

    try:
        for batch_index, batch_records in enumerate(planned_batches, start=1):
            service.ensure_storage_available()

            batch_files: list[MineruBatchFile] = []
            for record in batch_records:
                try:
                    batch_files.append(service.build_batch_file(record))
                except Exception as exc:  # noqa: BLE001
                    state["failed"] = int(state["failed"]) + 1
                    failures = list(state["failures"])
                    failures.append(
                        {
                            "doi": record.doi,
                            "error": str(exc),
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    state["failures"] = failures
                    write_status(status_path, state)
                    print(f"[batch {batch_index}] skip doi={record.doi} error={exc}", flush=True)

            if not batch_files:
                state["completed_batches"] = int(state["completed_batches"]) + 1
                write_status(status_path, state)
                continue

            try:
                upload_batch = service.create_upload_batch(batch_files)
            except MineruOfficialAPIError as exc:
                failures = list(state["failures"])
                for batch_file in batch_files:
                    state["failed"] = int(state["failed"]) + 1
                    failures.append(
                        {
                            "doi": batch_file.record.doi,
                            "data_id": batch_file.data_id,
                            "stage": "create-upload-batch",
                            "error": str(exc),
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                state["failures"] = failures
                state["completed_batches"] = int(state["completed_batches"]) + 1
                state["current_batch"] = None
                write_status(status_path, state)
                print(
                    f"[batch {batch_index}/{len(planned_batches)}] create-upload-failed "
                    f"failed={state['failed']} error={exc}",
                    flush=True,
                )
                continue

            state["current_batch"] = {
                "index": batch_index,
                "total_batches": len(planned_batches),
                "batch_id": upload_batch.batch_id,
                "size": len(batch_files),
                "started_at": datetime.now(UTC).isoformat(),
            }
            write_status(status_path, state)
            print(
                f"[batch {batch_index}/{len(planned_batches)}] upload-start batch_id={upload_batch.batch_id} "
                f"size={len(batch_files)}",
                flush=True,
            )

            uploaded_files, upload_failures = service.upload_batch_files(upload_batch, batch_files)
            if upload_failures:
                failures = list(state["failures"])
                for failure in upload_failures:
                    state["failed"] = int(state["failed"]) + 1
                    failures.append(
                        {
                            "doi": failure.batch_file.record.doi,
                            "data_id": failure.batch_file.data_id,
                            "batch_id": upload_batch.batch_id,
                            "stage": "upload",
                            "error": failure.error,
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    print(
                        f"[batch {batch_index}] upload-failed doi={failure.batch_file.record.doi} "
                        f"error={failure.error}",
                        flush=True,
                    )
                state["failures"] = failures
                write_status(status_path, state)
            print(
                f"[batch {batch_index}/{len(planned_batches)}] upload-done batch_id={upload_batch.batch_id} "
                f"uploaded={len(uploaded_files)} failed={len(upload_failures)}",
                flush=True,
            )

            if not uploaded_files:
                state["completed_batches"] = int(state["completed_batches"]) + 1
                state["current_batch"] = None
                write_status(status_path, state)
                print(
                    f"[batch {batch_index}/{len(planned_batches)}] complete no-successful-uploads "
                    f"failed={state['failed']}",
                    flush=True,
                )
                continue

            try:
                results = service.wait_for_batch_results(
                    upload_batch.batch_id,
                    expected_data_ids={item.data_id for item in uploaded_files},
                )
            except MineruOfficialAPIError as exc:
                failures = list(state["failures"])
                for batch_file in uploaded_files:
                    state["failed"] = int(state["failed"]) + 1
                    failures.append(
                        {
                            "doi": batch_file.record.doi,
                            "data_id": batch_file.data_id,
                            "batch_id": upload_batch.batch_id,
                            "stage": "fetch-results",
                            "error": str(exc),
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                state["failures"] = failures
                state["completed_batches"] = int(state["completed_batches"]) + 1
                state["current_batch"] = None
                write_status(status_path, state)
                print(
                    f"[batch {batch_index}/{len(planned_batches)}] result-fetch-failed "
                    f"failed={state['failed']} error={exc}",
                    flush=True,
                )
                continue

            results_by_data_id = {
                result.data_id: result
                for result in results
                if result.data_id
            }
            results_by_name = {
                result.file_name: result
                for result in results
                if result.file_name
            }

            for batch_file in uploaded_files:
                result = results_by_data_id.get(batch_file.data_id) or results_by_name.get(
                    batch_file.file_name
                )
                if result is None:
                    state["failed"] = int(state["failed"]) + 1
                    failures = list(state["failures"])
                    failures.append(
                        {
                            "doi": batch_file.record.doi,
                            "data_id": batch_file.data_id,
                            "error": f"No MinerU result returned for batch {upload_batch.batch_id}.",
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    state["failures"] = failures
                    write_status(status_path, state)
                    print(
                        f"[batch {batch_index}] missing-result doi={batch_file.record.doi} data_id={batch_file.data_id}",
                        flush=True,
                    )
                    continue

                if not result.is_success:
                    state["failed"] = int(state["failed"]) + 1
                    failures = list(state["failures"])
                    failures.append(
                        {
                            "doi": batch_file.record.doi,
                            "data_id": batch_file.data_id,
                            "batch_id": upload_batch.batch_id,
                            "state": result.state,
                            "error": result.err_msg or "MinerU result was not successful.",
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    state["failures"] = failures
                    write_status(status_path, state)
                    print(
                        f"[batch {batch_index}] result-failed doi={batch_file.record.doi} state={result.state} "
                        f"error={result.err_msg}",
                        flush=True,
                    )
                    continue

                try:
                    service.ensure_storage_available()
                    zip_bytes = service.download_result_archive(result)
                    artifact = service.write_parsed_artifact(
                        batch_file,
                        zip_bytes=zip_bytes,
                    )
                    store.attach_parsed_artifacts(
                        batch_file.record.doi,
                        json_path=artifact.json_path,
                    )
                    state["parsed"] = int(state["parsed"]) + 1
                    state["last_success"] = {
                        "doi": batch_file.record.doi,
                        "json_path": str(artifact.json_path),
                        "generation_mode": artifact.generation_mode,
                        "page_count": artifact.page_count,
                        "batch_id": upload_batch.batch_id,
                        "finished_at": datetime.now(UTC).isoformat(),
                    }
                    write_status(status_path, state)
                    print(
                        f"[batch {batch_index}] parsed doi={batch_file.record.doi} "
                        f"json={artifact.json_path}",
                        flush=True,
                    )
                except MineruStorageUnavailableError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    state["failed"] = int(state["failed"]) + 1
                    failures = list(state["failures"])
                    failures.append(
                        {
                            "doi": batch_file.record.doi,
                            "data_id": batch_file.data_id,
                            "batch_id": upload_batch.batch_id,
                            "error": str(exc),
                            "traceback": traceback.format_exc(),
                            "at": datetime.now(UTC).isoformat(),
                        }
                    )
                    state["failures"] = failures
                    write_status(status_path, state)
                    print(
                        f"[batch {batch_index}] parsed-failed doi={batch_file.record.doi} error={exc}",
                        flush=True,
                    )

            state["completed_batches"] = int(state["completed_batches"]) + 1
            state["current_batch"] = None
            write_status(status_path, state)
            print(
                f"[batch {batch_index}/{len(planned_batches)}] complete parsed={state['parsed']} failed={state['failed']}",
                flush=True,
            )

        pending_after = count_pending(settings.db_path)
        state["state"] = "completed"
        state["completed_at"] = datetime.now(UTC).isoformat()
        state["pending_after"] = pending_after
        write_status(status_path, state)
        print(
            f"[done] pending_after={pending_after} parsed={state['parsed']} failed={state['failed']}",
            flush=True,
        )
        return 0
    except MineruStorageUnavailableError as exc:
        state["state"] = "blocked"
        state["blocked_at"] = datetime.now(UTC).isoformat()
        state["blocking_reason"] = str(exc)
        write_status(status_path, state)
        print(f"[blocked] {exc}", flush=True)
        return 2
    except MineruOfficialAPIError as exc:
        pending_after = count_pending(settings.db_path)
        state["state"] = "failed"
        state["failed_at"] = datetime.now(UTC).isoformat()
        state["pending_after"] = pending_after
        state["fatal_error"] = str(exc)
        write_status(status_path, state)
        print(f"[fatal] {exc}", flush=True)
        return 1
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
