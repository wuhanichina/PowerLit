from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
import sys

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from powerlit.citations import format_gbt_7714  # noqa: E402
from powerlit.models import Author, DocumentType, PaperRecord  # noqa: E402
from powerlit.providers.crossref import map_crossref_type, parse_crossref_date  # noqa: E402
from powerlit.services.library_layout import normalize_known_source_title  # noqa: E402
from powerlit.services.publisher_links import resolve_publisher_url  # noqa: E402
from powerlit.settings import Settings  # noqa: E402

CROSSREF_WORKS_ENDPOINT = "https://api.crossref.org/works/{doi}"
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class AuditRow:
    dedupe_key: str
    doi: str
    title: str
    source_title: str | None
    year: int | None
    document_type: str | None
    volume: str | None
    issue: str | None
    pages: str | None
    article_number: str | None
    publisher: str | None
    publisher_url: str | None
    authors_json: str | None
    parsed_json_path: str | None
    parsed_md_path: str | None
    raw_json: str | None


@dataclass(slots=True)
class CrossrefMetadata:
    title: str | None
    source_title: str | None
    year: int | None
    published_date: str | None
    document_type: DocumentType
    volume: str | None
    issue: str | None
    pages: str | None
    article_number: str | None
    publisher: str | None
    publisher_url: str | None
    authors: list[Author]
    raw: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit parsed paper metadata against Crossref by DOI and optionally repair mismatches."
    )
    parser.add_argument("--limit", type=int, default=0, help="最多审计多少条；0 表示全部。")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--apply", action="store_true", help="实际写回数据库和解析 JSON。")
    parser.add_argument("--sleep", type=float, default=0.05, help="Crossref 请求间隔秒数。")
    parser.add_argument("--cache-dir", type=Path, default=PROJECT_ROOT / "output/analysis/crossref-cache")
    parser.add_argument(
        "--report-path",
        type=Path,
        default=PROJECT_ROOT
        / f"output/analysis/parsed-metadata-crossref-audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl",
    )
    parser.add_argument("--summary-path", type=Path, default=None)
    parser.add_argument("--only-suspicious", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = args.summary_path or args.report_path.with_suffix(".summary.json")

    rows = iter_rows(
        settings.db_path,
        limit=args.limit,
        offset=args.offset,
        only_suspicious=args.only_suspicious,
    )
    session = requests.Session()
    counters: dict[str, int] = {
        "loaded": 0,
        "checked": 0,
        "cache_hits": 0,
        "crossref_hits": 0,
        "crossref_missing": 0,
        "changed": 0,
        "updated": 0,
        "json_updated": 0,
        "failed": 0,
        "skipped_not_suspicious": 0,
    }
    reason_counts: dict[str, int] = {}

    with args.report_path.open("w", encoding="utf-8") as report:
        for index, row in enumerate(rows, start=1):
            counters["loaded"] += 1
            if args.only_suspicious and not is_suspicious(row):
                counters["skipped_not_suspicious"] += 1
                continue
            counters["checked"] += 1
            try:
                metadata, from_cache = load_crossref_metadata(
                    row.doi,
                    cache_dir=args.cache_dir,
                    session=session,
                    settings=settings,
                )
                if from_cache:
                    counters["cache_hits"] += 1
                else:
                    counters["crossref_hits"] += 1
                    if args.sleep:
                        time.sleep(args.sleep)
                if metadata is None:
                    counters["crossref_missing"] += 1
                    write_report(report, {"doi": row.doi, "status": "crossref_missing"})
                    continue

                changes, reasons = compute_changes(row, metadata)
                for reason in reasons:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                status = "changed" if changes else "ok"
                if changes:
                    counters["changed"] += 1
                    if args.apply:
                        update_database(settings.db_path, row, metadata, changes)
                        counters["updated"] += 1
                        if update_parsed_json(row, metadata, changes):
                            counters["json_updated"] += 1
                write_report(
                    report,
                    {
                        "index": index,
                        "doi": row.doi,
                        "status": status,
                        "reasons": reasons,
                        "changes": changes,
                    },
                )
                if index <= 5 or index % 100 == 0 or changes:
                    print(
                        f"[{index}] {status} doi={row.doi} "
                        f"changes={len(changes)} checked={counters['checked']}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001
                counters["failed"] += 1
                write_report(
                    report,
                    {
                        "index": index,
                        "doi": row.doi,
                        "status": "failed",
                        "error": str(exc),
                    },
                )

    summary = {
        "started_at": datetime.now(UTC).isoformat(),
        "apply": args.apply,
        "db_path": str(settings.db_path),
        "report_path": str(args.report_path),
        "counters": counters,
        "reason_counts": dict(sorted(reason_counts.items())),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if counters["failed"] == 0 else 1


def iter_rows(db_path: Path, *, limit: int, offset: int, only_suspicious: bool = False):
    sql = """
        select
            dedupe_key,
            doi,
            title,
            source_title,
            year,
            document_type,
            volume,
            issue,
            pages,
            article_number,
            publisher,
            publisher_url,
            authors_json,
            parsed_json_path,
            parsed_md_path,
            null as raw_json
        from papers
        where doi is not null and trim(doi) != ''
          and (
            (parsed_json_path is not null and trim(parsed_json_path) != '')
            or (parsed_md_path is not null and trim(parsed_md_path) != '')
          )
    """
    params: list[int] = []
    if only_suspicious:
        sql += """
          and (
            source_title is null
            or trim(source_title) = ''
            or source_title = title
            or length(title) > 180
            or length(source_title) > 80
            or (document_type = 'conference-paper' and lower(doi) like '10.1109/t%')
          )
        """
    if limit > 0:
        sql += " limit ? offset ?"
        params.extend([limit, offset])
    with sqlite3.connect(db_path, timeout=60) as conn:
        conn.row_factory = sqlite3.Row
        rows = [AuditRow(**dict(row)) for row in conn.execute(sql, params).fetchall()]
    yield from rows


def load_crossref_metadata(
    doi: str,
    *,
    cache_dir: Path,
    session: requests.Session,
    settings: Settings,
) -> tuple[CrossrefMetadata | None, bool]:
    cache_path = cache_dir / f"{safe_doi_filename(doi)}.json"
    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("status") == "missing":
            return None, True
        return parse_crossref_item(payload["message"], doi), True

    url = CROSSREF_WORKS_ENDPOINT.format(doi=quote(doi, safe=""))
    params = {"mailto": settings.crossref_mailto} if settings.crossref_mailto else None
    response = request_with_retries(session, url, params=params, timeout=settings.request_timeout)
    if response.status_code == 404:
        cache_path.write_text(json.dumps({"status": "missing"}, ensure_ascii=False), encoding="utf-8")
        return None, False
    response.raise_for_status()
    payload = response.json()
    message = payload.get("message")
    if not isinstance(message, dict):
        cache_path.write_text(json.dumps({"status": "missing"}, ensure_ascii=False), encoding="utf-8")
        return None, False
    cache_path.write_text(json.dumps({"status": "ok", "message": message}, ensure_ascii=False), encoding="utf-8")
    return parse_crossref_item(message, doi), False


def request_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str] | None,
    timeout: float,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(min(2**attempt, 20))
            continue
        if response.status_code not in TRANSIENT_STATUS:
            return response
        time.sleep(min(2**attempt, 20))
    if last_exc is not None:
        raise last_exc
    return response


def parse_crossref_item(item: dict[str, Any], doi: str) -> CrossrefMetadata:
    title = first_joined(item.get("title"))
    container = first_joined(item.get("container-title"))
    published = item.get("published-print") or item.get("published-online") or item.get("published") or {}
    published_date = parse_crossref_date(published.get("date-parts"))
    item_doi = str(item.get("DOI") or doi).strip().lower()
    source_title = normalize_known_source_title(container, doi=item_doi)
    authors = [
        Author(given=author.get("given"), family=author.get("family"), literal=author.get("name"))
        for author in item.get("author", [])
        if isinstance(author, dict)
    ]
    return CrossrefMetadata(
        title=title,
        source_title=source_title,
        year=published_date.year if published_date else None,
        published_date=published_date.isoformat() if published_date else None,
        document_type=map_crossref_type(item.get("type"), doi=item_doi),
        volume=optional_text(item.get("volume")),
        issue=optional_text(item.get("issue")),
        pages=optional_text(item.get("page")),
        article_number=optional_text(item.get("article-number")),
        publisher=optional_text(item.get("publisher")),
        publisher_url=resolve_publisher_url(item_doi, item.get("URL")),
        authors=authors,
        raw=item,
    )


def compute_changes(row: AuditRow, metadata: CrossrefMetadata) -> tuple[dict[str, dict[str, Any]], list[str]]:
    changes: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []

    compare_text_change(changes, reasons, "title", row.title, metadata.title, reason="title_mismatch")
    compare_text_change(
        changes,
        reasons,
        "source_title",
        row.source_title,
        metadata.source_title,
        reason="source_title_mismatch",
    )
    compare_exact_change(changes, reasons, "year", row.year, metadata.year, reason="year_mismatch")
    compare_exact_change(
        changes,
        reasons,
        "document_type",
        row.document_type,
        metadata.document_type.value,
        reason="document_type_mismatch",
    )
    compare_text_change(changes, reasons, "volume", row.volume, metadata.volume, reason="volume_mismatch")
    compare_text_change(changes, reasons, "issue", row.issue, metadata.issue, reason="issue_mismatch")
    return changes, sorted(set(reasons))


def compare_text_change(
    changes: dict[str, dict[str, Any]],
    reasons: list[str],
    field: str,
    old: str | None,
    new: str | None,
    *,
    reason: str,
) -> None:
    if not new:
        return
    if normalize_compare_text(old) == normalize_compare_text(new):
        return
    changes[field] = {"old": old, "new": new}
    reasons.append(reason)


def compare_exact_change(
    changes: dict[str, dict[str, Any]],
    reasons: list[str],
    field: str,
    old: Any,
    new: Any,
    *,
    reason: str,
) -> None:
    if new in (None, ""):
        return
    if old == new:
        return
    changes[field] = {"old": old, "new": new}
    reasons.append(reason)


def update_database(
    db_path: Path,
    row: AuditRow,
    metadata: CrossrefMetadata,
    changes: dict[str, dict[str, Any]],
) -> None:
    authors_json = changes.get("authors_json", {}).get("new", row.authors_json)
    record = PaperRecord(
        title=changes.get("title", {}).get("new", row.title),
        authors=metadata.authors or build_authors_from_json(authors_json),
        year=changes.get("year", {}).get("new", row.year),
        published_date=metadata.published_date,
        document_type=metadata.document_type,
        source_title=changes.get("source_title", {}).get("new", row.source_title),
        publisher=changes.get("publisher", {}).get("new", row.publisher),
        volume=changes.get("volume", {}).get("new", row.volume),
        issue=changes.get("issue", {}).get("new", row.issue),
        pages=changes.get("pages", {}).get("new", row.pages),
        article_number=changes.get("article_number", {}).get("new", row.article_number),
        doi=row.doi,
        publisher_url=changes.get("publisher_url", {}).get("new", row.publisher_url),
    )
    gbt7714 = format_gbt_7714(record)
    updated_at = datetime.now(UTC).isoformat()
    with sqlite3.connect(db_path, timeout=60) as conn:
        raw_json = row.raw_json
        if raw_json is None:
            raw_row = conn.execute(
                "select raw_json from papers where dedupe_key = ?",
                (row.dedupe_key,),
            ).fetchone()
            raw_json = raw_row[0] if raw_row else None
        raw_payload = merge_raw_json(raw_json, metadata.raw)
        conn.execute(
            """
            update papers
            set title = ?,
                gbt7714_citation = ?,
                authors_json = ?,
                published_date = ?,
                publisher = ?,
                volume = ?,
                issue = ?,
                pages = ?,
                article_number = ?,
                doi = ?,
                publisher_url = ?,
                year = ?,
                document_type = ?,
                source_title = ?,
                raw_json = ?,
                updated_at = ?
            where dedupe_key = ?
            """,
            (
                record.title,
                gbt7714,
                authors_json,
                metadata.published_date,
                record.publisher,
                record.volume,
                record.issue,
                record.pages,
                record.article_number,
                record.doi,
                record.publisher_url,
                record.year,
                record.document_type.value,
                record.source_title,
                json.dumps(raw_payload, ensure_ascii=False, default=str),
                updated_at,
                row.dedupe_key,
            ),
        )


def update_parsed_json(
    row: AuditRow,
    metadata: CrossrefMetadata,
    changes: dict[str, dict[str, Any]],
) -> bool:
    if not row.parsed_json_path:
        return False
    path = Path(row.parsed_json_path)
    if not path.exists():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    touched = False
    for field in ("title", "source_title", "doi", "page_count"):
        if field in changes:
            payload[field] = changes[field]["new"]
            touched = True
    if "title" in changes and isinstance(payload.get("content"), str):
        old_title = changes["title"]["old"]
        new_title = changes["title"]["new"]
        content = payload["content"]
        if old_title and content.startswith(f"# {old_title}"):
            payload["content"] = content.replace(f"# {old_title}", f"# {new_title}", 1)
            touched = True
    if touched:
        payload["metadata_audit"] = {
            "source": "crossref",
            "updated_at": datetime.now(UTC).isoformat(),
            "changes": changes,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return touched


def is_suspicious(row: AuditRow) -> bool:
    source_title = row.source_title or ""
    title = row.title or ""
    if not source_title or source_title == title:
        return True
    if len(title) > 180:
        return True
    if len(source_title) > 80:
        return True
    if row.document_type == "conference-paper" and row.doi.lower().startswith("10.1109/t"):
        return True
    return False


def merge_raw_json(raw_json: str | None, crossref_raw: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(raw_json) if raw_json else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload["crossref_audit"] = crossref_raw
    return payload


def normalize_json_authors(value: str | None) -> list[dict[str, Any]]:
    try:
        payload = json.loads(value) if value else []
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [
        {key: item[key] for key in ("given", "family", "literal") if isinstance(item, dict) and item.get(key)}
        for item in payload
    ]


def build_authors_from_json(value: str | None) -> list[Author]:
    return [Author(**item) for item in normalize_json_authors(value)]


def write_report(handle, payload: dict[str, Any]) -> None:  # noqa: ANN001
    handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    handle.flush()


def safe_doi_filename(doi: str) -> str:
    return doi.lower().replace("/", "__").replace(":", "_")


def first_joined(value: Any) -> str | None:
    if isinstance(value, list):
        text = " ".join(str(item) for item in value if str(item).strip())
    else:
        text = str(value) if value is not None else ""
    text = " ".join(text.split())
    return text or None


def optional_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    return text or None


def normalize_compare_text(value: Any) -> str:
    if value is None:
        return ""
    return "".join(ch.casefold() for ch in str(value) if ch.isalnum())


if __name__ == "__main__":
    raise SystemExit(main())
