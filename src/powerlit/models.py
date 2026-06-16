from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class DocumentType(StrEnum):
    JOURNAL = "journal-article"
    CONFERENCE = "conference-paper"
    UNKNOWN = "unknown"


class AcquisitionMethod(StrEnum):
    OPENALEX_PDF = "openalex_pdf"
    OPENALEX_TEI = "openalex_tei"
    UNPAYWALL_PDF = "unpaywall_pdf"
    PUBLISHER_DIRECT = "publisher_direct"
    BROWSER_AGENT = "browser_agent"
    MANUAL = "manual"


class AcquisitionStage(StrEnum):
    METADATA_INDEXED = "metadata_indexed"
    FULLTEXT_RESOLVED = "fulltext_resolved"
    DOWNLOADED = "downloaded"
    PARSED = "parsed"
    MARKDOWN_BUILT = "markdown_built"
    ANALYZED = "analyzed"


class FullTextFormat(StrEnum):
    PDF = "pdf"
    TEI = "tei"
    XML = "xml"
    HTML = "html"


class FullTextCandidate(BaseModel):
    source: str
    method: AcquisitionMethod
    format: FullTextFormat
    url: str
    landing_page_url: str | None = None
    requires_browser: bool = False
    license: str | None = None
    host_type: str | None = None
    evidence: str | None = None

    @field_validator("source", "url", "landing_page_url", "license", "host_type", "evidence")
    @classmethod
    def normalize_candidate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


class Author(BaseModel):
    given: str | None = None
    family: str | None = None
    literal: str | None = None

    @field_validator("given", "family", "literal")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @property
    def full_name(self) -> str:
        if self.literal:
            return self.literal
        parts = [part for part in [self.given, self.family] if part]
        return " ".join(parts)


class PaperRecord(BaseModel):
    title: str
    authors: list[Author] = Field(default_factory=list)
    year: int | None = None
    published_date: date | None = None
    document_type: DocumentType = DocumentType.UNKNOWN
    source_title: str | None = None
    publisher: str | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    article_number: str | None = None
    doi: str | None = None
    abstract: str | None = None
    publisher_url: str | None = None
    query_pack: str | None = None
    source_providers: list[str] = Field(default_factory=list)
    researchgate_url: str | None = None
    researchgate_lookup_url: str | None = None
    researchgate_match_status: str | None = None
    acquisition_method: AcquisitionMethod | None = None
    acquisition_stage: AcquisitionStage = AcquisitionStage.METADATA_INDEXED
    acquisition_source_url: str | None = None
    download_status: str = "pending"
    local_pdf_path: str | None = None
    parsed_json_path: str | None = None
    parsed_md_path: str | None = None
    analysis_md_path: str | None = None
    analysis_json_path: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @field_validator("document_type", mode="before")
    @classmethod
    def normalize_document_type(cls, value: Any) -> Any:
        if isinstance(value, DocumentType):
            return value
        if value is None:
            return DocumentType.UNKNOWN
        normalized = str(value).strip().lower().replace("_", "-")
        if not normalized:
            return DocumentType.UNKNOWN
        legacy_aliases = {
            "journal": DocumentType.JOURNAL,
            "article": DocumentType.JOURNAL,
            "journal-article": DocumentType.JOURNAL,
            "conference": DocumentType.CONFERENCE,
            "conference-paper": DocumentType.CONFERENCE,
            "proceedings-article": DocumentType.CONFERENCE,
            "unknown": DocumentType.UNKNOWN,
        }
        return legacy_aliases.get(normalized, value)

    @field_validator(
        "title",
        "source_title",
        "publisher",
        "volume",
        "issue",
        "pages",
        "acquisition_source_url",
    )
    @classmethod
    def normalize_whitespace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None

    @field_validator("doi")
    @classmethod
    def normalize_doi(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        normalized = normalized.removeprefix("https://doi.org/")
        normalized = normalized.removeprefix("http://doi.org/")
        normalized = normalized.removeprefix("doi:")
        normalized = normalized.strip()
        return normalized.lower() or None

    @property
    def dedupe_key(self) -> str:
        if self.doi:
            return f"doi:{self.doi.lower()}"
        normalized_title = "".join(ch.lower() for ch in self.title if ch.isalnum())
        year = self.year or 0
        return f"title:{normalized_title}:{year}"

    def export_row(self, citation: str) -> dict[str, str]:
        return {
            "title": self.title,
            "gbt7714_citation": citation,
            "doi": self.doi or "",
            "publisher_url": self.publisher_url or "",
            "researchgate_url": self.researchgate_url or "",
            "researchgate_lookup_url": self.researchgate_lookup_url or "",
            "researchgate_match_status": self.researchgate_match_status or "",
            "acquisition_method": self.acquisition_method.value if self.acquisition_method else "",
            "acquisition_stage": self.acquisition_stage.value,
            "acquisition_source_url": self.acquisition_source_url or "",
            "download_status": self.download_status,
            "local_pdf_path": self.local_pdf_path or "",
            "parsed_json_path": self.parsed_json_path or "",
            "parsed_md_path": self.parsed_md_path or "",
            "analysis_md_path": self.analysis_md_path or "",
            "analysis_json_path": self.analysis_json_path or "",
            "providers": ",".join(sorted(self.source_providers)),
            "year": str(self.year or ""),
            "document_type": self.document_type.value,
            "source_title": self.source_title or "",
            "query_pack": self.query_pack or "",
        }


class QuerySpec(BaseModel):
    name: str
    query: str
    providers: list[str] = Field(default_factory=lambda: ["crossref", "openalex"])
    limit: int = 20
    from_date: date | None = None
    until_date: date | None = None


class QueryBundle(BaseModel):
    defaults: QuerySpec | None = None
    queries: list[QuerySpec]


class JournalSpec(BaseModel):
    short_name: str
    title: str
    issns: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=lambda: ["crossref", "openalex"])
    from_year: int | None = None
    until_year: int | None = None
    limit: int = 200

    @field_validator("short_name", "title")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        return normalized

    @field_validator("issns")
    @classmethod
    def normalize_issns(cls, value: list[str]) -> list[str]:
        cleaned = ["".join(item.strip().split()) for item in value if "".join(item.strip().split())]
        return cleaned


class JournalBundle(BaseModel):
    journals: list[JournalSpec]


class SearchResponse(BaseModel):
    query: QuerySpec
    results: list[PaperRecord]


class AnalysisEvidence(BaseModel):
    claim: str
    support: str

    @field_validator("claim", "support")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        return normalized or "unknown"


class PaperAnalysis(BaseModel):
    title: str
    source_basis: str
    research_problem: str
    power_system_context: str
    methods: list[str] = Field(default_factory=list)
    data_and_case_studies: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    relevance: str
    keywords: list[str] = Field(default_factory=list)
    evidence_items: list[AnalysisEvidence] = Field(default_factory=list)
    caution: str

    @field_validator(
        "title",
        "source_basis",
        "research_problem",
        "power_system_context",
        "relevance",
        "caution",
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        return normalized or "unknown"

    @field_validator(
        "methods",
        "data_and_case_studies",
        "key_findings",
        "limitations",
        "keywords",
    )
    @classmethod
    def normalize_list(cls, value: list[str]) -> list[str]:
        normalized = [" ".join(item.split()) for item in value if " ".join(item.split())]
        return normalized or ["unknown"]


class AnalysisRequest(BaseModel):
    doi: str
    source_text: str | None = None


class AnalysisResponse(BaseModel):
    doi: str
    markdown_path: str | None = None
    json_path: str
    analysis: PaperAnalysis


class WorkspaceCreateRequest(BaseModel):
    name: str
    description: str | None = None

    @field_validator("name", "description")
    @classmethod
    def normalize_workspace_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        return normalized or None


class WorkspaceAddRequest(BaseModel):
    dois: list[str] = Field(default_factory=list)
    query_packs: list[str] = Field(default_factory=list)

    @field_validator("dois", "query_packs")
    @classmethod
    def normalize_workspace_lists(cls, values: list[str]) -> list[str]:
        normalized = [" ".join(item.split()) for item in values if " ".join(item.split())]
        return normalized


class PaperCardBuildRequest(BaseModel):
    doi: str
    force: bool = False
