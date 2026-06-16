from __future__ import annotations

import json
import logging
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from re import sub
from shutil import copy2

import click
import requests
import typer
import typer.rich_utils as typer_rich_utils
import uvicorn

from powerlit.models import JournalSpec, QuerySpec
from powerlit.providers.base import ProviderError
from powerlit.services.ai_analysis import AIServiceError, AnalysisService
from powerlit.services.assisted_download import (
    AssistedIncomingDownloadService,
    normalize_journal_filter,
)
from powerlit.services.artifact_metadata import enrich_paper_row
from powerlit.services.catalog_views import CatalogViewService
from powerlit.services.existing_note_audit import ExistingNoteAuditService
from powerlit.services.evidence_index import EvidenceIndexService
from powerlit.services.export import export_download_queue, export_records
from powerlit.services.fulltext_resolver import FullTextResolver
from powerlit.services.incoming_processor import (
    IncomingPDFProcessor,
    IncomingProcessorError,
    iter_incoming_pdfs,
)
from powerlit.services.incoming_oa_download import OAIncomingDownloadService
from powerlit.services.index import IndexStore
from powerlit.services.journal_issue_catalog import JournalIssueCatalogService
from powerlit.services.library_layout import (
    build_analysis_output_base,
    build_parsed_output_base,
    doi_to_suffix,
)
from powerlit.services.metadata_repair import (
    IndexedMetadataRepairService,
    MetadataRepairError,
)
from powerlit.services.oa_download import OADownloadError, OADownloadService
from powerlit.services.openscholar_rag import OpenScholarRAGError, OpenScholarRAGService
from powerlit.services.paper_cards import PaperCardService
from powerlit.services.pdf_parser import PDFParseError, PDFParserService
from powerlit.services.provider_health import (
    check_provider_connectivity,
    render_provider_check_line,
)
from powerlit.services.reports import write_weekly_report
from powerlit.services.search import SearchService, load_journal_bundle, load_query_bundle
from powerlit.services.status import provider_status
from powerlit.services.topics import load_topics
from powerlit.services.rag_index import RAGIndexService
from powerlit.services.rag_search import RAGSearchService
from powerlit.services.incoming_watcher import IncomingWatcherService
from powerlit.services.drive_upload import GoogleDriveService
from powerlit.settings import settings



def configure_console_streams() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(errors="replace")
        except ValueError:
            # Some redirected streams cannot be reconfigured at runtime.
            continue


def configure_library_logging() -> None:
    # pypdf emits many non-fatal font dictionary warnings for some publisher PDFs.
    # They do not indicate duplicate papers and only add noise to batch runs.
    logging.getLogger("pypdf").setLevel(logging.ERROR)


def localize_click_help_text(text: str) -> str:
    return text


def configure_click_help_localization() -> None:
    return None

configure_console_streams()
configure_library_logging()
configure_click_help_localization()

app = typer.Typer(help="PowerLit 文献检索与入库工具�?")
DEFAULT_QUERY_BUNDLE_ARGUMENT = typer.Argument(
    Path("config/queries.example.yml"),
    exists=True,
    dir_okay=False,
    readable=True,
    metavar="文件",
    help="查询集合 YAML 文件�?",
)
DEFAULT_TOPICS_FILE = Path("config/topics.power-system.yml")
DEFAULT_JOURNALS_FILE = Path("config/journals.watch.example.yml")
DEFAULT_JOURNAL_CATALOG_FILE = Path("config/journal_issue_catalogs.yml")
DEFAULT_TOPICS_FILE_OPTION = typer.Option(
    DEFAULT_TOPICS_FILE,
    exists=True,
    dir_okay=False,
    readable=True,
    metavar="文件",
    help="主题预设 YAML 文件�?",
)
DEFAULT_JOURNALS_FILE_OPTION = typer.Option(
    DEFAULT_JOURNALS_FILE,
    exists=True,
    dir_okay=False,
    readable=True,
    metavar="文件",
    help="期刊监控列表 YAML 文件�?",
)
PDF_FILE_OPTION = typer.Option(
    ...,
    exists=True,
    dir_okay=False,
    readable=True,
    metavar="文件",
    help="PDF 文件路径�?",
)
SOURCE_TEXT_FILE_OPTION = typer.Option(
    None,
    exists=True,
    dir_okay=False,
    readable=True,
    metavar="文件",
    help="可选：作为分析依据�?UTF-8 文本�?Markdown 文件�?",
)
OPTIONAL_PDF_FILE_OPTION = typer.Option(
    None,
    exists=True,
    dir_okay=False,
    readable=True,
    metavar="文件",
    help="可�?PDF 文件路径。默认使用索引中已挂载的本地 PDF�?",
)


@app.command()
def search(
    query: str = typer.Argument(..., help="检索关键词�?"),
    name: str | None = typer.Option(None, help="输出文件使用的查询名称�?"),
    providers: str = typer.Option("crossref,openalex", help="用逗号分隔的提供方列表�?"),
    limit: int = typer.Option(20, min=1, max=200, help="每个提供方最多返回多少条结果�?"),
    from_date: str | None = typer.Option(None, help="发表起始日期，格�?YYYY-MM-DD�?"),
    until_date: str | None = typer.Option(None, help="发表结束日期，格�?YYYY-MM-DD�?"),
) -> None:
    service = SearchService(settings)
    store = IndexStore(settings)
    query_name = name or slugify(query)
    spec = QuerySpec.model_validate(
        {
            "name": query_name,
            "query": query,
            "providers": split_csv(providers),
            "limit": limit,
            "from_date": from_date,
            "until_date": until_date,
        }
    )

    try:
        results = service.search(spec)
    except (ProviderError, ValueError) as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    output_base = settings.output_dir / query_name
    paths = export_records(results, output_base)
    store.upsert_records(results)
    typer.echo(
        f"已导�?{len(results)} 篇文献：{paths['json']}，{paths['csv']}"
    )


@app.command("openscholar-retrieve")
def openscholar_retrieve(
    question: str = typer.Argument(..., help="科研问题或综述问题。"),
    name: str | None = typer.Option(None, help="输出文件名，默认按问题自动生成。"),
    max_queries: int = typer.Option(3, min=1, max=8, help="最多生成多少条扩展检索词。"),
    papers_per_query: int = typer.Option(
        10,
        min=1,
        max=50,
        help="每条检索词从 Semantic Scholar 拉取多少篇论文。",
    ),
    pes2o_docs: int = typer.Option(
        20,
        min=1,
        max=100,
        help="PES2O 检索返回多少条 passage。",
    ),
    pes2o: bool = typer.Option(
        True,
        "--pes2o/--no-pes2o",
        help="是否同时调用 OpenScholar 风格的 PES2O passage 检索。",
    ),
) -> None:
    service = OpenScholarRAGService(settings)
    try:
        artifacts = service.retrieve(
            question,
            name=name,
            max_queries=max_queries,
            papers_per_query=papers_per_query,
            use_pes2o=pes2o,
            pes2o_n_docs=pes2o_docs,
        )
    except OpenScholarRAGError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(
        "OpenScholar 兼容检索结果已写入："
        f"{artifacts.json_path}，{artifacts.jsonl_path}"
    )
    typer.echo(
        f"扩展检索词 {len(artifacts.search_queries)} 条，"
        f"聚合 ctxs {len(artifacts.ctxs)} 条。"
    )
    if artifacts.search_queries:
        typer.echo("检索词：" + " | ".join(artifacts.search_queries))


@app.command("batch-search")
def batch_search(
    config_path: Path = DEFAULT_QUERY_BUNDLE_ARGUMENT,
) -> None:
    bundle = load_query_bundle(config_path)
    service = SearchService(settings)
    store = IndexStore(settings)

    for query in bundle.queries:
        try:
            results = service.search(query)
        except (ProviderError, ValueError) as exc:
            typer.secho(f"{query.name}：{exc}", fg=typer.colors.RED)
            continue
        output_base = settings.output_dir / query.name
        paths = export_records(results, output_base)
        store.upsert_records(results)
        typer.echo(f"{query.name}：已导出 {len(results)} 篇文献到 {paths['csv']}")


@app.command()
def topics(
    topics_file: Path = DEFAULT_TOPICS_FILE_OPTION,
) -> None:
    for name, payload in load_topics(topics_file).items():
        typer.echo(f"{name}：{payload.get('description', '')}")


@app.command()
def journals(
    journals_file: Path = DEFAULT_JOURNALS_FILE_OPTION,
) -> None:
    bundle = load_journal_bundle(journals_file)
    for journal in bundle.journals:
        providers = ",".join(journal.providers)
        typer.echo(
            f"{journal.short_name}：{journal.title} | ISSN={','.join(journal.issns)} | "
            f"提供方：{providers} | 起始年份={journal.from_year or '未设置'}"
        )


@app.command()
def providers() -> None:
    for item in provider_status(settings):
        typer.echo(f"{item['name']}：{item['status']}（{item['detail']}�?")


@app.command("check-providers")
def check_providers(
    fail_on_error: bool = typer.Option(
        False,
        help="只要有任一已配置提供方连通性检查失败，就以状态码 1 退出�?",
    ),
) -> None:
    results = check_provider_connectivity(settings)
    has_failure = False
    for item in results:
        status = str(item["status"])
        color = provider_check_color(status)
        typer.secho(render_provider_check_line(item), fg=color)
        if status not in {"ok", "needs_config"}:
            has_failure = True

    if fail_on_error and has_failure:
        raise typer.Exit(code=1)


@app.command("search-topic")
def search_topic(
    topic: str = typer.Argument(..., help="主题预设名称�?"),
    providers: str = typer.Option("crossref,openalex", help="用逗号分隔的提供方列表�?"),
    limit: int = typer.Option(20, min=1, max=200, help="每个提供方最多返回多少条结果�?"),
    from_date: str | None = typer.Option(None, help="发表起始日期，格�?YYYY-MM-DD�?"),
    until_date: str | None = typer.Option(None, help="发表结束日期，格�?YYYY-MM-DD�?"),
    topics_file: Path = DEFAULT_TOPICS_FILE_OPTION,
) -> None:
    presets = load_topics(topics_file)
    if topic not in presets:
        typer.secho(f"未知主题预设：{topic}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    query = presets[topic]["query"]
    search(
        query=query,
        name=topic,
        providers=providers,
        limit=limit,
        from_date=from_date,
        until_date=until_date,
    )


@app.command("sync-journal")
def sync_journal(
    short_name: str = typer.Argument(..., help="监控列表中的期刊简称�?"),
    journals_file: Path = DEFAULT_JOURNALS_FILE_OPTION,
    providers: str | None = typer.Option(None, help="覆盖默认提供方列表�?"),
    from_year: int | None = typer.Option(None, help="覆盖默认起始年份�?"),
    until_year: int | None = typer.Option(None, help="覆盖默认结束年份�?"),
    limit: int | None = typer.Option(None, min=1, max=5000, help="覆盖默认结果上限�?"),
    resolve_fulltext: bool = typer.Option(
        True,
        help="同步元数据后继续补全文链接候选�?",
    ),
    download_oa: bool = typer.Option(
        False,
        help="存在直链 PDF 时自动下�?OA 全文�?",
    ),
) -> None:
    bundle = load_journal_bundle(journals_file)
    try:
        journal = next(item for item in bundle.journals if item.short_name == short_name)
    except StopIteration as exc:
        typer.secho(f"未知期刊简称：{short_name}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    spec = build_runtime_journal_spec(
        journal,
        providers=providers,
        from_year=from_year,
        until_year=until_year,
        limit=limit,
    )
    summary = run_journal_sync(
        spec,
        resolve_fulltext=resolve_fulltext,
        download_oa=download_oa,
    )
    typer.echo(
        f"{spec.short_name}：已同步 {summary['total_records']} 篇文献，"
        f"新增 {summary['new_records']} 篇，下载 OA PDF {summary['downloaded_oa']} 篇�?"
        f"导出文件：{summary['csv_path']} | {summary['download_list_path']}"
    )


@app.command("sync-journal-bundle")
def sync_journal_bundle(
    journals_file: Path = DEFAULT_JOURNALS_FILE_OPTION,
    resolve_fulltext: bool = typer.Option(
        True,
        help="同步元数据后继续补全文链接候选�?",
    ),
    download_oa: bool = typer.Option(
        False,
        help="存在直链 PDF 时自动下�?OA 全文�?",
    ),
    write_report: bool = typer.Option(
        True,
        help="为本次新增文献生成周报�?",
    ),
) -> None:
    bundle = load_journal_bundle(journals_file)
    new_records_map: dict[str, list] = {}
    total_synced = 0
    total_new = 0
    total_downloaded = 0

    for journal in bundle.journals:
        summary = run_journal_sync(
            journal,
            resolve_fulltext=resolve_fulltext,
            download_oa=download_oa,
        )
        total_synced += summary["total_records"]
        total_new += summary["new_records"]
        total_downloaded += summary["downloaded_oa"]
        new_records_map[journal.short_name] = summary["new_record_objects"]
        typer.echo(
            f"{journal.short_name}：已同步 {summary['total_records']} 篇，"
            f"新增 {summary['new_records']} 篇，下载 OA {summary['downloaded_oa']} �?"
        )

    if write_report:
        report_path = write_weekly_report(
            settings.weekly_reports_dir,
            generated_at=datetime.now(),
            journals=bundle.journals,
            new_records=new_records_map,
        )
        typer.echo(f"周报：{report_path}")

    typer.echo(
        f"期刊批量同步完成：已同步 {total_synced} 篇文献，"
        f"新增 {total_new} 篇，下载 OA PDF {total_downloaded} 篇�?"
    )


@app.command("sync-journal-issue-catalogs")
def sync_journal_issue_catalogs(
    config_path: Path = typer.Option(
        DEFAULT_JOURNAL_CATALOG_FILE,
        exists=True,
        dir_okay=False,
        readable=True,
        metavar="文件",
        help="期刊卷期目录配置 YAML�?",
    ),
    journal: str | None = typer.Option(
        None,
        help="可选：按期刊简称或完整刊名筛选�?",
    ),
    from_year: int | None = typer.Option(
        None,
        min=1900,
        max=2100,
        help="覆盖配置里的起始年份�?",
    ),
    until_year: int | None = typer.Option(
        None,
        min=1900,
        max=2100,
        help="覆盖配置里的结束年份�?",
    ),
) -> None:
    bundle = load_journal_bundle(config_path)
    if journal:
        normalized_filter = journal.strip().lower()
        journals = [
            item
            for item in bundle.journals
            if item.short_name.lower() == normalized_filter
            or item.title.strip().lower() == normalized_filter
        ]
    else:
        journals = bundle.journals

    if not journals:
        typer.secho("没有期刊匹配当前筛选条件�?, fg=typer.colors.YELLOW")
        raise typer.Exit(code=1)

    service = JournalIssueCatalogService(settings)
    results = []
    with progressbar_context(
        journals,
        label="同步期刊卷期目录",
        item_show_func=lambda item: shorten_progress_text(item.title),
    ) as progress:
        for spec in progress:
            result = service.sync_journal(
                spec,
                from_year=from_year,
                until_year=until_year,
            )
            results.append(result)
            warning_parts = []
            if result.year_only_issue_count:
                warning_parts.append(f"{result.year_only_issue_count} year-only directories")
            if result.volume_only_issue_count:
                warning_parts.append(
                    f"{result.volume_only_issue_count} volume-only directories"
                )
            if (
                result.coverage_end_year is not None
                and result.coverage_end_year < result.until_year
            ):
                warning_parts.append(f"coverage stops at {result.coverage_end_year}")
            if result.cleaned_directory_count:
                warning_parts.append(
                    f"cleaned {result.cleaned_directory_count} stale directories"
                )
            if any(
                "could not be removed automatically" in warning.lower()
                for warning in result.warnings
            ):
                warning_parts.append("some stale directories could not be removed")
            warning_suffix = (
                f" Warnings: {', '.join(warning_parts)}."
                if warning_parts
                else ""
            )
            typer.echo(
                f"{result.source_title}: {result.issue_count} issues, "
                f"{result.article_count} articles, "
                f"{result.open_access_article_count} OA, "
                f"{result.incomplete_issue_count} incomplete issue catalogs."
                f"{warning_suffix}"
            )

    typer.echo(
        "Journal issue catalog sync complete: "
        f"{len(results)} journal(s), "
        f"{sum(item.issue_count for item in results)} issues, "
        f"{sum(item.article_count for item in results)} articles, "
        f"{sum(item.open_access_article_count for item in results)} OA, "
        f"{sum(item.incomplete_issue_count for item in results)} incomplete issue catalogs, "
        f"{sum(item.year_only_issue_count for item in results)} year-only directories, "
        f"{sum(item.volume_only_issue_count for item in results)} volume-only directories, "
        f"{sum(item.cleaned_directory_count for item in results)} cleaned stale directories."
    )


@app.command("refresh-catalog-views")
def refresh_catalog_views(
    journal: str | None = typer.Option(
        None,
        help="可选：按逗号分隔的期刊简称筛选�?",
    ),
    doi: str | None = typer.Option(
        None,
        help="可选：只刷新指�?DOI 所在卷期目录�?",
    ),
) -> None:
    service = CatalogViewService(settings)
    if doi:
        refreshed = service.refresh_for_doi(doi)
        typer.echo(f"目录视图刷新完成：按 DOI 命中 {refreshed} 个卷期目录�?")
        return

    journal_filters = split_csv(journal) if journal else None
    refreshed = service.refresh_all_journal_catalogs(journal_filters=journal_filters)
    typer.echo(f"目录视图刷新完成：已刷新 {refreshed} 个卷期目录�?")


@app.command("download-oa-pdfs")
def download_oa_pdfs(
    journal: str | None = typer.Option(
        None,
        help="可选：按逗号分隔的期刊简称或完整刊名筛选�?",
    ),
    from_year: int | None = typer.Option(
        None,
        min=1900,
        max=2100,
        help="只下载该年份及之后的 OA 文献�?",
    ),
    until_year: int | None = typer.Option(
        None,
        min=1900,
        max=2100,
        help="只下载该年份及之前的 OA 文献�?",
    ),
    doi: str | None = typer.Option(None, help="只下载指�?DOI �?OA 文献�?"),
    limit: int = typer.Option(
        200,
        min=1,
        max=20000,
        help="最多检查多少条 OA 目录候选文献�?",
    ),
    refresh_metadata: bool = typer.Option(
        True,
        help="下载前刷�?DOI 元数据和全文候选�?",
    ),
    echo_each: bool = typer.Option(
        False,
        help="每成功下载一篇就输出一行结果�?",
    ),
) -> None:
    service = OAIncomingDownloadService(settings)
    journal_filters = split_csv(journal) if journal else None
    candidates = service.discover_issue_catalog_candidates(
        journals=journal_filters,
        from_year=from_year,
        until_year=until_year,
        doi=doi,
        limit=limit,
    )
    if not candidates:
        typer.echo("没有 OA 候选文献匹配当前筛选条件�?")
        return

    typer.echo(f"Incoming 目录：{settings.incoming_pdf_dir}")
    typer.echo(f"发现 {len(candidates)} �?OA 目录候选文献�?")
    existing_incoming_dois = service.collect_existing_incoming_dois()
    typer.echo(f"Incoming 目录现有 DOI 指纹数：{len(existing_incoming_dois)}")

    downloaded = 0
    already_in_library = 0
    already_in_incoming = 0
    no_pdf_candidate = 0
    failed = 0
    with progressbar_context(
        candidates,
        label="下载 OA PDF",
        item_show_func=lambda item: shorten_progress_text(item.doi or item.title),
    ) as progress:
        for candidate in progress:
            try:
                outcome = service.download_candidate(
                    candidate,
                    existing_incoming_dois=existing_incoming_dois,
                    refresh_metadata=refresh_metadata,
                )
            except OADownloadError as exc:
                failed += 1
                typer.secho(f"OA 下载失败：{candidate.doi}，{exc}", fg=typer.colors.RED)
                continue

            if outcome.status == "downloaded":
                downloaded += 1
                if echo_each and outcome.path is not None:
                    typer.echo(
                        f"已下载：{candidate.doi} -> {outcome.path.name} "
                        f"（{outcome.source_url or '缺少来源链接'}�?"
                    )
            elif outcome.status == "already_in_library":
                already_in_library += 1
            elif outcome.status == "already_in_incoming":
                already_in_incoming += 1
            elif outcome.status == "no_pdf_candidate":
                no_pdf_candidate += 1
                typer.secho(
                    f"没有可直接下载的 OA PDF：{candidate.doi}",
                    fg=typer.colors.YELLOW,
                )
            else:
                failed += 1
                typer.secho(
                    f"未处理的 OA 下载状态：{candidate.doi}，{outcome.status}",
                    fg=typer.colors.RED,
                )

    typer.echo(
        "OA 下载完成�?"
        f"{downloaded} 条已下载�?"
        f"{already_in_library} 条库中已存在�?"
        f"{already_in_incoming} �?incoming_pdf 已存在，"
        f"{no_pdf_candidate} 条没有直�?OA PDF�?"
        f"{failed} 条失败�?"
    )


@app.command("download-assisted-pdfs")
def download_assisted_pdfs(
    journal: str | None = typer.Option(
        None,
        help="可选：按逗号分隔的期刊简称或完整刊名筛选�?",
    ),
    from_year: int | None = typer.Option(
        None,
        min=1900,
        max=2100,
        help="只检查该年份及之后的目录文献�?",
    ),
    until_year: int | None = typer.Option(
        None,
        min=1900,
        max=2100,
        help="只检查该年份及之前的目录文献�?",
    ),
    doi: str | None = typer.Option(None, help="只下载指�?DOI 的文献�?"),
    limit: int = typer.Option(
        100,
        min=1,
        max=20000,
        help="最多检查多少条目录候选文献�?",
    ),
    echo_each: bool = typer.Option(
        False,
        help="每成功下载一篇就输出一行结果�?",
    ),
) -> None:
    service = AssistedIncomingDownloadService(settings)
    try:
        journal_filters = split_csv(journal) if journal else None
        candidates = service.discover_issue_catalog_candidates(
            journals=journal_filters,
            from_year=from_year,
            until_year=until_year,
            doi=doi,
            limit=limit,
        )
        if not candidates:
            if journal_filters and not assisted_download_has_issue_catalogs(service, journal_filters):
                typer.secho(
                    "当前期刊还没有本地卷期目录缓存，所以辅助下载暂时找不到候选文献�?",
                    fg=typer.colors.YELLOW,
                )
                typer.echo("请先同步期刊卷期目录，然后再重新运行本命令：")
                typer.echo(render_issue_catalog_sync_command(journal_filters))
            typer.echo("没有候选文献匹配当前辅助下载筛选条件�?")
            return

        typer.echo(f"Incoming 目录：{settings.incoming_pdf_dir}")
        typer.echo(f"发现 {len(candidates)} 条辅助下载候选文献�?")
        existing_incoming_dois = service.collect_existing_incoming_dois()
        typer.echo(f"Incoming 目录现有 DOI 指纹数：{len(existing_incoming_dois)}")

        if any(item.provider == "cnki_navi" for item in candidates):
            browser_session = service.get_browser_cookie_session()
            if browser_session is None:
                typer.secho(
                    "CNKI 浏览器会话：无法直接复用本地浏览�?cookie�?"
                    "仍可通过弹出的辅�?Edge 窗口继续下载，在该窗口里完成一�?CNKI/VPN 登录即可�?",
                    fg=typer.colors.YELLOW,
                )
            else:
                typer.echo(
                    "CNKI 浏览器会话："
                    f"{browser_session.browser_name}/{browser_session.profile_name} "
                    f"（{browser_session.cookie_count} �?cookie�?"
                )

        downloaded = 0
        already_in_library = 0
        already_in_incoming = 0
        no_supported_route = 0
        login_required = 0
        failed = 0
        with progressbar_context(
            candidates,
            label="辅助下载 PDF",
            item_show_func=lambda item: shorten_progress_text(item.doi or item.title),
        ) as progress:
            for candidate in progress:
                try:
                    outcome = service.download_candidate(
                        candidate,
                        existing_incoming_dois=existing_incoming_dois,
                    )
                except requests.RequestException as exc:
                    failed += 1
                    typer.secho(
                        f"辅助下载失败：{candidate.doi or candidate.title}，{exc}",
                        fg=typer.colors.RED,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    message = str(exc).strip() or exc.__class__.__name__
                    lowered = message.lower()
                    if "invalid session id" in lowered or "browser has closed the connection" in lowered:
                        login_required += 1
                        typer.secho(
                            "辅助下载浏览器窗口已关闭或会话已断开�?"
                            "请保�?Edge 辅助窗口打开，并在该窗口内完�?IEEE/Elsevier 登录后重试�?",
                            fg=typer.colors.YELLOW,
                        )
                    else:
                        failed += 1
                        typer.secho(
                            f"辅助下载失败：{candidate.doi or candidate.title}：{message}",
                            fg=typer.colors.RED,
                        )
                    continue

                if outcome.status == "downloaded":
                    downloaded += 1
                    if echo_each and outcome.path is not None:
                        typer.echo(
                            f"已下载：{candidate.doi or candidate.title} -> {outcome.path.name} "
                            f"[{outcome.method or '未知方式'}]"
                        )
                elif outcome.status == "already_in_library":
                    already_in_library += 1
                elif outcome.status == "already_in_incoming":
                    already_in_incoming += 1
                elif outcome.status == "no_supported_route":
                    no_supported_route += 1
                elif outcome.status == "login_required":
                    login_required += 1
                    typer.secho(
                        f"需要登录：{candidate.doi or candidate.title}，{outcome.message or '浏览器会话不可用'}",
                        fg=typer.colors.YELLOW,
                    )
                else:
                    failed += 1
                    typer.secho(
                        f"辅助下载失败：{candidate.doi or candidate.title}�?"
                        f"{outcome.message or outcome.status}",
                        fg=typer.colors.RED,
                    )

        typer.echo(
            "辅助下载完成�?"
            f"{downloaded} 条已下载�?"
            f"{already_in_library} 条库中已存在�?"
            f"{already_in_incoming} �?incoming_pdf 已存在，"
            f"{no_supported_route} 条没有可用下载路由，"
            f"{login_required} 条需要浏览器登录�?"
            f"{failed} 条失败�?"
        )
    finally:
        service.close()


@app.command("list-papers")
def list_papers(
    limit: int = typer.Option(20, min=1, max=500, help="最多显示多少条已入库文献�?"),
    query_pack: str | None = typer.Option(None, help="�?query pack 筛选�?"),
) -> None:
    store = IndexStore(settings)
    rows = store.list_papers(limit=limit, query_pack=query_pack)
    rows = [enrich_paper_row(row) for row in rows]
    if not rows:
        typer.echo("没有找到已入库文献�?")
        return
    for index, row in enumerate(rows, start=1):
        typer.echo(f"{index}. {row['title']}")
        typer.echo(f"   DOI：{row['doi'] or '缺失'}")
        typer.echo(f"   引文：{row['gbt7714_citation']}")
        typer.echo(f"   下载状态：{row['download_status']}")
        if row["local_pdf_path"]:
            typer.echo(f"   本地 PDF：{row['local_pdf_path']}")
        if row["parsed_md_path"]:
            typer.echo(f"   解析 Markdown：{row['parsed_md_path']}")
        if row.get("paper_card_md_path"):
            typer.echo(f"   论文卡片：{row['paper_card_md_path']}")

        if row.get("total_ai_cost") is not None:
            typer.echo(
                f"   AI 成本：{float(row['total_ai_cost']):.6f} "
                f"{row.get('total_ai_cost_currency') or 'CNY'}"
            )
        typer.echo(
            f"   ResearchGate�?"
            f"{row['researchgate_url'] or row['researchgate_lookup_url'] or '缺失'}"
        )


@app.command("workspace-create")
def workspace_create(
    name: str = typer.Argument(..., help="工作区名称�?"),
    description: str | None = typer.Option(None, help="可选：工作区说明�?"),
) -> None:
    store = IndexStore(settings)
    try:
        created = store.create_workspace(name=name, description=description)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    workspace = store.get_workspace(name)
    if workspace is None:
        typer.secho("创建后查询工作区失败�?, fg=typer.colors.RED")
        raise typer.Exit(code=1)

    action = "已创建" if created else "工作区已存在"
    typer.echo(f"{action}：{workspace['name']}")
    if workspace.get("description"):
        typer.echo(f"说明：{workspace['description']}")


@app.command("workspace-list")
def workspace_list() -> None:
    store = IndexStore(settings)
    rows = store.list_workspaces()
    if not rows:
        typer.echo("没有找到工作区�?")
        return

    for index, row in enumerate(rows, start=1):
        typer.echo(f"{index}. {row['name']}")
        if row.get("description"):
            typer.echo(f"   说明：{row['description']}")
        typer.echo(
            f"   文献数：{int(row.get('paper_count') or 0)} | "
            f"Query Pack 数：{int(row.get('query_pack_count') or 0)}"
        )
        typer.echo(f"   更新时间：{row.get('updated_at') or '未知'}")


@app.command("workspace-show")
def workspace_show(
    name: str = typer.Argument(..., help="工作区名称�?"),
    limit: int = typer.Option(20, min=1, max=500, help="最多显示多少篇文献�?"),
) -> None:
    store = IndexStore(settings)
    workspace = store.get_workspace(name)
    if workspace is None:
        typer.secho(f"工作区不存在：{name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    rows = store.list_workspace_papers(name, limit=limit)
    rows = [enrich_paper_row(row) for row in rows]
    query_packs = store.list_workspace_query_packs(name)

    typer.echo(f"工作区：{workspace['name']}")
    if workspace.get("description"):
        typer.echo(f"说明：{workspace['description']}")
    typer.echo(f"文献数：{int(workspace.get('paper_count') or 0)}")
    typer.echo(f"Query Pack：{', '.join(query_packs) if query_packs else 'unknown'}")
    typer.echo(f"更新时间：{workspace.get('updated_at') or '未知'}")

    if not rows:
        typer.echo("工作区为空�?")
        return

    for index, row in enumerate(rows, start=1):
        typer.echo(f"{index}. {row['title']}")
        typer.echo(f"   DOI：{row['doi'] or '缺失'}")
        typer.echo(f"   引文：{row['gbt7714_citation']}")
        typer.echo(f"   下载状态：{row['download_status']}")
        if row["local_pdf_path"]:
            typer.echo(f"   本地 PDF：{row['local_pdf_path']}")
        if row["parsed_md_path"]:
            typer.echo(f"   解析 Markdown：{row['parsed_md_path']}")
        if row["analysis_md_path"]:
            typer.echo(f"   分析 Markdown：{row['analysis_md_path']}")
        if row.get("paper_card_md_path"):
            typer.echo(f"   论文卡片：{row['paper_card_md_path']}")


@app.command("workspace-add")
def workspace_add(
    name: str = typer.Argument(..., help="工作区名称�?"),
    doi: str | None = typer.Option(None, help="单个 DOI 或用逗号分隔的多�?DOI�?"),
    query_pack: str | None = typer.Option(
        None,
        help="单个 query pack 或用逗号分隔的多�?query pack�?",
    ),
) -> None:
    if not doi and not query_pack:
        typer.secho("至少提供 --doi �?--query-pack 之一�?, fg=typer.colors.RED")
        raise typer.Exit(code=1)

    store = IndexStore(settings)
    workspace = store.get_workspace(name)
    if workspace is None:
        typer.secho(f"工作区不存在：{name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    added_papers = 0
    duplicate_papers = 0
    added_query_packs = 0
    duplicate_query_packs = 0
    missing_inputs: list[str] = []

    for item in split_csv(doi) if doi else []:
        try:
            added = store.add_paper_to_workspace(name, item)
        except LookupError as exc:
            missing_inputs.append(str(exc))
            continue
        if added:
            added_papers += 1
        else:
            duplicate_papers += 1

    for item in split_csv_values(query_pack) if query_pack else []:
        try:
            added = store.add_query_pack_to_workspace(name, item)
        except LookupError as exc:
            missing_inputs.append(str(exc))
            continue
        if added > 0:
            added_query_packs += added
        else:
            duplicate_query_packs += 1

    typer.echo(f"工作区：{workspace['name']}")
    typer.echo(
        "添加完成�?"
        f"新增 DOI 文献 {added_papers} 篇，"
        f"已存�?DOI 文献 {duplicate_papers} 篇，"
        f"通过 query pack 新增文献 {added_query_packs} 篇，"
        f"未带来新文献�?query pack {duplicate_query_packs} 个�?"
    )
    for message in missing_inputs:
        typer.secho(message, fg=typer.colors.YELLOW)


@app.command("workspace-remove")
def workspace_remove(
    name: str = typer.Argument(..., help="工作区名称�?"),
    doi: str | None = typer.Option(None, help="单个 DOI 或用逗号分隔的多�?DOI�?"),
    query_pack: str | None = typer.Option(
        None,
        help="单个 query pack 或用逗号分隔的多�?query pack�?",
    ),
) -> None:
    if not doi and not query_pack:
        typer.secho("至少提供 --doi �?--query-pack 之一�?, fg=typer.colors.RED")
        raise typer.Exit(code=1)

    store = IndexStore(settings)
    workspace = store.get_workspace(name)
    if workspace is None:
        typer.secho(f"工作区不存在：{name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    removed_papers = 0
    missing_papers = 0
    removed_query_pack_papers = 0
    missing_query_packs = 0

    for item in split_csv(doi) if doi else []:
        removed = store.remove_paper_from_workspace(name, item)
        if removed:
            removed_papers += 1
        else:
            missing_papers += 1

    for item in split_csv_values(query_pack) if query_pack else []:
        removed = store.remove_query_pack_from_workspace(name, item)
        if removed > 0:
            removed_query_pack_papers += removed
        else:
            missing_query_packs += 1

    typer.echo(f"工作区：{workspace['name']}")
    typer.echo(
        "移除完成�?"
        f"移除 DOI 文献 {removed_papers} 篇，"
        f"工作区中不存在的 DOI 文献 {missing_papers} 篇，"
        f"通过 query pack 移除文献 {removed_query_pack_papers} 篇，"
        f"在工作区中没有匹配结果的 query pack {missing_query_packs} 个�?"
    )


@app.command("workspace-delete")
def workspace_delete(
    name: str = typer.Argument(..., help="工作区名称�?"),
    force: bool = typer.Option(
        False,
        help="确认删除工作区。为避免误删，必须显式传入�?",
    ),
) -> None:
    if not force:
        typer.secho("如需删除工作区，请加�?--force 后重试�?, fg=typer.colors.RED")
        raise typer.Exit(code=1)

    store = IndexStore(settings)
    deleted = store.delete_workspace(name)
    if not deleted:
        typer.secho(f"工作区不存在：{name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"已删除工作区：{name}")


@app.command("build-paper-card")
def build_paper_card(
    doi: str = typer.Option(..., help="已入库文献的 DOI�?"),
    force: bool = typer.Option(
        False,
        help="即使论文卡片已存在也强制重建�?",
    ),
) -> None:
    store = IndexStore(settings)
    paper = store.get_paper_by_doi(doi)
    if paper is None:
        typer.secho(f"未找�?DOI 对应的已入库文献：{doi}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if (
        not force
        and paper.get("paper_card_json_path")
    ):
        typer.echo(f"DOI {doi} 的论文卡片已存在�?")
        typer.echo(f"卡片 JSON：{paper['paper_card_json_path']}")
        return

    records = store.load_paper_records(limit=1, doi=doi, unresolved_only=False)
    if not records:
        typer.secho(f"未找�?DOI 对应的已入库文献：{doi}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    record = records[0]
    service = PaperCardService(settings)
    artifacts = service.build_card(record)
    persisted = store.attach_paper_card_artifacts(
        dedupe_key=record.dedupe_key,
        json_path=artifacts.json_path,
        markdown_path=artifacts.markdown_path,
    )
    if not persisted:
        typer.secho(f"挂载 DOI {doi} 的论文卡片失败�?, fg=typer.colors.RED")
        raise typer.Exit(code=1)

    typer.echo(f"已生�?DOI {doi} 的论文卡片�?")
    typer.echo(f"卡片 JSON：{artifacts.json_path}")


@app.command("build-workspace-cards")
def build_workspace_cards(
    name: str = typer.Argument(..., help="工作区名称�?"),
    limit: int = typer.Option(50, min=1, max=5000, help="最多处理多少篇工作区文献�?"),
    pending_only: bool = typer.Option(
        True,
        help="跳过已经生成卡片产物的文献�?",
    ),
    force: bool = typer.Option(
        False,
        help="即使卡片产物已存在也强制重建�?",
    ),
) -> None:
    store = IndexStore(settings)
    workspace = store.get_workspace(name)
    if workspace is None:
        typer.secho(f"工作区不存在：{name}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    rows = store.list_workspace_papers(name, limit=limit)
    if not rows:
        typer.echo("工作区为空，无需生成论文卡片�?")
        return
    rows_by_key = {str(row.get("dedupe_key") or ""): row for row in rows}

    records = store.load_workspace_records(name, limit=limit)
    service = PaperCardService(settings)
    built = 0
    skipped = 0
    failed = 0

    for record in records:
        row = rows_by_key.get(record.dedupe_key, {})
        if (
            not force
            and pending_only
            and row.get("paper_card_json_path")
        ):
            skipped += 1
            continue
        try:
            artifacts = service.build_card(record)
            attached = store.attach_paper_card_artifacts(
                dedupe_key=record.dedupe_key,
                json_path=artifacts.json_path,
                markdown_path=artifacts.markdown_path,
            )
        except Exception as exc:
            failed += 1
            typer.secho(
                f"生成论文卡片失败：{record.doi or record.title}，{exc}",
                fg=typer.colors.RED,
            )
            continue
        if not attached:
            failed += 1
            typer.secho(
                f"挂载论文卡片失败：{record.doi or record.title}",
                fg=typer.colors.RED,
            )
            continue
        built += 1

    typer.echo(
        f"工作区论文卡片生成完成：已生�?{built} 篇，跳过 {skipped} 篇，失败 {failed} 篇�?"
    )


@app.command("show-paper-card")
def show_paper_card(
    doi: str = typer.Option(..., help="已入库文献的 DOI�?"),
) -> None:
    store = IndexStore(settings)
    paper = store.get_paper_by_doi(doi)
    if paper is None:
        typer.secho(f"未找�?DOI 对应的已入库文献：{doi}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if not paper.get("paper_card_json_path"):
        typer.secho(f"未找�?DOI {doi} 的论文卡片�?, fg=typer.colors.RED")
        raise typer.Exit(code=1)

    typer.echo(f"DOI：{doi}")
    typer.echo(f"卡片 JSON：{paper['paper_card_json_path']}")


@app.command("download-queue")
def download_queue(
    limit: int = typer.Option(20, min=1, max=500, help="最多导出多少条下载队列�?"),
    query_pack: str | None = typer.Option(None, help="�?query pack 筛选�?"),
    pending_only: bool = typer.Option(True, help="只包含尚未标记为已下载的文献�?"),
    output_name: str | None = typer.Option(
        None,
        help="输出文件名前缀。默认使�?<query-pack>-download-list �?download-queue�?",
    ),
) -> None:
    store = IndexStore(settings)
    rows = store.list_download_queue(limit=limit, query_pack=query_pack, pending_only=pending_only)
    if not rows:
        typer.echo("没有找到下载队列条目�?")
        return
    file_stem = output_name or default_download_list_name(query_pack)
    output_path = export_download_queue(rows, settings.download_list_dir / f"{file_stem}.csv")
    typer.echo(f"已导�?{len(rows)} 条下载队列：{output_path}")


@app.command("resolve-fulltext")
def resolve_fulltext(
    limit: int = typer.Option(20, min=1, max=500, help="最多处理多少条已入库文献�?"),
    query_pack: str | None = typer.Option(None, help="�?query pack 筛选�?"),
    unresolved_only: bool = typer.Option(
        True,
        help="只处理仍停留�?metadata_indexed 阶段的文献�?",
    ),
    doi: str | None = typer.Option(None, help="只处理单个已入库 DOI�?"),
) -> None:
    store = IndexStore(settings)
    resolver = FullTextResolver(settings)
    records = store.load_paper_records(
        limit=limit,
        query_pack=query_pack,
        unresolved_only=unresolved_only,
        doi=doi,
    )
    if not records:
        typer.echo("没有可供补全文链接的已入库文献�?")
        return

    resolved_records = []
    resolved_count = 0
    failure_count = 0
    for record in records:
        try:
            resolved = resolver.resolve_record(record)
        except requests.RequestException as exc:
            failure_count += 1
            typer.secho(
                f"补全文链接失败：{record.doi or record.title}，{exc}",
                fg=typer.colors.YELLOW,
            )
            resolved_records.append(record)
            continue

        if (
            resolved.acquisition_source_url != record.acquisition_source_url
            or resolved.acquisition_stage != record.acquisition_stage
            or resolved.acquisition_method != record.acquisition_method
        ):
            resolved_count += 1
        resolved_records.append(resolved)

    store.upsert_records(resolved_records)
    unresolved_count = len(records) - resolved_count - failure_count
    typer.echo(
        "全文链接候选补全完成："
        f"已更�?{resolved_count} 篇，"
        f"仍未补全 {unresolved_count} 篇，"
        f"请求错误 {failure_count} 次�?"
    )


@app.command("attach-pdf")
def attach_pdf(
    doi: str = typer.Option(..., help="已入库文献的 DOI�?"),
    file: Path = PDF_FILE_OPTION,
) -> None:
    store = IndexStore(settings)
    attached = store.attach_pdf(doi=doi, file_path=file)
    if not attached:
        typer.secho(f"未找�?DOI 对应的已入库文献：{doi}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    paper = store.get_paper_by_doi(doi)
    managed_path = paper["local_pdf_path"] if paper else str(file.resolve())
    typer.echo(f"已为 DOI {doi} 挂载 PDF：{managed_path}")


@app.command("process-incoming-pdf")
def process_incoming_pdf(
    limit: int | None = typer.Option(
        None,
        min=1,
        max=5000,
        help="最多处理多少个 incoming_pdf 中的 PDF�?",
    ),
    skip_parse: bool = typer.Option(
        False,
        help="只做�?DOI/metadata 登记�?PDF 挂载后就停止�?",
    ),
    skip_analyze: bool = typer.Option(
        False,
        help="跳过 AI 分析，只完成 识别 -> 移动 -> 解析�?",
    ),
) -> None:
    run_incoming_processing_command(
        limit=limit,
        parse=not skip_parse,
        analyze=not skip_analyze and not skip_parse,
    )


@app.command("register-incoming-pdf")
def register_incoming_pdf(
    limit: int | None = typer.Option(
        None,
        min=1,
        max=5000,
        help="最多登记多少个 incoming_pdf 中的 PDF�?",
    ),
) -> None:
    run_incoming_processing_command(limit=limit, parse=False, analyze=False)


@app.command("register-incoming-file")
def register_incoming_file(
    file: Path = PDF_FILE_OPTION,
) -> None:
    processor = IncomingPDFProcessor(settings)
    try:
        item = processor.process_file(file, parse=False, analyze=False)
    except IncomingProcessorError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    typer.echo(f"已处理：{item.file_path.name}")
    typer.echo(f"  DOI：{item.doi}")
    typer.echo(f"  已存 PDF：{item.target_pdf_path}")


@app.command("list-work-items")
def list_work_items(
    stage: str = typer.Option(..., help="阶段：register | transcribe | audit"),
    limit: int | None = typer.Option(
        None,
        min=1,
        max=50000,
        help="筛选后最多返回多少条工作项�?",
    ),
    query_pack: str | None = typer.Option(None, help="�?query pack 筛选已入库工作项�?"),
    doi: str | None = typer.Option(None, help="�?DOI 筛选已入库工作项�?"),
    pending_only: bool = typer.Option(
        True,
        help="尽量只返回当前阶段仍待处理的工作项�?",
    ),
) -> None:
    try:
        normalized_stage = normalize_work_item_stage(stage)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    items = collect_work_items(
        stage=normalized_stage,
        limit=limit,
        query_pack=query_pack,
        doi=doi,
        pending_only=pending_only,
    )
    typer.echo(
        json.dumps(
            {
                "stage": normalized_stage,
                "count": len(items),
                "items": items,
            },
            ensure_ascii=False,
        )
    )


@app.command("parse-pdf")
def parse_pdf(
    doi: str = typer.Option(..., help="已入库文献的 DOI�?"),
    file: Path | None = OPTIONAL_PDF_FILE_OPTION,
) -> None:
    store = IndexStore(settings)
    records = store.load_paper_records(limit=1, doi=doi, unresolved_only=False)
    if not records:
        typer.secho(f"未找�?DOI 对应的已入库文献：{doi}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    record = records[0]
    pdf_path = resolve_pdf_path(record.local_pdf_path, file)
    parser = PDFParserService(settings)
    try:
        artifacts = parser.parse_record(record, pdf_path)
    except PDFParseError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if record.doi:
        store.attach_parsed_artifacts(
            doi=record.doi,
            json_path=artifacts.json_path,
            markdown_path=artifacts.markdown_path,
        )
    typer.echo(f"解析页数：{artifacts.page_count}")
    typer.echo(f"解析 JSON：{artifacts.json_path}")
    if artifacts.proofread_markdown_path:
        typer.echo(f"校对报告：{artifacts.proofread_markdown_path}")
    if artifacts.proofread_json_path:
        typer.echo(f"校对 JSON：{artifacts.proofread_json_path}")
    if artifacts.directory_audit_path:
        typer.echo(f"目录审计：{artifacts.directory_audit_path}")
    typer.echo(f"笔记模式：{artifacts.note_generation_mode}")
    typer.echo(
        "校对问题："
        f"{artifacts.review_issue_count} 个，"
        f"{artifacts.review_severe_issue_count} 个严重"
    )
    typer.echo(
        "推导审查："
        f"{artifacts.review_derivation_direct_error_count} 个仅识别错误，"
        f"{artifacts.review_derivation_consistency_count} 个自洽性复核"
    )
    if artifacts.note_usage and artifacts.note_usage.estimated_cost is not None:
        token_text = (
            f"{artifacts.note_usage.prompt_tokens}"
            f"+{artifacts.note_usage.completion_tokens} tokens"
        )
        typer.echo(
            "笔记成本："
            f"{artifacts.note_usage.estimated_cost:.6f} {artifacts.note_usage.currency} "
            f"({token_text})"
        )


@app.command("parse-query-pack")
def parse_query_pack(
    query_pack: str = typer.Option(..., help="已入库的 query pack 名称�?"),
    limit: int = typer.Option(20, min=1, max=500, help="最多处理多少条已入库文献�?"),
    pending_only: bool = typer.Option(
        True,
        help="跳过已经生成解析 Markdown 的文献�?",
    ),
    force: bool = typer.Option(
        False,
        help="即使已有产物也强制重新生成解析文本和笔记�?",
    ),
) -> None:
    summary = parse_indexed_records(
        query_pack=query_pack,
        limit=limit,
        pending_only=pending_only,
        force=force,
    )
    if summary is None:
        typer.echo("没有可供解析的已入库文献�?")
        return

    typer.echo(
        f"解析完成：生�?{summary['parsed']} 条，跳过 {summary['skipped']} 条，"
        f"失败 {summary['failed']} 条�?"
        f"预计笔记成本：{summary['total_note_cost']:.6f} CNY�?"
        f"校对问题：共 {summary['total_review_issues']} 个，"
        f"其中严重 {summary['total_review_severe_issues']} 个�?"
        f"推导审查：仅识别错误 {summary['direct_error_count']} 个，"
        f"自洽性复�?{summary['consistency_review_count']} 个�?"
    )


@app.command("transcribe-attached-pdfs")
def transcribe_attached_pdfs(
    limit: int = typer.Option(20, min=1, max=500, help="最多处理多少条已入库文献�?"),
    query_pack: str | None = typer.Option(None, help="�?query pack 筛选�?"),
    doi: str | None = typer.Option(None, help="只转写指�?DOI�?"),
    pending_only: bool = typer.Option(
        True,
        help="跳过已经有解�?Markdown 产物的文献�?",
    ),
    force: bool = typer.Option(
        False,
        help="即使已有产物也强制重新生成解析文本和笔记�?",
    ),
) -> None:
    summary = parse_indexed_records(
        query_pack=query_pack,
        limit=limit,
        pending_only=pending_only,
        force=force,
        doi=doi,
        echo_each=True,
    )
    if summary is None:
        typer.echo("没有可供转写的已入库文献�?")
        return
    typer.echo(
        f"转写完成：生成 {summary['parsed']} 条，跳过 {summary['skipped']} 条，"
        f"失败 {summary['failed']} 条。"
        f"校对问题：共 {summary['total_review_issues']} 个，"
        f"其中严重 {summary['total_review_severe_issues']} 个。"
        f"推导审查：仅识别错误 {summary['direct_error_count']} 个，"
        f"自洽性复核 {summary['consistency_review_count']} 个。"
    )


@app.command("audit-existing-notes")
def audit_existing_notes(
    limit: int = typer.Option(20, min=1, max=500, help="最多处理多少条已入库文献�?"),
    query_pack: str | None = typer.Option(None, help="�?query pack 筛选�?"),
    doi: str | None = typer.Option(None, help="只审核指�?DOI�?"),
    auto_retranscribe: bool = typer.Option(
        True,
        help="审核发现问题时自动重新转写�?",
    ),
    force_retranscribe: bool = typer.Option(
        False,
        help="即使审核未发现问题也强制重新转写�?",
    ),
) -> None:
    summary = audit_existing_note_records(
        query_pack=query_pack,
        limit=limit,
        doi=doi,
        auto_retranscribe=auto_retranscribe,
        force_retranscribe=force_retranscribe,
        echo_each=True,
    )
    if summary is None:
        typer.echo("没有可供审核的现有笔记�?")
        return
    typer.echo(
        f"审核完成：已审核 {summary['audited']} 条，重新转写 {summary['retranscribed']} 条，"
        f"跳过 {summary['skipped']} 条，失败 {summary['failed']} 条。"
        f"初始问题：共 {summary['initial_issue_count']} 个，"
        f"其中严重 {summary['initial_severe_issue_count']} 个。"
        f"最终问题：共 {summary['final_issue_count']} 个，"
        f"其中严重 {summary['final_severe_issue_count']} 个。"
        f"推导审查：仅识别错误 {summary['direct_error_count']} 个，"
        f"自洽性复核 {summary['consistency_review_count']} 个。"
    )


@app.command("repair-indexed-metadata")
def repair_indexed_metadata(
    limit: int = typer.Option(200, min=1, max=5000, help="最多检查多少条已入库记录�?"),
    query_pack: str | None = typer.Option(None, help="只检查指�?query pack�?"),
    doi: str | None = typer.Option(None, help="只修复指�?DOI�?"),
    attached_only: bool = typer.Option(
        True,
        "--attached-only/--all-records",
        help="默认只检查已经挂载本�?PDF 的记录�?",
    ),
    dry_run: bool = typer.Option(
        False,
        help="只预览修复结果，不移动文件，也不更新索引�?",
    ),
) -> None:
    summary = repair_indexed_metadata_records(
        query_pack=query_pack,
        limit=limit,
        doi=doi,
        attached_only=attached_only,
        dry_run=dry_run,
        echo_each=True,
    )
    if summary is None:
        if attached_only and summary is None:
            typer.echo("未发现需要修复的已挂�?PDF 记录�?")
        else:
            typer.echo("未发现需要修复的已入库记录�?")
        return
    verb = "计划修复" if dry_run else "已修复"
    typer.echo(
        f"Metadata 修复完成：{summary['repaired']} 条{verb}�?"
        f"{summary['failed']} 条失败，"
        f"{summary['moved_paths']} 个路径移动，"
        f"跳过 {summary['skipped_without_attached_pdf']} 条未挂载本地 PDF 的记录�?"
    )


@app.command("analyze-paper")
def analyze_paper(
    doi: str = typer.Option(..., help="已入库文献的 DOI�?"),
    source_file: Path | None = SOURCE_TEXT_FILE_OPTION,
) -> None:
    store = IndexStore(settings)
    records = store.load_paper_records(limit=1, doi=doi, unresolved_only=False)
    if not records:
        typer.secho(f"未找�?DOI 对应的已入库文献：{doi}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    source_text = read_optional_source_text(source_file)
    service = AnalysisService(settings)
    try:
        artifacts = service.analyze_record(records[0], source_text=source_text)
    except AIServiceError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    store.attach_analysis_artifacts(
        doi=doi,
        json_path=artifacts.json_path,
        markdown_path=artifacts.markdown_path,
    )
    typer.echo(f"分析 JSON：{artifacts.json_path}")
    if artifacts.usage and artifacts.usage.estimated_cost is not None:
        typer.echo(
            "分析成本："
            f"{artifacts.usage.estimated_cost:.6f} {artifacts.usage.currency} "
            f"({artifacts.usage.prompt_tokens}+{artifacts.usage.completion_tokens} tokens)"
        )


@app.command("analyze-query-pack")
def analyze_query_pack(
    query_pack: str = typer.Option(..., help="索引中已存在�?query pack 名称�?"),
    limit: int = typer.Option(20, min=1, max=500, help="最多处理多少条已入库文献�?"),
    pending_only: bool = typer.Option(
        True,
        help="跳过已经生成分析产物的文献�?",
    ),
    force: bool = typer.Option(
        False,
        help="即使分析产物已存在也强制重建�?",
    ),
) -> None:
    store = IndexStore(settings)
    records = store.load_paper_records(limit=limit, query_pack=query_pack, unresolved_only=False)
    if not records:
        typer.echo("没有可供分析的已入库文献�?")
        return

    service = AnalysisService(settings)
    completed = 0
    skipped = 0
    failed = 0
    total_analysis_cost = 0.0
    for record in records:
        if not force and pending_only and record.analysis_json_path:
            skipped += 1
            continue
        if not record.doi:
            skipped += 1
            typer.secho(f"已跳过无 DOI 文献：{record.title}", fg=typer.colors.YELLOW)
            continue
        try:
            artifacts = service.analyze_record(record)
        except AIServiceError as exc:
            failed += 1
            typer.secho(f"分析失败：{record.doi}，{exc}", fg=typer.colors.RED)
            continue
        store.attach_analysis_artifacts(
            doi=record.doi,
            json_path=artifacts.json_path,
            markdown_path=artifacts.markdown_path,
        )
        completed += 1
        if artifacts.usage and artifacts.usage.estimated_cost is not None:
            total_analysis_cost += artifacts.usage.estimated_cost

    typer.echo(
        f"分析完成：已生成 {completed} 篇，跳过 {skipped} 篇，失败 {failed} 篇�?"
        f"预计分析成本：{total_analysis_cost:.6f} CNY"
    )


def build_runtime_journal_spec(
    journal: JournalSpec,
    *,
    providers: str | None,
    from_year: int | None,
    until_year: int | None,
    limit: int | None,
) -> JournalSpec:
    payload = journal.model_dump()
    if providers:
        payload["providers"] = split_csv(providers)
    if from_year is not None:
        payload["from_year"] = from_year
    if until_year is not None:
        payload["until_year"] = until_year
    if limit is not None:
        payload["limit"] = limit
    return JournalSpec.model_validate(payload)


def run_journal_sync(
    spec: JournalSpec,
    *,
    resolve_fulltext: bool,
    download_oa: bool,
) -> dict[str, object]:
    service = SearchService(settings)
    store = IndexStore(settings)
    records = service.search_journal(spec)
    existing_keys = load_existing_dedupe_keys(store, records)
    new_records = [record for record in records if record.dedupe_key not in existing_keys]

    synced_records = records
    if resolve_fulltext:
        resolver = FullTextResolver(settings)
        synced_records = resolver.resolve_records(records)

    store.upsert_records(synced_records)

    downloaded_oa = 0
    if download_oa:
        downloader = OADownloadService(settings)
        for record in synced_records:
            if not record.doi:
                continue
            try:
                result = downloader.download_record(record)
            except OADownloadError as exc:
                typer.secho(str(exc), fg=typer.colors.YELLOW)
                continue
            if result is None:
                continue
            store.attach_pdf(record.doi, result.path)
            record.local_pdf_path = str(result.path.resolve())
            record.download_status = "downloaded"
            if result.downloaded:
                downloaded_oa += 1

    output_base = settings.metadata_dir / "journals" / spec.short_name
    paths = export_records(synced_records, output_base)
    download_rows = store.list_download_queue(
        limit=max(spec.limit, 50),
        query_pack=spec.short_name,
        pending_only=True,
    )
    download_list_path = export_download_queue(
        download_rows,
        settings.download_list_dir / f"{spec.short_name}-download-list.csv",
    )
    return {
        "total_records": len(synced_records),
        "new_records": len(new_records),
        "new_record_objects": new_records,
        "downloaded_oa": downloaded_oa,
        "csv_path": paths["csv"],
        "download_list_path": download_list_path,
    }


def load_existing_dedupe_keys(store: IndexStore, records: list) -> set[str]:
    keys: set[str] = set()
    for record in records:
        if record.doi and store.get_paper_by_doi(record.doi):
            keys.add(record.dedupe_key)
    return keys


@app.command("migrate-library")
def migrate_library(
    limit: int = typer.Option(200, min=1, max=5000, help="最多处理多少条已入库文献�?"),
    query_pack: str | None = typer.Option(None, help="�?query pack 筛选�?"),
) -> None:
    store = IndexStore(settings)
    records = store.load_paper_records(limit=limit, query_pack=query_pack, unresolved_only=False)
    if not records:
        typer.echo("没有可供迁移的已入库文献�?")
        return

    migrated_pdf = 0
    migrated_parsed = 0
    migrated_analysis = 0
    for record in records:
        managed_pdf_path: Path | None = None
        legacy_stem = doi_to_suffix(record.doi)

        if record.doi and record.local_pdf_path:
            source_pdf = Path(record.local_pdf_path)
            if source_pdf.exists():
                before = source_pdf.resolve()
                if store.attach_pdf(record.doi, source_pdf):
                    updated = store.get_paper_by_doi(record.doi)
                    after_path = Path(str(updated["local_pdf_path"])) if updated else before
                    managed_pdf_path = after_path
                    if after_path.resolve() != before:
                        migrated_pdf += 1
            elif source_pdf:
                managed_pdf_path = source_pdf

        if record.doi and record.parsed_md_path:
            source_md = first_existing_path(
                Path("output/parsed") / f"{legacy_stem}.md",
                Path(record.parsed_md_path),
            )
            if source_md.exists():
                target_base = build_parsed_output_base(settings.parsed_output_dir, record)
                target_md = target_base.with_suffix(".md")
                source_proofread_md = source_md.with_name(f"{source_md.stem}.proofread.md")
                source_proofread_json = source_md.with_name(f"{source_md.stem}.proofread.json")
                target_proofread_md = target_md.with_name(f"{target_md.stem}.proofread.md")
                target_proofread_json = target_md.with_name(f"{target_md.stem}.proofread.json")
                copy_if_needed(source_md, target_md)
                if source_proofread_md.exists():
                    copy_if_needed(source_proofread_md, target_proofread_md)
                if source_proofread_json.exists():
                    copy_if_needed(source_proofread_json, target_proofread_json)
                refresh_parsed_note_links(
                    note_path=target_md,
                    pdf_path=managed_pdf_path,
                    proofread_markdown_path=(
                        target_proofread_md if target_proofread_md.exists() else None
                    ),
                )
                if store.attach_parsed_artifacts(record.doi, target_md):
                    migrated_parsed += 1

        if record.doi and record.analysis_md_path and record.analysis_json_path:
            source_md = first_existing_path(
                Path("output/analysis") / f"{legacy_stem}.md",
                Path(record.analysis_md_path),
            )
            source_json = first_existing_path(
                Path("output/analysis") / f"{legacy_stem}.json",
                Path(record.analysis_json_path),
            )
            if source_md.exists() and source_json.exists():
                target_base = build_analysis_output_base(settings.analysis_output_dir, record)
                target_md = target_base.with_suffix(".md")
                target_json = target_base.with_suffix(".json")
                copy_if_needed(source_md, target_md)
                copy_if_needed(source_json, target_json)
                if store.attach_analysis_artifacts(
                    record.doi,
                    json_path=target_json,
                    markdown_path=target_md,
                ):
                    migrated_analysis += 1

    typer.echo(
        "库内迁移完成�?"
        f"PDF 路径 {migrated_pdf} 条，"
        f"解析产物 {migrated_parsed} 组，"
        f"分析产物 {migrated_analysis} 组�?"
    )


@app.command()
def serve(
    host: str = typer.Option(settings.api_host, help="FastAPI 服务监听地址�?"),
    port: int = typer.Option(settings.api_port, help="FastAPI 服务监听端口�?"),
) -> None:
    uvicorn.run("powerlit.api.app:app", host=host, port=port, reload=False)


def run_incoming_processing_command(
    *,
    limit: int | None,
    parse: bool,
    analyze: bool,
) -> None:
    incoming_files = iter_incoming_pdfs(settings.incoming_pdf_dir)
    typer.echo(f"Incoming 目录：{settings.incoming_pdf_dir}")
    typer.echo(f"发现 {len(incoming_files)} 个 PDF 文件。")
    if not incoming_files:
        typer.echo("没有发现 PDF 文件，无需处理。")
        return

    processor = IncomingPDFProcessor(settings)
    files_to_process = incoming_files if limit is None else incoming_files[:limit]
    results = []
    failures = []
    stage_label = "登记 Incoming PDF" if not parse else "处理 Incoming PDF"
    with progressbar_context(
        files_to_process,
        label=stage_label,
        item_show_func=progress_item_for_path,
    ) as progress:
        for pdf_path in progress:
            try:
                results.append(
                    processor.process_file(
                        pdf_path,
                        parse=parse,
                        analyze=analyze,
                    )
                )
            except IncomingProcessorError as exc:
                failures.append((pdf_path, str(exc)))

    for item in results:
        typer.echo(f"已处理：{item.file_path.name}")
        typer.echo(f"  DOI：{item.doi}")
        typer.echo(f"  已存 PDF：{item.target_pdf_path}")
        if item.parsed_json_path:
            typer.echo(f"  解析 JSON：{item.parsed_json_path}")
        else:
            typer.echo("  解析 JSON：已跳过")
        if item.analysis_json_path:
            typer.echo(f"  分析 JSON：{item.analysis_json_path}")
        elif analyze:
            typer.echo("  分析 JSON：已跳过")

    for pdf_path, message in failures:
        typer.secho(f"失败：{pdf_path.name} -> {message}", fg=typer.colors.YELLOW)

    typer.echo(f"{stage_label}完成：成功 {len(results)} 个，失败 {len(failures)} 个。")


def parse_indexed_records(
    *,
    query_pack: str | None,
    limit: int,
    pending_only: bool,
    force: bool,
    doi: str | None = None,
    echo_each: bool = False,
) -> dict[str, int | float] | None:
    store = IndexStore(settings)
    parser = PDFParserService(settings)
    records = store.load_paper_records(
        limit=limit,
        query_pack=query_pack,
        unresolved_only=False,
        doi=doi,
    )
    if not records:
        return None

    parsed = 0
    skipped = 0
    failed = 0
    total_note_cost = 0.0
    total_review_issues = 0
    total_review_severe_issues = 0
    direct_error_count = 0
    consistency_review_count = 0
    with progressbar_context(
        records,
        label="转写已挂�?PDF",
        item_show_func=progress_item_for_record,
    ) as progress:
        for record in progress:
            if not force and pending_only and (record.parsed_json_path or record.parsed_md_path):
                skipped += 1
                continue
            if not record.doi:
                skipped += 1
                typer.secho(f"已跳过无 DOI 文献：{record.title}", fg=typer.colors.YELLOW)
                continue
            if not record.local_pdf_path:
                skipped += 1
                typer.secho(
                    f"已跳过未挂载 PDF 文献：{record.doi}",
                    fg=typer.colors.YELLOW,
                )
                continue
            try:
                artifacts = parser.parse_record(record, Path(record.local_pdf_path))
            except PDFParseError as exc:
                failed += 1
                typer.secho(f"转写失败：{record.doi}，{exc}", fg=typer.colors.RED)
                continue
            store.attach_parsed_artifacts(
                doi=record.doi,
                json_path=artifacts.json_path,
                markdown_path=artifacts.markdown_path,
            )
            parsed += 1
            if artifacts.note_usage and artifacts.note_usage.estimated_cost is not None:
                total_note_cost += artifacts.note_usage.estimated_cost
            total_review_issues += artifacts.review_issue_count
            total_review_severe_issues += artifacts.review_severe_issue_count
            direct_error_count += artifacts.review_derivation_direct_error_count
            consistency_review_count += artifacts.review_derivation_consistency_count
            if echo_each:
                typer.echo(f"已转写：{record.doi}")
                typer.echo(f"  解析 JSON：{artifacts.json_path}")
                if artifacts.proofread_markdown_path:
                    typer.echo(f"  校对报告：{artifacts.proofread_markdown_path}")
                typer.echo(
                    "  推导审查："
                    f"{artifacts.review_derivation_direct_error_count} 个仅识别错误，"
                    f"{artifacts.review_derivation_consistency_count} 个自洽性复核"
                )

    return {
        "parsed": parsed,
        "skipped": skipped,
        "failed": failed,
        "total_note_cost": total_note_cost,
        "total_review_issues": total_review_issues,
        "total_review_severe_issues": total_review_severe_issues,
        "direct_error_count": direct_error_count,
        "consistency_review_count": consistency_review_count,
    }


def audit_existing_note_records(
    *,
    query_pack: str | None,
    limit: int,
    doi: str | None,
    auto_retranscribe: bool,
    force_retranscribe: bool,
    echo_each: bool = False,
) -> dict[str, int] | None:
    store = IndexStore(settings)
    audit_service = ExistingNoteAuditService(settings)
    records = store.load_paper_records(
        limit=limit,
        query_pack=query_pack,
        unresolved_only=False,
        doi=doi,
    )
    if not records:
        return None

    audited = 0
    retranscribed = 0
    skipped = 0
    failed = 0
    initial_issue_count = 0
    initial_severe_issue_count = 0
    final_issue_count = 0
    final_severe_issue_count = 0
    direct_error_count = 0
    consistency_review_count = 0
    with progressbar_context(
        records,
        label="审核现有笔记",
        item_show_func=progress_item_for_record,
    ) as progress:
        for record in progress:
            if not record.doi:
                skipped += 1
                typer.secho(f"已跳过无 DOI 文献：{record.title}", fg=typer.colors.YELLOW)
                continue
            if not record.parsed_md_path:
                skipped += 1
                typer.secho(
                    f"已跳过缺少笔记的文献：{record.doi}",
                    fg=typer.colors.YELLOW,
                )
                continue
            try:
                artifacts = audit_service.audit_record(
                    record,
                    auto_retranscribe=auto_retranscribe,
                    force_retranscribe=force_retranscribe,
                )
            except PDFParseError as exc:
                failed += 1
                typer.secho(f"审核失败：{record.doi}，{exc}", fg=typer.colors.RED)
                continue
            audited += 1
            if artifacts.retranscribed:
                retranscribed += 1
                store.attach_parsed_artifacts(
                    doi=record.doi,
                    markdown_path=artifacts.note_path,
                )
            initial_issue_count += artifacts.initial_issue_count
            initial_severe_issue_count += artifacts.initial_severe_issue_count
            final_issue_count += artifacts.final_issue_count
            final_severe_issue_count += artifacts.final_severe_issue_count
            direct_error_count += artifacts.final_direct_error_count
            consistency_review_count += artifacts.final_consistency_review_count
            if echo_each:
                action = "已重转写" if artifacts.retranscribed else "已审核"
                typer.echo(f"{action}：{record.doi}")
                typer.echo(f"  笔记：{artifacts.note_path}")
                typer.echo(f"  校对报告：{artifacts.proofread_markdown_path}")
                typer.echo(
                    f"  初始问题：共 {artifacts.initial_issue_count} 个，"
                    f"其中严重 {artifacts.initial_severe_issue_count} 个。"
                )
                typer.echo(
                    f"  最终问题：共 {artifacts.final_issue_count} 个，"
                    f"其中严重 {artifacts.final_severe_issue_count} 个。"
                )
                typer.echo(
                    "  推导审查："
                    f"{artifacts.final_direct_error_count} 个仅识别错误，"
                    f"{artifacts.final_consistency_review_count} 个自洽性复核"
                )

    return {
        "audited": audited,
        "retranscribed": retranscribed,
        "skipped": skipped,
        "failed": failed,
        "initial_issue_count": initial_issue_count,
        "initial_severe_issue_count": initial_severe_issue_count,
        "final_issue_count": final_issue_count,
        "final_severe_issue_count": final_severe_issue_count,
        "direct_error_count": direct_error_count,
        "consistency_review_count": consistency_review_count,
    }


def repair_indexed_metadata_records(
    *,
    query_pack: str | None,
    limit: int,
    doi: str | None,
    attached_only: bool,
    dry_run: bool,
    echo_each: bool = False,
) -> dict[str, int] | None:
    service = IndexedMetadataRepairService(settings)
    candidate_collection = service.collect_candidates_with_stats(
        limit=limit,
        query_pack=query_pack,
        doi=doi,
        attached_only=attached_only,
    )
    candidates = candidate_collection.candidates
    if not candidates:
        return None

    repaired = 0
    failed = 0
    moved_paths = 0
    with progressbar_context(
        candidates,
        label="修复已入�?Metadata",
        item_show_func=lambda item: progress_item_for_record(item.record),
    ) as progress:
        for candidate in progress:
            try:
                result = service.repair_candidate(candidate, dry_run=dry_run)
            except MetadataRepairError as exc:
                failed += 1
                typer.secho(
                    f"Metadata 修复失败：{candidate.record.doi or candidate.record.title}，{exc}",
                    fg=typer.colors.RED,
                )
                continue
            repaired += 1
            moved_paths += len(result.moved_paths)
            if echo_each:
                action = "计划修复" if dry_run else "已修复"
                typer.echo(f"{action}：{result.doi}")
                typer.echo(f"  原因：{format_metadata_repair_reasons(result.reasons)}")
                typer.echo(f"  标题：{result.old_title} -> {result.new_title}")
                typer.echo(
                    "  期刊�?"
                    f"{result.old_source_title or '未知'} -> {result.new_source_title or '未知'}"
                )
                if result.new_pdf_path:
                    typer.echo(f"  已存 PDF：{result.new_pdf_path}")
                if result.moved_paths:
                    typer.echo(f"  移动路径：{len(result.moved_paths)}")

    return {
        "repaired": repaired,
        "failed": failed,
        "moved_paths": moved_paths,
        "skipped_without_attached_pdf": candidate_collection.skipped_without_attached_pdf,
    }


def format_metadata_repair_reasons(reasons: list[str]) -> str:
    reason_labels = {
        "manual_request": "手动指定",
        "pdf_fallback_metadata": "来自 PDF fallback 元数�?",
        "title_too_long": "标题异常过长",
        "title_looks_like_body_text": "标题像正文段�?",
        "title_contains_metadata": "标题混入元数�?",
        "title_equals_source_title": "标题与期刊名相同",
        "source_title_looks_like_article_title": "期刊字段像文章标�?",
        "issue_catalog_title_mismatch": "与期目录标题不一�?",
        "issue_catalog_source_title_mismatch": "与期目录期刊名不一�?",
        "issue_catalog_volume_mismatch": "与期目录卷号不一�?",
        "issue_catalog_issue_mismatch": "与期目录期号不一�?",
        "pdf_title_matches_issue_catalog": "PDF 标题与期目录一�?",
        "pdf_title_mismatch_issue_catalog": "PDF 标题与期目录不一�?",
        "known_journal_doi_wrong_document_type": "已知期刊 DOI 被误判为会议",
    }
    return "，".join(reason_labels.get(reason, reason) for reason in reasons)


def has_existing_note(record) -> bool:  # noqa: ANN001
    if record.parsed_json_path and Path(record.parsed_json_path).exists():
        return True
    if record.parsed_md_path and Path(record.parsed_md_path).exists():
        return True
    return False


def normalize_work_item_stage(stage: str) -> str:
    normalized = stage.strip().lower()
    allowed = {"register", "transcribe", "audit"}
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Unknown stage: {stage}. Expected one of: {choices}")
    return normalized


def collect_work_items(
    *,
    stage: str,
    limit: int | None,
    query_pack: str | None,
    doi: str | None,
    pending_only: bool,
) -> list[dict[str, str]]:
    if stage == "register":
        incoming_files = iter_incoming_pdfs(settings.incoming_pdf_dir)
        if limit is not None:
            incoming_files = incoming_files[:limit]
        return [
            {
                "stage": stage,
                "identifier": str(path.resolve()),
                "display_name": path.name,
                "file_path": str(path.resolve()),
            }
            for path in incoming_files
        ]

    store = IndexStore(settings)
    records = store.load_paper_records(
        limit=1 if doi else 50000,
        query_pack=query_pack,
        unresolved_only=False,
        doi=doi,
    )

    items: list[dict[str, str]] = []
    for record in records:
        if stage == "transcribe":
            item = build_transcribe_work_item(record, pending_only=pending_only)
        else:
            item = build_audit_work_item(record)
        if item is not None:
            items.append(item)

    if limit is not None:
        return items[:limit]
    return items


def build_transcribe_work_item(
    record,
    *,
    pending_only: bool,
) -> dict[str, str] | None:  # noqa: ANN001
    if not record.doi or not record.local_pdf_path:
        return None
    if pending_only and has_existing_note(record):
        return None
    pdf_path = Path(record.local_pdf_path)
    if not pdf_path.exists():
        return None
    return {
        "stage": "transcribe",
        "identifier": record.doi,
        "display_name": work_item_display_name(record),
        "doi": record.doi,
        "title": record.title,
        "query_pack": record.query_pack or "",
        "file_path": str(pdf_path.resolve()),
    }


def build_audit_work_item(record) -> dict[str, str] | None:  # noqa: ANN001
    if not record.doi or not record.parsed_md_path:
        return None
    note_path = Path(record.parsed_md_path) if record.parsed_md_path else None
    if note_path is None or not note_path.exists():
        return None
    return {
        "stage": "audit",
        "identifier": record.doi,
        "display_name": work_item_display_name(record),
        "doi": record.doi,
        "title": record.title,
        "query_pack": record.query_pack or "",
        "note_path": str(note_path.resolve()),
    }


def work_item_display_name(record) -> str:  # noqa: ANN001
    if record.doi:
        return f"{record.doi} | {record.title}"
    return record.title


def progressbar_context(
    items: list,
    *,
    label: str,
    item_show_func,
):
    if not items or not should_render_progressbar():
        return nullcontext(items)
    safe_item_show_func = build_safe_item_show_func(item_show_func)
    return typer.progressbar(
        items,
        length=len(items),
        label=label,
        show_eta=True,
        show_percent=True,
        show_pos=True,
        item_show_func=safe_item_show_func,
    )


def should_render_progressbar() -> bool:
    stream = getattr(sys, "stdout", None)
    return bool(stream and hasattr(stream, "isatty") and stream.isatty())


def progress_item_for_record(record) -> str:  # noqa: ANN001
    if record is None:
        return ""
    return shorten_progress_text(getattr(record, "doi", None) or getattr(record, "title", None) or "")


def progress_item_for_path(path: Path) -> str:
    if path is None:
        return ""
    return shorten_progress_text(path.name)


def shorten_progress_text(value: str | None, limit: int = 72) -> str:
    if not value:
        return ""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def build_safe_item_show_func(item_show_func):  # noqa: ANN001
    def safe_item_show_func(item) -> str:  # noqa: ANN001
        if item is None:
            return ""
        try:
            return shorten_progress_text(item_show_func(item))
        except Exception:
            return ""

    return safe_item_show_func


def split_csv(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def split_csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def slugify(value: str) -> str:
    lowered = value.strip().lower()
    slug = sub(r"[^a-z0-9]+", "-", lowered)
    return slug.strip("-") or "search"


def provider_check_color(status: str) -> str | None:
    if status == "ok":
        return typer.colors.GREEN
    if status == "needs_config":
        return typer.colors.YELLOW
    return typer.colors.RED


def default_download_list_name(query_pack: str | None) -> str:
    if query_pack:
        return f"{slugify(query_pack)}-download-list"
    return "download-queue"


def build_repo_root_path(*parts: str) -> Path:
    return (settings.reference_dir.parents[1] / Path(*parts)).resolve()


def render_windows_command(parts: list[str]) -> str:
    rendered: list[str] = []
    for part in parts:
        if any(char in part for char in (' ', '(', ')', '[', ']', '{', '}', '&')):
            rendered.append(f'"{part}"')
        else:
            rendered.append(part)
    return " ".join(rendered)


def render_issue_catalog_sync_command(journal_filters: list[str]) -> str:
    powerlit_path = build_repo_root_path(".venv", "Scripts", "powerlit.exe")
    config_path = build_repo_root_path(*DEFAULT_JOURNAL_CATALOG_FILE.parts)
    command = [str(powerlit_path), "sync-journal-issue-catalogs", "--config-path", str(config_path)]
    for journal_name in journal_filters:
        command.extend(["--journal", journal_name])
    return render_windows_command(command)


def assisted_download_has_issue_catalogs(
    service: AssistedIncomingDownloadService,
    journal_filters: list[str] | None,
) -> bool:
    if not journal_filters:
        return True
    normalized_filters = {
        normalize_journal_filter(item)
        for item in journal_filters
        if normalize_journal_filter(item)
    }
    if not normalized_filters:
        return True
    return bool(service.iter_issue_catalog_payloads(normalized_filters))


def read_optional_source_text(path: Path | None) -> str | None:
    if path is None:
        return None
    return path.read_text(encoding="utf-8")


def resolve_pdf_path(attached_pdf_path: str | None, override_path: Path | None) -> Path:
    if override_path is not None:
        return override_path
    if attached_pdf_path:
        return Path(attached_pdf_path)
    raise PDFParseError("当前论文没有绑定本地 PDF，请先执�?attach-pdf 或显式传�?--file�?")


def copy_if_needed(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    copy2(source, target)


def first_existing_path(*candidates: Path) -> Path:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[-1]


def refresh_parsed_note_links(
    *,
    note_path: Path,
    pdf_path: Path | None,
    proofread_markdown_path: Path | None = None,
) -> None:
    if not note_path.exists():
        return
    content = note_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    if lines[:1] == ["---"]:
        in_frontmatter = True
        refreshed_lines = [lines[0]]
        for line in lines[1:]:
            if in_frontmatter and line == "---":
                in_frontmatter = False
                refreshed_lines.append(line)
                continue
            if in_frontmatter and line.startswith("raw_extraction: "):
                continue
            if in_frontmatter and line.startswith("pdf_file: "):
                value = workspace_obsidian_path(pdf_path)
                refreshed_lines.append(f'pdf_file: "{value}"')
                continue
            if in_frontmatter and line.startswith("proofread_report: "):
                value = workspace_obsidian_path(proofread_markdown_path)
                refreshed_lines.append(f'proofread_report: "{value}"')
                continue
            refreshed_lines.append(line)
        content = "\n".join(refreshed_lines).rstrip() + "\n"

    marker = "\n## 文件链接\n"
    if marker in content:
        content = content.split(marker, 1)[0].rstrip() + "\n"
    file_links = [
        "## 文件链接",
        "",
        f"- PDF: [[{workspace_obsidian_path(pdf_path)}]]",
        f"- 当前笔记: [[{workspace_obsidian_path(note_path)}]]",
    ]
    if proofread_markdown_path is not None:
        file_links.append(f"- 校对报告: [[{workspace_obsidian_path(proofread_markdown_path)}]]")
    file_links.append("")
    note_path.write_text(content.rstrip() + "\n\n" + "\n".join(file_links), encoding="utf-8")


def workspace_obsidian_path(path: Path | None) -> str:
    if path is None:
        return "unknown"
    root = settings.literature_root.parent.resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name



# --- RAG Subcommand Group ---

rag_app = typer.Typer(help="本地 RAG (检索增强生成) 向量数据库管理")
app.add_typer(rag_app, name="rag")
EVIDENCE_BUILD_VENUE_FOLDER_OPTION = typer.Option(
    None,
    "--venue-folder",
    help="只索引指定顶层期刊/来源目录，可重复传入。",
)
EVIDENCE_SEARCH_VENUE_FOLDER_OPTION = typer.Option(
    None,
    "--venue-folder",
    help="按顶层期刊/来源目录过滤，可重复传入。",
)


@rag_app.command("build-index")
def rag_build_index(
    force: bool = typer.Option(False, "--force", "-f", help="强制重建全量索引。"),
) -> None:
    """遍历 literature/json 所有的 MinerU 解析结果并构建向量索引"""
    service = RAGIndexService(settings)
    typer.echo("正在扫描 JSON 并生成向量，这可能需要几分钟...")
    count = service.build_full_index(force=force)
    if count > 0:
        typer.secho(f"✅ 成功构建索引，共计 {count} 个文本切片", fg=typer.colors.GREEN)
    else:
        typer.echo("没有新内容需要索引或索引已存在（使用 --force 重建）")


@rag_app.command("build-evidence-index")
def rag_build_evidence_index(
    force: bool = typer.Option(False, "--force", "-f", help="强制重建证据索引。"),
    venue_folder: list[str] | None = EVIDENCE_BUILD_VENUE_FOLDER_OPTION,
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-l",
        min=1,
        help="最多处理多少篇解析 JSON，用于冒烟测试。",
    ),
    json_output: bool = typer.Option(False, "--json", help="以 JSON 输出构建摘要。"),
) -> None:
    """构建毫秒级 SQLite FTS5 证据检索索引。"""
    service = EvidenceIndexService(settings)
    summary = service.build(force=force, venue_folders=venue_folder, limit=limit)
    if json_output:
        typer.echo(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        return
    typer.secho("证据索引构建完成", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"数据库：{summary.db_path}")
    typer.echo(f"JSON 根目录：{summary.json_root}")
    typer.echo(
        f"文档 {summary.documents} 篇，chunk {summary.chunks} 条，"
        f"跳过 {summary.skipped} 个文件，用时 {summary.elapsed_ms} ms"
    )


@rag_app.command("search")
def rag_search(
    query: str = typer.Argument(..., help="语义搜索关键词"),
    top_k: int = typer.Option(5, help="返回最相关的结果数量"),
) -> None:
    """在本地文献库中进行语义检索"""
    service = RAGSearchService(settings)
    results = service.search(query, top_k=top_k)
    
    if not results:
        typer.echo("未找到匹配结果")
        return
        
    for i, res in enumerate(results, start=1):
        typer.secho(f"\n[{i}] {res.title} (Score: {res.score:.4f})", fg=typer.colors.CYAN, bold=True)
        typer.echo(f"DOI: {res.doi} | Chunk: {res.chunk_index}")
        typer.echo("-" * 40)
        typer.echo(res.text)


@rag_app.command("evidence")
def rag_evidence(
    query: str = typer.Argument(..., help="证据检索查询。"),
    top: int = typer.Option(20, "--top", "-k", min=1, max=200, help="返回结果数量。"),
    venue_folder: list[str] | None = EVIDENCE_SEARCH_VENUE_FOLDER_OPTION,
    year_from: int | None = typer.Option(None, "--year-from", help="发表年份下限。"),
    year_to: int | None = typer.Option(None, "--year-to", help="发表年份上限。"),
    doi: str | None = typer.Option(None, "--doi", help="只检索指定 DOI。"),
    section: str | None = typer.Option(None, "--section", help="按章节标题模糊过滤。"),
    json_output: bool = typer.Option(False, "--json", help="以 JSON 输出，供 skill 调用。"),
) -> None:
    """在本地证据索引中进行毫秒级 FTS 检索。"""
    service = EvidenceIndexService(settings)
    payload = service.search(
        query,
        top=top,
        venue_folders=venue_folder,
        year_from=year_from,
        year_to=year_to,
        doi=doi,
        section=section,
    )
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    elif not payload["available"]:
        typer.secho(payload["message"], fg=typer.colors.RED)
    elif not payload["results"]:
        typer.echo("未找到匹配证据")
    else:
        typer.echo(
            f"候选源：{payload['candidate_source']} | "
            f"结果 {payload['count']} 条 | 用时 {payload['elapsed_ms']} ms"
        )
        for i, item in enumerate(payload["results"], start=1):
            typer.secho(
                f"\n[{i}] {item['title']} (score={item['score']})",
                fg=typer.colors.CYAN,
                bold=True,
            )
            typer.echo(
                f"DOI: {item['doi'] or 'unknown'} | "
                f"Venue: {item['venue_folder'] or 'unknown'} | "
                f"Year: {item['year'] or 'unknown'}"
            )
            typer.echo(
                f"Section: {item['section'] or 'unknown'} | "
                f"Page: {item['page_start'] or 'unknown'}"
            )
            typer.echo(item["snippet"])
            typer.echo(f"Parsed JSON: {item['parsed_json_path']}")
    if not payload["available"]:
        raise typer.Exit(code=2)


@rag_app.command("evidence-status")
def rag_evidence_status(
    json_output: bool = typer.Option(False, "--json", help="以 JSON 输出状态。"),
) -> None:
    """查看本地证据索引状态。"""
    service = EvidenceIndexService(settings)
    payload = service.status()
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not payload["available"]:
        typer.secho(payload["message"], fg=typer.colors.RED)
        typer.echo(f"数据库：{payload['db_path']}")
        raise typer.Exit(code=2)
    typer.secho("证据索引可用", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"数据库：{payload['db_path']}")
    typer.echo(f"JSON 根目录：{payload['json_root']}")
    typer.echo(f"文档 {payload['documents']} 篇，chunk {payload['chunks']} 条")


@rag_app.command("ingest-all")
def rag_ingest_all(
    limit: int = typer.Option(None, "--limit", "-l", help="限制处理的文件数量"),
    force: bool = typer.Option(True, "--force/--no-force", help="是否强制覆盖现有解析记录"),
    sync: bool = typer.Option(False, "--sync/--no-sync", help="是否在解析后立即同步至 Google Drive"),
    source: str = typer.Option("incoming", "--source", help="扫描源: incoming (待处理区) 或 reference (正式库)"),
) -> None:
    """批量处理 PDF 并同步到索引（可选从不同来源扫描）"""
    from powerlit.services.rag_index import RAGIndexService
    from powerlit.services.drive_upload import GoogleDriveService
    
    processor = IncomingPDFProcessor(settings)
    indexer = RAGIndexService(settings)
    drive = GoogleDriveService(settings)
    
    # 根据 source 选择目录
    scan_dir = settings.incoming_pdf_dir if source == "incoming" else settings.reference_dir
    pdf_files = iter_incoming_pdfs(scan_dir)
    total = len(pdf_files) if limit is None else min(len(pdf_files), limit)
    
    if total == 0:
        typer.echo(f"在 {source} 目录下未发现待处理的 PDF 文件")
        return
        
    typer.secho(f"发现 {total} 个 PDF 文件 (来源: {source})，准备开始批量入库...", fg=typer.colors.YELLOW, bold=True)
    
    success_count = 0
    fail_count = 0
    
    for i, pdf_path in enumerate(pdf_files, start=1):
        if limit is not None and i > limit:
            break
            
        typer.echo(f"\n[{i}/{total}] 正在处理: {pdf_path.name}")
        try:
            # 1. 基础处理 (DOI, 解析, 分析)
            result = processor.process_file(
                pdf_path,
                parse=True,
                analyze=True,
                force_overwrite=force,
                progress_callback=lambda msg: typer.secho(f"  {msg}", dim=True)
            )
            
            # 2. 增量索引
            if result.parsed_json_path:
                typer.echo(f"  - 正在更新向量索引 (DOI: {result.doi})...")
                indexer.incremental_index(result.parsed_json_path)
            
            # 3. 同步至 Google Drive (可选)
            if sync and result.doi:
                typer.echo(f"  - 正在同步至 Google Drive...")
                drive.upload_parsed_markdown(result.doi)
                
            typer.secho(f"  ✅ 处理完成: {result.doi}", fg=typer.colors.GREEN)
            success_count += 1
        except Exception as e:
            typer.secho(f"  ❌ 处理失败: {e}", fg=typer.colors.RED)
            fail_count += 1
            
    typer.echo("-" * 40)
    typer.secho(f"🏁 批量处理结束。成功: {success_count}, 失败: {fail_count}", bold=True)


@rag_app.command("watch")
def rag_watch() -> None:
    """启动后台监听服务，自动处理新入库的 PDF 并同步到 Drive"""
    service = IncomingWatcherService(settings)
    typer.secho("🚀 PowerLit 监听服务已启动...", fg=typer.colors.GREEN, bold=True)
    typer.echo(f"监听路径: {settings.incoming_pdf_dir}")
    typer.echo("按 Ctrl+C 停止服务")
    try:
        service.start()
    except KeyboardInterrupt:
        service.stop()
        typer.echo("\n服务已停止")


@rag_app.command("sync-drive")
def rag_sync_drive(
    doi: str = typer.Argument(..., help="要同步的文献 DOI"),
) -> None:
    """将指定文献的解析结果手动同步到 Google Drive (NotebookLM)"""
    service = GoogleDriveService(settings)
    typer.echo(f"正在同步 {doi} 到 Google Drive...")
    file_id = service.upload_parsed_markdown(doi)
    if file_id:
        typer.secho(f"✅ 同步成功，Drive ID: {file_id}", fg=typer.colors.GREEN)
    else:
        typer.secho("❌ 同步失败，请检查配置和日志")


if __name__ == "__main__":
    app()





