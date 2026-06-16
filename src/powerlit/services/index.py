from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2, move

from powerlit.citations import format_gbt_7714
from powerlit.models import DocumentType, PaperRecord
from powerlit.services.catalog_views import CatalogViewService
from powerlit.services.library_layout import (
    build_library_location,
    build_reference_pdf_path,
    is_known_journal_doi,
    normalize_known_source_title,
)
from powerlit.services.library_layout import (
    doi_to_suffix as layout_doi_to_suffix,
)
from powerlit.services.library_layout import (
    sanitize_filename as layout_sanitize_filename,
)
from powerlit.services.publisher_links import resolve_publisher_url
from powerlit.settings import Settings

_PATH_UNSET = object()


class IndexStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = settings.db_path
        self._catalog_views: CatalogViewService | None = None
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_workspace_dirs()
        self._initialize()

    def _ensure_workspace_dirs(self) -> None:
        managed_dirs = [
            self.settings.reference_dir,
            self.settings.md_dir,
            self.settings.metadata_dir,
            self.settings.index_dir,
            self.settings.vector_index_dir,
            self.settings.reports_dir,
            self.settings.weekly_reports_dir,
            self.settings.monthly_reports_dir,
            self.settings.download_list_dir,
        ]
        for directory in managed_dirs:
            directory.mkdir(parents=True, exist_ok=True)

    def _initialize(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    dedupe_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    gbt7714_citation TEXT NOT NULL,
                    authors_json TEXT,
                    published_date TEXT,
                    publisher TEXT,
                    volume TEXT,
                    issue TEXT,
                    pages TEXT,
                    article_number TEXT,
                    abstract TEXT,
                    doi TEXT,
                    publisher_url TEXT,
                    researchgate_url TEXT,
                    researchgate_lookup_url TEXT,
                    researchgate_match_status TEXT,
                    acquisition_method TEXT,
                    acquisition_stage TEXT NOT NULL DEFAULT 'metadata_indexed',
                    acquisition_source_url TEXT,
                    download_status TEXT NOT NULL DEFAULT 'pending',
                    local_pdf_path TEXT,
                    parsed_json_path TEXT,
                    parsed_md_path TEXT,
                    analysis_md_path TEXT,
                    analysis_json_path TEXT,
                    paper_card_md_path TEXT,
                    paper_card_json_path TEXT,
                    providers TEXT,
                    year INTEGER,
                    document_type TEXT,
                    source_title TEXT,
                    query_pack TEXT,
                    raw_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspaces (
                    name TEXT PRIMARY KEY,
                    description TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_papers (
                    workspace_name TEXT NOT NULL,
                    paper_dedupe_key TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_name, paper_dedupe_key)
                )
                """
            )
            self._ensure_columns(conn)
            self._clear_legacy_artifact_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(papers)").fetchall()
        }
        required_columns = {
            "authors_json": "TEXT",
            "published_date": "TEXT",
            "publisher": "TEXT",
            "volume": "TEXT",
            "issue": "TEXT",
            "pages": "TEXT",
            "article_number": "TEXT",
            "abstract": "TEXT",
            "researchgate_lookup_url": "TEXT",
            "researchgate_match_status": "TEXT",
            "acquisition_method": "TEXT",
            "acquisition_stage": "TEXT NOT NULL DEFAULT 'metadata_indexed'",
            "acquisition_source_url": "TEXT",
            "download_status": "TEXT NOT NULL DEFAULT 'pending'",
            "local_pdf_path": "TEXT",
            "parsed_json_path": "TEXT",
            "parsed_md_path": "TEXT",
            "analysis_md_path": "TEXT",
            "analysis_json_path": "TEXT",
            "paper_card_md_path": "TEXT",
            "paper_card_json_path": "TEXT",
            "query_pack": "TEXT",
            "raw_json": "TEXT",
            "updated_at": "TEXT",
        }
        for name, definition in required_columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE papers ADD COLUMN {name} {definition}")

    def _clear_legacy_artifact_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(papers)").fetchall()
        }
        if "parsed_text_path" in existing:
            conn.execute(
                """
                UPDATE papers
                SET parsed_text_path = NULL
                WHERE parsed_text_path IS NOT NULL AND parsed_text_path != ''
                """
            )

    def upsert_records(self, records: list[PaperRecord]) -> None:
        if not records:
            return
        updated_at = datetime.now(UTC).isoformat()
        rows = [self._build_row(record, updated_at) for record in records]
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO papers (
                    dedupe_key,
                    title,
                    gbt7714_citation,
                    authors_json,
                    published_date,
                    publisher,
                    volume,
                    issue,
                    pages,
                    article_number,
                    abstract,
                    doi,
                    publisher_url,
                    researchgate_url,
                    researchgate_lookup_url,
                    researchgate_match_status,
                    acquisition_method,
                    acquisition_stage,
                    acquisition_source_url,
                    download_status,
                    local_pdf_path,
                    parsed_json_path,
                    parsed_md_path,
                    analysis_md_path,
                    analysis_json_path,
                    paper_card_md_path,
                    paper_card_json_path,
                    providers,
                    year,
                    document_type,
                    source_title,
                    query_pack,
                    raw_json,
                    updated_at
                ) VALUES (
                    :dedupe_key,
                    :title,
                    :gbt7714_citation,
                    :authors_json,
                    :published_date,
                    :publisher,
                    :volume,
                    :issue,
                    :pages,
                    :article_number,
                    :abstract,
                    :doi,
                    :publisher_url,
                    :researchgate_url,
                    :researchgate_lookup_url,
                    :researchgate_match_status,
                    :acquisition_method,
                    :acquisition_stage,
                    :acquisition_source_url,
                    :download_status,
                    :local_pdf_path,
                    :parsed_json_path,
                    :parsed_md_path,
                    :analysis_md_path,
                    :analysis_json_path,
                    :paper_card_md_path,
                    :paper_card_json_path,
                    :providers,
                    :year,
                    :document_type,
                    :source_title,
                    :query_pack,
                    :raw_json,
                    :updated_at
                )
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    title = excluded.title,
                    gbt7714_citation = excluded.gbt7714_citation,
                    authors_json = excluded.authors_json,
                    published_date = excluded.published_date,
                    publisher = excluded.publisher,
                    volume = excluded.volume,
                    issue = excluded.issue,
                    pages = excluded.pages,
                    article_number = excluded.article_number,
                    abstract = COALESCE(excluded.abstract, papers.abstract),
                    doi = excluded.doi,
                    publisher_url = excluded.publisher_url,
                    researchgate_url = excluded.researchgate_url,
                    researchgate_lookup_url = excluded.researchgate_lookup_url,
                    researchgate_match_status = excluded.researchgate_match_status,
                    acquisition_method = COALESCE(
                        papers.acquisition_method,
                        excluded.acquisition_method
                    ),
                    acquisition_stage = CASE
                        WHEN papers.acquisition_stage IS NOT NULL
                             AND papers.acquisition_stage != ''
                             AND papers.acquisition_stage != 'metadata_indexed'
                        THEN papers.acquisition_stage
                        ELSE excluded.acquisition_stage
                    END,
                    acquisition_source_url = COALESCE(
                        papers.acquisition_source_url,
                        excluded.acquisition_source_url
                    ),
                    download_status = CASE
                        WHEN papers.local_pdf_path IS NOT NULL THEN papers.download_status
                        ELSE excluded.download_status
                    END,
                    local_pdf_path = COALESCE(papers.local_pdf_path, excluded.local_pdf_path),
                    parsed_json_path = COALESCE(
                        papers.parsed_json_path,
                        excluded.parsed_json_path
                    ),
                    parsed_md_path = COALESCE(papers.parsed_md_path, excluded.parsed_md_path),
                    analysis_md_path = COALESCE(papers.analysis_md_path, excluded.analysis_md_path),
                    analysis_json_path = COALESCE(
                        papers.analysis_json_path,
                        excluded.analysis_json_path
                    ),
                    paper_card_md_path = COALESCE(
                        papers.paper_card_md_path,
                        excluded.paper_card_md_path
                    ),
                    paper_card_json_path = COALESCE(
                        papers.paper_card_json_path,
                        excluded.paper_card_json_path
                    ),
                    providers = excluded.providers,
                    year = excluded.year,
                    document_type = excluded.document_type,
                    source_title = excluded.source_title,
                    query_pack = excluded.query_pack,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    def list_papers(self, limit: int = 20, query_pack: str | None = None) -> list[dict[str, str]]:
        sql = """
            SELECT
                dedupe_key,
                title,
                gbt7714_citation,
                doi,
                publisher_url,
                researchgate_url,
                researchgate_lookup_url,
                researchgate_match_status,
                acquisition_method,
                acquisition_stage,
                acquisition_source_url,
                download_status,
                local_pdf_path,
                parsed_json_path,
                parsed_md_path,
                analysis_md_path,
                analysis_json_path,
                paper_card_md_path,
                paper_card_json_path,
                providers,
                year,
                document_type,
                source_title,
                query_pack,
                updated_at
            FROM papers
        """
        params: list[str | int] = []
        if query_pack:
            sql += " WHERE query_pack = ?"
            params.append(query_pack)
        sql += " ORDER BY year DESC, updated_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._normalize_row(dict(row)) for row in rows]

    def get_paper_by_doi(self, doi: str) -> dict[str, str] | None:
        normalized_doi = doi.strip().lower()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    dedupe_key,
                    title,
                    doi,
                    publisher_url,
                    year,
                    query_pack,
                    acquisition_method,
                    acquisition_stage,
                    acquisition_source_url,
                    download_status,
                    local_pdf_path,
                    parsed_json_path,
                    parsed_md_path,
                    analysis_md_path,
                    analysis_json_path,
                    paper_card_md_path,
                    paper_card_json_path
                FROM papers
                WHERE lower(doi) = ?
                LIMIT 1
                """,
                (normalized_doi,),
            ).fetchone()
        return self._normalize_row(dict(row)) if row else None

    def get_paper_by_doi_suffix(self, doi_suffix: str) -> dict[str, str] | None:
        normalized_suffix = doi_suffix.strip().lower().strip("-")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    dedupe_key,
                    title,
                    doi,
                    publisher_url,
                    year,
                    query_pack,
                    acquisition_method,
                    acquisition_stage,
                    acquisition_source_url,
                    download_status,
                    local_pdf_path,
                    parsed_json_path,
                    parsed_md_path,
                    analysis_md_path,
                    analysis_json_path,
                    paper_card_md_path,
                    paper_card_json_path
                FROM papers
                WHERE doi IS NOT NULL AND doi != ''
                """,
            ).fetchall()
        for row in rows:
            payload = dict(row)
            doi_value = str(payload.get("doi") or "")
            if doi_to_suffix(doi_value) == normalized_suffix:
                return self._normalize_row(payload)
        return None

    def load_paper_records(
        self,
        *,
        limit: int = 20,
        query_pack: str | None = None,
        unresolved_only: bool = True,
        doi: str | None = None,
    ) -> list[PaperRecord]:
        sql = """
            SELECT
                title,
                authors_json,
                published_date,
                publisher,
                volume,
                issue,
                pages,
                article_number,
                abstract,
                doi,
                publisher_url,
                researchgate_url,
                researchgate_lookup_url,
                researchgate_match_status,
                acquisition_method,
                acquisition_stage,
                acquisition_source_url,
                download_status,
                local_pdf_path,
                parsed_json_path,
                parsed_md_path,
                analysis_md_path,
                analysis_json_path,
                paper_card_md_path,
                paper_card_json_path,
                providers,
                year,
                document_type,
                source_title,
                query_pack,
                raw_json
            FROM papers
        """
        filters: list[str] = []
        params: list[str | int] = []
        if query_pack:
            filters.append("query_pack = ?")
            params.append(query_pack)
        if doi:
            filters.append("lower(doi) = ?")
            params.append(doi.strip().lower())
        if unresolved_only:
            filters.append(
                "(acquisition_stage IS NULL OR acquisition_stage = '' "
                "OR acquisition_stage = 'metadata_indexed')"
            )
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY year DESC, updated_at DESC"
        if doi is None:
            sql += " LIMIT ?"
            params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(dict(row)) for row in rows]

    def list_query_packs(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT query_pack
                FROM papers
                WHERE query_pack IS NOT NULL AND query_pack != ''
                ORDER BY query_pack
                """
            ).fetchall()
        return [row[0] for row in rows]

    def load_workspace_records(
        self,
        name: str,
        *,
        limit: int = 20,
    ) -> list[PaperRecord]:
        workspace_name = normalize_workspace_name(name)
        with sqlite3.connect(self.db_path) as conn:
            self._ensure_workspace_exists(conn, workspace_name)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    p.title,
                    p.authors_json,
                    p.published_date,
                    p.publisher,
                    p.volume,
                    p.issue,
                    p.pages,
                    p.article_number,
                    p.abstract,
                    p.doi,
                    p.publisher_url,
                    p.researchgate_url,
                    p.researchgate_lookup_url,
                    p.researchgate_match_status,
                    p.acquisition_method,
                    p.acquisition_stage,
                    p.acquisition_source_url,
                    p.download_status,
                    p.local_pdf_path,
                    p.parsed_json_path,
                    p.parsed_md_path,
                    p.analysis_md_path,
                    p.analysis_json_path,
                    p.providers,
                    p.year,
                    p.document_type,
                    p.source_title,
                    p.query_pack,
                    p.raw_json
                FROM workspace_papers wp
                JOIN papers p
                    ON p.dedupe_key = wp.paper_dedupe_key
                WHERE wp.workspace_name = ?
                ORDER BY p.year DESC, p.updated_at DESC
                LIMIT ?
                """,
                (workspace_name, limit),
            ).fetchall()
        return [self._row_to_record(dict(row)) for row in rows]

    def create_workspace(self, name: str, description: str | None = None) -> bool:
        workspace_name = normalize_workspace_name(name)
        if not workspace_name:
            raise ValueError("Workspace name cannot be empty.")
        normalized_description = normalize_optional_text(description)
        timestamp = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO workspaces (
                    name,
                    description,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    workspace_name,
                    normalized_description,
                    timestamp,
                    timestamp,
                ),
            )
            return cursor.rowcount > 0

    def delete_workspace(self, name: str) -> bool:
        workspace_name = normalize_workspace_name(name)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                DELETE FROM workspace_papers
                WHERE workspace_name = ?
                """,
                (workspace_name,),
            )
            cursor = conn.execute(
                """
                DELETE FROM workspaces
                WHERE name = ?
                """,
                (workspace_name,),
            )
            return cursor.rowcount > 0

    def list_workspaces(self) -> list[dict[str, str | int | None]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    w.name,
                    w.description,
                    w.created_at,
                    w.updated_at,
                    COUNT(wp.paper_dedupe_key) AS paper_count,
                    COUNT(DISTINCT CASE
                        WHEN p.query_pack IS NOT NULL AND p.query_pack != '' THEN p.query_pack
                    END) AS query_pack_count,
                    MAX(p.updated_at) AS latest_paper_update
                FROM workspaces w
                LEFT JOIN workspace_papers wp
                    ON wp.workspace_name = w.name
                LEFT JOIN papers p
                    ON p.dedupe_key = wp.paper_dedupe_key
                GROUP BY
                    w.name,
                    w.description,
                    w.created_at,
                    w.updated_at
                ORDER BY
                    w.updated_at DESC,
                    w.name ASC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_workspace(self, name: str) -> dict[str, str | int | None] | None:
        workspace_name = normalize_workspace_name(name)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT
                    w.name,
                    w.description,
                    w.created_at,
                    w.updated_at,
                    COUNT(wp.paper_dedupe_key) AS paper_count,
                    COUNT(DISTINCT CASE
                        WHEN p.query_pack IS NOT NULL AND p.query_pack != '' THEN p.query_pack
                    END) AS query_pack_count,
                    MAX(p.updated_at) AS latest_paper_update
                FROM workspaces w
                LEFT JOIN workspace_papers wp
                    ON wp.workspace_name = w.name
                LEFT JOIN papers p
                    ON p.dedupe_key = wp.paper_dedupe_key
                WHERE w.name = ?
                GROUP BY
                    w.name,
                    w.description,
                    w.created_at,
                    w.updated_at
                LIMIT 1
                """,
                (workspace_name,),
            ).fetchone()
        return dict(row) if row else None

    def list_workspace_papers(
        self,
        name: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, str | int | None]]:
        workspace_name = normalize_workspace_name(name)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    p.dedupe_key,
                    p.title,
                    p.gbt7714_citation,
                    p.doi,
                    p.publisher_url,
                    p.researchgate_url,
                    p.researchgate_lookup_url,
                    p.researchgate_match_status,
                    p.acquisition_method,
                    p.acquisition_stage,
                    p.acquisition_source_url,
                    p.download_status,
                    p.local_pdf_path,
                    p.parsed_json_path,
                    p.parsed_md_path,
                    p.analysis_md_path,
                    p.analysis_json_path,
                    p.paper_card_md_path,
                    p.paper_card_json_path,
                    p.providers,
                    p.year,
                    p.document_type,
                    p.source_title,
                    p.query_pack,
                    p.updated_at,
                    wp.added_at AS workspace_added_at
                FROM workspace_papers wp
                JOIN papers p
                    ON p.dedupe_key = wp.paper_dedupe_key
                WHERE wp.workspace_name = ?
                ORDER BY p.year DESC, p.updated_at DESC
                LIMIT ?
                """,
                (workspace_name, limit),
            ).fetchall()
        return [self._normalize_row(dict(row)) for row in rows]

    def add_paper_to_workspace(self, name: str, doi: str) -> bool:
        workspace_name = normalize_workspace_name(name)
        paper_dedupe_key = self._get_paper_dedupe_key_by_doi(doi)
        if paper_dedupe_key is None:
            raise LookupError(f"No indexed paper found for DOI: {doi}")
        timestamp = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            self._ensure_workspace_exists(conn, workspace_name)
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO workspace_papers (
                    workspace_name,
                    paper_dedupe_key,
                    added_at
                ) VALUES (?, ?, ?)
                """,
                (workspace_name, paper_dedupe_key, timestamp),
            )
            if cursor.rowcount > 0:
                self._touch_workspace(conn, workspace_name, timestamp)
                return True
            return False

    def add_query_pack_to_workspace(self, name: str, query_pack: str) -> int:
        workspace_name = normalize_workspace_name(name)
        normalized_query_pack = normalize_optional_text(query_pack)
        if not normalized_query_pack:
            raise ValueError("Query pack cannot be empty.")
        timestamp = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            self._ensure_workspace_exists(conn, workspace_name)
            existing_count = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO workspace_papers (
                    workspace_name,
                    paper_dedupe_key,
                    added_at
                )
                SELECT ?, dedupe_key, ?
                FROM papers
                WHERE query_pack = ?
                """,
                (workspace_name, timestamp, normalized_query_pack),
            )
            added = conn.total_changes - existing_count
            if added <= 0:
                existing = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM papers
                    WHERE query_pack = ?
                    """,
                    (normalized_query_pack,),
                ).fetchone()
                if not existing or int(existing[0] or 0) == 0:
                    raise LookupError(f"No indexed papers found for query pack: {query_pack}")
                return 0
            self._touch_workspace(conn, workspace_name, timestamp)
            return added

    def remove_paper_from_workspace(self, name: str, doi: str) -> bool:
        workspace_name = normalize_workspace_name(name)
        normalized_doi = doi.strip().lower()
        timestamp = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            self._ensure_workspace_exists(conn, workspace_name)
            cursor = conn.execute(
                """
                DELETE FROM workspace_papers
                WHERE workspace_name = ?
                  AND paper_dedupe_key IN (
                      SELECT dedupe_key
                      FROM papers
                      WHERE lower(doi) = ?
                  )
                """,
                (workspace_name, normalized_doi),
            )
            if cursor.rowcount > 0:
                self._touch_workspace(conn, workspace_name, timestamp)
                return True
            return False

    def remove_query_pack_from_workspace(self, name: str, query_pack: str) -> int:
        workspace_name = normalize_workspace_name(name)
        normalized_query_pack = normalize_optional_text(query_pack)
        if not normalized_query_pack:
            raise ValueError("Query pack cannot be empty.")
        timestamp = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            self._ensure_workspace_exists(conn, workspace_name)
            cursor = conn.execute(
                """
                DELETE FROM workspace_papers
                WHERE workspace_name = ?
                  AND paper_dedupe_key IN (
                      SELECT dedupe_key
                      FROM papers
                      WHERE query_pack = ?
                  )
                """,
                (workspace_name, normalized_query_pack),
            )
            removed = cursor.rowcount
            if removed > 0:
                self._touch_workspace(conn, workspace_name, timestamp)
            return removed

    def list_workspace_query_packs(self, name: str) -> list[str]:
        workspace_name = normalize_workspace_name(name)
        with sqlite3.connect(self.db_path) as conn:
            self._ensure_workspace_exists(conn, workspace_name)
            rows = conn.execute(
                """
                SELECT DISTINCT p.query_pack
                FROM workspace_papers wp
                JOIN papers p
                    ON p.dedupe_key = wp.paper_dedupe_key
                WHERE wp.workspace_name = ?
                  AND p.query_pack IS NOT NULL
                  AND p.query_pack != ''
                ORDER BY p.query_pack
                """,
                (workspace_name,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def summary(self) -> dict[str, int | str | None]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_papers,
                    SUM(
                        CASE WHEN local_pdf_path IS NOT NULL THEN 1 ELSE 0 END
                    ) AS downloaded_papers,
                    SUM(
                        CASE WHEN local_pdf_path IS NULL THEN 1 ELSE 0 END
                    ) AS pending_downloads,
                    SUM(
                        CASE WHEN doi IS NOT NULL AND doi != '' THEN 1 ELSE 0 END
                    ) AS papers_with_doi,
                    SUM(
                        CASE WHEN analysis_json_path IS NOT NULL THEN 1 ELSE 0 END
                    ) AS analyzed_papers,
                    SUM(
                        CASE WHEN parsed_json_path IS NOT NULL THEN 1 ELSE 0 END
                    ) AS parsed_papers,
                    SUM(
                        CASE WHEN paper_card_json_path IS NOT NULL THEN 1 ELSE 0 END
                    ) AS carded_papers,
                    (
                        SELECT COUNT(*)
                        FROM workspaces
                    ) AS workspace_count,
                    COUNT(DISTINCT CASE
                        WHEN query_pack IS NOT NULL AND query_pack != '' THEN query_pack
                    END) AS query_pack_count,
                    MAX(updated_at) AS latest_update
                FROM papers
                """
            ).fetchone()
        return {
            "total_papers": int(row[0] or 0),
            "downloaded_papers": int(row[1] or 0),
            "pending_downloads": int(row[2] or 0),
            "papers_with_doi": int(row[3] or 0),
            "analyzed_papers": int(row[4] or 0),
            "parsed_papers": int(row[5] or 0),
            "carded_papers": int(row[6] or 0),
            "workspace_count": int(row[7] or 0),
            "query_pack_count": int(row[8] or 0),
            "latest_update": row[9],
        }

    def list_download_queue(
        self,
        limit: int = 20,
        query_pack: str | None = None,
        pending_only: bool = True,
    ) -> list[dict[str, str]]:
        sql = """
            SELECT
                title,
                gbt7714_citation,
                doi,
                publisher_url,
                researchgate_url,
                researchgate_lookup_url,
                source_title,
                volume,
                issue,
                year,
                query_pack,
                acquisition_method,
                acquisition_stage,
                acquisition_source_url,
                download_status,
                local_pdf_path
            FROM papers
        """
        filters: list[str] = []
        params: list[str | int] = []
        if query_pack:
            filters.append("query_pack = ?")
            params.append(query_pack)
        if pending_only:
            filters.append("(local_pdf_path IS NULL OR download_status != 'downloaded')")
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY year DESC, updated_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        queue: list[dict[str, str]] = []
        for row in rows:
            item = self._normalize_row(dict(row))
            normalized_source_title = normalize_known_source_title(
                str(item.get("source_title") or ""),
                doi=item.get("doi"),
            )
            location = build_library_location(
                self.settings.reference_dir,
                source_title=normalized_source_title,
                volume=str(item.get("volume") or ""),
                issue=str(item.get("issue") or ""),
                year=int(item["year"]) if item.get("year") else None,
            )
            target_path = build_reference_pdf_path(
                self.settings.reference_dir,
                title=item["title"],
                doi=item.get("doi"),
                source_title=normalized_source_title,
                year=int(item["year"]) if item.get("year") else None,
                volume=str(item.get("volume") or ""),
                issue=str(item.get("issue") or ""),
            )
            if normalized_source_title:
                item["source_title"] = normalized_source_title
            item["journal_short_name"] = location.journal_short_name
            item["volume_folder"] = location.volume_folder
            item["issue_folder"] = location.issue_folder or ""
            item["target_reference_dir"] = str(location.directory.resolve())
            item["suggested_filename"] = target_path.name
            item["target_pdf_path"] = str(target_path.resolve())
            queue.append(item)
        return queue

    def attach_pdf(self, doi: str, file_path: Path) -> bool:
        normalized_doi = doi.strip().lower()
        records = self.load_paper_records(limit=1, doi=normalized_doi, unresolved_only=False)
        if not records:
            return False
        record = records[0]
        source_path = file_path.resolve()
        normalized_source_title = normalize_known_source_title(
            record.source_title,
            doi=record.doi,
        )
        corrected_document_type = (
            DocumentType.JOURNAL if is_known_journal_doi(record.doi) else record.document_type
        )
        citation_record = record.model_copy(
            update={
                "source_title": normalized_source_title or record.source_title,
                "document_type": corrected_document_type,
            }
        )
        corrected_citation = format_gbt_7714(citation_record)
        target_path = build_reference_pdf_path(
            self.settings.reference_dir,
            title=record.title,
            doi=record.doi,
            source_title=normalized_source_title,
            year=record.year,
            volume=record.volume,
            issue=record.issue,
            original_filename=source_path.name,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.parent.resolve() == target_path.parent.resolve():
            managed_path = source_path
        else:
            if is_path_within_directory(source_path, self.settings.incoming_pdf_dir):
                if target_path.exists():
                    target_path.unlink()
                move(str(source_path), str(target_path))
            else:
                copy2(source_path, target_path)
            managed_path = target_path
        resolved = str(managed_path.resolve())
        updated_at = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
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
                (
                    corrected_citation,
                    resolved,
                    normalized_source_title,
                    corrected_document_type.value if corrected_document_type else None,
                    updated_at,
                    normalized_doi,
                ),
            )
            updated = cursor.rowcount > 0
        if updated:
            self._refresh_catalog_views_for_doi(normalized_doi)
        return updated

    def attach_parsed_artifacts(
        self,
        doi: str,
        json_path: Path | None = None,
        markdown_path: Path | None = None,
    ) -> bool:
        resolved_markdown_path = markdown_path
        if (
            resolved_markdown_path is None
            and json_path is not None
            and json_path.suffix.lower() != ".json"
        ):
            resolved_markdown_path = json_path
        resolved_json_path = resolve_artifact_path(
            json_path,
            resolved_markdown_path,
            preferred_suffix=".json",
        )
        if resolved_json_path is None:
            raise ValueError("A parsed JSON path is required.")
        normalized_doi = doi.strip().lower()
        updated_at = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE papers
                SET parsed_json_path = ?,
                    parsed_md_path = ?,
                    acquisition_stage = 'parsed',
                    updated_at = ?
                WHERE lower(doi) = ?
                """,
                (
                    str(resolved_json_path.resolve()),
                    (
                        str(resolved_markdown_path.resolve())
                        if resolved_markdown_path is not None
                        else None
                    ),
                    updated_at,
                    normalized_doi,
                ),
            )
            updated = cursor.rowcount > 0
        if updated:
            self._refresh_catalog_views_for_doi(normalized_doi)
        return updated

    def attach_analysis_artifacts(
        self,
        doi: str,
        json_path: Path,
        markdown_path: Path | None = None,
    ) -> bool:
        normalized_doi = doi.strip().lower()
        updated_at = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE papers
                SET analysis_md_path = ?,
                    analysis_json_path = ?,
                    acquisition_stage = 'analyzed',
                    updated_at = ?
                WHERE lower(doi) = ?
                """,
                (
                    str(markdown_path.resolve()) if markdown_path is not None else None,
                    str(json_path.resolve()),
                    updated_at,
                    normalized_doi,
                ),
            )
            updated = cursor.rowcount > 0
        if updated:
            self._refresh_catalog_views_for_doi(normalized_doi)
        return updated

    def update_artifact_paths(
        self,
        doi: str,
        *,
        local_pdf_path: str | Path | None | object = _PATH_UNSET,
        parsed_json_path: str | Path | None | object = _PATH_UNSET,
        parsed_md_path: str | Path | None | object = _PATH_UNSET,
        analysis_md_path: str | Path | None | object = _PATH_UNSET,
        analysis_json_path: str | Path | None | object = _PATH_UNSET,
    ) -> bool:
        normalized_doi = doi.strip().lower()
        assignments: list[str] = []
        params: list[str | None] = []
        for column, value in (
            ("local_pdf_path", local_pdf_path),
            ("parsed_json_path", parsed_json_path),
            ("parsed_md_path", parsed_md_path),
            ("analysis_md_path", analysis_md_path),
            ("analysis_json_path", analysis_json_path),
        ):
            if value is _PATH_UNSET:
                continue
            assignments.append(f"{column} = ?")
            if value is None:
                params.append(None)
            else:
                params.append(str(Path(value).resolve()))

        if not assignments:
            return False

        updated_at = datetime.now(UTC).isoformat()
        assignments.append("updated_at = ?")
        params.append(updated_at)
        params.append(normalized_doi)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"""
                UPDATE papers
                SET {", ".join(assignments)}
                WHERE lower(doi) = ?
                """,
                params,
            )
            updated = cursor.rowcount > 0
        if updated:
            self._refresh_catalog_views_for_doi(normalized_doi)
        return updated

    def attach_paper_card_artifacts(
        self,
        dedupe_key: str,
        json_path: Path,
        markdown_path: Path | None = None,
    ) -> bool:
        updated_at = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE papers
                SET paper_card_md_path = ?,
                    paper_card_json_path = ?,
                    updated_at = ?
                WHERE dedupe_key = ?
                """,
                (
                    str(markdown_path.resolve()) if markdown_path is not None else None,
                    str(json_path.resolve()),
                    updated_at,
                    dedupe_key,
                ),
            )
            return cursor.rowcount > 0

    def _build_row(self, record: PaperRecord, updated_at: str) -> dict[str, str | int | None]:
        return {
            "dedupe_key": record.dedupe_key,
            "title": record.title,
            "gbt7714_citation": format_gbt_7714(record),
            "authors_json": json.dumps(
                [author.model_dump(mode="json") for author in record.authors],
                ensure_ascii=False,
            ),
            "published_date": (
                record.published_date.isoformat() if record.published_date else None
            ),
            "publisher": record.publisher,
            "volume": record.volume,
            "issue": record.issue,
            "pages": record.pages,
            "article_number": record.article_number,
            "abstract": record.abstract,
            "doi": record.doi,
            "publisher_url": record.publisher_url,
            "researchgate_url": record.researchgate_url,
            "researchgate_lookup_url": record.researchgate_lookup_url,
            "researchgate_match_status": record.researchgate_match_status,
            "acquisition_method": (
                record.acquisition_method.value if record.acquisition_method else None
            ),
            "acquisition_stage": record.acquisition_stage.value,
            "acquisition_source_url": record.acquisition_source_url,
            "download_status": record.download_status,
            "local_pdf_path": record.local_pdf_path,
            "parsed_json_path": record.parsed_json_path,
            "parsed_md_path": record.parsed_md_path,
            "analysis_md_path": record.analysis_md_path,
            "analysis_json_path": record.analysis_json_path,
            "paper_card_md_path": None,
            "paper_card_json_path": None,
            "providers": ",".join(sorted(record.source_providers)),
            "year": record.year,
            "document_type": record.document_type.value,
            "source_title": record.source_title,
            "query_pack": record.query_pack,
            "raw_json": json.dumps(record.raw, ensure_ascii=False, default=str),
            "updated_at": updated_at,
        }

    def _row_to_record(self, row: dict[str, str | int | None]) -> PaperRecord:
        authors_json = row.pop("authors_json", None)
        raw_json = row.pop("raw_json", None)
        providers = row.pop("providers", "") or ""
        authors_payload = json.loads(authors_json) if authors_json else []
        raw_payload = json.loads(raw_json) if raw_json else {}
        row["publisher_url"] = resolve_publisher_url(
            str(row.get("doi") or ""),
            str(row.get("publisher_url") or ""),
        )
        return PaperRecord.model_validate(
            {
                **row,
                "authors": authors_payload,
                "source_providers": [item for item in str(providers).split(",") if item],
                "raw": raw_payload,
            }
        )

    def _normalize_row(self, row: dict[str, str | int | None]) -> dict[str, str | int | None]:
        row["publisher_url"] = resolve_publisher_url(
            str(row.get("doi") or ""),
            str(row.get("publisher_url") or ""),
        )
        return row

    def _refresh_catalog_views_for_doi(self, doi: str) -> None:
        if not self.settings.catalog_view_auto_refresh:
            return
        try:
            self._get_catalog_views().refresh_for_doi(doi)
        except Exception:
            return

    def _get_catalog_views(self) -> CatalogViewService:
        if self._catalog_views is None:
            self._catalog_views = CatalogViewService(self.settings)
        return self._catalog_views

    def _ensure_workspace_exists(self, conn: sqlite3.Connection, workspace_name: str) -> None:
        row = conn.execute(
            """
            SELECT 1
            FROM workspaces
            WHERE name = ?
            LIMIT 1
            """,
            (workspace_name,),
        ).fetchone()
        if row is None:
            raise LookupError(f"Workspace not found: {workspace_name}")

    def _touch_workspace(
        self,
        conn: sqlite3.Connection,
        workspace_name: str,
        timestamp: str,
    ) -> None:
        conn.execute(
            """
            UPDATE workspaces
            SET updated_at = ?
            WHERE name = ?
            """,
            (timestamp, workspace_name),
        )

    def _get_paper_dedupe_key_by_doi(self, doi: str) -> str | None:
        normalized_doi = doi.strip().lower()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT dedupe_key
                FROM papers
                WHERE lower(doi) = ?
                LIMIT 1
                """,
                (normalized_doi,),
            ).fetchone()
        return str(row[0]) if row else None


def resolve_artifact_path(
    primary: Path | None,
    fallback: Path | None,
    *,
    preferred_suffix: str,
) -> Path | None:
    for candidate in (primary, fallback):
        if candidate is None:
            continue
        if candidate.suffix.lower() == preferred_suffix.lower():
            return candidate
    return primary or fallback


def suggest_pdf_filename(title: str, doi: str | None, year: int | None) -> str:
    stem = sanitize_filename(title)
    suffix = doi_to_suffix(doi) if doi else f"year-{year or 'unknown'}"
    return f"{stem}__{suffix}.pdf"


def sanitize_filename(value: str) -> str:
    return layout_sanitize_filename(value)


def doi_to_suffix(doi: str) -> str:
    return layout_doi_to_suffix(doi)


def is_path_within_directory(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def normalize_workspace_name(value: str) -> str:
    return normalize_optional_text(value) or ""
