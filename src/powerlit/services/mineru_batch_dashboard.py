from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from rich.text import Text

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATUS_PATH = PROJECT_ROOT / "output/analysis/full-library-mineru-api-batch-live.json"
DEFAULT_LOG_PATH = PROJECT_ROOT / "output/analysis/full-library-mineru-api-batch-live.log"
DEFAULT_BATCH_SCRIPT = PROJECT_ROOT / "scripts/maintenance/run_full_library_mineru_api_batch.py"


@dataclass(slots=True)
class NetworkSnapshot:
    collected_at: float
    rx_bytes: int
    tx_bytes: int
    interfaces: tuple[str, ...]


class LogPump:
    def __init__(self, stream: TextIO | None, log_path: Path, *, keep_lines: int = 10):
        self._stream = stream
        self._log_path = log_path
        self._keep_lines = keep_lines
        self._recent: deque[str] = deque(maxlen=keep_lines)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._stream is None:
            return
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="mineru-log-pump", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._stream is not None
        with self._log_path.open("w", encoding="utf-8") as handle:
            for raw_line in self._stream:
                line = raw_line.rstrip("\n")
                handle.write(line + "\n")
                handle.flush()
                with self._lock:
                    self._recent.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._recent)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Launch the full-library MinerU batch parser with an in-place live dashboard."
    )
    parser.add_argument("--status-path", type=Path, default=DEFAULT_STATUS_PATH)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--limit", type=int, default=10000)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--refresh-interval", type=float, default=0.5)
    return parser.parse_args(argv)


def load_status(status_path: Path) -> dict[str, object] | None:
    if not status_path.exists():
        return None
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_netstat_link_snapshot(payload: str) -> NetworkSnapshot:
    rx_total = 0
    tx_total = 0
    interfaces: list[str] = []
    seen: set[str] = set()
    for raw_line in payload.splitlines()[1:]:
        columns = raw_line.split()
        if len(columns) < 10:
            continue
        name = columns[0]
        network = columns[2]
        if not network.startswith("<Link#"):
            continue
        if name.startswith("lo"):
            continue
        if name in seen:
            continue
        seen.add(name)
        try:
            rx_total += int(columns[6].replace(",", ""))
            tx_total += int(columns[9].replace(",", ""))
        except ValueError:
            continue
        interfaces.append(name)
    return NetworkSnapshot(
        collected_at=time.monotonic(),
        rx_bytes=rx_total,
        tx_bytes=tx_total,
        interfaces=tuple(interfaces),
    )


def read_network_snapshot() -> NetworkSnapshot | None:
    try:
        completed = subprocess.run(
            ["netstat", "-ibn"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return parse_netstat_link_snapshot(completed.stdout)


def compute_bytes_per_second(
    current: NetworkSnapshot | None,
    previous: NetworkSnapshot | None,
) -> tuple[float, float]:
    if current is None or previous is None:
        return 0.0, 0.0
    elapsed = current.collected_at - previous.collected_at
    if elapsed <= 0:
        return 0.0, 0.0
    rx = max(0.0, (current.rx_bytes - previous.rx_bytes) / elapsed)
    tx = max(0.0, (current.tx_bytes - previous.tx_bytes) / elapsed)
    return rx, tx


def format_bytes(value: int | float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_rate(value: float) -> str:
    return f"{format_bytes(value)}/s"


def format_seconds(value: float | None) -> str:
    if value is None or value < 0:
        return "--"
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def shorten(text: str | None, *, limit: int = 56) -> str:
    if not text:
        return "--"
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def detect_stage(status: dict[str, object] | None, recent_logs: list[str]) -> str:
    if status is None:
        return "starting"
    state = str(status.get("state") or "unknown")
    if state != "running":
        return state
    last_line = recent_logs[-1] if recent_logs else ""
    if "upload-start" in last_line or "upload-failed" in last_line:
        return "uploading"
    if "upload-done" in last_line:
        return "waiting-results"
    if "parsed doi=" in last_line or "parsed-failed" in last_line:
        return "downloading-writing"
    if "result-failed" in last_line or "missing-result" in last_line:
        return "result-handling"
    if status.get("current_batch"):
        return "batch-running"
    return "idle"


def estimate_eta_seconds(status: dict[str, object] | None, *, now_utc: datetime) -> float | None:
    if not status:
        return None
    started_at = status.get("started_at")
    if not isinstance(started_at, str):
        return None
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return None
    parsed = int(status.get("parsed") or 0)
    failed = int(status.get("failed") or 0)
    processed = parsed + failed
    total = int(status.get("pending_before") or 0)
    remaining = max(0, total - processed)
    if processed <= 0 or remaining <= 0:
        return None
    elapsed = (now_utc - started).total_seconds()
    if elapsed <= 0:
        return None
    rate = processed / elapsed
    if rate <= 0:
        return None
    return remaining / rate


def build_dashboard(
    status: dict[str, object] | None,
    *,
    recent_logs: list[str],
    process: subprocess.Popen[str],
    current_network: NetworkSnapshot | None,
    previous_network: NetworkSnapshot | None,
) -> Group:
    now_utc = datetime.now(UTC)
    state = str(status.get("state") or "starting") if status else "starting"
    stage = detect_stage(status, recent_logs)

    pending_before = int(status.get("pending_before") or 0) if status else 0
    parsed = int(status.get("parsed") or 0) if status else 0
    failed = int(status.get("failed") or 0) if status else 0
    skipped = int(status.get("skipped") or 0) if status else 0
    processed = parsed + failed
    remaining = max(0, pending_before - processed)
    completed_batches = int(status.get("completed_batches") or 0) if status else 0
    planned_batches = int(status.get("planned_batches") or 0) if status else 0
    current_batch = status.get("current_batch") if status else None
    last_success = status.get("last_success") if status else None
    status_path = status is not None

    progress = Progress(
        TextColumn("[bold cyan]全库进度"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        expand=True,
    )
    progress_total = max(1, pending_before or 1)
    progress.add_task("library", total=progress_total, completed=min(processed, progress_total))

    rx_rate, tx_rate = compute_bytes_per_second(current_network, previous_network)
    interface_text = ", ".join((current_network.interfaces if current_network else ())) or "--"
    if len(interface_text) > 48:
        interface_text = interface_text[:47] + "…"

    summary = Table.grid(expand=True)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    summary.add_row(
        f"[bold]状态[/bold] {state} / {stage}",
        f"[bold]批次[/bold] {completed_batches}/{planned_batches}",
        f"[bold]剩余[/bold] {remaining}",
    )
    summary.add_row(
        f"[bold]成功[/bold] {parsed}",
        f"[bold]失败[/bold] {failed}",
        f"[bold]跳过[/bold] {skipped}",
    )

    current_batch_text = "--"
    if isinstance(current_batch, dict):
        current_batch_text = (
            f"{current_batch.get('index', '--')}/{current_batch.get('total_batches', '--')} "
            f"batch_id={shorten(str(current_batch.get('batch_id') or '--'), limit=26)}"
        )
    summary.add_row(
        f"[bold]当前批[/bold] {current_batch_text}",
        f"[bold]系统下行[/bold] {format_rate(rx_rate)}",
        f"[bold]系统上行[/bold] {format_rate(tx_rate)}",
    )

    eta_seconds = estimate_eta_seconds(status, now_utc=now_utc)
    last_success_text = "--"
    if isinstance(last_success, dict):
        doi_text = shorten(str(last_success.get("doi") or "--"), limit=46)
        finished_at = last_success.get("finished_at")
        if isinstance(finished_at, str):
            try:
                finished_at = datetime.fromisoformat(finished_at).astimezone().strftime("%m-%d %H:%M:%S")
            except ValueError:
                pass
        last_success_text = f"{doi_text} @ {finished_at}"
    summary.add_row(
        f"[bold]ETA[/bold] {format_seconds(eta_seconds)}",
        f"[bold]网络接口[/bold] {interface_text}",
        f"[bold]最近成功[/bold] {last_success_text}",
    )

    process_info = Table.grid(expand=True)
    process_info.add_column(ratio=1)
    process_info.add_column(ratio=1)
    process_info.add_row(
        f"[bold]PID[/bold] {process.pid}",
        f"[bold]退出码[/bold] {'运行中' if process.poll() is None else process.returncode}",
    )
    if not status_path:
        process_info.add_row("[yellow]状态文件尚未生成[/yellow]", "")

    logs = recent_logs or ["(暂无输出，可能还在启动或等待网络响应)"]
    log_panel = Panel(
        Text("\n".join(logs[-8:])),
        title="最近日志",
        border_style="blue",
    )

    return Group(
        Panel(summary, title="MinerU 批量解析监控", border_style="cyan"),
        progress,
        Panel(process_info, title="任务进程", border_style="magenta"),
        log_panel,
    )


def resolve_python_executable() -> str:
    venv_python = PROJECT_ROOT / ".venv/bin/python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def spawn_batch_process(args: argparse.Namespace) -> subprocess.Popen[str]:
    command = [
        resolve_python_executable(),
        "-u",
        str(DEFAULT_BATCH_SCRIPT),
        "--status-path",
        str(args.status_path),
        "--limit",
        str(args.limit),
        "--batch-size",
        str(args.batch_size),
        "--max-batches",
        str(args.max_batches),
    ]
    env = os.environ.copy()
    src_path = str(PROJECT_ROOT / "src")
    existing_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}:{existing_pythonpath}"
    return subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=5)
        return
    except Exception:
        pass
    try:
        process.terminate()
        process.wait(timeout=5)
        return
    except Exception:
        pass
    process.kill()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    process = spawn_batch_process(args)
    log_pump = LogPump(process.stdout, args.log_path)
    log_pump.start()

    current_network = read_network_snapshot()
    previous_network = current_network
    last_network_sample_at = time.monotonic()

    live_console = Live(
        refresh_per_second=max(1, int(round(1 / args.refresh_interval))),
        transient=False,
    )
    try:
        with live_console as live:
            while True:
                now = time.monotonic()
                if now - last_network_sample_at >= 1.0:
                    previous_network = current_network
                    current_network = read_network_snapshot()
                    last_network_sample_at = now

                status = load_status(args.status_path)
                live.update(
                    build_dashboard(
                        status,
                        recent_logs=log_pump.snapshot(),
                        process=process,
                        current_network=current_network,
                        previous_network=previous_network,
                    )
                )

                if process.poll() is not None and not log_pump.is_alive():
                    break
                time.sleep(args.refresh_interval)
    except KeyboardInterrupt:
        terminate_process(process)
        return 130

    process.wait()
    return int(process.returncode or 0)
