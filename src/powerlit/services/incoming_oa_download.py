from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from powerlit.models import DocumentType, PaperRecord
from powerlit.providers.base import ProviderError
from powerlit.providers.openalex import OpenAlexProvider
from powerlit.services.fulltext_resolver import FullTextResolver
from powerlit.services.incoming_processor import (
    extract_doi_from_pdf,
    identify_doi_from_filename,
    iter_incoming_pdfs,
)
from powerlit.services.index import IndexStore
from powerlit.services.journal_issue_catalog import ISSUE_CATALOG_JSON_FILENAME
from powerlit.services.library_layout import infer_annual_volume, resolve_journal_short_name
from powerlit.services.metadata_lookup import MetadataLookupService
from powerlit.services.oa_download import OADownloadService
from powerlit.services.search import merge_records
from powerlit.settings import Settings


@dataclass(slots=True)
class OAIncomingCandidate:
    doi: str
    title: str
    source_title: str | None
    journal_short_name: str
    year: int | None
    volume: str | None
    issue: str | None
    publisher_url: str | None
    issue_catalog_path: Path


@dataclass(slots=True)
class OAIncomingDownloadOutcome:
    candidate: OAIncomingCandidate
    status: str
    path: Path | None = None
    source_url: str | None = None
    downloaded: bool = False
    message: str | None = None


class OAIncomingDownloadService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = IndexStore(settings)
        self.lookup = MetadataLookupService(settings)
        self.openalex = OpenAlexProvider(settings)
        self.resolver = FullTextResolver(settings)
        self.downloader = OADownloadService(settings)

    def discover_issue_catalog_candidates(
        self,
        *,
        journals: list[str] | None = None,
        from_year: int | None = None,
        until_year: int | None = None,
        doi: str | None = None,
        limit: int | None = None,
    ) -> list[OAIncomingCandidate]:
        normalized_doi = normalize_doi(doi)
        journal_filters = {normalize_journal_filter(item) for item in (journals or []) if item}
        candidates: list[OAIncomingCandidate] = []
        seen_dois: set[str] = set()
        for path in sorted(self.settings.reference_dir.rglob(ISSUE_CATALOG_JSON_FILENAME)):
            payload = json.loads(path.read_text(encoding="utf-8"))
            source_title = string_or_none(payload.get("source_title"))
            journal_short_name = string_or_none(payload.get("journal_short_name")) or resolve_journal_short_name(
                source_title
            )
            if journal_filters and not matches_journal_filter(
                journal_short_name=journal_short_name,
                source_title=source_title,
                journal_filters=journal_filters,
            ):
                continue
            year = parse_year(payload.get("year"))
            if from_year is not None and (year is None or year < from_year):
                continue
            if until_year is not None and (year is None or year > until_year):
                continue
            for article in payload.get("articles") or []:
                if article.get("is_open_access") is not True:
                    continue
                article_doi = normalize_doi(article.get("doi"))
                if not article_doi:
                    continue
                if normalized_doi and article_doi != normalized_doi:
                    continue
                if article_doi in seen_dois:
                    continue
                seen_dois.add(article_doi)
                candidates.append(
                    OAIncomingCandidate(
                        doi=article_doi,
                        title=string_or_none(article.get("title")) or article_doi,
                        source_title=source_title,
                        journal_short_name=journal_short_name,
                        year=year,
                        volume=string_or_none(payload.get("volume")),
                        issue=string_or_none(payload.get("issue")),
                        publisher_url=string_or_none(article.get("publisher_url")),
                        issue_catalog_path=path,
                    )
                )
                if limit is not None and len(candidates) >= limit:
                    return candidates
        return candidates

    def collect_existing_incoming_dois(self) -> set[str]:
        existing_dois: set[str] = set()
        for pdf_path in iter_incoming_pdfs(self.settings.incoming_pdf_dir):
            doi = identify_doi_from_filename(pdf_path, self.store) or extract_doi_from_pdf(pdf_path)
            if doi:
                existing_dois.add(doi.strip().lower())
        return existing_dois

    def download_candidate(
        self,
        candidate: OAIncomingCandidate,
        *,
        existing_incoming_dois: set[str],
        refresh_metadata: bool = True,
    ) -> OAIncomingDownloadOutcome:
        if candidate.doi in existing_incoming_dois:
            return OAIncomingDownloadOutcome(
                candidate=candidate,
                status="already_in_incoming",
            )

        existing_row = self.store.get_paper_by_doi(candidate.doi)
        if existing_row and existing_row.get("local_pdf_path"):
            local_pdf_path = Path(str(existing_row["local_pdf_path"]))
            if local_pdf_path.exists():
                return OAIncomingDownloadOutcome(
                    candidate=candidate,
                    status="already_in_library",
                    path=local_pdf_path,
                )

        record = self.prepare_record(candidate, refresh_metadata=refresh_metadata)
        result = self.downloader.download_record_to_directory(record, self.settings.incoming_pdf_dir)
        if result is None:
            return OAIncomingDownloadOutcome(
                candidate=candidate,
                status="no_pdf_candidate",
            )
        existing_incoming_dois.add(candidate.doi)
        return OAIncomingDownloadOutcome(
            candidate=candidate,
            status="downloaded" if result.downloaded else "already_in_incoming",
            path=result.path,
            source_url=result.source_url,
            downloaded=result.downloaded,
        )

    def prepare_record(
        self,
        candidate: OAIncomingCandidate,
        *,
        refresh_metadata: bool,
    ) -> PaperRecord:
        existing_records = self.store.load_paper_records(
            limit=1,
            doi=candidate.doi,
            unresolved_only=False,
        )
        candidate_record = build_candidate_record(candidate)
        record = existing_records[0] if existing_records else candidate_record
        record = merge_records(record, candidate_record)
        record = apply_issue_catalog_priority(record, candidate_record)
        if refresh_metadata:
            crossref_record = self.lookup.lookup_by_doi(
                candidate.doi,
                query_pack=candidate.journal_short_name,
            )
            if crossref_record is not None:
                record = merge_records(record, crossref_record)
            try:
                openalex_record = self.openalex.lookup_by_doi(
                    candidate.doi,
                    query_pack=candidate.journal_short_name,
                )
            except ProviderError:
                openalex_record = None
            if openalex_record is not None:
                record = merge_records(record, openalex_record)
        resolved_record = self.resolver.resolve_record(record)
        resolved_record = apply_issue_catalog_priority(resolved_record, candidate_record)
        if not resolved_record.volume:
            resolved_record.volume = infer_annual_volume(
                resolved_record.source_title,
                resolved_record.year,
            )
        self.store.upsert_records([resolved_record])
        refreshed_records = self.store.load_paper_records(
            limit=1,
            doi=candidate.doi,
            unresolved_only=False,
        )
        return refreshed_records[0] if refreshed_records else resolved_record


def build_candidate_record(candidate: OAIncomingCandidate) -> PaperRecord:
    return PaperRecord(
        title=candidate.title,
        year=candidate.year,
        document_type=DocumentType.JOURNAL,
        source_title=candidate.source_title,
        volume=candidate.volume,
        issue=candidate.issue,
        doi=candidate.doi,
        publisher_url=candidate.publisher_url,
        query_pack=candidate.journal_short_name,
        source_providers=["issue_catalog"],
        raw={
            "issue_catalog_oa": {
                "issue_catalog_path": str(candidate.issue_catalog_path.resolve()),
                "journal_short_name": candidate.journal_short_name,
            }
        },
    )


def apply_issue_catalog_priority(record: PaperRecord, candidate_record: PaperRecord) -> PaperRecord:
    if candidate_record.source_title:
        record.source_title = candidate_record.source_title
    if candidate_record.year:
        record.year = candidate_record.year
    if candidate_record.volume:
        record.volume = candidate_record.volume
    if candidate_record.issue:
        record.issue = candidate_record.issue
    return record


def matches_journal_filter(
    *,
    journal_short_name: str | None,
    source_title: str | None,
    journal_filters: set[str],
) -> bool:
    normalized_short_name = normalize_journal_filter(journal_short_name)
    normalized_source_title = normalize_journal_filter(source_title)
    return (
        normalized_short_name in journal_filters
        or normalized_source_title in journal_filters
    )


def normalize_journal_filter(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    normalized = normalized.removeprefix("doi:")
    normalized = normalized.strip()
    return normalized.lower() or None


def string_or_none(value) -> str | None:  # noqa: ANN001
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def parse_year(value) -> int | None:  # noqa: ANN001
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
