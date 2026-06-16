from __future__ import annotations

import argparse
import builtins
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2, move

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from powerlit.citations import format_gbt_7714  # noqa: E402
from powerlit.models import DocumentType, PaperRecord  # noqa: E402
from powerlit.services.incoming_processor import (  # noqa: E402
    IncomingDOIIdentification,
    IncomingPDFProcessor,
    IncomingProcessorError,
    iter_incoming_pdfs,
)
from powerlit.services.index import is_path_within_directory  # noqa: E402
from powerlit.services.library_layout import (  # noqa: E402
    build_reference_pdf_path,
    infer_annual_volume,
    is_known_journal_doi,
    normalize_known_source_title,
)
from powerlit.settings import Settings  # noqa: E402


def print(*args, **kwargs) -> None:  # noqa: A001
    try:
        builtins.print(*args, **kwargs)
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass

DEFAULT_STATUS_PATH = PROJECT_ROOT / "output/analysis/incoming-mineru-api-live.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "output/analysis/incoming-mineru-api-live.log"
DEFAULT_SUMMARY_PATH = PROJECT_ROOT / "output/analysis/incoming-ingest-and-parse-summary.json"
BATCH_SCRIPT = PROJECT_ROOT / "scripts/maintenance/run_full_library_mineru_api_batch.py"
DASHBOARD_SCRIPT = PROJECT_ROOT / "scripts/maintenance/run_full_library_mineru_api_batch_dashboard.py"


@dataclass(slots=True)
class RegistrationResult:
    source_path: str
    doi: str
    target_pdf_path: str


@dataclass(slots=True)
class RegistrationFailure:
    source_path: str
    error: str


@dataclass(slots=True)
class RegistrationSummary:
    incoming_dir: str
    discovered: int
    attempted: int
    succeeded: int
    failed: int
    results: list[RegistrationResult]
    failures: list[RegistrationFailure]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "One-click pipeline: register PDFs from incoming_pdf, then run the official "
            "MinerU batch API for all indexed PDFs that still lack parsed artifacts."
        )
    )
    parser.add_argument(
        "--incoming-limit",
        type=int,
        default=None,
        help="最多登记多少个 incoming_pdf 中的 PDF；默认处理全部。",
    )
    parser.add_argument(
        "--parse-limit",
        type=int,
        default=10000,
        help="最多加载多少条已入库记录来检查缺失解析；默认 10000。",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        help="官方 MinerU API 每批上传数量；慢网络建议 1-3，默认 3。",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="本次最多跑多少批；0 表示不限。",
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        default=DEFAULT_STATUS_PATH,
        help="MinerU 批量解析状态 JSON 路径。",
    )
    parser.add_argument(
        "--log-path",
        type=Path,
        default=DEFAULT_LOG_PATH,
        help="MinerU 批量解析日志路径。",
    )
    parser.add_argument(
        "--summary-path",
        type=Path,
        default=DEFAULT_SUMMARY_PATH,
        help="一键任务总摘要 JSON 路径。",
    )
    parser.add_argument(
        "--upload-timeout",
        type=float,
        default=3600.0,
        help="单个文件上传最长等待秒数；默认 3600。",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=1800.0,
        help="单个结果包下载最长等待秒数；默认 1800。",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=120.0,
        help="普通 API 请求最长等待秒数；默认 120。",
    )
    parser.add_argument(
        "--batch-timeout",
        type=float,
        default=14400.0,
        help="单批 MinerU 解析最长等待秒数；默认 14400。",
    )
    parser.add_argument(
        "--register-workers",
        type=int,
        default=4,
        help="登记前并发预识别 DOI 的 worker 数；NAS 压力大可设为 1，默认 4。",
    )
    parser.add_argument(
        "--doi-scan-pages",
        type=int,
        default=None,
        help="从每篇 PDF 前多少页识别 DOI；默认使用配置值 2，漏识别时可设为 3。",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="不用原地刷新仪表盘，改为普通滚动日志输出。",
    )
    parser.add_argument(
        "--online-metadata",
        action="store_true",
        help="登记 incoming_pdf 时访问 Crossref 获取精确元数据；默认离线兜底，适合无 VPN 环境。",
    )
    parser.add_argument(
        "--refresh-catalogs-during-register",
        action="store_true",
        help="登记每篇 PDF 后即时刷新目录 Markdown；默认关闭以避免 5000 篇批量入库极慢。",
    )
    parser.add_argument(
        "--parse-only",
        action="store_true",
        help="跳过 incoming_pdf 登记，只解析数据库中尚未解析的 PDF。",
    )
    parser.add_argument(
        "--register-only",
        action="store_true",
        help="只登记 incoming_pdf，不调用 MinerU 官方 API。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将处理的数量，不移动文件、不联网。",
    )
    return parser.parse_args(argv)


def resolve_python_executable() -> str:
    venv_python = PROJECT_ROOT / ".venv/bin/python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def build_child_env(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = (
        str(SRC_DIR) if not existing_pythonpath else f"{SRC_DIR}:{existing_pythonpath}"
    )
    env["POWERLIT_MINERU_API_UPLOAD_TIMEOUT"] = str(args.upload_timeout)
    env["POWERLIT_MINERU_API_DOWNLOAD_TIMEOUT"] = str(args.download_timeout)
    env["POWERLIT_MINERU_API_REQUEST_TIMEOUT"] = str(args.request_timeout)
    env["POWERLIT_MINERU_API_BATCH_TIMEOUT"] = str(args.batch_timeout)
    return env


def count_pending_parses(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    try:
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
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise


def load_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_summary(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def log(message: str) -> None:
    print(message, flush=True)


def register_incoming_pdfs(
    settings: Settings,
    *,
    incoming_limit: int | None,
    register_workers: int,
    dry_run: bool,
) -> RegistrationSummary:
    incoming_files = iter_incoming_pdfs(settings.incoming_pdf_dir)
    files_to_process = incoming_files
    if incoming_limit is not None:
        files_to_process = incoming_files[: max(0, incoming_limit)]

    summary = RegistrationSummary(
        incoming_dir=str(settings.incoming_pdf_dir),
        discovered=len(incoming_files),
        attempted=len(files_to_process),
        succeeded=0,
        failed=0,
        results=[],
        failures=[],
    )
    log(f"[incoming] dir={settings.incoming_pdf_dir}")
    log(f"[incoming] discovered={len(incoming_files)} selected={len(files_to_process)}")

    if dry_run:
        for path in files_to_process[:20]:
            log(f"[dry-run] would-register {path}")
        if len(files_to_process) > 20:
            log(f"[dry-run] ... and {len(files_to_process) - 20} more")
        return summary

    if not files_to_process:
        log("[incoming] no PDFs to register")
        return summary

    identifications: dict[Path, IncomingDOIIdentification] = {}
    identification_failures: dict[Path, str] = {}
    if register_workers > 1:
        identifications, identification_failures = identify_incoming_pdfs_parallel(
            settings,
            files_to_process,
            register_workers=register_workers,
        )
        if settings.metadata_lookup_offline and not settings.catalog_view_auto_refresh:
            return bulk_register_identified_pdfs(
                settings,
                files_to_process,
                identifications=identifications,
                identification_failures=identification_failures,
                summary=summary,
            )

    processor = IncomingPDFProcessor(settings)
    total = len(files_to_process)
    for index, pdf_path in enumerate(files_to_process, start=1):
        progress_prefix = f"[register {index}/{total}]"
        log(f"[register {index}/{total}] start {pdf_path.name}")
        if pdf_path in identification_failures:
            message = identification_failures[pdf_path]
            summary.failures.append(RegistrationFailure(source_path=str(pdf_path), error=message))
            summary.failed += 1
            log(f"[register {index}/{total}] skipped {pdf_path.name}: {message}")
            continue
        identification = identifications.get(pdf_path)
        try:
            result = processor.process_file(
                pdf_path,
                parse=False,
                analyze=False,
                identified_doi=identification.doi if identification else None,
                pdf_header_text=identification.pdf_header_text if identification else None,
                progress_callback=lambda item, prefix=progress_prefix: log(f"{prefix} {item}"),
            )
        except IncomingProcessorError as exc:
            summary.failures.append(RegistrationFailure(source_path=str(pdf_path), error=str(exc)))
            summary.failed += 1
            log(f"[register {index}/{total}] failed {pdf_path.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            summary.failures.append(RegistrationFailure(source_path=str(pdf_path), error=message))
            summary.failed += 1
            log(f"[register {index}/{total}] failed {pdf_path.name}: {message}")
            continue
        summary.results.append(
            RegistrationResult(
                source_path=str(result.file_path),
                doi=result.doi,
                target_pdf_path=str(result.target_pdf_path),
            )
        )
        summary.succeeded += 1
        log(f"[register {index}/{total}] done doi={result.doi}")
        log(f"[register {index}/{total}] stored={result.target_pdf_path}")

    log(f"[incoming] completed succeeded={summary.succeeded} failed={summary.failed}")
    return summary


def bulk_register_identified_pdfs(
    settings: Settings,
    files_to_process: list[Path],
    *,
    identifications: dict[Path, IncomingDOIIdentification],
    identification_failures: dict[Path, str],
    summary: RegistrationSummary,
) -> RegistrationSummary:
    processor = IncomingPDFProcessor(settings)
    plans: list[tuple[Path, IncomingDOIIdentification, PaperRecord]] = []
    total = len(files_to_process)
    log("[bulk-register] building offline metadata records")

    for index, pdf_path in enumerate(files_to_process, start=1):
        if pdf_path in identification_failures:
            message = identification_failures[pdf_path]
            summary.failures.append(RegistrationFailure(source_path=str(pdf_path), error=message))
            summary.failed += 1
            continue
        identification = identifications.get(pdf_path)
        if identification is None:
            message = "DOI was not pre-identified."
            summary.failures.append(RegistrationFailure(source_path=str(pdf_path), error=message))
            summary.failed += 1
            continue
        try:
            record = processor.lookup.lookup_by_doi(
                identification.doi,
                pdf_path=pdf_path,
                pdf_header_text=identification.pdf_header_text,
            )
            if record is None:
                raise IncomingProcessorError(f"无法生成离线元数据: {identification.doi}")
            if not record.volume:
                record.volume = infer_annual_volume(record.source_title, record.year)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            summary.failures.append(RegistrationFailure(source_path=str(pdf_path), error=message))
            summary.failed += 1
            log(f"[bulk-register metadata {index}/{total}] failed {pdf_path.name}: {message}")
            continue
        plans.append((pdf_path, identification, record))
        if index <= 5 or index % 500 == 0:
            log(f"[bulk-register metadata {index}/{total}] doi={identification.doi}")

    if not plans:
        log("[bulk-register] no valid PDFs to register")
        return summary

    records = [record for _, _, record in plans]
    log(f"[bulk-register] upserting metadata rows={len(records)}")
    processor.store.upsert_records(records)

    update_rows: list[tuple[str, str, str | None, str | None, str, str]] = []
    moved_items: list[RegistrationResult] = []
    log(f"[bulk-register] moving PDFs and preparing path updates rows={len(plans)}")
    for index, (pdf_path, identification, record) in enumerate(plans, start=1):
        try:
            target_path, citation, source_title, document_type = build_managed_pdf_update(
                settings,
                record,
                pdf_path,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            source_path = pdf_path.resolve()
            if source_path.parent.resolve() == target_path.parent.resolve():
                managed_path = source_path
            else:
                if target_path.exists():
                    target_path.unlink()
                if is_path_within_directory(source_path, settings.incoming_pdf_dir):
                    move(str(source_path), str(target_path))
                else:
                    copy2(source_path, target_path)
                managed_path = target_path
            update_rows.append(
                (
                    citation,
                    str(managed_path.resolve()),
                    source_title,
                    document_type.value if document_type else None,
                    datetime.now(UTC).isoformat(),
                    identification.doi.strip().lower(),
                )
            )
            moved_items.append(
                RegistrationResult(
                    source_path=str(pdf_path),
                    doi=identification.doi,
                    target_pdf_path=str(managed_path.resolve()),
                )
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            summary.failures.append(RegistrationFailure(source_path=str(pdf_path), error=message))
            summary.failed += 1
            log(f"[bulk-register move {index}/{len(plans)}] failed {pdf_path.name}: {message}")
            continue
        if index <= 5 or index % 100 == 0:
            log(f"[bulk-register move {index}/{len(plans)}] doi={identification.doi}")

    if update_rows:
        log(f"[bulk-register] updating attached PDF paths rows={len(update_rows)}")
        bulk_update_attached_pdf_paths(settings, update_rows)
        summary.results.extend(moved_items)
        summary.succeeded += len(moved_items)

    log(f"[incoming] completed succeeded={summary.succeeded} failed={summary.failed}")
    return summary


def build_managed_pdf_update(
    settings: Settings,
    record: PaperRecord,
    pdf_path: Path,
) -> tuple[Path, str, str | None, DocumentType | None]:
    normalized_source_title = normalize_known_source_title(record.source_title, doi=record.doi)
    corrected_document_type = (
        DocumentType.JOURNAL if is_known_journal_doi(record.doi) else record.document_type
    )
    citation_record = record.model_copy(
        update={
            "source_title": normalized_source_title or record.source_title,
            "document_type": corrected_document_type,
        }
    )
    target_path = build_reference_pdf_path(
        settings.reference_dir,
        title=record.title,
        doi=record.doi,
        source_title=normalized_source_title,
        year=record.year,
        volume=record.volume,
        issue=record.issue,
        original_filename=pdf_path.name,
    )
    return target_path, format_gbt_7714(citation_record), normalized_source_title, corrected_document_type


def bulk_update_attached_pdf_paths(
    settings: Settings,
    update_rows: list[tuple[str, str, str | None, str | None, str, str]],
) -> None:
    with sqlite3.connect(settings.db_path, timeout=60) as conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_papers_lower_doi ON papers(lower(doi))"
        )
        conn.executemany(
            """
            UPDATE papers
            SET gbt7714_citation = ?,
                local_pdf_path = ?,
                download_status = 'downloaded',
                acquisition_stage = 'downloaded',
                acquisition_method = COALESCE(acquisition_method, 'manual'),
                source_title = COALESCE(?, source_title),
                document_type = COALESCE(?, document_type),
                updated_at = ?
            WHERE lower(doi) = ?
            """,
            update_rows,
        )


def identify_incoming_pdfs_parallel(
    settings: Settings,
    files_to_process: list[Path],
    *,
    register_workers: int,
) -> tuple[dict[Path, IncomingDOIIdentification], dict[Path, str]]:
    total = len(files_to_process)
    worker_count = max(1, min(register_workers, total))
    log(
        "[identify] "
        f"pre-scanning DOI with workers={worker_count} "
        f"pages={settings.incoming_pdf_doi_scan_pages}"
    )
    thread_local = threading.local()

    def get_processor() -> IncomingPDFProcessor:
        processor = getattr(thread_local, "processor", None)
        if processor is None:
            processor = IncomingPDFProcessor(settings)
            thread_local.processor = processor
        return processor

    def identify_one(index: int, pdf_path: Path) -> tuple[Path, IncomingDOIIdentification]:
        identification = get_processor().identify_pdf(pdf_path)
        return pdf_path, identification

    identifications: dict[Path, IncomingDOIIdentification] = {}
    failures: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(identify_one, index, pdf_path): (index, pdf_path)
            for index, pdf_path in enumerate(files_to_process, start=1)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            index, pdf_path = futures[future]
            try:
                path, identification = future.result()
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                failures[pdf_path] = message
                log(
                    f"[identify {completed}/{total}] failed "
                    f"register_index={index} file={pdf_path.name}: {message}"
                )
                continue
            identifications[path] = identification
            log(
                f"[identify {completed}/{total}] doi={identification.doi} "
                f"register_index={index} file={pdf_path.name}"
            )
    log(f"[identify] completed identified={len(identifications)} failed={len(failures)}")
    return identifications, failures


def build_batch_command(args: argparse.Namespace, *, dashboard: bool) -> list[str]:
    script = DASHBOARD_SCRIPT if dashboard else BATCH_SCRIPT
    command = [
        resolve_python_executable(),
        "-u",
        str(script),
        "--status-path",
        str(args.status_path),
        "--limit",
        str(args.parse_limit),
        "--batch-size",
        str(args.batch_size),
        "--max-batches",
        str(args.max_batches),
    ]
    if dashboard:
        command.extend(["--log-path", str(args.log_path)])
    return command


def run_plain_batch(args: argparse.Namespace, env: dict[str, str]) -> int:
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_batch_command(args, dashboard=False)
    log(f"[parse] command={' '.join(command)}")
    with args.log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for raw_line in process.stdout:
            print(raw_line, end="", flush=True)
            log_handle.write(raw_line)
            log_handle.flush()
        return int(process.wait() or 0)


def run_dashboard_batch(args: argparse.Namespace, env: dict[str, str]) -> int:
    command = build_batch_command(args, dashboard=True)
    log(f"[parse] dashboard command={' '.join(command)}")
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
    )
    return int(completed.returncode or 0)


def token_configured(settings: Settings) -> bool:
    return bool((settings.mineru_api_token or "").strip())


def validate_required_paths(settings: Settings, args: argparse.Namespace) -> list[str]:
    missing: list[str] = []
    if not args.parse_only and not settings.incoming_pdf_dir.exists():
        missing.append(f"incoming_pdf directory is unavailable: {settings.incoming_pdf_dir}")
    if not args.register_only and not settings.db_path.parent.exists():
        missing.append(f"database directory is unavailable: {settings.db_path.parent}")
    return missing


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.parse_only and args.register_only:
        log("[fatal] --parse-only 和 --register-only 不能同时使用")
        return 2

    settings = Settings()
    settings_updates: dict[str, object] = {}
    if not args.online_metadata:
        settings_updates["metadata_lookup_offline"] = True
    if not args.refresh_catalogs_during_register:
        settings_updates["catalog_view_auto_refresh"] = False
    if args.doi_scan_pages is not None:
        settings_updates["incoming_pdf_doi_scan_pages"] = max(1, min(args.doi_scan_pages, 10))
    if settings_updates:
        settings = settings.model_copy(update=settings_updates)
    missing_paths = validate_required_paths(settings, args)
    if missing_paths:
        for message in missing_paths:
            log(f"[fatal] {message}")
        log("[hint] 如果这些路径在 NAS 上，请先确认 NAS 已经挂载，再重新运行本脚本。")
        return 2

    started_at = datetime.now(UTC).isoformat()
    summary_payload: dict[str, object] = {
        "state": "running",
        "started_at": started_at,
        "project_root": str(PROJECT_ROOT),
        "incoming_dir": str(settings.incoming_pdf_dir),
        "db_path": str(settings.db_path),
        "status_path": str(args.status_path),
        "log_path": str(args.log_path),
        "dry_run": bool(args.dry_run),
        "metadata_mode": "online_crossref" if args.online_metadata else "offline_pdf_filename",
    }
    write_summary(args.summary_path, summary_payload)

    pending_before = count_pending_parses(settings.db_path)
    summary_payload["pending_before_registration"] = pending_before
    log(f"[start] {started_at}")
    log(f"[db] pending_before_registration={pending_before}")

    registration_summary = RegistrationSummary(
        incoming_dir=str(settings.incoming_pdf_dir),
        discovered=0,
        attempted=0,
        succeeded=0,
        failed=0,
        results=[],
        failures=[],
    )
    if not args.parse_only:
        registration_summary = register_incoming_pdfs(
            settings,
            incoming_limit=args.incoming_limit,
            register_workers=args.register_workers,
            dry_run=args.dry_run,
        )
        summary_payload["registration"] = asdict(registration_summary)
        write_summary(args.summary_path, summary_payload)
    else:
        log("[incoming] parse-only enabled; registration skipped")

    pending_after_registration = count_pending_parses(settings.db_path)
    summary_payload["pending_after_registration"] = pending_after_registration
    log(f"[db] pending_after_registration={pending_after_registration}")

    if args.dry_run:
        summary_payload["state"] = "dry_run_completed"
        summary_payload["completed_at"] = datetime.now(UTC).isoformat()
        write_summary(args.summary_path, summary_payload)
        return 0

    if args.register_only:
        exit_code = 1 if registration_summary.failed else 0
        summary_payload["state"] = "completed" if exit_code == 0 else "completed_with_failures"
        summary_payload["completed_at"] = datetime.now(UTC).isoformat()
        summary_payload["exit_code"] = exit_code
        write_summary(args.summary_path, summary_payload)
        return exit_code

    if pending_after_registration <= 0:
        log("[parse] no pending PDFs need MinerU parsing")
        exit_code = 1 if registration_summary.failed else 0
        summary_payload["state"] = "completed" if exit_code == 0 else "completed_with_failures"
        summary_payload["completed_at"] = datetime.now(UTC).isoformat()
        summary_payload["exit_code"] = exit_code
        write_summary(args.summary_path, summary_payload)
        return exit_code

    if not token_configured(settings):
        log("[fatal] POWERLIT_MINERU_API_TOKEN is required before calling the official MinerU API")
        log("[hint] export POWERLIT_MINERU_API_TOKEN='你的 token' 后重新运行本脚本")
        summary_payload["state"] = "failed"
        summary_payload["fatal_error"] = "POWERLIT_MINERU_API_TOKEN is required."
        summary_payload["failed_at"] = datetime.now(UTC).isoformat()
        write_summary(args.summary_path, summary_payload)
        return 2

    env = build_child_env(args)
    args.status_path.parent.mkdir(parents=True, exist_ok=True)
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    dashboard = not args.plain
    parse_exit_code = (
        run_dashboard_batch(args, env) if dashboard else run_plain_batch(args, env)
    )
    parse_status = load_json(args.status_path) or {}
    pending_after_parse = count_pending_parses(settings.db_path)
    mineru_failed = int(parse_status.get("failed") or 0)

    summary_payload["parse_exit_code"] = parse_exit_code
    summary_payload["parse_status"] = parse_status
    summary_payload["pending_after_parse"] = pending_after_parse
    summary_payload["completed_at"] = datetime.now(UTC).isoformat()

    exit_code = parse_exit_code
    if exit_code == 0 and (registration_summary.failed or mineru_failed):
        exit_code = 1
    summary_payload["exit_code"] = exit_code
    summary_payload["state"] = "completed" if exit_code == 0 else "completed_with_failures"
    write_summary(args.summary_path, summary_payload)

    log(
        "[done] "
        f"registered={registration_summary.succeeded} "
        f"registration_failed={registration_summary.failed} "
        f"mineru_parsed={parse_status.get('parsed', 0)} "
        f"mineru_failed={mineru_failed} "
        f"pending_after_parse={pending_after_parse}"
    )
    log(f"[summary] {args.summary_path}")
    log(f"[status] {args.status_path}")
    log(f"[log] {args.log_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
