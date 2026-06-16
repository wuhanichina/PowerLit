from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from re import IGNORECASE, finditer, fullmatch, sub

from pypdf import PdfReader

from powerlit.models import DocumentType, PaperRecord
from powerlit.services.ai_analysis import AIServiceError, AnalysisService
from powerlit.services.index import IndexStore
from powerlit.services.library_layout import (
    infer_annual_volume,
    is_known_journal_doi,
    normalize_known_source_title,
    resolve_journal_short_name,
)
from powerlit.services.metadata_lookup import MetadataLookupService
from powerlit.services.pdf_parser import PDFParseError, PDFParserService
from powerlit.settings import Settings

DOI_REGEX = r"10\.\d{4,9}/[-._;()/:a-z0-9]+"


class IncomingProcessorError(RuntimeError):
    """Raised when an incoming PDF cannot be processed automatically."""


@dataclass(slots=True)
class IncomingProcessResult:
    file_path: Path
    doi: str
    target_pdf_path: Path
    parsed_json_path: Path | None = None
    parsed_md_path: Path | None = None
    analysis_json_path: Path | None = None
    analysis_md_path: Path | None = None


@dataclass(slots=True)
class IncomingDOIIdentification:
    doi: str
    pdf_header_text: str | None = None


class IncomingPDFProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = IndexStore(settings)
        self.lookup = MetadataLookupService(settings)
        self.parser = PDFParserService(settings)
        self.analysis = AnalysisService(settings)

    def process_all(
        self,
        *,
        limit: int | None = None,
        parse: bool = True,
        analyze: bool = True,
        force_overwrite: bool = True,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[list[IncomingProcessResult], list[tuple[Path, str]]]:
        results: list[IncomingProcessResult] = []
        failures: list[tuple[Path, str]] = []
        incoming_files = iter_incoming_pdfs(self.settings.incoming_pdf_dir)
        total_files = len(incoming_files) if limit is None else min(len(incoming_files), limit)
        for index, pdf_path in enumerate(incoming_files, start=1):
            if limit is not None and index > limit:
                break
            try:
                self._emit(
                    progress_callback,
                    f"[{index}/{total_files}] Processing: {pdf_path.name}",
                )
                results.append(
                    self.process_file(
                        pdf_path,
                        parse=parse,
                        analyze=analyze,
                        force_overwrite=force_overwrite,
                        progress_callback=progress_callback,
                    )
                )
            except IncomingProcessorError as exc:
                failures.append((pdf_path, str(exc)))
        return results, failures

    def process_file(
        self,
        pdf_path: Path,
        *,
        parse: bool = True,
        analyze: bool = True,
        force_overwrite: bool = True,
        identified_doi: str | None = None,
        pdf_header_text: str | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> IncomingProcessResult:
        if not parse:
            analyze = False
        if identified_doi:
            doi = identified_doi
        else:
            self._emit(progress_callback, "  - Identifying DOI...")
            identification = self.identify_pdf(pdf_path)
            doi = identification.doi
            pdf_header_text = identification.pdf_header_text
        self._emit(progress_callback, f"  - DOI: {doi}")
        self._emit(progress_callback, "  - Ensuring metadata record...")
        self._ensure_record(doi, pdf_path, pdf_header_text=pdf_header_text)

        self._emit(progress_callback, "  - Moving PDF into literature/reference ...")
        if not self.store.attach_pdf(doi, pdf_path):
            raise IncomingProcessorError(f"无法绑定 PDF 到 DOI: {doi}")

        updated_record = self._load_record(doi)
        local_pdf = Path(str(updated_record.local_pdf_path or ""))
        if not local_pdf.exists():
            raise IncomingProcessorError(f"绑定后的 PDF 不存在: {local_pdf}")

        if not parse:
            self._emit(
                progress_callback,
                "  - Metadata registration completed. OCR/transcription was skipped.",
            )
            return IncomingProcessResult(
                file_path=pdf_path,
                doi=doi,
                target_pdf_path=local_pdf,
            )

        if not force_overwrite and updated_record.parsed_json_path:
            self._emit(progress_callback, "  - Already parsed. Skipping transcription.")
            parsed_json = Path(updated_record.parsed_json_path)
            parsed_md = Path(updated_record.parsed_md_path) if updated_record.parsed_md_path else None
        else:
            self._emit(
                progress_callback,
                "  - Parsing PDF and building structured JSON via AI. "
                "This can take several minutes for long papers...",
            )
            try:
                parsed = self.parser.parse_record(updated_record, local_pdf)
                parsed_json = parsed.json_path
                parsed_md = parsed.markdown_path
            except PDFParseError as exc:
                raise IncomingProcessorError(str(exc)) from exc
            self.store.attach_parsed_artifacts(
                doi=doi,
                json_path=parsed_json,
                markdown_path=parsed_md,
            )
            self._emit(
                progress_callback,
                f"  - Parsed JSON ready: {parsed_json.name}",
            )

        analysis_json_path: Path | None = None
        analysis_md_path: Path | None = None
        if analyze:
            if not force_overwrite and updated_record.analysis_json_path:
                self._emit(progress_callback, "  - Already analyzed. Skipping.")
                analysis_json_path = Path(updated_record.analysis_json_path)
                analysis_md_path = Path(updated_record.analysis_md_path) if updated_record.analysis_md_path else None
            else:
                self._emit(progress_callback, "  - Running structured AI analysis...")
                try:
                    analysis = self.analysis.analyze_record(self._load_record(doi))
                except AIServiceError as exc:
                    raise IncomingProcessorError(str(exc)) from exc
                self.store.attach_analysis_artifacts(
                    doi=doi,
                    json_path=analysis.json_path,
                    markdown_path=analysis.markdown_path,
                )
                analysis_json_path = analysis.json_path
                analysis_md_path = analysis.markdown_path
                self._emit(progress_callback, f"  - Analysis JSON ready: {analysis_json_path.name}")

        return IncomingProcessResult(
            file_path=pdf_path,
            doi=doi,
            target_pdf_path=local_pdf,
            parsed_json_path=parsed_json,
            parsed_md_path=parsed_md,
            analysis_json_path=analysis_json_path,
            analysis_md_path=analysis_md_path,
        )

    def identify_pdf(self, pdf_path: Path) -> IncomingDOIIdentification:
        doi_from_filename = identify_doi_from_filename(pdf_path, self.store)
        if doi_from_filename:
            return IncomingDOIIdentification(doi=doi_from_filename)
        doi_from_pdf, pdf_header_text = extract_doi_and_header_text_from_pdf(
            pdf_path,
            max_pages=self.settings.incoming_pdf_doi_scan_pages,
        )
        if doi_from_pdf:
            return IncomingDOIIdentification(
                doi=doi_from_pdf,
                pdf_header_text=pdf_header_text,
            )
        raise IncomingProcessorError(f"无法从文件名或 PDF 首页识别 DOI: {pdf_path.name}")

    def _identify_doi(self, pdf_path: Path) -> str:
        return self.identify_pdf(pdf_path).doi

    def _ensure_record(
        self,
        doi: str,
        pdf_path: Path,
        *,
        pdf_header_text: str | None = None,
    ) -> PaperRecord:
        existing = self.store.load_paper_records(limit=1, doi=doi, unresolved_only=False)
        if existing:
            record = existing[0]
            if should_refresh_existing_record(record):
                refreshed = self.lookup.lookup_by_doi(
                    doi,
                    pdf_path=pdf_path,
                    pdf_header_text=pdf_header_text,
                )
                if refreshed is not None:
                    refreshed.local_pdf_path = record.local_pdf_path
                    refreshed.download_status = record.download_status
                    if not refreshed.year:
                        refreshed.year = record.year
                    if not refreshed.volume:
                        refreshed.volume = record.volume
                    if not refreshed.issue:
                        refreshed.issue = record.issue
                    if not refreshed.volume:
                        refreshed.volume = infer_annual_volume(
                            refreshed.source_title or record.source_title,
                            refreshed.year or record.year,
                        )
                    self.store.upsert_records([refreshed])
                    return self._load_record(doi)
            return record
        record = self.lookup.lookup_by_doi(
            doi,
            pdf_path=pdf_path,
            pdf_header_text=pdf_header_text,
        )
        if record is None:
            raise IncomingProcessorError(f"无法通过 DOI 拉取元数据: {doi}")
        if not record.volume:
            record.volume = infer_annual_volume(record.source_title, record.year)
        self.store.upsert_records([record])
        return self._load_record(doi)

    def _load_record(self, doi: str) -> PaperRecord:
        records = self.store.load_paper_records(limit=1, doi=doi, unresolved_only=False)
        if not records:
            raise IncomingProcessorError(f"数据库中不存在 DOI: {doi}")
        return records[0]

    def _emit(
        self,
        progress_callback: Callable[[str], None] | None,
        message: str,
    ) -> None:
        if progress_callback is not None:
            progress_callback(message)


def should_refresh_existing_record(record: PaperRecord) -> bool:
    if not record.source_title:
        return True
    if not record.volume and infer_annual_volume(record.source_title, record.year):
        return True
    if record.source_title == record.title:
        return True
    normalized_source_title = normalize_known_source_title(record.source_title, doi=record.doi)
    if normalized_source_title and normalized_source_title != record.source_title:
        return True
    if resolve_journal_short_name(normalized_source_title or record.source_title) == "unknown_journal":
        return True
    return bool(
        record.document_type != DocumentType.JOURNAL and is_known_journal_doi(record.doi)
    )


def iter_incoming_pdfs(incoming_dir: Path) -> list[Path]:
    if not incoming_dir.exists():
        return []
    paths: list[Path] = []
    for root, _, filenames in os.walk(incoming_dir):
        root_path = Path(root)
        for filename in filenames:
            if filename.lower().endswith(".pdf"):
                paths.append(root_path / filename)
    return sorted(paths, key=lambda item: item.name.lower())


def identify_doi_from_filename(pdf_path: Path, store: IndexStore) -> str | None:
    stem = pdf_path.stem
    suffix = stem.rsplit("__", 1)[-1] if "__" in stem else ""
    if suffix:
        matched = store.get_paper_by_doi_suffix(suffix)
        if matched and matched.get("doi"):
            return str(matched["doi"])
        restored = restore_doi_from_suffix(suffix)
        if restored:
            return restored
    direct = first_plausible_doi(pdf_path.name.lower(), allow_contextless=True)
    if direct:
        return direct
    return None


def extract_doi_from_pdf(pdf_path: Path, *, max_pages: int = 3) -> str | None:
    doi, _ = extract_doi_and_header_text_from_pdf(pdf_path, max_pages=max_pages)
    return doi


def extract_doi_and_header_text_from_pdf(
    pdf_path: Path,
    *,
    max_pages: int = 3,
) -> tuple[str | None, str]:
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:  # pragma: no cover
        return None, ""

    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # pragma: no cover
            continue
    header_text = "\n".join(chunks)
    return extract_doi_from_text(header_text), header_text


def extract_doi_from_text(value: str) -> str | None:
    candidate = value.lower()
    doi = first_plausible_doi(candidate)
    if doi:
        return doi

    lines = candidate.splitlines()
    for index, line in enumerate(lines):
        windows = [line]
        if index + 1 < len(lines):
            windows.append(line + lines[index + 1])
        for window in windows:
            compact = sub(r"\s+", "", window)
            doi = first_plausible_doi(compact)
            if doi:
                return doi
    return None


def first_plausible_doi(value: str, *, allow_contextless: bool = False) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for match in finditer(DOI_REGEX, value, flags=IGNORECASE):
        doi = clean_doi_match(match.group(0))
        if is_plausible_doi(doi):
            score = score_doi_candidate(value, match.start(), doi)
            if score > 0 or allow_contextless:
                candidates.append((score, match.start(), doi))
    if not candidates:
        return None
    _, _, doi = max(candidates, key=lambda item: (item[0], -item[1]))
    return doi


def score_doi_candidate(value: str, start_index: int, doi: str) -> int:
    context_before = value[max(0, start_index - 140) : start_index]
    score = 0
    if any(
        marker in context_before
        for marker in ("doi", "doi.org", "dx.doi.org", "digital object identifier")
    ):
        score += 100
    if is_known_journal_doi(doi):
        score += 50
    return score


def is_plausible_doi(value: str) -> bool:
    normalized = clean_doi_match(value)
    if not fullmatch(DOI_REGEX, normalized, flags=IGNORECASE):
        return False
    _, suffix = normalized.split("/", 1)
    if not any(char.isdigit() for char in suffix):
        return False
    return True


def restore_doi_from_suffix(value: str) -> str | None:
    suffix = value.strip().lower().strip("-")
    if not suffix.startswith("10-"):
        return None

    exact_patterns: tuple[tuple[str, Callable[[tuple[str, ...]], str]], ...] = (
        (
            r"10-(7500)-(aeps[0-9a-z]+)",
            lambda groups: f"10.{groups[0]}/{groups[1]}",
        ),
        (
            r"10-(13335)-j-(1000)-(3673)-pst-(.+)",
            lambda groups: f"10.{groups[0]}/j.{groups[1]}-{groups[2]}.pst.{groups[3].replace('-', '.')}",
        ),
        (
            r"10-(13334)-j-(0258)-(8013)-pcsee-(.+)",
            lambda groups: f"10.{groups[0]}/j.{groups[1]}-{groups[2]}.pcsee.{groups[3].replace('-', '.')}",
        ),
        (
            r"10-(1109)-(.+)",
            lambda groups: f"10.{groups[0]}/{groups[1].replace('-', '.')}",
        ),
        (
            r"10-(1016)-j-([a-z0-9]+)-(.+)",
            lambda groups: f"10.{groups[0]}/j.{groups[1]}.{groups[2].replace('-', '.')}",
        ),
    )
    for pattern, builder in exact_patterns:
        match = fullmatch(pattern, suffix, flags=IGNORECASE)
        if match:
            restored = clean_doi_match(builder(match.groups()))
            return restored if is_plausible_doi(restored) else None
    generic = fullmatch(r"10-(\d{4,9})-([a-z0-9]+(?:-[a-z0-9]+)*)", suffix, flags=IGNORECASE)
    if generic:
        prefix, rest = generic.groups()
        restored = clean_doi_match(f"10.{prefix}/{rest.replace('-', '.')}")
        return restored if is_plausible_doi(restored) else None
    return None


def clean_doi_match(value: str) -> str:
    normalized = value.strip().rstrip(").,;]")
    if normalized.endswith("pdf"):
        normalized = normalized[:-3].rstrip(".")
    return normalized.lower()
