from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from powerlit.services.export import write_markdown
from powerlit.services.library_layout import build_library_location, resolve_journal_short_name
from powerlit.settings import Settings

ISSUE_CATALOG_JSON_FILENAME = "_issue_catalog.json"
ISSUE_CATALOG_MARKDOWN_FILENAME = "_issue_catalog.md"
JOURNAL_CATALOG_JSON_FILENAME = "_journal_catalog.json"
JOURNAL_CATALOG_MARKDOWN_FILENAME = "_journal_catalog.md"
SUMMARY_CHAR_LIMIT = 220
DOWNLOADED_LABEL = "\u5df2\u4e0b\u8f7d"
PARSED_LABEL = "\u5df2\u89e3\u6790"
ANALYZED_LABEL = "\u5df2\u5206\u6790"


class CatalogViewService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def refresh_for_doi(self, doi: str) -> int:
        normalized_doi = normalize_doi(doi)
        if not normalized_doi:
            return 0
        refreshed = 0
        for issue_json_path in self.find_issue_catalog_paths_for_doi(normalized_doi):
            if self.refresh_issue_catalog(issue_json_path):
                refreshed += 1
        return refreshed

    def refresh_journal_catalog(self, journal_dir: str | Path) -> bool:
        resolved_dir = self.resolve_journal_dir(journal_dir)
        if not resolved_dir.exists():
            return False
        refreshed = False
        for issue_json_path in self.iter_issue_catalog_json_paths(resolved_dir):
            if self.refresh_issue_catalog(issue_json_path):
                refreshed = True
        return refreshed

    def refresh_all_journal_catalogs(self, journal_filters: list[str] | None = None) -> int:
        refreshed = 0
        normalized_filters = {
            normalize_journal_filter(item)
            for item in (journal_filters or [])
            if normalize_journal_filter(item)
        }
        journal_dirs = sorted(
            path for path in self.settings.reference_dir.iterdir() if path.is_dir()
        )
        for journal_dir in journal_dirs:
            if (
                normalized_filters
                and normalize_journal_filter(journal_dir.name) not in normalized_filters
            ):
                continue
            for issue_json_path in self.iter_issue_catalog_json_paths(journal_dir):
                if self.refresh_issue_catalog(issue_json_path):
                    refreshed += 1
        return refreshed

    def refresh_issue_catalog(self, issue_json_path: str | Path) -> bool:
        resolved_path = self.resolve_workspace_path(issue_json_path)
        if not resolved_path.exists():
            return False
        payload = load_json(resolved_path)
        states = self.build_issue_article_states(payload)
        markdown_path = resolved_path.with_name(ISSUE_CATALOG_MARKDOWN_FILENAME)
        write_markdown(markdown_path, render_issue_catalog_view(payload, states))
        return True

    def summarize_issue_payload(self, payload: dict[str, Any]) -> dict[str, int]:
        states = self.build_issue_article_states(payload)
        return {
            "article_count": len(states),
            "downloaded_count": sum(1 for item in states if item["downloaded"]),
            "parsed_count": sum(1 for item in states if item["parsed"]),
            "analyzed_count": sum(1 for item in states if item["analyzed"]),
        }

    def build_issue_article_states(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        articles = payload.get("articles") or []
        doi_rows = self.load_rows_by_dois(
            [normalize_doi(article.get("doi")) for article in articles if article.get("doi")]
        )
        title_rows = self.load_rows_by_source_and_titles(
            source_title=string_or_none(payload.get("source_title")),
            titles=[string_or_none(article.get("title")) for article in articles],
        )

        states: list[dict[str, Any]] = []
        for article in articles:
            matched_row = select_matching_row(
                article=article,
                issue_payload=payload,
                doi_rows=doi_rows,
                title_rows=title_rows,
            )
            states.append(build_article_state(article=article, row=matched_row))
        return states

    def find_issue_catalog_paths_for_doi(self, doi: str) -> list[Path]:
        canonical_match = self.find_canonical_issue_catalog_path(doi)
        if canonical_match is not None:
            return [canonical_match]

        paths: list[Path] = []
        for issue_json_path in self.iter_issue_catalog_json_paths():
            payload = load_json(issue_json_path)
            for article in payload.get("articles") or []:
                if normalize_doi(article.get("doi")) == doi:
                    paths.append(issue_json_path)
                    break
        return paths

    def find_canonical_issue_catalog_path(self, doi: str) -> Path | None:
        row = self.load_row_by_doi(doi)
        if row is None:
            return None
        location = build_library_location(
            self.settings.reference_dir,
            source_title=row.get("source_title"),
            volume=row.get("volume"),
            issue=row.get("issue"),
            year=row.get("year"),
        )
        issue_json_path = location.directory / ISSUE_CATALOG_JSON_FILENAME
        if issue_json_path.exists():
            payload = load_json(issue_json_path)
            if any(
                normalize_doi(article.get("doi")) == doi
                for article in payload.get("articles") or []
            ):
                return issue_json_path
        journal_dir = self.settings.reference_dir / resolve_journal_short_name(
            row.get("source_title")
        )
        if not journal_dir.exists():
            return None
        for candidate in self.iter_issue_catalog_json_paths(journal_dir):
            payload = load_json(candidate)
            if any(
                normalize_doi(article.get("doi")) == doi
                for article in payload.get("articles") or []
            ):
                return candidate
        return None

    def iter_issue_catalog_json_paths(self, journal_dir: Path | None = None) -> list[Path]:
        if journal_dir is not None:
            search_root = journal_dir
            if not search_root.exists():
                return []
            return sorted(search_root.rglob(ISSUE_CATALOG_JSON_FILENAME))
        return sorted(self.settings.reference_dir.rglob(ISSUE_CATALOG_JSON_FILENAME))

    def resolve_journal_dir_for_issue_catalog(self, issue_json_path: Path) -> Path | None:
        try:
            relative = issue_json_path.resolve().relative_to(self.settings.reference_dir.resolve())
        except ValueError:
            return None
        if not relative.parts:
            return None
        return self.settings.reference_dir / relative.parts[0]

    def resolve_journal_dir(self, value: str | Path) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path.resolve()
        candidate = self.settings.reference_dir / path
        if candidate.exists():
            return candidate.resolve()
        return (self.settings.reference_dir / normalize_journal_filter(path.as_posix())).resolve()

    def resolve_workspace_path(self, value: str | Path | None) -> Path:
        if value is None:
            return self.settings.reference_dir / "__missing__"
        path = Path(value)
        if path.is_absolute():
            return path.resolve()
        workspace_root = self.settings.reference_dir.parents[1].resolve()
        return (workspace_root / path).resolve()

    def load_row_by_doi(self, doi: str) -> dict[str, Any] | None:
        try:
            with sqlite3.connect(self.settings.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT
                        doi,
                        title,
                        year,
                        source_title,
                        volume,
                        issue
                    FROM papers
                    WHERE lower(doi) = ?
                    LIMIT 1
                    """,
                    (doi,),
                ).fetchone()
        except sqlite3.OperationalError:
            return None
        return dict(row) if row else None

    def load_rows_by_dois(self, dois: list[str | None]) -> dict[str, list[dict[str, Any]]]:
        normalized_dois = sorted({doi for doi in dois if doi})
        if not normalized_dois:
            return {}
        placeholders = ", ".join("?" for _ in normalized_dois)
        try:
            with sqlite3.connect(self.settings.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"""
                    SELECT
                        title,
                        authors_json,
                        abstract,
                        doi,
                        source_title,
                        year,
                        volume,
                        issue,
                        download_status,
                        local_pdf_path,
                        parsed_json_path,
                        parsed_md_path,
                        analysis_md_path,
                        analysis_json_path
                    FROM papers
                    WHERE lower(doi) IN ({placeholders})
                    """,
                    normalized_dois,
                ).fetchall()
        except sqlite3.OperationalError:
            return {}
        mapping: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            payload = dict(row)
            mapping.setdefault(normalize_doi(payload.get("doi")) or "", []).append(payload)
        return mapping

    def load_rows_by_source_and_titles(
        self,
        *,
        source_title: str | None,
        titles: list[str | None],
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_source = normalize_journal_filter(source_title)
        normalized_titles = {
            normalize_title_key(title)
            for title in titles
            if normalize_title_key(title)
        }
        if not normalized_source or not normalized_titles:
            return {}
        try:
            with sqlite3.connect(self.settings.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT
                        title,
                        authors_json,
                        abstract,
                        doi,
                        source_title,
                        year,
                        volume,
                        issue,
                        download_status,
                        local_pdf_path,
                        parsed_json_path,
                        parsed_md_path,
                        analysis_md_path,
                        analysis_json_path
                    FROM papers
                    WHERE lower(source_title) = ?
                    """,
                    (normalized_source,),
                ).fetchall()
        except sqlite3.OperationalError:
            return {}
        mapping: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            payload = dict(row)
            key = normalize_title_key(payload.get("title"))
            if key and key in normalized_titles:
                mapping.setdefault(key, []).append(payload)
        return mapping


def build_article_state(*, article: dict[str, Any], row: dict[str, Any] | None) -> dict[str, Any]:
    local_pdf_path = valid_existing_path(row.get("local_pdf_path") if row else None)
    parsed_json_path = valid_existing_path(row.get("parsed_json_path") if row else None)
    parsed_md_path = valid_existing_path(row.get("parsed_md_path") if row else None)
    analysis_json_path = valid_existing_path(row.get("analysis_json_path") if row else None)
    analysis_md_path = valid_existing_path(row.get("analysis_md_path") if row else None)
    abstract = build_article_summary(row)
    return {
        "title": string_or_none(article.get("title")) or "unknown",
        "section": string_or_none(article.get("section")),
        "doi": normalize_doi(article.get("doi")),
        "authors": merge_author_text(
            article.get("authors"),
            row.get("authors_json") if row else None,
        ),
        "is_open_access": article.get("is_open_access"),
        "publisher_url": string_or_none(article.get("publisher_url")),
        "pages": string_or_none(article.get("pages")),
        "published_date": string_or_none(article.get("published_date")),
        "downloaded": local_pdf_path is not None,
        "parsed": parsed_json_path is not None or parsed_md_path is not None,
        "analyzed": analysis_json_path is not None or analysis_md_path is not None,
        "download_status": string_or_none(row.get("download_status") if row else None) or "pending",
        "local_pdf_path": str(local_pdf_path) if local_pdf_path else None,
        "parsed_json_path": str(parsed_json_path) if parsed_json_path else None,
        "parsed_md_path": str(parsed_md_path) if parsed_md_path else None,
        "analysis_json_path": str(analysis_json_path) if analysis_json_path else None,
        "analysis_md_path": str(analysis_md_path) if analysis_md_path else None,
        "abstract": abstract,
    }


def build_article_summary(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    analysis_json_path = valid_existing_path(row.get("analysis_json_path"))
    if analysis_json_path is not None:
        summary = extract_analysis_summary(analysis_json_path)
        if summary:
            return summary
    abstract = normalize_inline_text(row.get("abstract"))
    if abstract:
        return shorten_summary(abstract)
    return None


def extract_analysis_summary(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    analysis = payload.get("analysis") or {}
    if not isinstance(analysis, dict):
        return None
    candidates: list[str] = []
    for key in ("research_problem", "power_system_context", "relevance", "caution"):
        value = normalize_inline_text(analysis.get(key))
        if value:
            candidates.append(value)
    key_findings = analysis.get("key_findings") or []
    if isinstance(key_findings, list):
        for value in key_findings:
            normalized = normalize_inline_text(value)
            if normalized:
                candidates.append(normalized)
    for candidate in candidates:
        return shorten_summary(candidate)
    return None


def select_matching_row(
    *,
    article: dict[str, Any],
    issue_payload: dict[str, Any],
    doi_rows: dict[str, list[dict[str, Any]]],
    title_rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    article_doi = normalize_doi(article.get("doi"))
    if article_doi and article_doi in doi_rows:
        rows = doi_rows[article_doi]
    else:
        rows = title_rows.get(normalize_title_key(article.get("title")), [])
    if not rows:
        return None
    issue_year = parse_int(issue_payload.get("year"))
    issue_volume = normalize_numeric_text(issue_payload.get("volume"))
    issue_number = normalize_numeric_text(issue_payload.get("issue"))
    scored_rows = sorted(
        rows,
        key=lambda row: (
            score_row_match(
                row,
                issue_year=issue_year,
                issue_volume=issue_volume,
                issue_number=issue_number,
            ),
            bool(row.get("abstract")),
            bool(row.get("analysis_json_path")),
        ),
        reverse=True,
    )
    return scored_rows[0]


def score_row_match(
    row: dict[str, Any],
    *,
    issue_year: int | None,
    issue_volume: str | None,
    issue_number: str | None,
) -> int:
    score = 0
    if issue_year is not None and parse_int(row.get("year")) == issue_year:
        score += 3
    if issue_volume and normalize_numeric_text(row.get("volume")) == issue_volume:
        score += 3
    if issue_number and normalize_numeric_text(row.get("issue")) == issue_number:
        score += 2
    if row.get("local_pdf_path"):
        score += 1
    return score


def render_issue_catalog_view(payload: dict[str, Any], states: list[dict[str, Any]]) -> str:
    downloaded_count = sum(1 for item in states if item["downloaded"])
    parsed_count = sum(1 for item in states if item["parsed"])
    analyzed_count = sum(1 for item in states if item["analyzed"])
    lines = [
        "# Issue Catalog",
        "",
        f"- Journal: {payload.get('source_title') or 'unknown'}",
        f"- Provider: {payload.get('provider') or 'unknown'}",
        f"- Year: {payload.get('year') or 'unknown'}",
        f"- Volume: {payload.get('volume') or 'unknown'}",
        f"- Issue: {payload.get('issue') or 'unknown'}",
        f"- Directory: {payload.get('directory') or 'unknown'}",
        f"- Article Metadata Status: {payload.get('article_metadata_status') or 'unknown'}",
        f"- Articles: {payload.get('article_count') or len(states)}",
        f"- Open Access Articles: {payload.get('open_access_article_count') or 0}",
        f"- Generated At: {payload.get('generated_at') or 'unknown'}",
        f"- Downloaded Articles: {downloaded_count}",
        f"- Parsed Articles: {parsed_count}",
        f"- Analyzed Articles: {analyzed_count}",
    ]
    if payload.get("issue_source_url"):
        lines.append(f"- Issue Source URL: {payload['issue_source_url']}")
    if payload.get("cover_image_url"):
        lines.append(f"- Cover Image URL: {payload['cover_image_url']}")
    if payload.get("notes"):
        lines.append("- Notes:")
        for note in payload["notes"]:
            lines.append(f"  - {note}")
    lines.extend(["", "## Articles", ""])
    if not states:
        lines.append("No article-level entries were written for this issue.")
        lines.append("")
        return "\n".join(lines)

    for index, article in enumerate(states, start=1):
        lines.extend(
            [
                f"### {index}. {article['title']}",
                "",
                f"- {render_checkbox(article['downloaded'])} {DOWNLOADED_LABEL}",
                f"- {render_checkbox(article['parsed'])} {PARSED_LABEL}",
                f"- {render_checkbox(article['analyzed'])} {ANALYZED_LABEL}",
                f"- DOI: {article['doi'] or 'missing'}",
                f"- Authors: {article['authors'] or 'unknown'}",
                f"- Section: {article['section'] or 'unknown'}",
                f"- Pages: {article['pages'] or 'unknown'}",
                f"- OA: {render_oa_state(article['is_open_access'])}",
                f"- Download Status: {article['download_status']}",
                f"- Publisher URL: {article['publisher_url'] or 'missing'}",
                f"- Local PDF: {article['local_pdf_path'] or 'missing'}",
                f"- Parsed Markdown: {article['parsed_md_path'] or 'missing'}",
                f"- Analysis Markdown: {article['analysis_md_path'] or 'missing'}",
                f"- Summary: {article['abstract'] or 'pending'}",
                "",
            ]
        )
    return "\n".join(lines)


def render_journal_catalog_view(
    payload: dict[str, Any],
    *,
    issue_summaries: list[dict[str, Any]],
    total_articles: int,
    total_downloaded: int,
    total_parsed: int,
    total_analyzed: int,
) -> str:
    lines = [
        "# Journal Catalog",
        "",
        f"- Journal: {payload.get('source_title') or 'unknown'}",
        f"- Providers: {', '.join(payload.get('providers') or ['unknown'])}",
        f"- From Year: {payload.get('from_year') or 'unknown'}",
        f"- Until Year: {payload.get('until_year') or 'unknown'}",
        f"- Latest Synced Year: {payload.get('coverage_end_year') or 'none'}",
        f"- Issues: {payload.get('issue_count') or len(issue_summaries)}",
        f"- Articles: {payload.get('article_count') or total_articles}",
        f"- Open Access Articles: {payload.get('open_access_article_count') or 0}",
        f"- Incomplete Issues: {payload.get('incomplete_issue_count') or 0}",
        f"- Year-Only Directories: {payload.get('year_only_issue_count') or 0}",
        f"- Volume-Only Directories: {payload.get('volume_only_issue_count') or 0}",
        f"- Cleaned Stale Directories: {payload.get('cleaned_directory_count') or 0}",
        f"- Generated At: {payload.get('generated_at') or 'unknown'}",
        f"- Downloaded Articles: {total_downloaded}/{total_articles}",
        f"- Parsed Articles: {total_parsed}/{total_articles}",
        f"- Analyzed Articles: {total_analyzed}/{total_articles}",
    ]
    if payload.get("warnings"):
        lines.append("- Warnings:")
        for warning in payload["warnings"]:
            lines.append(f"  - {warning}")
    lines.extend(["", "## Issues", ""])
    if not issue_summaries:
        lines.append("No issues were written for this journal.")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            (
                "| Year | Volume | Issue | Articles | Downloaded | Parsed | "
                "Analyzed | OA | Status | Directory |"
            ),
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for issue in issue_summaries:
        lines.append(
            (
                "| {year} | {volume} | {issue_no} | {articles} | {downloaded} | {parsed} | "
                "{analyzed} | {oa} | {status} | {directory} |"
            ).format(
                year=issue.get("year") or "unknown",
                volume=issue.get("volume") or "unknown",
                issue_no=issue.get("issue") or "unknown",
                articles=issue.get("article_count") or 0,
                downloaded=issue.get("downloaded_count") or 0,
                parsed=issue.get("parsed_count") or 0,
                analyzed=issue.get("analyzed_count") or 0,
                oa=issue.get("open_access_article_count") or 0,
                status=issue.get("article_metadata_status") or "unknown",
                directory=issue.get("directory") or "unknown",
            )
        )
    lines.append("")
    return "\n".join(lines)


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def normalize_doi(value: Any) -> str | None:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    normalized = normalized.removeprefix("doi:")
    normalized = normalized.strip()
    return normalized.lower() or None


def normalize_title_key(value: Any) -> str:
    text = normalize_inline_text(value)
    if not text:
        return ""
    return "".join(char for char in text.casefold() if char.isalnum())


def normalize_journal_filter(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def normalize_inline_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = " ".join(str(value).split())
    return text or None


def normalize_numeric_text(value: Any) -> str | None:
    text = normalize_inline_text(value)
    if not text:
        return None
    digits = "".join(char for char in text if char.isdigit())
    if digits:
        return digits.lstrip("0") or "0"
    return text.casefold()


def render_checkbox(flag: bool) -> str:
    return "[x]" if flag else "[ ]"


def render_oa_state(value: Any) -> str:
    if value is True:
        return "OA"
    if value is False:
        return "Closed"
    return "Unknown"


def merge_author_text(article_authors: Any, authors_json: Any) -> str | None:
    normalized_article_authors = normalize_inline_text(article_authors)
    if normalized_article_authors:
        return normalized_article_authors
    if authors_json in (None, ""):
        return None
    try:
        payload = json.loads(authors_json) if isinstance(authors_json, str) else authors_json
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    names: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        literal = normalize_inline_text(item.get("literal"))
        if literal:
            names.append(literal)
            continue
        parts = [
            normalize_inline_text(item.get("given")),
            normalize_inline_text(item.get("family")),
        ]
        full_name = " ".join(part for part in parts if part)
        if full_name:
            names.append(full_name)
    if not names:
        return None
    return ", ".join(names)


def shorten_summary(value: str) -> str:
    normalized = normalize_inline_text(value) or ""
    if len(normalized) <= SUMMARY_CHAR_LIMIT:
        return normalized
    return normalized[: SUMMARY_CHAR_LIMIT - 3].rstrip() + "..."


def string_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def valid_existing_path(value: Any) -> Path | None:
    text = string_or_none(value)
    if not text:
        return None
    path = Path(text)
    if path.exists():
        return path.resolve()
    return None
