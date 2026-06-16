from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from powerlit.settings import Settings

EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_CHUNK_TARGET_CHARS = 1400
DEFAULT_CHUNK_MAX_CHARS = 2200
PAGE_HEADING_RE = re.compile(r"^\s*#{0,6}\s*page\s+(\d+)(?:\s+of\s+\d+)?\s*$", re.I)
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")


@dataclass(slots=True)
class EvidenceBuildSummary:
    ok: bool
    db_path: Path
    json_root: Path
    documents: int
    chunks: int
    skipped: int
    elapsed_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "db_path": str(self.db_path),
            "json_root": str(self.json_root),
            "documents": self.documents,
            "chunks": self.chunks,
            "skipped": self.skipped,
            "elapsed_ms": self.elapsed_ms,
        }


class EvidenceIndexService:
    """Fast local evidence retrieval backed by SQLite FTS5."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.json_root = settings.json_root or settings.parsed_output_dir
        self.index_root = settings.index_root or settings.index_dir / "evidence"
        self.db_path = self.index_root / "evidence.db"

    def build(
        self,
        *,
        force: bool = False,
        venue_folders: list[str] | None = None,
        limit: int | None = None,
    ) -> EvidenceBuildSummary:
        started = time.perf_counter()
        self.index_root.mkdir(parents=True, exist_ok=True)
        if force and self.db_path.exists():
            self.db_path.unlink()

        requested_venues = normalize_list(venue_folders)
        with sqlite3.connect(self.db_path) as conn:
            initialize_schema(conn)
            paper_paths = load_paper_paths(self.settings)
            documents = 0
            chunks = 0
            skipped = 0
            for json_path in iter_candidate_json_files(
                self.json_root,
                rag_output_dir=self.settings.rag_output_dir,
                venue_folders=requested_venues,
            ):
                if limit is not None and limit > 0 and documents >= limit:
                    break
                payload = load_json_object(json_path)
                if not payload:
                    skipped += 1
                    continue
                record = build_document_record(
                    payload,
                    json_path=json_path,
                    json_root=self.json_root,
                    paper_paths=paper_paths,
                )
                if not record or not record["content"]:
                    skipped += 1
                    continue
                document_chunks = build_chunks(record)
                if not document_chunks:
                    skipped += 1
                    continue
                delete_existing_document(conn, record)
                document_id = insert_document(conn, record)
                inserted = insert_chunks(conn, document_id, record, document_chunks)
                documents += 1
                chunks += inserted
            update_manifest(conn, documents=documents, chunks=chunks, skipped=skipped)

        return EvidenceBuildSummary(
            ok=True,
            db_path=self.db_path,
            json_root=self.json_root,
            documents=documents,
            chunks=chunks,
            skipped=skipped,
            elapsed_ms=elapsed_ms(started),
        )

    def search(
        self,
        query: str,
        *,
        top: int = 20,
        venue_folders: list[str] | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        doi: str | None = None,
        section: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        normalized_query = " ".join(str(query or "").split())
        if not self.db_path.exists():
            return {
                "available": False,
                "query": normalized_query,
                "candidate_source": "none",
                "message": (
                    "PowerLit evidence index is unavailable. "
                    "Run build-evidence-index first."
                ),
                "elapsed_ms": elapsed_ms(started),
                "count": 0,
                "results": [],
            }
        terms = normalize_terms(normalized_query)
        if not terms:
            return build_empty_search_payload(normalized_query, started)

        sql, params = build_search_sql(
            fts_query(terms),
            top=top,
            venue_folders=normalize_list(venue_folders),
            year_from=year_from,
            year_to=year_to,
            doi=doi,
            section=section,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]

        results = [build_search_result(row, terms) for row in rows]
        results.sort(key=lambda item: item["score"], reverse=True)
        results = results[: max(top, 1)]
        return {
            "available": True,
            "query": normalized_query,
            "candidate_source": "powerlit_evidence_fts",
            "elapsed_ms": elapsed_ms(started),
            "count": len(results),
            "results": results,
        }

    def status(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "available": False,
                "db_path": str(self.db_path),
                "message": "PowerLit evidence index is unavailable.",
            }
        with sqlite3.connect(self.db_path) as conn:
            documents = count_rows(conn, "documents")
            chunks = count_rows(conn, "chunks")
            manifest = load_manifest(conn)
        return {
            "available": True,
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "db_path": str(self.db_path),
            "json_root": str(self.json_root),
            "documents": documents,
            "chunks": chunks,
            "manifest": manifest,
        }


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            document_id INTEGER PRIMARY KEY,
            doi TEXT,
            title TEXT,
            source_title TEXT,
            year INTEGER,
            venue_folder TEXT,
            local_pdf_path TEXT,
            parsed_json_path TEXT NOT NULL UNIQUE,
            analysis_json_path TEXT,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            chunk_id TEXT PRIMARY KEY,
            document_id INTEGER NOT NULL,
            doi TEXT,
            title TEXT,
            source_title TEXT,
            year INTEGER,
            venue_folder TEXT,
            section TEXT,
            page_start INTEGER,
            page_end INTEGER,
            chunk_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            parsed_json_path TEXT NOT NULL,
            local_pdf_path TEXT,
            FOREIGN KEY(document_id) REFERENCES documents(document_id)
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            title,
            source_title,
            section,
            text,
            content='chunks',
            content_rowid='rowid'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_manifest (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_doi ON documents(doi)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doi ON chunks(doi)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_venue ON chunks(venue_folder)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_year ON chunks(year)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_doi_hash ON chunks(doi, text_hash)"
    )


def iter_candidate_json_files(
    json_root: Path,
    *,
    rag_output_dir: Path,
    venue_folders: list[str],
) -> list[Path]:
    if not json_root.exists():
        return []
    roots = [json_root / venue for venue in venue_folders] if venue_folders else [json_root]
    files: list[Path] = []
    resolved_rag = resolve_optional(rag_output_dir)
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            if path.name.endswith("-analysis.json"):
                continue
            resolved = resolve_optional(path)
            if resolved_rag is not None and resolved is not None:
                try:
                    resolved.relative_to(resolved_rag)
                    continue
                except ValueError:
                    pass
            files.append(path)
    return files


def load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def build_document_record(
    payload: dict[str, Any],
    *,
    json_path: Path,
    json_root: Path,
    paper_paths: dict[str, dict[str, str | None]],
) -> dict[str, Any] | None:
    content = str(payload.get("content") or "").strip()
    if not content:
        return None
    doi = normalize_optional_text(payload.get("doi"))
    path_payload = paper_paths.get(doi.lower()) if doi else None
    return {
        "doi": doi,
        "title": normalize_optional_text(payload.get("title")) or json_path.stem,
        "source_title": normalize_optional_text(payload.get("source_title")),
        "year": coerce_int(payload.get("year")),
        "venue_folder": infer_venue_folder(json_path, json_root),
        "local_pdf_path": (path_payload or {}).get("local_pdf_path"),
        "parsed_json_path": str(json_path.resolve()),
        "analysis_json_path": (path_payload or {}).get("analysis_json_path"),
        "content": content,
        "content_hash": sha1_text(content),
        "pages": payload.get("pages") if isinstance(payload.get("pages"), list) else None,
        "page_map": payload.get("page_map") if isinstance(payload.get("page_map"), list) else None,
    }


def build_chunks(record: dict[str, Any]) -> list[dict[str, Any]]:
    page_chunks = chunks_from_structured_pages(record)
    if page_chunks:
        return page_chunks
    return chunks_from_markdown_content(record["content"])


def chunks_from_structured_pages(record: dict[str, Any]) -> list[dict[str, Any]]:
    pages = record.get("pages") or record.get("page_map")
    if not isinstance(pages, list):
        return []
    chunks: list[dict[str, Any]] = []
    for item in pages:
        if not isinstance(item, dict):
            continue
        text = normalize_text_block(
            item.get("text") or item.get("content") or item.get("markdown") or ""
        )
        if not text:
            continue
        page = coerce_int(item.get("page") or item.get("page_index") or item.get("page_number"))
        section = normalize_optional_text(item.get("section"))
        chunks.extend(split_blocks_to_chunks(text, page_start=page, page_end=page, section=section))
    return chunks


def chunks_from_markdown_content(content: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    section: str | None = None
    page: int | None = None
    current_blocks: list[str] = []

    def flush() -> None:
        nonlocal current_blocks
        if current_blocks:
            text = normalize_text_block("\n\n".join(current_blocks))
            if text:
                chunks.extend(
                    split_blocks_to_chunks(text, page_start=page, page_end=page, section=section)
                )
            current_blocks = []

    for raw_line in content.splitlines():
        page_match = PAGE_HEADING_RE.match(raw_line)
        if page_match:
            flush()
            page = int(page_match.group(1))
            continue
        heading_match = MARKDOWN_HEADING_RE.match(raw_line)
        if heading_match:
            flush()
            heading = normalize_heading(heading_match.group(2))
            if heading:
                section = heading
            continue
        current_blocks.append(raw_line)
    flush()
    return chunks


def split_blocks_to_chunks(
    text: str,
    *,
    page_start: int | None,
    page_end: int | None,
    section: str | None,
) -> list[dict[str, Any]]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_len = 0
    for block in blocks:
        block_len = len(block)
        if current and current_len + block_len > DEFAULT_CHUNK_MAX_CHARS:
            chunks.append(
                {
                    "text": normalize_text_block("\n\n".join(current)),
                    "section": section,
                    "page_start": page_start,
                    "page_end": page_end,
                }
            )
            current = []
            current_len = 0
        current.append(block)
        current_len += block_len
        if current_len >= DEFAULT_CHUNK_TARGET_CHARS:
            chunks.append(
                {
                    "text": normalize_text_block("\n\n".join(current)),
                    "section": section,
                    "page_start": page_start,
                    "page_end": page_end,
                }
            )
            current = []
            current_len = 0
    if current:
        chunks.append(
            {
                "text": normalize_text_block("\n\n".join(current)),
                "section": section,
                "page_start": page_start,
                "page_end": page_end,
            }
        )
    return [chunk for chunk in chunks if chunk["text"]]


def delete_existing_document(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    if record.get("doi"):
        rows = conn.execute(
            "SELECT rowid FROM chunks WHERE lower(doi) = ?",
            (record["doi"].lower(),),
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (row[0],))
        conn.execute("DELETE FROM chunks WHERE lower(doi) = ?", (record["doi"].lower(),))
        conn.execute("DELETE FROM documents WHERE lower(doi) = ?", (record["doi"].lower(),))
        return
    rows = conn.execute(
        "SELECT rowid FROM chunks WHERE parsed_json_path = ?",
        (record["parsed_json_path"],),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (row[0],))
    conn.execute("DELETE FROM chunks WHERE parsed_json_path = ?", (record["parsed_json_path"],))
    conn.execute(
        "DELETE FROM documents WHERE parsed_json_path = ?",
        (record["parsed_json_path"],),
    )


def insert_document(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO documents (
            doi,
            title,
            source_title,
            year,
            venue_folder,
            local_pdf_path,
            parsed_json_path,
            analysis_json_path,
            content_hash,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.get("doi"),
            record.get("title"),
            record.get("source_title"),
            record.get("year"),
            record.get("venue_folder"),
            record.get("local_pdf_path"),
            record.get("parsed_json_path"),
            record.get("analysis_json_path"),
            record.get("content_hash"),
            datetime.now(UTC).isoformat(),
        ),
    )
    return int(cursor.lastrowid)


def insert_chunks(
    conn: sqlite3.Connection,
    document_id: int,
    record: dict[str, Any],
    chunks: list[dict[str, Any]],
) -> int:
    inserted = 0
    seen_hashes: set[str] = set()
    for index, chunk in enumerate(chunks):
        text = chunk["text"]
        text_hash = sha1_text(text)
        dedupe_key = f"{record.get('doi') or record['parsed_json_path']}:{text_hash}"
        if dedupe_key in seen_hashes:
            continue
        seen_hashes.add(dedupe_key)
        chunk_id = build_chunk_id(record, index, text_hash)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO chunks (
                chunk_id,
                document_id,
                doi,
                title,
                source_title,
                year,
                venue_folder,
                section,
                page_start,
                page_end,
                chunk_index,
                text,
                text_hash,
                parsed_json_path,
                local_pdf_path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                document_id,
                record.get("doi"),
                record.get("title"),
                record.get("source_title"),
                record.get("year"),
                record.get("venue_folder"),
                chunk.get("section"),
                chunk.get("page_start"),
                chunk.get("page_end"),
                index,
                text,
                text_hash,
                record.get("parsed_json_path"),
                record.get("local_pdf_path"),
            ),
        )
        if cursor.rowcount <= 0:
            continue
        rowid = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO chunks_fts(rowid, chunk_id, title, source_title, section, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                rowid,
                chunk_id,
                record.get("title"),
                record.get("source_title"),
                chunk.get("section"),
                text,
            ),
        )
        inserted += 1
    return inserted


def build_search_sql(
    match_query: str,
    *,
    top: int,
    venue_folders: list[str],
    year_from: int | None,
    year_to: int | None,
    doi: str | None,
    section: str | None,
) -> tuple[str, list[Any]]:
    filters = ["chunks_fts MATCH ?"]
    params: list[Any] = [match_query]
    if venue_folders:
        placeholders = ", ".join("?" for _ in venue_folders)
        filters.append(f"c.venue_folder IN ({placeholders})")
        params.extend(venue_folders)
    if year_from is not None:
        filters.append("c.year >= ?")
        params.append(year_from)
    if year_to is not None:
        filters.append("c.year <= ?")
        params.append(year_to)
    if doi:
        filters.append("lower(c.doi) = ?")
        params.append(doi.strip().lower())
    if section:
        filters.append("c.section LIKE ?")
        params.append(f"%{section.strip()}%")

    candidate_limit = max(top * 20, 100)
    params.append(candidate_limit)
    return (
        f"""
        SELECT
            c.chunk_id,
            c.doi,
            c.title,
            c.source_title,
            c.year,
            c.venue_folder,
            c.section,
            c.page_start,
            c.page_end,
            c.chunk_index,
            c.text,
            c.parsed_json_path,
            c.local_pdf_path,
            d.analysis_json_path,
            bm25(chunks_fts) AS bm25_score
        FROM chunks_fts
        JOIN chunks c ON c.rowid = chunks_fts.rowid
        JOIN documents d ON d.document_id = c.document_id
        WHERE {" AND ".join(filters)}
        ORDER BY bm25_score
        LIMIT ?
        """,
        params,
    )


def build_search_result(row: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    text = str(row.get("text") or "")
    title = str(row.get("title") or "")
    section = str(row.get("section") or "")
    matched_terms = match_terms(" ".join([title, section, text]), terms)
    score = -float(row.get("bm25_score") or 0.0)
    score += 0.25 * len(match_terms(title, terms))
    score += 0.15 * len(match_terms(section, terms))
    return {
        "score": round(score, 6),
        "chunk_id": row.get("chunk_id") or "",
        "doi": row.get("doi") or "",
        "title": title,
        "source_title": row.get("source_title") or "",
        "year": row.get("year"),
        "venue_folder": row.get("venue_folder") or "",
        "section": section,
        "page_start": row.get("page_start"),
        "page_end": row.get("page_end"),
        "chunk_index": row.get("chunk_index"),
        "matched_terms": matched_terms,
        "snippet": get_snippet(text, matched_terms or terms),
        "parsed_json_path": row.get("parsed_json_path") or "",
        "local_pdf_path": row.get("local_pdf_path") or "",
        "analysis_json_path": row.get("analysis_json_path") or "",
    }


def update_manifest(
    conn: sqlite3.Connection,
    *,
    documents: int,
    chunks: int,
    skipped: int,
) -> None:
    values = {
        "schema_version": str(EVIDENCE_SCHEMA_VERSION),
        "built_at": datetime.now(UTC).isoformat(),
        "documents_last_build": str(documents),
        "chunks_last_build": str(chunks),
        "skipped_last_build": str(skipped),
    }
    conn.executemany(
        """
        INSERT INTO index_manifest(key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        values.items(),
    )


def load_manifest(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM index_manifest ORDER BY key").fetchall()
    return {str(key): str(value) for key, value in rows}


def load_paper_paths(settings: Settings) -> dict[str, dict[str, str | None]]:
    if not settings.db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT doi, local_pdf_path, analysis_json_path
            FROM papers
            WHERE doi IS NOT NULL AND doi != ''
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {
        str(row["doi"]).lower(): {
            "local_pdf_path": row["local_pdf_path"],
            "analysis_json_path": row["analysis_json_path"],
        }
        for row in rows
    }


def build_empty_search_payload(query: str, started: float) -> dict[str, Any]:
    return {
        "available": True,
        "query": query,
        "candidate_source": "powerlit_evidence_fts",
        "elapsed_ms": elapsed_ms(started),
        "count": 0,
        "results": [],
    }


def infer_venue_folder(json_path: Path, json_root: Path) -> str:
    try:
        relative = json_path.resolve().relative_to(json_root.resolve())
    except ValueError:
        return "_root"
    return relative.parts[0] if len(relative.parts) > 1 else "_root"


def build_chunk_id(record: dict[str, Any], index: int, text_hash: str) -> str:
    source = record.get("doi") or record.get("parsed_json_path") or "unknown"
    source_hash = sha1_text(str(source))[:12]
    return f"{source_hash}:{index:05d}:{text_hash[:12]}"


def normalize_terms(query: str) -> list[str]:
    phrase = query.strip()
    terms: list[str] = [phrase] if phrase else []
    for part in re.split(r"[^\w+\-]+", phrase, flags=re.UNICODE):
        cleaned = part.strip()
        if len(cleaned) >= 2:
            terms.append(cleaned)
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(term)
    return unique


def fts_query(terms: list[str]) -> str:
    quoted = []
    for term in terms:
        cleaned = term.replace('"', '""').strip()
        if cleaned:
            quoted.append(f'"{cleaned}"')
    return " OR ".join(quoted) if quoted else '""'


def match_terms(text: str, terms: list[str]) -> list[str]:
    text_cf = text.casefold()
    matched: list[str] = []
    for term in terms:
        if term.casefold() in text_cf:
            matched.append(term)
    return matched


def get_snippet(text: str, terms: list[str]) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return ""
    compact_cf = compact.casefold()
    for term in terms:
        pos = compact_cf.find(term.casefold())
        if pos >= 0:
            start = max(0, pos - 160)
            return compact[start : start + 520].strip()
    return compact[:420].strip()


def normalize_heading(value: str) -> str | None:
    cleaned = re.sub(r"[*_`]+", "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def normalize_text_block(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def normalize_optional_text(value: object) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def normalize_list(values: list[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def coerce_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def resolve_optional(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row else 0


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
