from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MINERU_RUNTIME_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "powerlit-mineru"


class Settings(BaseSettings):
    crossref_mailto: str | None = None
    unpaywall_email: str | None = None
    ieee_api_key: str | None = None
    elsevier_api_key: str | None = None
    elsevier_insttoken: str | None = None
    serpapi_api_key: str | None = None
    dashscope_api_key: str | None = None
    ai_provider: str = "siliconflow"
    ai_base_url: str = "https://api.siliconflow.cn/v1"
    ai_api_key: str | None = None
    ai_model: str = "Qwen/Qwen2.5-72B-Instruct"
    semantic_scholar_api_key: str | None = None
    openscholar_pes2o_url: str | None = None
    ai_temperature: float = 0.1
    ai_timeout: float | None = None
    ai_note_timeout: float | None = 600.0
    ai_source_char_limit: int = 16000
    ai_note_source_char_limit: int = 90000
    ai_note_chunk_char_limit: int = 6000
    note_review_enabled: bool = True
    ai_currency: str = "CNY"
    ai_input_price_per_mtokens: float | None = None
    ai_output_price_per_mtokens: float | None = None
    ai_config_path: Path = Field(default=PROJECT_ROOT / "config/ai.yml")
    debug_output_dir: Path = Field(default=PROJECT_ROOT / "debug/output")
    ai_file_processing_timeout: float = 180.0
    ai_file_processing_poll_interval: float = 2.0
    ai_delete_uploaded_files_after_note: bool = True
    ai_local_pdf_text_timeout: float = 30.0
    pdf_transcription_backend: str = "mineru"
    mineru_backend: str = "hybrid-auto-engine"
    mineru_source: str = "huggingface"
    mineru_runtime_dir: Path = Field(default=DEFAULT_MINERU_RUNTIME_DIR)
    mineru_api_token: str | None = None
    mineru_api_base_url: str = "https://mineru.net/api/v4"
    mineru_api_model_version: str = "vlm"
    mineru_api_language: str = "ch"
    mineru_api_enable_formula: bool = True
    mineru_api_enable_table: bool = True
    mineru_api_is_ocr: bool = False
    mineru_api_batch_size: int = Field(default=50, ge=1, le=200)
    mineru_api_poll_interval: float = Field(default=10.0, ge=1.0, le=300.0)
    mineru_api_request_timeout: float = Field(default=60.0, ge=5.0, le=3600.0)
    mineru_api_upload_timeout: float = Field(default=120.0, ge=30.0, le=7200.0)
    mineru_api_download_timeout: float = Field(default=900.0, ge=30.0, le=7200.0)
    mineru_api_batch_timeout: float = Field(default=14400.0, ge=60.0, le=172800.0)
    # Multi-page PDFs: transcribe each page then merge (avoids single-response output limits).
    ai_direct_pdf_page_by_page: bool = True
    ai_direct_pdf_min_pages_for_page_mode: int = 2
    # Parallel page requests (ThreadPoolExecutor max_workers); 1 = sequential.
    ai_direct_pdf_page_max_concurrency: int = Field(default=3, ge=1, le=32)

    literature_root: Path = Field(default=PROJECT_ROOT / "literature")
    reference_dir: Path = Field(default=PROJECT_ROOT / "literature/reference")
    md_dir: Path = Field(default=PROJECT_ROOT / "literature/md")
    metadata_dir: Path = Field(default=PROJECT_ROOT / "literature/metadata")
    index_dir: Path = Field(default=PROJECT_ROOT / "literature/index")
    vector_index_dir: Path = Field(default=PROJECT_ROOT / "literature/index/vector_index")
    reports_dir: Path = Field(default=PROJECT_ROOT / "literature/reports")
    weekly_reports_dir: Path = Field(default=PROJECT_ROOT / "literature/reports/weekly")
    monthly_reports_dir: Path = Field(default=PROJECT_ROOT / "literature/reports/monthly")

    output_dir: Path = Field(default=PROJECT_ROOT / "literature/metadata")
    download_list_dir: Path = Field(default=PROJECT_ROOT / "literature/metadata/download_list")
    rag_output_dir: Path = Field(default=PROJECT_ROOT / "literature/json/rag")
    cas_journal_list_path: Path = Field(default=PROJECT_ROOT / "config/cas_journal_whitelist.xlsx")
    cas_max_quartile: int = 2
    db_path: Path = Field(default=PROJECT_ROOT / "literature/metadata/papers.db")
    incoming_pdf_dir: Path = Field(default=PROJECT_ROOT / "incoming_pdf")
    parsed_output_dir: Path = Field(default=PROJECT_ROOT / "literature/json")
    analysis_output_dir: Path = Field(default=PROJECT_ROOT / "literature/json")
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "mps"
    embedding_batch_size: int = 16
    google_client_secret_path: Path | None = None
    google_token_path: Path | None = None
    google_drive_folder_id: str | None = None
    request_timeout: float = 30.0
    metadata_lookup_offline: bool = False
    incoming_pdf_doi_scan_pages: int = Field(default=2, ge=1, le=10)
    catalog_view_auto_refresh: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    provider_self_check_on_startup: bool = True

    model_config = SettingsConfigDict(
        env_prefix="POWERLIT_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def user_agent(self) -> str:
        suffix = f" ({self.crossref_mailto})" if self.crossref_mailto else ""
        return f"powerlit/0.1{suffix}"

    @model_validator(mode="after")
    def resolve_repo_relative_paths(self) -> Settings:
        for field_name in (
            "literature_root",
            "reference_dir",
            "md_dir",
            "metadata_dir",
            "index_dir",
            "vector_index_dir",
            "reports_dir",
            "weekly_reports_dir",
            "monthly_reports_dir",
            "output_dir",
            "download_list_dir",
            "rag_output_dir",
            "cas_journal_list_path",
            "db_path",
            "incoming_pdf_dir",
            "parsed_output_dir",
            "analysis_output_dir",
            "ai_config_path",
            "debug_output_dir",
            "mineru_runtime_dir",
            "google_client_secret_path",
            "google_token_path",
        ):
            value = getattr(self, field_name)
            if isinstance(value, Path) and not value.is_absolute():
                setattr(self, field_name, (PROJECT_ROOT / value).resolve())
        return self

    @field_validator("pdf_transcription_backend")
    @classmethod
    def validate_pdf_transcription_backend(cls, value: str) -> str:
        return _normalize_choice(
            value,
            supported={"mineru", "ai_direct"},
            label="pdf_transcription_backend",
        )

    @field_validator("mineru_backend")
    @classmethod
    def validate_mineru_backend(cls, value: str) -> str:
        return _normalize_choice(
            value,
            supported={
                "pipeline",
                "vlm-http-client",
                "hybrid-http-client",
                "vlm-auto-engine",
                "hybrid-auto-engine",
            },
            label="mineru_backend",
        )

    @field_validator("mineru_source")
    @classmethod
    def validate_mineru_source(cls, value: str) -> str:
        return _normalize_choice(
            value,
            supported={"huggingface", "modelscope", "local"},
            label="mineru_source",
        )

    @field_validator("mineru_api_model_version")
    @classmethod
    def validate_mineru_api_model_version(cls, value: str) -> str:
        return _normalize_choice(
            value,
            supported={"pipeline", "vlm", "mineru-html"},
            label="mineru_api_model_version",
        )

    @property
    def unpaywall_contact_email(self) -> str | None:
        for candidate in (self.unpaywall_email, self.crossref_mailto):
            if is_real_contact_email(candidate):
                return candidate
        return None

    @property
    def effective_ai_timeout(self) -> float:
        return self.ai_timeout or self.request_timeout

    @property
    def effective_ai_note_timeout(self) -> float:
        return self.ai_note_timeout or max(self.effective_ai_timeout, 180.0)


def is_real_contact_email(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    if "@" not in normalized:
        return False
    return not normalized.endswith("@example.com")


def _normalize_choice(value: object, *, supported: set[str], label: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in supported:
        return normalized
    supported_values = ", ".join(sorted(supported))
    raise ValueError(f"{label} must be one of: {supported_values}.")


settings = Settings()
