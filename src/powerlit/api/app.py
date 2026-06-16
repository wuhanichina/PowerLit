from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import sqlite3
from shutil import copyfileobj
from urllib.parse import quote_plus

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from powerlit.models import (
    AnalysisRequest,
    AnalysisResponse,
    PaperCardBuildRequest,
    QuerySpec,
    SearchResponse,
    WorkspaceAddRequest,
    WorkspaceCreateRequest,
)
from powerlit.providers.base import ProviderError
from powerlit.services.ai_analysis import AIServiceError, AnalysisService
from powerlit.services.artifact_metadata import enrich_paper_row
from powerlit.services.export import export_records
from powerlit.services.index import IndexStore, sanitize_filename, suggest_pdf_filename
from powerlit.services.paper_cards import PaperCardService
from powerlit.services.provider_health import (
    check_provider_connectivity,
    emit_provider_check_report,
)
from powerlit.services.search import SearchService
from powerlit.services.status import provider_status
from powerlit.services.topics import load_topics
from powerlit.settings import Settings, settings

PACKAGE_DIR = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
WORKSPACE_ROOT = PACKAGE_DIR.parents[1]
UPLOAD_FILE_FIELD = File(...)
UI_PAPER_COLUMNS = """
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
"""
UI_QUEUE_COLUMNS = """
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
"""
UI_UNKNOWN_SUMMARY = {
    "total_papers": "unknown",
    "downloaded_papers": "unknown",
    "pending_downloads": "unknown",
    "papers_with_doi": "unknown",
    "analyzed_papers": "unknown",
    "parsed_papers": "unknown",
    "carded_papers": "unknown",
    "workspace_count": "unknown",
    "query_pack_count": "unknown",
    "latest_update": None,
}


def create_app(app_settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app_settings.provider_self_check_on_startup:
            checks = check_provider_connectivity(app_settings)
            app.state.provider_checks = checks
            emit_provider_check_report(checks, print)
        else:
            app.state.provider_checks = []
        yield

    app = FastAPI(title="PowerLit API", version="0.1.0", lifespan=lifespan)
    service = SearchService(app_settings)
    analysis_service = AnalysisService(app_settings)
    card_service = PaperCardService(app_settings)
    store = IndexStore(app_settings)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/providers")
    def providers() -> list[dict[str, str]]:
        return provider_status(app_settings)

    @app.get("/topics")
    def topics() -> dict[str, dict[str, str]]:
        return load_topics()

    @app.get("/papers")
    def papers(limit: int = 20, query_pack: str | None = None) -> list[dict[str, object]]:
        return enrich_rows(store.list_papers(limit=limit, query_pack=query_pack))

    @app.get("/workspaces")
    def workspaces() -> list[dict[str, object]]:
        return store.list_workspaces()

    @app.get("/workspaces/{name}")
    def workspace(name: str, limit: int = 20) -> dict[str, object]:
        summary = store.get_workspace(name)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"Workspace not found: {name}")
        return {
            "workspace": summary,
            "query_packs": store.list_workspace_query_packs(name),
            "papers": enrich_rows(store.list_workspace_papers(name, limit=limit)),
        }

    @app.post("/workspaces")
    def create_workspace(payload: WorkspaceCreateRequest) -> dict[str, object]:
        try:
            store.create_workspace(payload.name, payload.description)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        summary = store.get_workspace(payload.name)
        if summary is None:
            raise HTTPException(status_code=500, detail="Workspace create succeeded but lookup failed.")
        return {"workspace": summary}

    @app.post("/workspaces/{name}/members")
    def add_workspace_members(name: str, payload: WorkspaceAddRequest) -> dict[str, object]:
        if not payload.dois and not payload.query_packs:
            raise HTTPException(status_code=400, detail="Provide at least one DOI or query pack.")
        if store.get_workspace(name) is None:
            raise HTTPException(status_code=404, detail=f"Workspace not found: {name}")

        added_papers = 0
        duplicate_papers = 0
        added_query_pack_papers = 0
        missing_inputs: list[str] = []

        for doi in payload.dois:
            try:
                added = store.add_paper_to_workspace(name, doi)
            except LookupError as exc:
                missing_inputs.append(str(exc))
                continue
            if added:
                added_papers += 1
            else:
                duplicate_papers += 1

        for query_pack in payload.query_packs:
            try:
                added_query_pack_papers += store.add_query_pack_to_workspace(name, query_pack)
            except LookupError as exc:
                missing_inputs.append(str(exc))

        return {
            "workspace": store.get_workspace(name),
            "added_papers": added_papers,
            "duplicate_papers": duplicate_papers,
            "added_query_pack_papers": added_query_pack_papers,
            "missing_inputs": missing_inputs,
        }

    @app.get("/download-queue")
    def download_queue(
        limit: int = 20,
        query_pack: str | None = None,
        pending_only: bool = True,
    ) -> list[dict[str, object]]:
        return store.list_download_queue(
            limit=limit,
            query_pack=query_pack,
            pending_only=pending_only,
        )

    @app.post("/search", response_model=SearchResponse)
    def search(spec: QuerySpec) -> SearchResponse:
        try:
            results = execute_search(service, store, app_settings, spec)
        except (ProviderError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SearchResponse(query=spec, results=results)

    @app.get("/analysis")
    def get_analysis(doi: str) -> dict[str, str]:
        paper = store.get_paper_by_doi(doi)
        if paper is None:
            raise HTTPException(status_code=404, detail=f"库中未找到 DOI：{doi}")
        if not paper.get("analysis_json_path"):
            raise HTTPException(status_code=404, detail=f"该论文尚未生成分析结果：{doi}")
        return {
            "doi": doi,
            "json_path": str(paper["analysis_json_path"]),
        }

    @app.get("/paper-card")
    def get_paper_card(doi: str, include_payload: bool = False) -> dict[str, object]:
        paper = store.get_paper_by_doi(doi)
        if paper is None:
            raise HTTPException(status_code=404, detail=f"No indexed paper found for DOI: {doi}")
        json_path = paper.get("paper_card_json_path")
        if not json_path:
            raise HTTPException(status_code=404, detail=f"Paper card not found for DOI: {doi}")

        response: dict[str, object] = {
            "doi": doi,
            "json_path": str(json_path),
        }
        if include_payload:
            response["payload"] = json.loads(Path(str(json_path)).read_text(encoding="utf-8"))
        return response

    @app.post("/paper-card/build")
    def build_paper_card(payload: PaperCardBuildRequest) -> dict[str, object]:
        paper = store.get_paper_by_doi(payload.doi)
        if paper is None:
            raise HTTPException(
                status_code=404,
                detail=f"No indexed paper found for DOI: {payload.doi}",
            )
        if (
            not payload.force
            and paper.get("paper_card_json_path")
        ):
            return {
                "doi": payload.doi,
                "json_path": str(paper["paper_card_json_path"]),
                "skipped": True,
            }

        records = store.load_paper_records(limit=1, doi=payload.doi, unresolved_only=False)
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"No indexed paper found for DOI: {payload.doi}",
            )
        record = records[0]
        artifacts = card_service.build_card(record)
        attached = store.attach_paper_card_artifacts(
            dedupe_key=record.dedupe_key,
            json_path=artifacts.json_path,
            markdown_path=artifacts.markdown_path,
        )
        if not attached:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to attach paper card for DOI: {payload.doi}",
            )
        return {
            "doi": payload.doi,
            "json_path": str(artifacts.json_path),
            "skipped": False,
        }

    @app.get("/local-file")
    def local_file(path: str):
        file_path = resolve_workspace_file(path, app_settings)
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(status_code=404, detail="文件不存在。")
        return FileResponse(file_path)

    @app.post("/analyze", response_model=AnalysisResponse)
    def analyze(payload: AnalysisRequest) -> AnalysisResponse:
        records = store.load_paper_records(limit=1, doi=payload.doi, unresolved_only=False)
        if not records:
            raise HTTPException(status_code=404, detail=f"库中未找到 DOI：{payload.doi}")
        try:
            artifacts = analysis_service.analyze_record(
                records[0],
                source_text=payload.source_text,
            )
        except AIServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if records[0].doi:
            store.attach_analysis_artifacts(
                doi=records[0].doi,
                json_path=artifacts.json_path,
                markdown_path=artifacts.markdown_path,
            )
        return artifacts.to_response(records[0].doi)

    @app.get("/")
    def dashboard(request: Request, message: str | None = None):
        context = base_context(request, store, app_settings)
        context.update(
            {
                "message": message,
                "recent_papers": list_ui_papers(store, limit=10),
                "queue_preview": list_ui_download_queue(store, app_settings, limit=8),
            }
        )
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/library")
    def library(
        request: Request,
        limit: int = 30,
        query_pack: str | None = None,
        message: str | None = None,
    ):
        context = base_context(request, store, app_settings)
        context.update(
            {
                "message": message,
                "papers": list_ui_papers(store, limit=limit, query_pack=query_pack),
                "selected_query_pack": query_pack,
                "limit": limit,
            }
        )
        return templates.TemplateResponse(request, "library.html", context)

    @app.get("/queue")
    def queue(
        request: Request,
        limit: int = 30,
        query_pack: str | None = None,
        pending_only: bool = True,
        message: str | None = None,
    ):
        context = base_context(request, store, app_settings)
        context.update(
            {
                "message": message,
                "queue_items": list_ui_download_queue(
                    store,
                    app_settings,
                    limit=limit,
                    query_pack=query_pack,
                    pending_only=pending_only,
                ),
                "selected_query_pack": query_pack,
                "pending_only": pending_only,
                "limit": limit,
            }
        )
        return templates.TemplateResponse(request, "queue.html", context)

    @app.post("/ui/search")
    async def search_from_form(request: Request):
        form = await request.form()
        spec = build_query_spec_from_form(form)
        try:
            results = execute_search(service, store, app_settings, spec)
        except (ProviderError, ValueError) as exc:
            context = base_context(request, store, app_settings)
            context.update(
                {
                    "message": str(exc),
                    "results": [],
                    "query_name": spec.name,
                    "query_text": spec.query,
                    "export_paths": {},
                }
            )
            return templates.TemplateResponse(request, "results.html", context, status_code=400)

        result_rows = [record.export_row(record_citation(record)) for record in results]
        context = base_context(request, store, app_settings)
        context.update(
            {
                "message": f"检索完成，共获得 {len(results)} 篇记录。",
                "results": result_rows,
                "query_name": spec.name,
                "query_text": spec.query,
                "export_paths": export_path_map(app_settings.output_dir / spec.name),
            }
        )
        return templates.TemplateResponse(request, "results.html", context)

    @app.post("/ui/search-topic")
    async def search_topic_from_form(
        request: Request,
        topic: str = Form(...),
        limit: int = Form(20),
        from_date: str | None = Form(None),
        until_date: str | None = Form(None),
    ):
        providers = (await request.form()).getlist("providers")
        topics_map = load_topics()
        if topic not in topics_map:
            return redirect_with_message("/?message=", f"未知主题：{topic}")
        spec = QuerySpec.model_validate(
            {
                "name": topic,
                "query": topics_map[topic]["query"],
                "providers": providers or ["crossref", "openalex"],
                "limit": limit,
                "from_date": empty_to_none(from_date),
                "until_date": empty_to_none(until_date),
            }
        )
        try:
            results = execute_search(service, store, app_settings, spec)
        except (ProviderError, ValueError) as exc:
            return redirect_with_message("/", str(exc))

        context = base_context(request, store, app_settings)
        context.update(
            {
                "message": f"主题 {topic} 检索完成，共获得 {len(results)} 篇记录。",
                "results": [record.export_row(record_citation(record)) for record in results],
                "query_name": spec.name,
                "query_text": spec.query,
                "export_paths": export_path_map(app_settings.output_dir / spec.name),
            }
        )
        return templates.TemplateResponse(request, "results.html", context)

    @app.post("/ui/upload-pdf")
    async def upload_pdf(
        doi: str = Form(...),
        file: UploadFile = UPLOAD_FILE_FIELD,
    ):
        paper = store.get_paper_by_doi(doi)
        if paper is None:
            return redirect_with_message("/queue", f"库中未找到 DOI：{doi}")
        if not file.filename:
            return redirect_with_message("/queue", "未选择 PDF 文件。")

        target_path = build_upload_path(app_settings, paper, file.filename)
        app_settings.incoming_pdf_dir.mkdir(parents=True, exist_ok=True)
        with target_path.open("wb") as handle:
            copyfileobj(file.file, handle)

        attached = store.attach_pdf(doi=doi, file_path=target_path)
        if not attached:
            return redirect_with_message("/queue", f"PDF 绑定失败：{doi}")
        return redirect_with_message("/queue", f"已绑定 PDF：{doi}")

    return app


def execute_search(
    service: SearchService,
    store: IndexStore,
    app_settings: Settings,
    spec: QuerySpec,
):
    results = service.search(spec)
    export_records(results, app_settings.output_dir / spec.name)
    store.upsert_records(results)
    return results


def base_context(request: Request, store: IndexStore, app_settings: Settings) -> dict[str, object]:
    providers = provider_status(app_settings)
    return {
        "request": request,
        "summary": build_ui_summary(store),
        "cost_summary": build_cost_summary(store),
        "providers": providers,
        "search_providers": [item for item in providers if item["kind"] == "metadata"],
        "topics": load_topics(),
        "query_packs": list_ui_query_packs(store),
        "default_providers": ["crossref", "openalex"],
    }


def build_query_spec_from_form(form) -> QuerySpec:
    providers = form.getlist("providers") or ["crossref", "openalex"]
    name = (form.get("name") or "").strip()
    query = (form.get("query") or "").strip()
    if not query:
        raise ValueError("检索词不能为空。")
    return QuerySpec.model_validate(
        {
            "name": name or slugify(query),
            "query": query,
            "providers": providers,
            "limit": int(form.get("limit") or 20),
            "from_date": empty_to_none(form.get("from_date")),
            "until_date": empty_to_none(form.get("until_date")),
        }
    )


def redirect_with_message(path: str, message: str) -> RedirectResponse:
    response = RedirectResponse(
        url=f"{path}?message={quote_plus(message)}",
        status_code=303,
    )
    return response


def empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def build_upload_path(app_settings: Settings, paper: dict[str, str], original_name: str) -> Path:
    suffix = Path(original_name).suffix.lower()
    if suffix != ".pdf":
        suffix = ".pdf"
    suggested = suggest_pdf_filename(paper["title"], paper.get("doi"), paper.get("year"))
    target = app_settings.incoming_pdf_dir / Path(suggested).with_suffix(suffix)
    return unique_path(target)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = sanitize_filename(path.stem)
    suffix = path.suffix
    parent = path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def export_path_map(output_base: Path) -> dict[str, str]:
    return {
        "json": str(output_base.with_suffix(".json")),
        "csv": str(output_base.with_suffix(".csv")),
        "md": "",
    }


def slugify(value: str) -> str:
    return sanitize_filename(value.lower()).replace(" ", "-")


def enrich_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [enrich_paper_row(row) for row in rows]


def build_ui_summary(store: IndexStore) -> dict[str, object]:
    summary = dict(UI_UNKNOWN_SUMMARY)
    try:
        with sqlite3.connect(store.db_path, timeout=1) as conn:
            row = conn.execute(
                """
                SELECT updated_at
                FROM papers
                ORDER BY rowid DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error:
        return summary
    if row:
        summary["latest_update"] = row[0]
    return summary


def build_cost_summary(store: IndexStore) -> dict[str, object]:
    # Keep page rendering lightweight: artifact-level costs live in JSON files and
    # scanning them across the library can block the basic Web UI on large stores.
    _ = store
    return {
        "note_cost": None,
        "analysis_cost": None,
        "total_cost": None,
        "currency": "CNY",
    }


def list_ui_query_packs(store: IndexStore, limit: int = 100) -> list[str]:
    query_limit = clamp_ui_limit(limit, default=100, maximum=500)
    try:
        with sqlite3.connect(store.db_path, timeout=1) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT query_pack
                FROM (
                    SELECT query_pack
                    FROM papers
                    WHERE query_pack IS NOT NULL AND query_pack != ''
                    ORDER BY rowid DESC
                    LIMIT ?
                )
                ORDER BY query_pack
                """,
                (query_limit,),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [str(row[0]) for row in rows]


def list_ui_papers(
    store: IndexStore,
    *,
    limit: int = 20,
    query_pack: str | None = None,
) -> list[dict[str, object]]:
    row_limit = clamp_ui_limit(limit, default=20, maximum=500)
    sample_limit = ui_sample_limit(row_limit)
    sql = f"SELECT {UI_PAPER_COLUMNS} FROM papers ORDER BY rowid DESC LIMIT ?"
    try:
        with sqlite3.connect(store.db_path, timeout=1) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, (sample_limit,)).fetchall()
    except sqlite3.Error:
        return []
    papers: list[dict[str, object]] = []
    for row in rows:
        item = with_empty_artifact_metadata(store._normalize_row(dict(row)))
        if query_pack and item.get("query_pack") != query_pack:
            continue
        papers.append(item)
        if len(papers) >= row_limit:
            break
    return papers


def list_ui_download_queue(
    store: IndexStore,
    app_settings: Settings,
    *,
    limit: int = 20,
    query_pack: str | None = None,
    pending_only: bool = True,
) -> list[dict[str, object]]:
    row_limit = clamp_ui_limit(limit, default=20, maximum=500)
    sample_limit = ui_sample_limit(row_limit, multiplier=20, maximum=2000)
    sql = f"SELECT {UI_QUEUE_COLUMNS} FROM papers ORDER BY rowid DESC LIMIT ?"
    try:
        with sqlite3.connect(store.db_path, timeout=1) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, (sample_limit,)).fetchall()
    except sqlite3.Error:
        return []

    queue: list[dict[str, object]] = []
    for row in rows:
        item = store._normalize_row(dict(row))
        if query_pack and item.get("query_pack") != query_pack:
            continue
        if pending_only and item.get("local_pdf_path") and item.get("download_status") == "downloaded":
            continue
        suggested = suggest_pdf_filename(
            str(item.get("title") or "untitled"),
            str(item.get("doi") or "") or None,
            int(item["year"]) if item.get("year") else None,
        )
        target_path = app_settings.reference_dir / suggested
        item["suggested_filename"] = suggested
        item["target_pdf_path"] = str(target_path)
        queue.append(item)
        if len(queue) >= row_limit:
            break
    return queue


def with_empty_artifact_metadata(row: dict[str, object]) -> dict[str, object]:
    row["parsed_note_cost"] = None
    row["parsed_note_currency"] = None
    row["parsed_note_generation_mode"] = None
    row["parsed_note_prompt_tokens"] = None
    row["parsed_note_completion_tokens"] = None
    row["parsed_note_total_tokens"] = None
    row["analysis_cost"] = None
    row["analysis_currency"] = None
    row["analysis_prompt_tokens"] = None
    row["analysis_completion_tokens"] = None
    row["analysis_total_tokens"] = None
    row["total_ai_cost"] = None
    row["total_ai_cost_currency"] = None
    return row


def clamp_ui_limit(value: int | None, *, default: int, maximum: int) -> int:
    try:
        limit = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))


def ui_sample_limit(limit: int, *, multiplier: int = 5, maximum: int = 200) -> int:
    return max(limit, min(maximum, limit * multiplier))


def resolve_workspace_file(path: str, app_settings: Settings) -> Path:
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = (WORKSPACE_ROOT / file_path).resolve()
    else:
        file_path = file_path.resolve()
    allowed_roots = managed_file_roots(app_settings)
    if not any(root == file_path or root in file_path.parents for root in allowed_roots):
        raise HTTPException(status_code=400, detail="文件路径超出工作区范围。")
    return file_path


def managed_file_roots(app_settings: Settings) -> list[Path]:
    roots = [
        WORKSPACE_ROOT,
        app_settings.literature_root,
        app_settings.reference_dir,
        app_settings.md_dir,
        app_settings.metadata_dir,
        app_settings.output_dir,
        app_settings.parsed_output_dir,
        app_settings.analysis_output_dir,
        app_settings.reports_dir,
        app_settings.incoming_pdf_dir,
    ]
    resolved: list[Path] = []
    for root in roots:
        candidate = Path(root).resolve()
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def record_citation(record) -> str:
    from powerlit.citations import format_gbt_7714

    return format_gbt_7714(record)


app = create_app(settings)
