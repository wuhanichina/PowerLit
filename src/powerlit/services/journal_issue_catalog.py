from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from re import search, sub
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from powerlit.services.catalog_views import CatalogViewService
from powerlit.models import JournalSpec
from powerlit.services.library_layout import (
    build_library_location,
    format_issue_folder,
    infer_annual_volume,
    normalize_numeric_token,
    resolve_journal_short_name,
)
from powerlit.settings import Settings

ISSUE_CATALOG_MARKDOWN_FILENAME = "_issue_catalog.md"
ISSUE_CATALOG_JSON_FILENAME = "_issue_catalog.json"
JOURNAL_CATALOG_MARKDOWN_FILENAME = "_journal_catalog.md"
JOURNAL_CATALOG_JSON_FILENAME = "_journal_catalog.json"
CATALOG_ARTIFACT_FILENAMES = {
    ISSUE_CATALOG_MARKDOWN_FILENAME,
    ISSUE_CATALOG_JSON_FILENAME,
}
OPENALEX_WORKS_ENDPOINT = "https://api.openalex.org/works"
CROSSREF_WORKS_ENDPOINT = "https://api.crossref.org/works/{doi}"
CSEE_PERIOD_TREE_ENDPOINT = "https://epjournal.csee.org.cn/rc-pub/front/front-period/getPeriodTree"
CSEE_ARTICLE_TOC_ENDPOINT = (
    "https://epjournal.csee.org.cn/rc-pub/front/front-article/"
    "getArticlesByPeriodicalIdGroupByColumn/{issue_id}"
)
CSEE_PORTAL_HOME_URL = "https://epjournal.csee.org.cn/zh/volumn/home"
CSEE_ARTICLE_PAGE_URL_TEMPLATE = "https://epjournal.csee.org.cn/zh/article/doi/{doi}/"
CSEE_PORTAL_SITE_IDS = {
    "中国电机工程学报": 964,
}
CNKI_NAVI_DETAIL_URLS = {
    "aeps": (
        "http://navi--cnki--net--https.cnki.mdjsf.utuvpn.utuedu.com:9000/"
        "knavi/detail?p=6g8PAuFSOvL0ODsQsGYLZafsxizNfojLJOnDxUDbkTrexcKjXLjdvUSSbyAhq5kL"
        "B5OfZMM9FGRiIHMprImsm2oilfrLXiwxN_iya7EN7H95MojhGYndrQ==&uniplatform=NZKPT"
        "&language=CHS"
    ),
    "pcsee": (
        "http://navi--cnki--net--https.cnki.mdjsf.utuvpn.utuedu.com:9000/"
        "knavi/detail?p=6g8PAuFSOvJHy_shrkU6CzFhT13sdjBIIeBQvpFXJ3qiYOG6CrHXquAzw7ANtnVl"
        "_XgLU2a_WASym7HXV4fZm01QfmoYQyXbxdHfFU_Z1LxKZQs_7G2wDA==&uniplatform=NZKPT"
        "&language=CHS"
    ),
    "pst": (
        "http://navi--cnki--net--https.cnki.mdjsf.utuvpn.utuedu.com:9000/"
        "knavi/detail?p=6g8PAuFSOvLWXUiZwaFvBfPRUQ_muRGdQTyZ-QiKh9vecsP-CLpv8I_2P496hbxy-"
        "VvhkdbrUjCqKuL-Q5CQa8O7d2idv_BQ2FHU1SGGLRwELiU9_c384g==&uniplatform=NZKPT"
        "&language=CHS"
    ),
}
OPENALEX_SOURCE_IDS = {
    "电力系统自动化": "S2764498263",
    "电网技术": "S2764649243",
}
OPENALEX_EXCLUDED_DOI_PREFIXES = {
    "电网技术": ("10.52783/pst.",),
}
OPENALEX_EXCLUDED_HOST_SUBSTRINGS = {
    "电网技术": ("powertechjournal.com",),
}
CSEE_FRONT_MATTER_KEYWORDS = (
    "目录",
    "目次",
    "英文大摘要",
    "中文英文目录",
    "征稿",
    "编委会",
    "版权",
    "版权页",
    "封面",
    "封二",
    "封三",
    "封底",
)


"""
Normalize Chinese journal mappings here so provider resolution and directory
generation use stable names even if earlier literals were imported from a
garbled source file.
"""
CSEE_PORTAL_SITE_IDS = {
    "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5": 964,
}
OPENALEX_SOURCE_IDS = {
    "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316": "S2764498263",
    "\u7535\u7f51\u6280\u672f": "S2764649243",
}
OPENALEX_EXCLUDED_DOI_PREFIXES = {
    "\u7535\u7f51\u6280\u672f": ("10.52783/pst.",),
}
OPENALEX_EXCLUDED_HOST_SUBSTRINGS = {
    "\u7535\u7f51\u6280\u672f": ("powertechjournal.com",),
}
OPENALEX_ALLOWED_DOI_PREFIXES = {
    "applied_energy": ("10.1016/j.apenergy.",),
    "energy": ("10.1016/j.energy.",),
    "ieee_tpwrs": ("10.1109/tpwrs.",),
    "ieee_tsg": ("10.1109/tsg.",),
    "ieee_tste": ("10.1109/tste.",),
    "ijepes": ("10.1016/j.ijepes.",),
}
CSEE_FRONT_MATTER_KEYWORDS = (
    "\u76ee\u5f55",
    "\u76ee\u6b21",
    "\u82f1\u6587\u5927\u6458\u8981",
    "\u4e2d\u82f1\u6587\u76ee\u5f55",
    "\u5f81\u7a3f",
    "\u7f16\u59d4\u4f1a",
    "\u7248\u6743",
    "\u7248\u6743\u9875",
    "\u5c01\u9762",
    "\u5c01\u4e8c",
    "\u5c01\u4e09",
    "\u5c01\u5e95",
)
OPENALEX_FRONT_MATTER_TITLE_KEYWORDS = (
    "table of contents",
    "blank page",
    "information for authors",
    "publication information",
    "scholarship plus",
    "introducing the ieee",
    "ieee pes resource center",
)

@dataclass(slots=True)
class IssueArticleEntry:
    title: str
    section: str | None
    doi: str | None
    is_open_access: bool | None
    publisher_url: str | None
    pages: str | None
    published_date: str | None
    authors: str | None = None


@dataclass(slots=True)
class IssueCatalogEntry:
    journal_short_name: str
    source_title: str
    provider: str
    year: int
    volume: str | None
    issue: str | None
    directory: Path
    issue_source_url: str | None = None
    cover_image_url: str | None = None
    article_metadata_status: str = "complete"
    notes: list[str] = field(default_factory=list)
    articles: list[IssueArticleEntry] = field(default_factory=list)

    @property
    def open_access_article_count(self) -> int:
        return sum(1 for article in self.articles if article.is_open_access)

    @property
    def article_count(self) -> int:
        return len(self.articles)


@dataclass(slots=True)
class JournalIssueCatalogSyncResult:
    journal_short_name: str
    source_title: str
    from_year: int
    until_year: int
    issue_count: int
    article_count: int
    open_access_article_count: int
    incomplete_issue_count: int
    year_only_issue_count: int
    volume_only_issue_count: int
    coverage_end_year: int | None
    cleaned_directory_count: int
    warnings: list[str]
    journal_index_path: Path | None


@dataclass(slots=True)
class JournalCatalogAudit:
    year_only_issue_count: int
    volume_only_issue_count: int
    coverage_end_year: int | None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CatalogCleanupResult:
    removed_count: int
    failed_directories: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class CnkiNaviContext:
    detail_url: str
    base_url: str
    pykm: str
    pcode: str
    time_token: str
    language: str
    platform: str


@dataclass(slots=True)
class CnkiNaviIssueRef:
    year: int
    issue: str | None
    year_issue_token: str


class JournalIssueCatalogService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})
        self._primed_csee_site_ids: set[int] = set()
        self.catalog_views = CatalogViewService(settings)

    def sync_journal(
        self,
        spec: JournalSpec,
        *,
        from_year: int | None = None,
        until_year: int | None = None,
    ) -> JournalIssueCatalogSyncResult:
        effective_from_year = from_year or spec.from_year or 2000
        effective_until_year = until_year or spec.until_year or datetime.now(UTC).year
        issue_entries = self.fetch_issue_catalogs(
            spec,
            from_year=effective_from_year,
            until_year=effective_until_year,
        )
        for entry in issue_entries:
            self.write_issue_catalog(entry)
        audit = audit_journal_issue_entries(
            issue_entries,
            from_year=effective_from_year,
            until_year=effective_until_year,
        )
        if should_cleanup_issue_catalog_directories(
            spec,
            from_year=effective_from_year,
            until_year=effective_until_year,
        ):
            cleanup_result = self.cleanup_unreferenced_issue_directories(
                spec,
                issue_entries=issue_entries,
            )
        else:
            cleanup_result = CatalogCleanupResult(removed_count=0)
            audit.warnings.append(
                "Skipped stale-directory cleanup because this sync covered only a "
                "partial year range."
            )
        if cleanup_result.failed_directories:
            failed_directories = ", ".join(
                path.as_posix() for path in cleanup_result.failed_directories[:3]
            )
            if len(cleanup_result.failed_directories) > 3:
                failed_directories += ", ..."
            audit.warnings.append(
                "Some stale issue directories could not be removed automatically: "
                f"{failed_directories}."
            )
        return JournalIssueCatalogSyncResult(
            journal_short_name=resolve_journal_short_name(spec.title),
            source_title=spec.title,
            from_year=effective_from_year,
            until_year=effective_until_year,
            issue_count=len(issue_entries),
            article_count=sum(entry.article_count for entry in issue_entries),
            open_access_article_count=sum(
                entry.open_access_article_count for entry in issue_entries
            ),
            incomplete_issue_count=sum(
                1 for entry in issue_entries if entry.article_metadata_status != "complete"
            ),
            year_only_issue_count=audit.year_only_issue_count,
            volume_only_issue_count=audit.volume_only_issue_count,
            coverage_end_year=audit.coverage_end_year,
            cleaned_directory_count=cleanup_result.removed_count,
            warnings=audit.warnings,
            journal_index_path=None,
        )

    def fetch_issue_catalogs(
        self,
        spec: JournalSpec,
        *,
        from_year: int,
        until_year: int,
    ) -> list[IssueCatalogEntry]:
        provider = self.resolve_catalog_provider(spec)
        if provider == "csee_portal":
            try:
                entries = self.fetch_csee_issue_catalogs(
                    spec,
                    from_year=from_year,
                    until_year=until_year,
                )
            except requests.RequestException:
                entries = []
            if entries:
                return entries
            if can_use_cnki_navi(spec):
                return self.fetch_cnki_navi_issue_catalogs(
                    spec,
                    from_year=from_year,
                    until_year=until_year,
                )
            return entries
        if provider == "cnki_navi":
            return self.fetch_cnki_navi_issue_catalogs(
                spec,
                from_year=from_year,
                until_year=until_year,
            )
        return self.fetch_openalex_issue_catalogs(
            spec,
            from_year=from_year,
            until_year=until_year,
        )

    def resolve_catalog_provider(self, spec: JournalSpec) -> str:
        providers = [item.strip().lower() for item in spec.providers if item.strip()]
        for provider in providers:
            if provider in {"cnki_navi", "csee_portal", "openalex"}:
                return provider
        if spec.short_name in CNKI_NAVI_DETAIL_URLS:
            return "cnki_navi"
        if resolve_journal_short_name(spec.title) == "中国电机工程学报":
            return "csee_portal"
        return "openalex"

    def fetch_openalex_issue_catalogs(
        self,
        spec: JournalSpec,
        *,
        from_year: int,
        until_year: int,
    ) -> list[IssueCatalogEntry]:
        if not spec.issns:
            raise ValueError(f"OpenAlex issue catalog sync requires ISSN: {spec.title}")

        issue_map: dict[tuple[int, str | None, str | None], IssueCatalogEntry] = {}
        seen_articles: set[str] = set()
        crossref_cache: dict[str, dict[str, str | None]] = {}
        cursor = "*"
        while cursor:
            params: dict[str, str | int] = {
                "filter": ",".join(
                    [
                        "type:article",
                        "is_paratext:false",
                        resolve_openalex_source_filter(spec),
                        f"from_publication_date:{from_year}-01-01",
                        f"to_publication_date:{until_year}-12-31",
                    ]
                ),
                "per-page": 200,
                "cursor": cursor,
                "sort": "publication_date:asc",
            }
            if self.settings.crossref_mailto:
                params["mailto"] = self.settings.crossref_mailto

            payload = self.get_json(OPENALEX_WORKS_ENDPOINT, params=params)
            items = payload.get("results") or []
            if not items:
                break

            for item in items:
                biblio = item.get("biblio") or {}
                doi = normalize_doi(item.get("doi") or (item.get("ids") or {}).get("doi"))
                volume = string_or_none(biblio.get("volume"))
                issue = string_or_none(biblio.get("issue"))
                first_page = string_or_none(biblio.get("first_page"))
                last_page = string_or_none(biblio.get("last_page"))
                pages = f"{first_page}-{last_page}" if first_page and last_page else first_page
                crossref_metadata: dict[str, str | None] = {}
                if doi and (not volume or not issue or not pages):
                    crossref_metadata = crossref_cache.get(doi) or self.fetch_crossref_work_metadata(
                        doi
                    )
                    crossref_cache[doi] = crossref_metadata
                    volume = volume or crossref_metadata.get("volume")
                    issue = issue or crossref_metadata.get("issue")
                    pages = pages or crossref_metadata.get("pages")

                article = build_openalex_article_entry(
                    item,
                    pages_override=pages,
                    publisher_url_override=crossref_metadata.get("publisher_url"),
                )
                if article is None:
                    continue
                if not should_include_openalex_article(spec, item, article):
                    continue
                dedupe_key = article.doi or build_article_fallback_key(article)
                if dedupe_key in seen_articles:
                    continue
                seen_articles.add(dedupe_key)

                year = parse_publication_year(item)
                if year is None or year < from_year or year > until_year:
                    continue
                issue_key = (year, volume, issue)
                catalog = issue_map.get(issue_key)
                if catalog is None:
                    directory = build_issue_catalog_directory(
                        self.settings.reference_dir,
                        year=year,
                        source_title=spec.title,
                        volume=volume,
                        issue=issue,
                    )
                    catalog = IssueCatalogEntry(
                        journal_short_name=resolve_journal_short_name(spec.title),
                        source_title=spec.title,
                        provider="openalex",
                        year=year,
                        volume=volume,
                        issue=issue,
                        directory=directory,
                    )
                    issue_map[issue_key] = catalog
                catalog.articles.append(article)

            cursor = payload.get("meta", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(0.1)

        entries = [
            entry
            for entry in finalize_issue_catalog_entries(
                self.settings.reference_dir,
                list(issue_map.values()),
            )
            if is_reference_ready_openalex_entry(entry)
        ]
        for entry in entries:
            entry.articles.sort(key=article_sort_key)
        return sorted(entries, key=issue_sort_key)

    def fetch_crossref_work_metadata(self, doi: str) -> dict[str, str | None]:
        try:
            payload = self.get_json(
                CROSSREF_WORKS_ENDPOINT.format(doi=requests.utils.quote(doi, safe="")),
                params={"mailto": self.settings.crossref_mailto}
                if self.settings.crossref_mailto
                else None,
            )
        except requests.RequestException:
            return {}
        item = payload.get("message") or {}
        return {
            "volume": string_or_none(item.get("volume")),
            "issue": string_or_none(item.get("issue")),
            "pages": string_or_none(item.get("page")),
            "publisher_url": string_or_none(item.get("URL")) or doi_url(doi),
        }

    def fetch_csee_issue_catalogs(
        self,
        spec: JournalSpec,
        *,
        from_year: int,
        until_year: int,
    ) -> list[IssueCatalogEntry]:
        site_id = resolve_csee_site_id(spec)
        self.prime_csee_portal(site_id)
        payload = self.get_json(
            CSEE_PERIOD_TREE_ENDPOINT,
            params={
                "siteId": site_id,
                "groupByYear": 1,
                "periodAsc": 1,
                "isBack": 1,
            },
        )
        roots = payload.get("data") or []
        if not roots:
            return []
        root = roots[0]
        entries: list[IssueCatalogEntry] = []
        for decade_key, years in root.items():
            if decade_key == "publicationName":
                continue
            if not isinstance(years, list):
                continue
            for year_entry in years:
                year = int(year_entry.get("year") or 0)
                if year < from_year or year > until_year:
                    continue
                for issue_entry in year_entry.get("periods") or []:
                    issue_id = issue_entry.get("id")
                    volume = string_or_none(issue_entry.get("volume"))
                    issue = string_or_none(issue_entry.get("issue"))
                    directory = build_issue_catalog_directory(
                        self.settings.reference_dir,
                        year=year,
                        source_title=spec.title,
                        volume=volume,
                        issue=issue,
                    )
                    try:
                        articles, toc_notes = self.fetch_csee_issue_articles(
                            site_id=site_id,
                            issue_id=issue_id,
                        )
                        if articles:
                            article_metadata_status = "complete"
                            notes = toc_notes
                        else:
                            article_metadata_status = "issue_tree_only"
                            notes = toc_notes or [
                                (
                                    "The official TOC endpoint returned no article-level "
                                    "records for this issue during sync."
                                )
                            ]
                    except requests.RequestException as exc:
                        articles = []
                        article_metadata_status = "issue_tree_only"
                        notes = [
                            (
                                "Article-level TOC sync failed for this issue: "
                                f"{exc.__class__.__name__}"
                            )
                        ]
                    entries.append(
                        IssueCatalogEntry(
                            journal_short_name=resolve_journal_short_name(spec.title),
                            source_title=spec.title,
                            provider="csee_portal",
                            year=year,
                            volume=volume,
                            issue=issue,
                            directory=directory,
                            cover_image_url=string_or_none(issue_entry.get("pictureUrl")),
                            article_metadata_status=article_metadata_status,
                            notes=notes,
                            articles=articles,
                        )
                    )
        entries = finalize_issue_catalog_entries(self.settings.reference_dir, entries)
        return sorted(entries, key=issue_sort_key)

    def fetch_csee_issue_articles(
        self,
        *,
        site_id: int,
        issue_id: int | str | None,
    ) -> tuple[list[IssueArticleEntry], list[str]]:
        if issue_id in (None, ""):
            return [], ["The issue tree entry did not provide a stable issue id for TOC sync."]
        payload = self.get_json(
            CSEE_ARTICLE_TOC_ENDPOINT.format(issue_id=issue_id),
            params={
                "showCover": "true",
                "timestamps": int(time.time() * 1000),
            },
            headers={
                "siteId": str(site_id),
                "language": "zh",
            },
        )
        seen_articles: set[str] = set()
        articles: list[IssueArticleEntry] = []
        filtered_count = 0
        groups = payload.get("data") or []
        for group in groups:
            default_section = string_or_none(group.get("column"))
            for item in group.get("content") or []:
                article = build_csee_article_entry(item, default_section=default_section)
                if article is None:
                    filtered_count += 1
                    continue
                dedupe_key = article.doi or build_article_fallback_key(article)
                if dedupe_key in seen_articles:
                    continue
                seen_articles.add(dedupe_key)
                articles.append(article)
        articles.sort(key=article_sort_key)
        notes: list[str] = []
        if filtered_count:
            notes.append(
                f"Filtered {filtered_count} front-matter or non-article TOC entries "
                "from the official issue feed."
            )
        missing_doi_count = sum(1 for article in articles if not article.doi)
        if missing_doi_count:
            notes.append(
                f"The official TOC omitted DOI values for {missing_doi_count} article "
                "entries in this issue."
            )
        missing_page_count = sum(1 for article in articles if not article.pages)
        if missing_page_count:
            notes.append(
                f"The official TOC omitted page ranges for {missing_page_count} article "
                "entries in this issue."
            )
        return articles, notes

    def fetch_cnki_navi_issue_catalogs(
        self,
        spec: JournalSpec,
        *,
        from_year: int,
        until_year: int,
    ) -> list[IssueCatalogEntry]:
        detail_url = resolve_cnki_navi_detail_url(spec)
        context = self.fetch_cnki_navi_context(detail_url)
        year_list_html = self.fetch_cnki_navi_year_list_html(context)
        issue_refs = parse_cnki_navi_issue_refs(
            year_list_html,
            from_year=from_year,
            until_year=until_year,
        )
        openalex_year_cache: dict[int, dict[str, list[IssueArticleEntry]]] = {}
        entries: list[IssueCatalogEntry] = []
        for issue_ref in issue_refs:
            article_metadata_status = "complete"
            try:
                issue_html = self.fetch_cnki_navi_issue_html(
                    context,
                    year_issue_token=issue_ref.year_issue_token,
                )
            except requests.RequestException as exc:
                articles = []
                notes = [
                    (
                        "CNKI issue TOC sync failed for this issue: "
                        f"{exc.__class__.__name__}"
                    )
                ]
                article_metadata_status = "issue_tree_only"
            else:
                openalex_index = openalex_year_cache.get(issue_ref.year)
                if openalex_index is None:
                    openalex_index = self.fetch_openalex_title_index_for_year(
                        spec,
                        year=issue_ref.year,
                    )
                    openalex_year_cache[issue_ref.year] = openalex_index
                articles, notes = build_cnki_navi_issue_articles(
                    issue_html,
                    year=issue_ref.year,
                    openalex_index=openalex_index,
                )
            issue_source_url = self.fetch_cnki_navi_issue_source_url(
                context,
                year_issue_token=issue_ref.year_issue_token,
            )
            if issue_source_url:
                notes.append(
                    "Issue source URL points to the CNKI original catalog or reading page "
                    "and may still require an authenticated browser session."
                )
            directory = build_issue_catalog_directory(
                self.settings.reference_dir,
                year=issue_ref.year,
                source_title=spec.title,
                volume=None,
                issue=issue_ref.issue,
            )
            entries.append(
                IssueCatalogEntry(
                    journal_short_name=resolve_journal_short_name(spec.title),
                    source_title=spec.title,
                    provider="cnki_navi",
                    year=issue_ref.year,
                    volume=None,
                    issue=issue_ref.issue,
                    directory=directory,
                    issue_source_url=issue_source_url,
                    article_metadata_status=article_metadata_status,
                    notes=notes,
                    articles=articles,
                )
            )
        entries = finalize_issue_catalog_entries(self.settings.reference_dir, entries)
        return sorted(entries, key=issue_sort_key)

    def fetch_cnki_navi_context(self, detail_url: str) -> CnkiNaviContext:
        response = self.session.get(
            detail_url,
            headers=build_cnki_navi_headers(),
            timeout=self.settings.request_timeout,
        )
        response.raise_for_status()
        html = response.text
        parsed = urlparse(detail_url)
        pykm = extract_cnki_hidden_input(html, "pykm")
        pcode = extract_cnki_hidden_input(html, "pCode")
        time_token = extract_cnki_hidden_input(html, "time")
        platform = extract_cnki_hidden_input(html, "hidDefaultPlatForm") or "NZKPT"
        language = extract_cnki_hidden_input(html, "hidDefaultLanguage") or "CHS"
        if not pykm or not pcode or not time_token:
            raise ValueError("CNKI detail page did not expose the expected issue catalog fields.")
        return CnkiNaviContext(
            detail_url=detail_url,
            base_url=f"{parsed.scheme}://{parsed.netloc}",
            pykm=pykm,
            pcode=pcode,
            time_token=time_token,
            language=language,
            platform=platform,
        )

    def fetch_cnki_navi_year_list_html(self, context: CnkiNaviContext) -> str:
        response = self.session.post(
            f"{context.base_url}/knavi/journals/{context.pykm}/yearList",
            headers=build_cnki_navi_headers(context=context, referer=context.detail_url),
            data={
                "pIdx": "0",
                "time": context.time_token,
                "isEpublish": "0",
                "pcode": context.pcode,
            },
            timeout=self.settings.request_timeout,
        )
        response.raise_for_status()
        return response.text

    def fetch_cnki_navi_issue_html(
        self,
        context: CnkiNaviContext,
        *,
        year_issue_token: str,
    ) -> str:
        response = self.session.post(
            (
                f"{context.base_url}/knavi/journals/{context.pykm}/papers"
                f"?yearIssue={year_issue_token}&pageIdx=0&pcode={context.pcode}&isEpublish=0"
            ),
            headers=build_cnki_navi_headers(context=context, referer=context.detail_url),
            timeout=self.settings.request_timeout,
        )
        response.raise_for_status()
        return response.text

    def fetch_cnki_navi_issue_source_url(
        self,
        context: CnkiNaviContext,
        *,
        year_issue_token: str,
    ) -> str | None:
        try:
            response = self.session.get(
                f"{context.base_url}/knavi/journals/{context.pykm}/catalog/exist",
                params={"yearIssue": year_issue_token},
                headers=build_cnki_navi_headers(context=context, referer=context.detail_url),
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
        except requests.RequestException:
            return None
        return string_or_none(response.text)

    def fetch_openalex_title_index_for_year(
        self,
        spec: JournalSpec,
        *,
        year: int,
    ) -> dict[str, list[IssueArticleEntry]]:
        if not spec.issns:
            return {}
        title_index: dict[str, list[IssueArticleEntry]] = {}
        cursor = "*"
        while cursor:
            params: dict[str, str | int] = {
                "filter": ",".join(
                    [
                        "type:article",
                        "is_paratext:false",
                        resolve_openalex_source_filter(spec),
                        f"from_publication_date:{year}-01-01",
                        f"to_publication_date:{year}-12-31",
                    ]
                ),
                "per-page": 200,
                "cursor": cursor,
                "sort": "publication_date:asc",
            }
            if self.settings.crossref_mailto:
                params["mailto"] = self.settings.crossref_mailto
            payload = self.get_json(OPENALEX_WORKS_ENDPOINT, params=params)
            items = payload.get("results") or []
            if not items:
                break
            for item in items:
                article = build_openalex_article_entry(item)
                if article is None or not should_include_openalex_article(spec, item, article):
                    continue
                title_key = normalize_title_lookup_key(article.title)
                if not title_key:
                    continue
                title_index.setdefault(title_key, []).append(article)
            cursor = payload.get("meta", {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(0.1)
        return title_index

    def write_issue_catalog(self, entry: IssueCatalogEntry) -> None:
        entry.directory.mkdir(parents=True, exist_ok=True)
        payload = build_issue_payload(self.settings, entry)
        json_path = entry.directory / ISSUE_CATALOG_JSON_FILENAME
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.catalog_views.refresh_issue_catalog(json_path)

    def write_journal_catalog(
        self,
        spec: JournalSpec,
        *,
        issue_entries: list[IssueCatalogEntry],
        from_year: int,
        until_year: int,
        audit: JournalCatalogAudit,
        cleaned_directory_count: int,
    ) -> Path:
        journal_short_name = resolve_journal_short_name(spec.title)
        journal_directory = self.settings.reference_dir / journal_short_name
        journal_directory.mkdir(parents=True, exist_ok=True)
        payload = build_journal_payload(
            self.settings,
            spec,
            issue_entries=issue_entries,
            from_year=from_year,
            until_year=until_year,
            audit=audit,
            cleaned_directory_count=cleaned_directory_count,
        )
        json_path = journal_directory / JOURNAL_CATALOG_JSON_FILENAME
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.catalog_views.refresh_journal_catalog(journal_directory)
        return journal_directory / JOURNAL_CATALOG_MARKDOWN_FILENAME

    def cleanup_unreferenced_issue_directories(
        self,
        spec: JournalSpec,
        *,
        issue_entries: list[IssueCatalogEntry],
    ) -> CatalogCleanupResult:
        journal_directory = self.settings.reference_dir / resolve_journal_short_name(spec.title)
        if not journal_directory.exists():
            return CatalogCleanupResult(removed_count=0)
        referenced_directories = {entry.directory.resolve() for entry in issue_entries}
        candidate_directories = sorted(
            (
                path
                for path in journal_directory.rglob("*")
                if path.is_dir() and contains_catalog_artifacts(path)
            ),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        cleaned_directory_count = 0
        failed_directories: list[Path] = []
        for directory in candidate_directories:
            if not directory.exists():
                continue
            if directory.resolve() in referenced_directories:
                continue
            if not is_safe_generated_catalog_directory(directory):
                continue
            try:
                shutil.rmtree(directory, onexc=retry_catalog_delete_with_chmod)
            except OSError:
                failed_directories.append(directory.relative_to(journal_directory))
                continue
            cleaned_directory_count += 1
            prune_empty_catalog_parents(directory.parent, stop=journal_directory)
        empty_cleanup_result = cleanup_empty_orphan_directories(
            journal_directory,
            referenced_directories=referenced_directories,
        )
        cleaned_directory_count += empty_cleanup_result.removed_count
        failed_directories.extend(
            path.relative_to(journal_directory) for path in empty_cleanup_result.failed_directories
        )
        return CatalogCleanupResult(
            removed_count=cleaned_directory_count,
            failed_directories=failed_directories,
        )

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> dict:
        for attempt in range(retries):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.settings.request_timeout,
                )
                if response.status_code == 429 and attempt + 1 < retries:
                    time.sleep(2**attempt)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException:
                if attempt + 1 >= retries:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError(f"Unable to fetch JSON from {url}")

    def prime_csee_portal(self, site_id: int) -> None:
        if site_id in self._primed_csee_site_ids:
            return
        try:
            self.session.get(
                CSEE_PORTAL_HOME_URL,
                headers={
                    "siteId": str(site_id),
                    "language": "zh",
                },
                timeout=self.settings.request_timeout,
            )
        except requests.RequestException:
            return
        self._primed_csee_site_ids.add(site_id)


def _resolve_catalog_provider_fixed(
    self: JournalIssueCatalogService,
    spec: JournalSpec,
) -> str:
    providers = [item.strip().lower() for item in spec.providers if item.strip()]
    if spec.short_name in CNKI_NAVI_DETAIL_URLS:
        for provider in providers:
            if provider in {"cnki_navi", "csee_portal"}:
                return provider
        return "cnki_navi"
    for provider in providers:
        if provider in {"cnki_navi", "csee_portal", "openalex"}:
            return provider
    if (
        resolve_journal_short_name(spec.title)
        == "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5"
    ):
        return "csee_portal"
    return "openalex"


JournalIssueCatalogService.resolve_catalog_provider = _resolve_catalog_provider_fixed


def build_openalex_article_entry(
    item: dict,
    *,
    pages_override: str | None = None,
    publisher_url_override: str | None = None,
) -> IssueArticleEntry | None:
    title = string_or_none(item.get("display_name"))
    if not title:
        return None
    doi = normalize_doi(item.get("doi") or (item.get("ids") or {}).get("doi"))
    open_access = item.get("open_access") or {}
    primary_location = item.get("primary_location") or {}
    best_oa_location = item.get("best_oa_location") or {}
    biblio = item.get("biblio") or {}
    first_page = string_or_none(biblio.get("first_page"))
    last_page = string_or_none(biblio.get("last_page"))
    if first_page and last_page:
        pages = f"{first_page}-{last_page}"
    else:
        pages = first_page
    publisher_url = (
        publisher_url_override
        or string_or_none(primary_location.get("landing_page_url"))
        or string_or_none(best_oa_location.get("landing_page_url"))
        or doi_url(doi)
    )
    is_open_access = open_access.get("is_oa")
    if is_open_access is not None:
        is_open_access = bool(is_open_access)
    authors = ", ".join(
        name
        for name in (
            clean_text((authorship.get("author") or {}).get("display_name"))
            for authorship in item.get("authorships") or []
        )
        if name
    ) or None
    return IssueArticleEntry(
        title=title,
        section=None,
        doi=doi,
        authors=authors,
        is_open_access=is_open_access,
        publisher_url=publisher_url,
        pages=pages_override or pages,
        published_date=string_or_none(item.get("publication_date")),
    )


def build_csee_article_entry(
    item: dict,
    *,
    default_section: str | None,
) -> IssueArticleEntry | None:
    title = clean_text(item.get("resName"))
    section = clean_text(item.get("part") or default_section or item.get("lanmu"))
    doi = normalize_doi(item.get("doi"))
    first_page = string_or_none(item.get("firstPageNum"))
    last_page = string_or_none(item.get("lastPageNum"))
    pages = format_page_range(first_page, last_page)
    authors = clean_text(item.get("authors"))
    if not title or is_csee_front_matter(title, section, doi=doi, authors=authors):
        return None
    if not doi and not authors:
        return None
    return IssueArticleEntry(
        title=title,
        section=section,
        doi=doi,
        authors=authors,
        is_open_access=parse_csee_open_access(item.get("oa")),
        publisher_url=build_csee_article_url(item, doi=doi),
        pages=pages,
        published_date=string_or_none(item.get("onlineDate")),
    )


def build_cnki_navi_issue_articles(
    issue_html: str,
    *,
    year: int,
    openalex_index: dict[str, list[IssueArticleEntry]],
) -> tuple[list[IssueArticleEntry], list[str]]:
    soup = BeautifulSoup(issue_html, "html.parser")
    seen_articles: set[str] = set()
    articles: list[IssueArticleEntry] = []
    current_section: str | None = None
    enriched_count = 0
    ambiguous_match_count = 0
    for node in soup.find_all(["dt", "dd"]):
        classes = set(node.get("class") or [])
        if node.name == "dt" and "tit" in classes:
            current_section = clean_text(node.get_text(" ", strip=True))
            continue
        if node.name != "dd" or "row" not in classes:
            continue
        article, was_enriched, was_ambiguous = build_cnki_navi_article_entry(
            node,
            default_section=current_section,
            year=year,
            openalex_index=openalex_index,
        )
        if article is None:
            continue
        dedupe_key = article.doi or build_article_fallback_key(article)
        if dedupe_key in seen_articles:
            continue
        seen_articles.add(dedupe_key)
        articles.append(article)
        if was_enriched:
            enriched_count += 1
        if was_ambiguous:
            ambiguous_match_count += 1
    articles.sort(key=article_sort_key)
    notes: list[str] = []
    if enriched_count:
        notes.append(
            f"Filled DOI and OA metadata from OpenAlex for {enriched_count} article "
            "entries by exact title match."
        )
    missing_doi_count = sum(1 for article in articles if not article.doi)
    if missing_doi_count:
        notes.append(
            f"DOI enrichment is still missing for {missing_doi_count} article entries "
            "in this issue."
        )
    unknown_oa_count = sum(1 for article in articles if article.is_open_access is None)
    if unknown_oa_count:
        notes.append(
            f"Open-access status is still unknown for {unknown_oa_count} article entries "
            "because the CNKI TOC does not expose stable OA flags directly."
        )
    if ambiguous_match_count:
        notes.append(
            f"Skipped {ambiguous_match_count} ambiguous OpenAlex title matches to avoid "
            "assigning an incorrect DOI."
        )
    return articles, notes


def build_cnki_navi_article_entry(
    node,
    *,
    default_section: str | None,
    year: int,
    openalex_index: dict[str, list[IssueArticleEntry]],
) -> tuple[IssueArticleEntry | None, bool, bool]:
    title_link = node.select_one("span.name a")
    title = clean_text(title_link.get_text(" ", strip=True) if title_link else None)
    if not title:
        return None, False, False
    section = clean_text(default_section)
    publisher_url = string_or_none(title_link.get("href") if title_link else None)
    page_span = node.select_one("span.company")
    pages = clean_text(page_span.get("title") if page_span else None)
    selected_match, was_ambiguous = select_openalex_title_match(
        openalex_index.get(normalize_title_lookup_key(title), []),
        expected_pages=pages,
    )
    was_enriched = selected_match is not None
    return (
        IssueArticleEntry(
            title=title,
            section=section,
            doi=selected_match.doi if selected_match else None,
            authors=selected_match.authors if selected_match else None,
            is_open_access=selected_match.is_open_access if selected_match else None,
            publisher_url=(
                selected_match.publisher_url
                if selected_match and selected_match.publisher_url
                else publisher_url
            ),
            pages=pages,
            published_date=selected_match.published_date if selected_match else f"{year}-01-01",
        ),
        was_enriched,
        was_ambiguous,
    )


def build_article_fallback_key(article: IssueArticleEntry) -> str:
    normalized_title = sub(r"\s+", " ", article.title.strip().lower())
    return "|".join(
        [
            article.section or "",
            normalized_title,
            article.pages or "",
            article.published_date or "",
        ]
    )


def parse_publication_year(item: dict) -> int | None:
    year = item.get("publication_year")
    if year:
        return int(year)
    published_date = string_or_none(item.get("publication_date"))
    if not published_date or len(published_date) < 4:
        return None
    try:
        return int(published_date[:4])
    except ValueError:
        return None


def article_sort_key(article: IssueArticleEntry) -> tuple[int, str]:
    page_token = article.pages or ""
    match = search(r"\d+", page_token)
    first_page = int(match.group(0)) if match else 10**9
    return (first_page, article.title.lower())


def issue_sort_key(entry: IssueCatalogEntry) -> tuple[int, int, int, str]:
    return (
        entry.year,
        parse_numeric_token(entry.volume),
        parse_numeric_token(entry.issue),
        entry.source_title.lower(),
    )


def build_issue_payload(settings: Settings, entry: IssueCatalogEntry) -> dict:
    return {
        "journal_short_name": entry.journal_short_name,
        "source_title": entry.source_title,
        "provider": entry.provider,
        "year": entry.year,
        "volume": entry.volume,
        "issue": entry.issue,
        "directory": workspace_path(settings, entry.directory),
        "issue_source_url": entry.issue_source_url,
        "cover_image_url": entry.cover_image_url,
        "article_metadata_status": entry.article_metadata_status,
        "article_count": entry.article_count,
        "open_access_article_count": entry.open_access_article_count,
        "notes": entry.notes,
        "articles": [
            {
                "title": article.title,
                "section": article.section,
                "doi": article.doi,
                "authors": article.authors,
                "is_open_access": article.is_open_access,
                "publisher_url": article.publisher_url,
                "pages": article.pages,
                "published_date": article.published_date,
            }
            for article in entry.articles
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }


def build_journal_payload(
    settings: Settings,
    spec: JournalSpec,
    *,
    issue_entries: list[IssueCatalogEntry],
    from_year: int,
    until_year: int,
    audit: JournalCatalogAudit,
    cleaned_directory_count: int,
) -> dict:
    return {
        "journal_short_name": resolve_journal_short_name(spec.title),
        "source_title": spec.title,
        "providers": list(
            dict.fromkeys([*spec.providers, *(entry.provider for entry in issue_entries)])
        ),
        "from_year": from_year,
        "until_year": until_year,
        "issue_count": len(issue_entries),
        "article_count": sum(entry.article_count for entry in issue_entries),
        "open_access_article_count": sum(
            entry.open_access_article_count for entry in issue_entries
        ),
        "incomplete_issue_count": sum(
            1 for entry in issue_entries if entry.article_metadata_status != "complete"
        ),
        "year_only_issue_count": audit.year_only_issue_count,
        "volume_only_issue_count": audit.volume_only_issue_count,
        "coverage_end_year": audit.coverage_end_year,
        "cleaned_directory_count": cleaned_directory_count,
        "warnings": audit.warnings,
        "issues": [
            {
                "year": entry.year,
                "volume": entry.volume,
                "issue": entry.issue,
                "directory": workspace_path(settings, entry.directory),
                "provider": entry.provider,
                "article_metadata_status": entry.article_metadata_status,
                "article_count": entry.article_count,
                "open_access_article_count": entry.open_access_article_count,
                "issue_catalog_markdown_path": workspace_path(
                    settings,
                    entry.directory / ISSUE_CATALOG_MARKDOWN_FILENAME,
                ),
                "issue_catalog_json_path": workspace_path(
                    settings,
                    entry.directory / ISSUE_CATALOG_JSON_FILENAME,
                ),
            }
            for entry in issue_entries
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }


def render_issue_catalog_markdown(payload: dict) -> str:
    lines = [
        "# Issue Catalog",
        "",
        f"- Journal: {payload['source_title']}",
        f"- Provider: {payload['provider']}",
        f"- Year: {payload['year']}",
        f"- Volume: {payload['volume'] or 'unknown'}",
        f"- Issue: {payload['issue'] or 'unknown'}",
        f"- Directory: {payload['directory']}",
        f"- Article Metadata Status: {payload['article_metadata_status']}",
        f"- Articles: {payload['article_count']}",
        f"- Open Access Articles: {payload['open_access_article_count']}",
        f"- Generated At: {payload['generated_at']}",
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

    if not payload["articles"]:
        lines.append("No article-level entries were written for this issue.")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "| # | Section | OA | DOI | Pages | Title | Publisher URL |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for index, article in enumerate(payload["articles"], start=1):
        oa = (
            "OA"
            if article["is_open_access"] is True
            else "Closed"
            if article["is_open_access"] is False
            else "Unknown"
        )
        section = (article.get("section") or "-").replace("|", "\\|")
        doi = article["doi"] or "missing"
        pages = article["pages"] or "missing"
        title = article["title"].replace("|", "\\|")
        publisher_url = article["publisher_url"] or "missing"
        lines.append(
            f"| {index} | {section} | {oa} | {doi} | {pages} | {title} | {publisher_url} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_journal_catalog_markdown(payload: dict) -> str:
    lines = [
        "# Journal Catalog",
        "",
        f"- Journal: {payload['source_title']}",
        f"- Providers: {', '.join(payload['providers'])}",
        f"- From Year: {payload['from_year']}",
        f"- Until Year: {payload['until_year']}",
        f"- Latest Synced Year: {payload['coverage_end_year'] or 'none'}",
        f"- Issues: {payload['issue_count']}",
        f"- Articles: {payload['article_count']}",
        f"- Open Access Articles: {payload['open_access_article_count']}",
        f"- Incomplete Issues: {payload['incomplete_issue_count']}",
        f"- Year-Only Directories: {payload['year_only_issue_count']}",
        f"- Volume-Only Directories: {payload['volume_only_issue_count']}",
        f"- Cleaned Stale Directories: {payload['cleaned_directory_count']}",
        f"- Generated At: {payload['generated_at']}",
    ]
    if payload.get("warnings"):
        lines.append("- Warnings:")
        for warning in payload["warnings"]:
            lines.append(f"  - {warning}")
    lines.extend(["", "## Issues", ""])
    if not payload["issues"]:
        lines.append("No issues were written for this journal.")
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "| Year | Volume | Issue | Articles | OA | Status | Directory |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for issue in payload["issues"]:
        lines.append(
            "| {year} | {volume} | {issue_no} | {articles} | {oa} | {status} | {directory} |".format(
                year=issue["year"],
                volume=issue["volume"] or "unknown",
                issue_no=issue["issue"] or "unknown",
                articles=issue["article_count"],
                oa=issue["open_access_article_count"],
                status=issue["article_metadata_status"],
                directory=issue["directory"],
            )
        )
    lines.append("")
    return "\n".join(lines)


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    return normalized.lower() or None


def doi_url(doi: str | None) -> str | None:
    if not doi:
        return None
    return f"https://doi.org/{doi}"


def string_or_none(value) -> str | None:  # noqa: ANN001
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def clean_text(value) -> str | None:  # noqa: ANN001
    text = string_or_none(value)
    if not text:
        return None
    normalized = unescape(text.replace("\xa0", " "))
    normalized = sub(r"<[^>]+>", "", normalized)
    normalized = sub(r"\s+", " ", normalized).strip()
    return normalized or None


def normalize_title_lookup_key(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return "".join(char for char in text.casefold() if char.isalnum())


def format_page_range(first_page: str | None, last_page: str | None) -> str | None:
    if first_page and last_page and first_page != last_page and last_page != "0":
        return f"{first_page}-{last_page}"
    return first_page or last_page


def is_csee_front_matter(
    title: str,
    section: str | None,
    *,
    doi: str | None,
    authors: str | None,
) -> bool:
    lowered_title = title.lower()
    lowered_section = (section or "").lower()
    has_front_matter_keyword = any(
        keyword.lower() in lowered_title or keyword.lower() in lowered_section
        for keyword in CSEE_FRONT_MATTER_KEYWORDS
    )
    return has_front_matter_keyword and not doi and not authors


def is_openalex_front_matter(article: IssueArticleEntry) -> bool:
    """Detect non-article front/back matter returned by OpenAlex (IEEE indexes,
    TOC pages, blank pages, scholarship ads, etc.)."""
    if article.authors:
        return False
    title = article.title.lower().strip()
    if title.startswith("[") and title.endswith("]"):
        title = title[1:-1].strip()
    if any(kw in title for kw in OPENALEX_FRONT_MATTER_TITLE_KEYWORDS):
        return True
    if search(r"\b\d{4}\s+index\s+ieee\b", title):
        return True
    return False


def parse_csee_open_access(value) -> bool | None:  # noqa: ANN001
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "oa"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    return None


def build_csee_article_url(item: dict, *, doi: str | None) -> str | None:
    full_text_url = string_or_none(item.get("fullTextUrl"))
    if full_text_url:
        return urljoin("https://epjournal.csee.org.cn", full_text_url)
    if doi:
        return CSEE_ARTICLE_PAGE_URL_TEMPLATE.format(doi=doi)
    return None


def parse_numeric_token(value: str | None) -> int:
    if not value:
        return 0
    match = search(r"\d+", value)
    if not match:
        return 0
    return int(match.group(0))


def normalize_page_lookup_key(value: str | None) -> str:
    text = clean_text(value)
    if not text:
        return ""
    return "".join(char for char in text if char.isalnum())


def select_openalex_title_match(
    candidates: list[IssueArticleEntry],
    *,
    expected_pages: str | None,
) -> tuple[IssueArticleEntry | None, bool]:
    if not candidates:
        return None, False
    if len(candidates) == 1:
        return candidates[0], False
    page_key = normalize_page_lookup_key(expected_pages)
    if page_key:
        page_matches = [
            candidate
            for candidate in candidates
            if normalize_page_lookup_key(candidate.pages) == page_key
        ]
        if len(page_matches) == 1:
            return page_matches[0], False
        if page_matches:
            return None, True
    doi_matches = [candidate for candidate in candidates if candidate.doi]
    if len(doi_matches) == 1:
        return doi_matches[0], False
    return None, True


def resolve_csee_site_id(spec: JournalSpec) -> int:
    journal_key = resolve_journal_short_name(spec.title)
    if journal_key in CSEE_PORTAL_SITE_IDS:
        return CSEE_PORTAL_SITE_IDS[journal_key]
    if spec.title in CSEE_PORTAL_SITE_IDS:
        return CSEE_PORTAL_SITE_IDS[spec.title]
    raise ValueError(f"No CSEE portal site id configured for journal: {spec.title}")


def can_use_cnki_navi(spec: JournalSpec) -> bool:
    providers = [item.strip().lower() for item in spec.providers if item.strip()]
    return "cnki_navi" in providers or spec.short_name in CNKI_NAVI_DETAIL_URLS


def resolve_cnki_navi_detail_url(spec: JournalSpec) -> str:
    detail_url = CNKI_NAVI_DETAIL_URLS.get(spec.short_name)
    if detail_url:
        return detail_url
    raise ValueError(f"No CNKI Navigator detail URL configured for journal: {spec.short_name}")


def extract_cnki_hidden_input(html: str, field_name: str) -> str | None:
    match = search(rf'id="{field_name}"[^>]*value="([^"]*)"', html)
    return string_or_none(match.group(1)) if match else None


def parse_cnki_navi_issue_refs(
    year_list_html: str,
    *,
    from_year: int,
    until_year: int,
) -> list[CnkiNaviIssueRef]:
    soup = BeautifulSoup(year_list_html, "html.parser")
    seen_tokens: set[str] = set()
    refs: list[CnkiNaviIssueRef] = []
    for year_block in soup.select('#YearIssueTree dl[id$="_Year_Issue"]'):
        block_id = string_or_none(year_block.get("id"))
        if not block_id:
            continue
        year_text = block_id.split("_", 1)[0]
        try:
            year = int(year_text)
        except ValueError:
            continue
        if year < from_year or year > until_year:
            continue
        for issue_link in year_block.select("dd a[id^='yq']"):
            token = string_or_none(issue_link.get("value"))
            if not token or token in seen_tokens:
                continue
            seen_tokens.add(token)
            refs.append(
                CnkiNaviIssueRef(
                    year=year,
                    issue=extract_cnki_issue_number(issue_link.get_text(" ", strip=True)),
                    year_issue_token=token,
                )
            )
    return sorted(
        refs,
        key=lambda item: (item.year, parse_numeric_token(item.issue), item.year_issue_token),
    )


def extract_cnki_issue_number(text: str | None) -> str | None:
    raw_text = clean_text(text)
    if not raw_text:
        return None
    match = search(r"(\d+)", raw_text)
    if match:
        digits = match.group(1)
        return digits.zfill(2) if len(digits) < 2 else digits
    return raw_text


def build_cnki_navi_headers(
    *,
    context: CnkiNaviContext | None = None,
    referer: str | None = None,
) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if context:
        headers["language"] = context.language
        headers["uniplatform"] = context.platform
        headers["X-Requested-With"] = "XMLHttpRequest"
    if referer:
        headers["Referer"] = referer
    return headers


def workspace_path(settings: Settings, path: Path) -> str:
    root = settings.literature_root.parent.resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path.resolve())


def resolve_openalex_source_filter(spec: JournalSpec) -> str:
    journal_key = resolve_journal_short_name(spec.title)
    source_id = OPENALEX_SOURCE_IDS.get(journal_key) or OPENALEX_SOURCE_IDS.get(spec.title)
    if source_id:
        return f"primary_location.source.id:{source_id}"
    if not spec.issns:
        raise ValueError(f"OpenAlex issue catalog sync requires ISSN or mapped source id: {spec.title}")
    return "|".join(f"locations.source.issn:{issn}" for issn in spec.issns)


def should_include_openalex_article(
    spec: JournalSpec,
    item: dict,
    article: IssueArticleEntry,
) -> bool:
    if is_openalex_front_matter(article):
        return False
    journal_key = resolve_journal_short_name(spec.title)
    doi = (article.doi or "").lower()
    if not doi:
        return False
    allowed_doi_prefixes = OPENALEX_ALLOWED_DOI_PREFIXES.get(journal_key, ())
    if allowed_doi_prefixes and not any(doi.startswith(prefix) for prefix in allowed_doi_prefixes):
        return False
    excluded_doi_prefixes = OPENALEX_EXCLUDED_DOI_PREFIXES.get(journal_key, ())
    if any(doi.startswith(prefix) for prefix in excluded_doi_prefixes):
        return False
    landing_candidates = [
        article.publisher_url,
        string_or_none((item.get("primary_location") or {}).get("landing_page_url")),
        string_or_none((item.get("best_oa_location") or {}).get("landing_page_url")),
        string_or_none((item.get("primary_location") or {}).get("pdf_url")),
        string_or_none((item.get("best_oa_location") or {}).get("pdf_url")),
    ]
    excluded_hosts = OPENALEX_EXCLUDED_HOST_SUBSTRINGS.get(journal_key, ())
    lowered_candidates = [candidate.lower() for candidate in landing_candidates if candidate]
    return not any(host in candidate for host in excluded_hosts for candidate in lowered_candidates)


def is_reference_ready_openalex_entry(entry: IssueCatalogEntry) -> bool:
    if not entry.volume:
        return False
    if entry.journal_short_name.startswith("ieee_") and not entry.issue:
        return False
    return True


def build_issue_catalog_directory(
    base_dir: Path,
    *,
    year: int,
    source_title: str,
    volume: str | None,
    issue: str | None,
) -> Path:
    effective_volume = volume or infer_annual_journal_volume(source_title, year)
    if effective_volume:
        return build_library_location(
            base_dir,
            source_title=source_title,
            volume=effective_volume,
            issue=issue,
            year=year,
        ).directory
    journal_short_name = resolve_journal_short_name(source_title)
    directory = base_dir / journal_short_name / f"y{year:04d}"
    issue_folder = format_issue_folder(issue)
    if issue_folder:
        directory /= issue_folder
    return directory


def audit_journal_issue_entries(
    issue_entries: list[IssueCatalogEntry],
    *,
    from_year: int,
    until_year: int,
) -> JournalCatalogAudit:
    year_only_by_year: dict[int, int] = {}
    volume_only_by_year: dict[int, int] = {}
    coverage_end_year = max((entry.year for entry in issue_entries), default=None)
    for entry in issue_entries:
        if not entry.volume:
            year_only_by_year[entry.year] = year_only_by_year.get(entry.year, 0) + 1
        elif not entry.issue:
            volume_only_by_year[entry.year] = volume_only_by_year.get(entry.year, 0) + 1
    warnings: list[str] = []
    if year_only_by_year:
        warnings.append(
            "Year-only directories were used for "
            f"{sum(year_only_by_year.values())} issue catalogs because source volume "
            f"metadata was missing. Years: {format_year_count_map(year_only_by_year)}."
        )
    if volume_only_by_year:
        warnings.append(
            "Issue-level subdirectories were missing for "
            f"{sum(volume_only_by_year.values())} issue catalogs because source issue "
            f"metadata was missing. Years: {format_year_count_map(volume_only_by_year)}."
        )
    if coverage_end_year is None:
        warnings.append(
            "No issue catalogs were synced for the requested year range "
            f"{from_year}-{until_year}."
        )
    elif coverage_end_year < until_year:
        warnings.append(
            f"Requested coverage extends through {until_year}, but the latest synced "
            f"issue year is {coverage_end_year}."
        )
    return JournalCatalogAudit(
        year_only_issue_count=sum(year_only_by_year.values()),
        volume_only_issue_count=sum(volume_only_by_year.values()),
        coverage_end_year=coverage_end_year,
        warnings=warnings,
    )


def finalize_issue_catalog_entries(
    reference_dir: Path,
    entries: list[IssueCatalogEntry],
) -> list[IssueCatalogEntry]:
    if not entries:
        return []
    year_to_observed_volumes: dict[int, set[str]] = {}
    for entry in entries:
        normalized_volume = normalize_numeric_token(entry.volume)
        if normalized_volume:
            year_to_observed_volumes.setdefault(entry.year, set()).add(normalized_volume)

    merged_entries: dict[tuple[str, int, str | None, str | None], IssueCatalogEntry] = {}
    for entry in entries:
        inferred_volume = None
        if not entry.volume:
            inferred_volume = infer_issue_catalog_volume(
                source_title=entry.source_title,
                year=entry.year,
                observed_year_volumes=year_to_observed_volumes.get(entry.year, set()),
            )
        if inferred_volume:
            entry.volume = inferred_volume
            entry.directory = build_issue_catalog_directory(
                reference_dir,
                year=entry.year,
                source_title=entry.source_title,
                volume=inferred_volume,
                issue=entry.issue,
            )
        key = (
            entry.journal_short_name,
            entry.year,
            normalize_numeric_token(entry.volume),
            normalize_numeric_token(entry.issue),
        )
        existing = merged_entries.get(key)
        if existing is None:
            merged_entries[key] = entry
            continue
        merge_issue_catalog_entry(existing, entry)
    return drop_redundant_volume_only_entries(list(merged_entries.values()))


def infer_issue_catalog_volume(
    *,
    source_title: str | None,
    year: int,
    observed_year_volumes: set[str],
) -> str | None:
    annual_volume = infer_annual_journal_volume(source_title, year)
    if annual_volume:
        return annual_volume
    if len(observed_year_volumes) == 1:
        return next(iter(observed_year_volumes))
    return None


def infer_annual_journal_volume(source_title: str | None, year: int) -> str | None:
    return infer_annual_volume(source_title, year)


def merge_issue_catalog_entry(target: IssueCatalogEntry, incoming: IssueCatalogEntry) -> None:
    target.issue_source_url = target.issue_source_url or incoming.issue_source_url
    target.cover_image_url = target.cover_image_url or incoming.cover_image_url
    if target.article_metadata_status != "complete" and incoming.article_metadata_status == "complete":
        target.article_metadata_status = incoming.article_metadata_status
    target.notes = list(dict.fromkeys([*target.notes, *incoming.notes]))
    existing_article_keys = {
        article.doi or build_article_fallback_key(article) for article in target.articles
    }
    for article in incoming.articles:
        article_key = article.doi or build_article_fallback_key(article)
        if article_key in existing_article_keys:
            continue
        target.articles.append(article)
        existing_article_keys.add(article_key)


def drop_redundant_volume_only_entries(
    entries: list[IssueCatalogEntry],
) -> list[IssueCatalogEntry]:
    issue_level_article_keys: dict[tuple[str, int, str], set[str]] = {}
    for entry in entries:
        normalized_volume = normalize_numeric_token(entry.volume)
        if not normalized_volume or not entry.issue:
            continue
        issue_level_article_keys.setdefault(
            (entry.journal_short_name, entry.year, normalized_volume),
            set(),
        ).update(issue_catalog_article_keys(entry))

    filtered: list[IssueCatalogEntry] = []
    for entry in entries:
        normalized_volume = normalize_numeric_token(entry.volume)
        if normalized_volume and not entry.issue:
            key = (entry.journal_short_name, entry.year, normalized_volume)
            covered_keys = issue_level_article_keys.get(key)
            volume_keys = issue_catalog_article_keys(entry)
            if covered_keys and volume_keys and volume_keys.issubset(covered_keys):
                continue
        filtered.append(entry)
    return filtered


def issue_catalog_article_keys(entry: IssueCatalogEntry) -> set[str]:
    return {
        normalize_doi(article.doi) or build_article_fallback_key(article)
        for article in entry.articles
    }


def should_cleanup_issue_catalog_directories(
    spec: JournalSpec,
    *,
    from_year: int,
    until_year: int,
) -> bool:
    configured_from_year = spec.from_year or 2000
    configured_until_year = spec.until_year or datetime.now(UTC).year
    return from_year <= configured_from_year and until_year >= configured_until_year


def format_year_count_map(year_counts: dict[int, int]) -> str:
    return ", ".join(
        f"{year}({count})" for year, count in sorted(year_counts.items())
    )


def contains_catalog_artifacts(directory: Path) -> bool:
    try:
        return any(
            child.is_file() and child.name in CATALOG_ARTIFACT_FILENAMES
            for child in directory.iterdir()
        )
    except OSError:
        return False


def is_safe_generated_catalog_directory(directory: Path) -> bool:
    has_catalog_artifact = False
    try:
        for child in directory.rglob("*"):
            if child.is_symlink():
                return False
            if child.is_file():
                if child.name not in CATALOG_ARTIFACT_FILENAMES:
                    return False
                has_catalog_artifact = True
    except OSError:
        return False
    return has_catalog_artifact


def prune_empty_catalog_parents(directory: Path, *, stop: Path) -> None:
    current = directory
    stop_resolved = stop.resolve()
    while current.exists() and current.resolve() != stop_resolved:
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


def retry_catalog_delete_with_chmod(function, path: str, excinfo) -> None:  # noqa: ANN001
    target = Path(path)
    try:
        target.chmod(0o700 if target.is_dir() else 0o600)
    except OSError:
        pass
    function(path)


def cleanup_empty_orphan_directories(
    journal_directory: Path,
    *,
    referenced_directories: set[Path],
) -> CatalogCleanupResult:
    cleaned_directory_count = 0
    failed_directories: list[Path] = []
    root_resolved = journal_directory.resolve()
    for directory in sorted(
        (path for path in journal_directory.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if not directory.exists():
            continue
        resolved = directory.resolve()
        if resolved == root_resolved:
            continue
        if any(reference.is_relative_to(resolved) for reference in referenced_directories):
            continue
        try:
            next(directory.iterdir())
        except StopIteration:
            if remove_empty_directory_with_retries(directory):
                cleaned_directory_count += 1
            else:
                failed_directories.append(directory)
            continue
        except OSError:
            continue
    return CatalogCleanupResult(
        removed_count=cleaned_directory_count,
        failed_directories=failed_directories,
    )


def remove_empty_directory_with_retries(directory: Path, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            directory.rmdir()
            return True
        except OSError:
            if attempt + 1 >= retries:
                return False
            time.sleep(0.2 * (attempt + 1))
    return False
