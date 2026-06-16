from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from powerlit.services.ai_analysis import AIServiceError, OpenAICompatibleAIClient
from powerlit.settings import Settings

SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
KEYWORD_EXTRACTION_SYSTEM = (
    "You expand scientific literature search queries. "
    "Return only short, comma-separated search queries."
)
KEYWORD_EXTRACTION_PROMPT = """
Suggest up to {max_queries} short scientific literature search queries for finding papers that answer the question below.
Keep each query concise and domain-specific.
Return only comma-separated queries.

Question: {question}
Queries:
""".strip()


class OpenScholarRAGError(RuntimeError):
    """Raised when OpenScholar-compatible retrieval cannot be completed."""


@dataclass(slots=True)
class OpenScholarRetrievalArtifacts:
    question: str
    search_queries: list[str]
    ctxs: list[dict[str, Any]]
    json_path: Path
    jsonl_path: Path
    retrieval_payload: dict[str, Any]


class OpenScholarRAGService:
    """OpenScholar-compatible retrieval bridge for the local PowerLit workflow."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})
        self.ai_client = OpenAICompatibleAIClient(settings)

    def retrieve(
        self,
        question: str,
        *,
        name: str | None = None,
        max_queries: int = 3,
        papers_per_query: int = 10,
        use_pes2o: bool = True,
        pes2o_n_docs: int = 20,
    ) -> OpenScholarRetrievalArtifacts:
        normalized_question = " ".join(question.split())
        if not normalized_question:
            raise OpenScholarRAGError("Question cannot be empty.")

        if not self.settings.semantic_scholar_api_key and not (
            use_pes2o and self.settings.openscholar_pes2o_url
        ):
            raise OpenScholarRAGError(
                "Configure POWERLIT_SEMANTIC_SCHOLAR_API_KEY or POWERLIT_OPENSCHOLAR_PES2O_URL first."
            )

        search_queries = self.suggest_search_queries(normalized_question, max_queries=max_queries)
        raw_ctxs: list[dict[str, Any]] = []
        source_counts = {"semantic_scholar": 0, "pes2o": 0}

        if self.settings.semantic_scholar_api_key:
            for query in search_queries:
                query_ctxs = self._search_semantic_scholar(query, limit=papers_per_query)
                raw_ctxs.extend(query_ctxs)
                source_counts["semantic_scholar"] += len(query_ctxs)

        if use_pes2o and self.settings.openscholar_pes2o_url:
            pes2o_ctxs = self._retrieve_pes2o_passages(normalized_question, n_docs=pes2o_n_docs)
            raw_ctxs.extend(pes2o_ctxs)
            source_counts["pes2o"] += len(pes2o_ctxs)

        ctxs = dedupe_contexts(raw_ctxs)
        item = build_openscholar_input_item(
            normalized_question,
            ctxs=ctxs,
            search_queries=search_queries,
            retrieval_metadata={
                "generated_at": datetime.now(UTC).isoformat(),
                "source_counts": source_counts,
                "semantic_scholar_enabled": bool(self.settings.semantic_scholar_api_key),
                "pes2o_enabled": bool(use_pes2o and self.settings.openscholar_pes2o_url),
            },
        )
        payload = {"data": [item]}
        output_base = self.settings.rag_output_dir / slugify(name or normalized_question)
        artifacts = write_retrieval_artifacts(output_base, payload)
        return OpenScholarRetrievalArtifacts(
            question=normalized_question,
            search_queries=search_queries,
            ctxs=ctxs,
            json_path=artifacts["json_path"],
            jsonl_path=artifacts["jsonl_path"],
            retrieval_payload=payload,
        )

    def suggest_search_queries(self, question: str, *, max_queries: int = 3) -> list[str]:
        if not self.settings.ai_api_key:
            return [question]

        messages = [
            {"role": "system", "content": KEYWORD_EXTRACTION_SYSTEM},
            {
                "role": "user",
                "content": KEYWORD_EXTRACTION_PROMPT.format(
                    max_queries=max_queries,
                    question=question,
                ),
            },
        ]
        try:
            result = self.ai_client.chat_text(messages)
        except AIServiceError:
            return [question]
        queries = parse_search_queries(result.text, max_queries=max_queries)
        return queries or [question]

    def _search_semantic_scholar(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        response = self.session.get(
            SEMANTIC_SCHOLAR_SEARCH_URL,
            params={
                "query": query,
                "limit": limit,
                "minCitationCount": 10,
                "sort": "citationCount:desc",
                "fields": (
                    "title,year,abstract,authors.name,citationCount,url,paperId,"
                    "externalIds,openAccessPdf"
                ),
            },
            headers={"x-api-key": self.settings.semantic_scholar_api_key or ""},
            timeout=self.settings.request_timeout,
        )
        if response.status_code >= 400:
            raise OpenScholarRAGError(
                f"Semantic Scholar search failed for '{query}': "
                f"HTTP {response.status_code}: {response.text}"
            )
        payload = response.json()
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []

        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = normalize_text(item.get("title"))
            abstract = normalize_text(item.get("abstract"))
            text = abstract or title
            if not text:
                continue
            normalized.append(
                {
                    "title": title,
                    "text": text,
                    "abstract": abstract,
                    "url": normalize_text(
                        item.get("url") or (item.get("openAccessPdf") or {}).get("url")
                    ),
                    "citation_counts": coerce_int(item.get("citationCount")),
                    "type": "semantic_scholar_abstract" if abstract else "semantic_scholar_title_only",
                    "paper_id": normalize_text(item.get("paperId")),
                    "year": coerce_int(item.get("year")),
                    "authors": extract_author_names(item.get("authors")),
                    "external_ids": item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {},
                    "query": query,
                }
            )
        return normalized

    def _retrieve_pes2o_passages(self, query: str, *, n_docs: int) -> list[dict[str, Any]]:
        response = self.session.post(
            self.settings.openscholar_pes2o_url,
            json={"query": query, "n_docs": n_docs, "domains": "pes2o"},
            headers={"Content-Type": "application/json"},
            timeout=self.settings.request_timeout,
        )
        if response.status_code >= 400:
            raise OpenScholarRAGError(
                f"PES2O retrieval failed for '{query}': "
                f"HTTP {response.status_code}: {response.text}"
            )
        return normalize_pes2o_contexts(response.json())


def build_openscholar_input_item(
    question: str,
    *,
    ctxs: list[dict[str, Any]],
    search_queries: list[str],
    retrieval_metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "question": question,
        "query": question,
        "input": question,
        "ctxs": ctxs,
        "search_queries": search_queries,
        "retrieval": retrieval_metadata,
    }


def write_retrieval_artifacts(output_base: Path, payload: dict[str, Any]) -> dict[str, Path]:
    json_path = output_base.with_suffix(".json")
    jsonl_path = output_base.with_suffix(".jsonl")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = payload.get("data")
    if isinstance(lines, list):
        jsonl_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines),
            encoding="utf-8",
        )
    else:
        jsonl_path.write_text("", encoding="utf-8")
    return {"json_path": json_path, "jsonl_path": jsonl_path}


def parse_search_queries(text: str, *, max_queries: int) -> list[str]:
    raw_text = str(text or "").strip()
    if not raw_text:
        return []
    cleaned = re.sub(r"(?i)^search queries:\s*", "", raw_text)
    parts = re.split(r"[,;\n]+", cleaned)
    results: list[str] = []
    seen: set[str] = set()
    for part in parts:
        query = re.sub(r"^\d+[\).\s-]+", "", part).strip()
        if not query:
            continue
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        results.append(query)
        if len(results) >= max_queries:
            break
    return results


def normalize_pes2o_contexts(payload: Any) -> list[dict[str, Any]]:
    contexts = extract_context_items(payload)
    normalized: list[dict[str, Any]] = []
    for item in contexts:
        if not isinstance(item, dict):
            continue
        text = coerce_context_text(item.get("text") or item.get("retrieval text"))
        title = normalize_text(item.get("title"))
        if not text:
            continue
        normalized.append(
            {
                "title": title,
                "text": text,
                "abstract": normalize_text(item.get("abstract")),
                "url": normalize_text(item.get("url")),
                "citation_counts": coerce_int(
                    item.get("citation_counts") or item.get("citationCount")
                ),
                "type": normalize_text(item.get("type")) or "pes2o_passage",
            }
        )
    return normalized


def extract_context_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("ctxs", "results", "data", "passages", "documents"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def dedupe_contexts(ctxs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for ctx in ctxs:
        text = coerce_context_text(ctx.get("text"))
        title = normalize_text(ctx.get("title"))
        if not text:
            continue
        normalized_ctx = dict(ctx)
        normalized_ctx["text"] = text
        normalized_ctx["title"] = title
        normalized_ctx["abstract"] = normalize_text(ctx.get("abstract"))
        normalized_ctx["url"] = normalize_text(ctx.get("url"))
        normalized_ctx["citation_counts"] = coerce_int(ctx.get("citation_counts"))
        normalized_ctx["type"] = normalize_text(ctx.get("type")) or "unknown"
        key = f"{text[:100]}::{title}".casefold()
        deduped[key] = normalized_ctx
    return list(deduped.values())


def extract_author_names(payload: Any) -> list[str]:
    if not isinstance(payload, list):
        return []
    authors: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = normalize_text(item.get("name"))
        if name:
            authors.append(name)
    return authors


def coerce_context_text(value: Any) -> str:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, list):
        return normalize_text(" ".join(str(item) for item in value if item is not None))
    if isinstance(value, dict):
        contexts = value.get("contexts")
        if isinstance(contexts, list):
            return normalize_text(" ".join(str(item) for item in contexts if item is not None))
    return ""


def coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def slugify(value: str) -> str:
    normalized = re.sub(r"[^\w\-]+", "-", value.strip().lower())
    compacted = re.sub(r"-{2,}", "-", normalized).strip("-")
    return compacted or "openscholar-rag"
