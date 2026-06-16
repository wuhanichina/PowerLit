from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from shutil import move

from powerlit.models import DocumentType, PaperRecord
from powerlit.services.ai_analysis import build_analysis_output_base
from powerlit.services.directory_audit import DirectoryAuditService
from powerlit.services.index import IndexStore
from powerlit.services.journal_issue_catalog import (
    ISSUE_CATALOG_JSON_FILENAME,
)
from powerlit.services.library_layout import (
    build_library_location,
    build_reference_pdf_path,
    canonicalize_source_title,
    infer_annual_volume,
    is_known_journal_doi,
    normalize_known_source_title,
    resolve_journal_short_name,
)
from powerlit.services.metadata_lookup import MetadataLookupService, build_fallback_record_from_pdf
from powerlit.services.pdf_parser import build_output_paths
from powerlit.settings import Settings


class MetadataRepairError(RuntimeError):
    """Raised when a stored metadata/path repair cannot be completed."""


@dataclass(slots=True)
class MetadataRepairCandidate:
    record: PaperRecord
    reasons: list[str]
    issue_catalog_match: IssueCatalogArticleMatch | None = None
    observed_pdf_title: str | None = None


@dataclass(slots=True)
class MetadataRepairCandidateCollection:
    candidates: list[MetadataRepairCandidate]
    skipped_without_attached_pdf: int = 0


@dataclass(slots=True)
class MetadataRepairResult:
    doi: str
    reasons: list[str]
    old_title: str
    new_title: str
    old_source_title: str | None
    new_source_title: str | None
    old_pdf_path: Path | None
    new_pdf_path: Path | None
    moved_paths: list[tuple[Path, Path]] = field(default_factory=list)


@dataclass(slots=True)
class IssueCatalogArticleMatch:
    doi: str
    title: str
    source_title: str | None
    year: int | None
    volume: str | None
    issue: str | None
    issue_catalog_path: Path


class IndexedMetadataRepairService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = IndexStore(settings)
        self.lookup = MetadataLookupService(settings)
        self.directory_audit = DirectoryAuditService(settings)
        self._issue_catalog_by_doi: dict[str, IssueCatalogArticleMatch] | None = None

    def collect_candidates(
        self,
        *,
        limit: int,
        query_pack: str | None = None,
        doi: str | None = None,
        attached_only: bool = True,
    ) -> list[MetadataRepairCandidate]:
        return self.collect_candidates_with_stats(
            limit=limit,
            query_pack=query_pack,
            doi=doi,
            attached_only=attached_only,
        ).candidates

    def collect_candidates_with_stats(
        self,
        *,
        limit: int,
        query_pack: str | None = None,
        doi: str | None = None,
        attached_only: bool = True,
    ) -> MetadataRepairCandidateCollection:
        records = self.store.load_paper_records(
            limit=limit,
            query_pack=query_pack,
            unresolved_only=False,
            doi=doi,
        )
        candidates: list[MetadataRepairCandidate] = []
        skipped_without_attached_pdf = 0
        for record in records:
            if attached_only and not has_attached_pdf(record):
                skipped_without_attached_pdf += 1
                continue
            observed_pdf_title = self.extract_observed_pdf_title(record)
            issue_catalog_match = self.lookup_issue_catalog_match_for_record(
                record,
                observed_pdf_title=observed_pdf_title,
            )
            reasons = detect_metadata_repair_reasons(
                record,
                issue_catalog_match=issue_catalog_match,
                observed_pdf_title=observed_pdf_title,
            )
            reasons = merge_reason_lists(
                reasons,
                detect_additional_metadata_repair_reasons(
                    record,
                    issue_catalog_match=issue_catalog_match,
                ),
            )
            if doi or should_repair_record(reasons):
                candidates.append(
                    MetadataRepairCandidate(
                        record=record,
                        reasons=reasons or ["manual_request"],
                        issue_catalog_match=issue_catalog_match,
                        observed_pdf_title=observed_pdf_title,
                    )
                )
        return MetadataRepairCandidateCollection(
            candidates=candidates,
            skipped_without_attached_pdf=skipped_without_attached_pdf,
        )

    def repair_candidate(
        self,
        candidate: MetadataRepairCandidate,
        *,
        dry_run: bool = False,
    ) -> MetadataRepairResult:
        record = candidate.record
        if not record.doi:
            raise MetadataRepairError("Cannot repair a record without DOI.")
        if not record.local_pdf_path:
            raise MetadataRepairError(f"Record has no attached PDF: {record.doi}")

        source_pdf_path = Path(record.local_pdf_path)
        if not source_pdf_path.exists():
            raise MetadataRepairError(f"Attached PDF does not exist: {source_pdf_path}")

        refreshed = self.lookup.lookup_by_doi(
            record.doi,
            query_pack=record.query_pack or "incoming_pdf_auto",
            pdf_path=source_pdf_path,
        )
        if refreshed is None:
            raise MetadataRepairError(f"Could not rebuild metadata for DOI: {record.doi}")

        refreshed.source_title = (
            normalize_known_source_title(refreshed.source_title, doi=refreshed.doi)
            or normalize_known_source_title(record.source_title, doi=record.doi)
            or canonicalize_source_title(record.source_title)
        )
        if is_known_journal_doi(refreshed.doi):
            refreshed.document_type = DocumentType.JOURNAL
        if not refreshed.year:
            refreshed.year = record.year
        if not refreshed.volume:
            refreshed.volume = record.volume
        if not refreshed.issue:
            refreshed.issue = record.issue
        if not refreshed.volume:
            refreshed.volume = infer_annual_volume(refreshed.source_title, refreshed.year)

        issue_catalog_match = candidate.issue_catalog_match or self.lookup_issue_catalog_match_for_record(
            record,
            observed_pdf_title=candidate.observed_pdf_title,
        )
        if issue_catalog_match is not None:
            refreshed = apply_issue_catalog_match(refreshed, issue_catalog_match)

        refreshed.query_pack = record.query_pack
        refreshed.acquisition_method = record.acquisition_method
        refreshed.acquisition_stage = record.acquisition_stage
        refreshed.download_status = record.download_status
        refreshed.local_pdf_path = record.local_pdf_path
        refreshed.parsed_md_path = record.parsed_md_path
        refreshed.analysis_md_path = record.analysis_md_path
        refreshed.analysis_json_path = record.analysis_json_path

        new_pdf_path = build_reference_pdf_path(
            self.settings.reference_dir,
            title=refreshed.title,
            doi=refreshed.doi,
            source_title=refreshed.source_title,
            year=refreshed.year,
            volume=refreshed.volume,
            issue=refreshed.issue,
            original_filename=source_pdf_path.name,
        ).resolve()

        parsed_paths = build_output_paths(self.settings, refreshed)
        analysis_base = build_analysis_output_base(self.settings.analysis_output_dir, refreshed)
        new_analysis_md_path = analysis_base.with_suffix(".md")
        new_analysis_json_path = analysis_base.with_suffix(".json")

        moves: list[tuple[Path, Path]] = []
        touched_dirs: set[Path] = set()

        self._relocate(source_pdf_path, new_pdf_path, dry_run=dry_run, moves=moves, touched_dirs=touched_dirs)

        old_note_path = existing_path(record.parsed_md_path)
        old_proofread_md_path = derive_old_proofread_markdown_path(record)
        old_proofread_json_path = derive_old_proofread_json_path(record)
        old_analysis_md_path = existing_path(record.analysis_md_path)
        old_analysis_json_path = existing_path(record.analysis_json_path)

        self._relocate(old_note_path, parsed_paths.markdown_path, dry_run=dry_run, moves=moves, touched_dirs=touched_dirs)
        self._relocate(
            old_proofread_md_path,
            parsed_paths.proofread_markdown_path,
            dry_run=dry_run,
            moves=moves,
            touched_dirs=touched_dirs,
        )
        self._relocate(
            old_proofread_json_path,
            parsed_paths.proofread_json_path,
            dry_run=dry_run,
            moves=moves,
            touched_dirs=touched_dirs,
        )
        self._relocate(
            old_analysis_md_path,
            new_analysis_md_path,
            dry_run=dry_run,
            moves=moves,
            touched_dirs=touched_dirs,
        )
        self._relocate(
            old_analysis_json_path,
            new_analysis_json_path,
            dry_run=dry_run,
            moves=moves,
            touched_dirs=touched_dirs,
        )

        repaired = MetadataRepairResult(
            doi=record.doi,
            reasons=candidate.reasons,
            old_title=record.title,
            new_title=refreshed.title,
            old_source_title=record.source_title,
            new_source_title=refreshed.source_title,
            old_pdf_path=source_pdf_path,
            new_pdf_path=new_pdf_path,
            moved_paths=moves,
        )

        if dry_run:
            return repaired

        refreshed.local_pdf_path = str(new_pdf_path)
        refreshed.parsed_md_path = str(parsed_paths.markdown_path) if parsed_paths.markdown_path.exists() else None
        refreshed.analysis_md_path = str(new_analysis_md_path) if new_analysis_md_path.exists() else None
        refreshed.analysis_json_path = str(new_analysis_json_path) if new_analysis_json_path.exists() else None
        self.store.upsert_records([refreshed])
        self.store.update_artifact_paths(
            record.doi,
            local_pdf_path=new_pdf_path,
            parsed_md_path=parsed_paths.markdown_path if parsed_paths.markdown_path.exists() else None,
            analysis_md_path=new_analysis_md_path if new_analysis_md_path.exists() else None,
            analysis_json_path=new_analysis_json_path if new_analysis_json_path.exists() else None,
        )
        for source_path, _destination_path in moves:
            prune_empty_parent_directories(
                source_path.parent,
                stop_roots=(self.settings.reference_dir, self.settings.md_dir),
            )

        for directory in touched_dirs:
            if directory.exists() and is_path_within(directory, self.settings.md_dir):
                self.directory_audit.update_directory_summary(directory)

        return repaired

    def _relocate(
        self,
        source: Path | None,
        destination: Path,
        *,
        dry_run: bool,
        moves: list[tuple[Path, Path]],
        touched_dirs: set[Path],
    ) -> None:
        if source is None or not source.exists():
            return

        source_resolved = source.resolve()
        destination_resolved = destination.resolve()
        if source_resolved == destination_resolved:
            touched_dirs.add(destination_resolved.parent)
            return

        moves.append((source_resolved, destination_resolved))
        touched_dirs.add(source_resolved.parent)
        touched_dirs.add(destination_resolved.parent)
        if dry_run:
            return

        destination_resolved.parent.mkdir(parents=True, exist_ok=True)
        if destination_resolved.exists():
            destination_resolved.unlink()
        move(str(source_resolved), str(destination_resolved))

    def lookup_issue_catalog_match(self, doi: str | None) -> IssueCatalogArticleMatch | None:
        normalized_doi = normalize_doi(doi)
        if not normalized_doi:
            return None
        if self._issue_catalog_by_doi is None:
            self._issue_catalog_by_doi = self.load_issue_catalog_index()
        return self._issue_catalog_by_doi.get(normalized_doi)

    def lookup_issue_catalog_match_for_record(
        self,
        record: PaperRecord,
        *,
        observed_pdf_title: str | None = None,
    ) -> IssueCatalogArticleMatch | None:
        match = self.lookup_issue_catalog_match(record.doi)
        if match is not None:
            return match
        return self.lookup_issue_catalog_title_match(record, observed_pdf_title=observed_pdf_title)

    def lookup_issue_catalog_title_match(
        self,
        record: PaperRecord,
        *,
        observed_pdf_title: str | None = None,
    ) -> IssueCatalogArticleMatch | None:
        candidate_titles = [
            observed_pdf_title,
            record.title,
        ]
        title_candidates = [title for title in candidate_titles if normalize_title_key(title)]
        if not title_candidates:
            return None

        matches: list[tuple[int, IssueCatalogArticleMatch]] = []
        for issue_path in self.iter_issue_catalog_paths_for_record(record):
            payload = read_json_payload(issue_path)
            if not payload:
                continue
            source_title = string_or_none(payload.get("source_title"))
            year = payload.get("year")
            volume = string_or_none(payload.get("volume"))
            issue = string_or_none(payload.get("issue"))
            for article in payload.get("articles") or []:
                article_title = string_or_none(article.get("title"))
                if not article_title:
                    continue
                score = max(
                    title_match_score(title_candidate, article_title)
                    for title_candidate in title_candidates
                )
                if score <= 0:
                    continue
                matches.append(
                    (
                        score,
                        IssueCatalogArticleMatch(
                            doi=normalize_doi(record.doi) or "",
                            title=article_title,
                            source_title=source_title,
                            year=year if isinstance(year, int) else None,
                            volume=volume,
                            issue=issue,
                            issue_catalog_path=issue_path,
                        ),
                    )
                )

        if not matches:
            return None
        matches.sort(
            key=lambda item: (item[0], *issue_catalog_match_priority(item[1])),
            reverse=True,
        )
        best_score = matches[0][0]
        best_matches = [match for score, match in matches if score == best_score]
        if len(best_matches) != 1:
            return None
        return best_matches[0]

    def extract_observed_pdf_title(self, record: PaperRecord) -> str | None:
        pdf_path = existing_path(record.local_pdf_path)
        normalized_doi = normalize_doi(record.doi)
        if pdf_path is None or not pdf_path.exists() or not normalized_doi:
            return None
        fallback_record = build_fallback_record_from_pdf(
            pdf_path,
            normalized_doi,
            record.query_pack or "incoming_pdf_auto",
        )
        if fallback_record is None:
            return None
        return fallback_record.title or None

    def iter_issue_catalog_paths_for_record(self, record: PaperRecord) -> list[Path]:
        candidates: list[Path] = []
        seen_paths: set[Path] = set()
        if record.source_title and (record.volume or record.issue):
            location = build_library_location(
                self.settings.reference_dir,
                source_title=record.source_title,
                volume=record.volume,
                issue=record.issue,
                year=record.year,
            )
            issue_path = location.directory / ISSUE_CATALOG_JSON_FILENAME
            if issue_path.exists():
                resolved_path = issue_path.resolve()
                seen_paths.add(resolved_path)
                candidates.append(resolved_path)

        journal_short_name = resolve_journal_short_name(record.source_title)
        journal_dir = self.settings.reference_dir / journal_short_name
        if journal_dir.exists():
            for issue_path in sorted(journal_dir.rglob(ISSUE_CATALOG_JSON_FILENAME)):
                resolved_path = issue_path.resolve()
                if resolved_path in seen_paths:
                    continue
                seen_paths.add(resolved_path)
                candidates.append(resolved_path)
        return candidates

    def load_issue_catalog_index(self) -> dict[str, IssueCatalogArticleMatch]:
        issue_payloads: list[tuple[Path, dict]] = []
        seen_paths: set[Path] = set()
        for journal_dir in sorted(path for path in self.settings.reference_dir.iterdir() if path.is_dir()):
            for issue_path in sorted(journal_dir.rglob(ISSUE_CATALOG_JSON_FILENAME)):
                resolved_path = issue_path.resolve()
                if resolved_path in seen_paths:
                    continue
                payload = read_json_payload(resolved_path)
                if not payload:
                    continue
                seen_paths.add(resolved_path)
                issue_payloads.append((resolved_path, payload))

        index: dict[str, IssueCatalogArticleMatch] = {}
        for issue_path, payload in issue_payloads:
            source_title = string_or_none(payload.get("source_title"))
            year = payload.get("year")
            volume = string_or_none(payload.get("volume"))
            issue = string_or_none(payload.get("issue"))
            for article in payload.get("articles") or []:
                normalized_doi = normalize_doi(article.get("doi"))
                title = string_or_none(article.get("title"))
                if not normalized_doi or not title:
                    continue
                candidate = IssueCatalogArticleMatch(
                    doi=normalized_doi,
                    title=title,
                    source_title=source_title,
                    year=year if isinstance(year, int) else None,
                    volume=volume,
                    issue=issue,
                    issue_catalog_path=issue_path,
                )
                existing = index.get(normalized_doi)
                if existing is None or issue_catalog_match_priority(candidate) > issue_catalog_match_priority(existing):
                    index[normalized_doi] = candidate
        return index


def detect_metadata_repair_reasons(
    record: PaperRecord,
    *,
    issue_catalog_match: IssueCatalogArticleMatch | None = None,
    observed_pdf_title: str | None = None,
) -> list[str]:
    reasons: list[str] = []
    title = record.title or ""
    source_title = record.source_title or ""
    providers = {item.lower() for item in record.source_providers}
    raw = record.raw or {}

    if "pdf_fallback" in providers or raw.get("metadata_fallback") == "pdf_first_pages":
        reasons.append("pdf_fallback_metadata")
    if len(title) > 80:
        reasons.append("title_too_long")
    if "。" in title or title.count("，") >= 3:
        reasons.append("title_looks_like_body_text")
    if title.lower().startswith(("doi:", "doi：")) or "文章编号" in title or "文献标识码" in title:
        reasons.append("title_contains_metadata")
    if source_title and source_title == title:
        reasons.append("title_equals_source_title")
    if looks_like_article_title(source_title) and source_title not in known_journal_titles():
        reasons.append("source_title_looks_like_article_title")
    if issue_catalog_match is not None:
        if not titles_match(title, issue_catalog_match.title):
            reasons.append("issue_catalog_title_mismatch")
        if source_title and issue_catalog_match.source_title and not titles_match(source_title, issue_catalog_match.source_title):
            reasons.append("issue_catalog_source_title_mismatch")
        if record.volume and issue_catalog_match.volume and normalize_numeric_text(record.volume) != normalize_numeric_text(issue_catalog_match.volume):
            reasons.append("issue_catalog_volume_mismatch")
        if record.issue and issue_catalog_match.issue and normalize_numeric_text(record.issue) != normalize_numeric_text(issue_catalog_match.issue):
            reasons.append("issue_catalog_issue_mismatch")
        if observed_pdf_title and titles_match(observed_pdf_title, issue_catalog_match.title):
            reasons.append("pdf_title_matches_issue_catalog")
        elif observed_pdf_title and not titles_match(observed_pdf_title, issue_catalog_match.title):
            reasons.append("pdf_title_mismatch_issue_catalog")
    return sorted(set(reasons))


def should_repair_record(reasons: list[str]) -> bool:
    ignored = {"pdf_fallback_metadata", "pdf_title_matches_issue_catalog"}
    actionable = [reason for reason in reasons if reason not in ignored]
    return bool(actionable)


def merge_reason_lists(*groups: list[str]) -> list[str]:
    merged: set[str] = set()
    for group in groups:
        merged.update(group)
    return sorted(merged)


def detect_additional_metadata_repair_reasons(
    record: PaperRecord,
    *,
    issue_catalog_match: IssueCatalogArticleMatch | None = None,
) -> list[str]:
    reasons: list[str] = []
    source_title = record.source_title or ""
    normalized_source_title = normalize_known_source_title(source_title, doi=record.doi)

    if not source_title:
        reasons.append("source_title_missing")
    elif normalized_source_title and normalized_source_title != source_title:
        reasons.append("source_title_needs_canonicalization")
    elif resolve_journal_short_name(normalized_source_title or source_title) == "unknown_journal":
        reasons.append("source_title_unknown_journal")

    if record.document_type != DocumentType.JOURNAL and is_known_journal_doi(record.doi):
        reasons.append("known_journal_doi_wrong_document_type")

    if issue_catalog_match is not None:
        if not source_title and issue_catalog_match.source_title:
            reasons.append("issue_catalog_source_title_missing")
        if not record.volume and issue_catalog_match.volume:
            reasons.append("issue_catalog_volume_missing")
        if not record.issue and issue_catalog_match.issue:
            reasons.append("issue_catalog_issue_missing")
    return reasons


def looks_like_article_title(value: str | None) -> bool:
    if not value:
        return False
    if len(value) > 50:
        return True
    if any(token in value for token in ("方法", "模型", "研究", "分析", "优化", "控制", "调度", "预测")):
        return True
    return False


def known_journal_titles() -> set[str]:
    return {
        "电力系统自动化",
        "电网技术",
        "中国电机工程学报",
        "Proceedings of the CSEE",
        "Journal of Modern Power Systems and Clean Energy",
        "IEEE Transactions on Power Systems",
        "IEEE Transactions on Power Delivery",
        "IEEE Transactions on Smart Grid",
        "IEEE Transactions on Sustainable Energy",
        "IEEE Access",
        "Applied Energy",
        "Energy",
        "Electric Power Systems Research",
        "International Journal of Electrical Power & Energy Systems",
    }


def existing_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value)


def derive_old_proofread_markdown_path(record: PaperRecord) -> Path | None:
    if not record.parsed_md_path:
        return None
    note_path = Path(record.parsed_md_path)
    return note_path.parent / f"{note_path.stem}.proofread.md"


def derive_old_proofread_json_path(record: PaperRecord) -> Path | None:
    if not record.parsed_md_path:
        return None
    note_path = Path(record.parsed_md_path)
    return note_path.parent / f"{note_path.stem}.proofread.json"


def is_path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def prune_empty_parent_directories(directory: Path, *, stop_roots: tuple[Path, ...]) -> None:
    current = directory.resolve()
    resolved_stop_roots = tuple(root.resolve() for root in stop_roots)
    while True:
        stop_root = next(
            (root for root in resolved_stop_roots if is_path_within(current, root)),
            None,
        )
        if stop_root is None or current == stop_root or not current.exists():
            return
        try:
            next(current.iterdir())
        except StopIteration:
            try:
                current.rmdir()
            except OSError:
                return
            current = current.parent
            continue
        except OSError:
            return
        return


def has_attached_pdf(record: PaperRecord) -> bool:
    pdf_path = existing_path(record.local_pdf_path)
    return pdf_path is not None and pdf_path.exists()


def apply_issue_catalog_match(record: PaperRecord, match: IssueCatalogArticleMatch) -> PaperRecord:
    record.title = match.title
    if match.source_title:
        record.source_title = match.source_title
    if match.year:
        record.year = match.year
    if match.volume:
        record.volume = match.volume
    if match.issue:
        record.issue = match.issue
    providers = {item.lower() for item in record.source_providers}
    if "issue_catalog" not in providers:
        record.source_providers.append("issue_catalog")
    record.raw = {
        **record.raw,
        "issue_catalog_match": {
            "title": match.title,
            "source_title": match.source_title,
            "year": match.year,
            "volume": match.volume,
            "issue": match.issue,
            "issue_catalog_path": str(match.issue_catalog_path),
        },
    }
    return record


def read_json_payload(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    normalized = normalized.removeprefix("doi:")
    normalized = normalized.strip().lower()
    return normalized or None


def normalize_title_key(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def titles_match(left: str | None, right: str | None) -> bool:
    return title_match_score(left, right) > 0


def title_match_score(left: str | None, right: str | None) -> int:
    left_key = normalize_title_key(left)
    right_key = normalize_title_key(right)
    if not left_key or not right_key:
        return 0
    if left_key == right_key:
        return 2
    if left_key in right_key or right_key in left_key:
        return 1
    left_cjk_key = normalize_cjk_title_key(left)
    right_cjk_key = normalize_cjk_title_key(right)
    if len(left_cjk_key) >= 6 and len(right_cjk_key) >= 6:
        if left_cjk_key == right_cjk_key:
            return 2
        if left_cjk_key in right_cjk_key or right_cjk_key in left_cjk_key:
            return 1
    return 0


def normalize_cjk_title_key(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value if "\u4e00" <= ch <= "\u9fff")


def normalize_numeric_text(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value if ch.isdigit())


def string_or_none(value) -> str | None:  # noqa: ANN001
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def issue_catalog_match_priority(match: IssueCatalogArticleMatch) -> tuple[int, int, int]:
    return (
        1 if match.volume else 0,
        1 if match.issue else 0,
        1 if match.source_title else 0,
    )
