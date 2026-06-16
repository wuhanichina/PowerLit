from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from click import ClickException

from powerlit.models import PaperRecord
from powerlit.settings import Settings


class MineruRuntimeError(RuntimeError):
    """Raised when MinerU cannot complete a transcription run."""


@dataclass(slots=True)
class MineruMarkdownArtifacts:
    markdown: str
    generation_mode: str


class MineruTranscriptionService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def transcribe_pdf(
        self,
        record: PaperRecord,
        *,
        pdf_path: Path,
        output_path: Path | None = None,
    ) -> MineruMarkdownArtifacts:
        if output_path is not None:
            cleanup_stale_asset_dir(output_path)
        runtime_root = self.settings.mineru_runtime_dir
        jobs_dir = runtime_root / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(
            prefix="mineru-",
            dir=jobs_dir,
        ) as temp_dir:
            output_root = Path(temp_dir)
            runner_pdf_path = stage_pdf_for_mineru(pdf_path, output_root)
            self._run_mineru_cli(
                pdf_path=runner_pdf_path,
                output_root=output_root,
                lang=resolve_mineru_language(record, pdf_path),
            )
            result_dir = locate_mineru_result_dir(output_root, runner_pdf_path, self.settings)
            markdown_path = result_dir / f"{runner_pdf_path.stem}.md"
            if not markdown_path.exists():
                raise MineruRuntimeError(f"MinerU markdown output not found: {markdown_path}")

            markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
            return MineruMarkdownArtifacts(
                markdown=markdown,
                generation_mode=f"mineru_{normalize_backend_label(self.settings.mineru_backend)}",
            )

    def _run_mineru_cli(
        self,
        *,
        pdf_path: Path,
        output_root: Path,
        lang: str,
    ) -> None:
        prepare_mineru_runtime_environment(self.settings)

        args = [
            "-p",
            str(pdf_path),
            "-o",
            str(output_root),
            "-b",
            self.settings.mineru_backend,
            "--source",
            self.settings.mineru_source,
            "-l",
            lang,
        ]

        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        try:
            from mineru.cli.client import main as mineru_main

            with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
                mineru_main.main(args=args, standalone_mode=False)
        except ClickException as exc:
            detail = stderr_buffer.getvalue().strip() or stdout_buffer.getvalue().strip()
            if detail:
                raise MineruRuntimeError(f"MinerU failed: {detail}") from exc
            raise MineruRuntimeError(f"MinerU failed: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive
            detail = stderr_buffer.getvalue().strip() or stdout_buffer.getvalue().strip()
            if detail:
                raise MineruRuntimeError(f"MinerU failed: {detail}") from exc
            raise MineruRuntimeError(f"MinerU failed: {exc}") from exc


def prepare_mineru_runtime_environment(settings: Settings) -> None:
    runtime_root = settings.mineru_runtime_dir
    fastlang_dir = runtime_root / "fastlang"
    fastlang_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ["FTLANG_CACHE"] = str(fastlang_dir)

    import fast_langdetect.ft_detect.infer as infer

    source_model_path = infer.LOCAL_SMALL_MODEL_PATH
    target_model_path = fastlang_dir / source_model_path.name
    if (
        not target_model_path.exists()
        or target_model_path.stat().st_size != source_model_path.stat().st_size
    ):
        shutil.copy2(source_model_path, target_model_path)
    infer.LOCAL_SMALL_MODEL_PATH = target_model_path


def locate_mineru_result_dir(output_root: Path, pdf_path: Path, settings: Settings) -> Path:
    for backend_dir_name in build_result_dir_candidates(settings.mineru_backend):
        backend_dir = output_root / pdf_path.stem / backend_dir_name
        if backend_dir.exists():
            return backend_dir
    target_markdown_name = f"{pdf_path.stem}.md"
    for path in sorted(output_root.rglob(target_markdown_name)):
        if path.is_file():
            return path.parent
    matches = sorted(
        path
        for path in output_root.rglob("*")
        if path.is_dir() and path.name in build_result_dir_candidates(settings.mineru_backend)
    )
    if matches:
        return matches[0]
    raise MineruRuntimeError(f"MinerU output directory not found under: {output_root}")
def stage_pdf_for_mineru(source_pdf: Path, output_root: Path) -> Path:
    staged_dir = output_root / "_input"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged_path = staged_dir / f"document-{uuid4().hex}{source_pdf.suffix.lower()}"
    shutil.copy2(source_pdf, staged_path)
    return staged_path


def cleanup_stale_asset_dir(output_path: Path) -> None:
    asset_dir = output_path.parent / f"{output_path.stem}.mineru.assets"
    if asset_dir.exists():
        shutil.rmtree(asset_dir, ignore_errors=True)


def resolve_mineru_language(record: PaperRecord, pdf_path: Path) -> str:
    candidates = [
        record.title,
        record.source_title,
        pdf_path.name,
    ]
    if any(contains_cjk(candidate or "") for candidate in candidates):
        return "ch"
    return "en"


def contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def normalize_backend_label(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def build_result_dir_candidates(backend: str) -> list[str]:
    normalized = normalize_backend_label(backend)
    candidates = [normalized]
    if normalized.endswith("_auto_engine"):
        candidates.append(normalized.removesuffix("_engine"))
    if normalized.endswith("_http_client"):
        candidates.append(normalized.removesuffix("_client"))
    return list(dict.fromkeys(candidates))
