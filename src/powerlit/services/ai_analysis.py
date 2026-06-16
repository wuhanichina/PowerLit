from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BufferedReader
from pathlib import Path
from typing import Any

import requests

from powerlit.models import AnalysisResponse, PaperAnalysis, PaperRecord
from powerlit.services.ai_config import AIProfileConfig, resolve_ai_profile
from powerlit.services.ai_pricing import AIUsageMetrics, build_usage_metrics
from powerlit.services.library_layout import build_analysis_output_base
from powerlit.settings import Settings


class AIServiceError(RuntimeError):
    """Raised when an external AI provider request fails."""


@dataclass(slots=True)
class AIChatResult:
    text: str
    usage: AIUsageMetrics | None = None


@dataclass(slots=True)
class AIUploadedFile:
    file_id: str
    status: str
    filename: str | None = None
    bytes_count: int | None = None


@dataclass(slots=True)
class AIUploadProgress:
    bytes_sent: int
    total_bytes: int
    speed_bps: float


@dataclass(slots=True)
class AnalysisArtifacts:
    analysis: PaperAnalysis
    markdown_path: Path | None
    json_path: Path
    usage: AIUsageMetrics | None = None

    def to_response(self, doi: str | None) -> AnalysisResponse:
        return AnalysisResponse(
            doi=doi or "",
            markdown_path=(
                str(self.markdown_path.resolve()) if self.markdown_path is not None else None
            ),
            json_path=str(self.json_path.resolve()),
            analysis=self.analysis,
        )


class OpenAICompatibleAIClient:
    def __init__(self, settings: Settings, *, profile: AIProfileConfig | None = None):
        self.settings = settings
        self.profile = profile
        api_key = profile.api_key if profile else settings.ai_api_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
                "User-Agent": settings.user_agent,
            }
        )

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        result = self.chat_text(messages, timeout=timeout)
        return parse_json_object(result.text)

    def chat_text(
        self,
        messages: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> AIChatResult:
        p = self.profile
        api_key = p.api_key if p else self.settings.ai_api_key
        if not api_key:
            raise AIServiceError("未配置 AI API Key，无法调用外部 AI。")

        base_url = p.base_url if p else self.settings.ai_base_url
        model = p.model if p else self.settings.ai_model
        temperature = p.temperature if p else self.settings.ai_temperature
        provider = p.provider if p else self.settings.ai_provider
        default_timeout = p.effective_timeout if p else self.settings.effective_ai_timeout

        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model,
            "temperature": temperature,
            "messages": messages,
        }
        try:
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=timeout or default_timeout,
            )
        except requests.RequestException as exc:
            raise AIServiceError(f"{provider} AI 请求失败：{exc}") from exc

        if response.status_code >= 400:
            raise AIServiceError(
                f"{provider} AI 请求失败，"
                f"HTTP {response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIServiceError("AI 返回内容中不包含可解析的 message.content。") from exc

        if not isinstance(content, str):
            raise AIServiceError("AI 返回内容不是字符串，无法继续处理。")

        usage = build_usage_metrics(self.settings, payload.get("usage"), profile=self.profile)
        return AIChatResult(text=content, usage=usage)

    def responses_text(
        self,
        input_items: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> AIChatResult:
        p = self.profile
        api_key = p.api_key if p else self.settings.ai_api_key
        if not api_key:
            raise AIServiceError("Missing AI API key for responses request.")

        base_url = p.base_url if p else self.settings.ai_base_url
        model = p.model if p else self.settings.ai_model
        provider = p.provider if p else self.settings.ai_provider
        default_timeout = p.effective_timeout if p else self.settings.effective_ai_timeout

        endpoint = build_responses_endpoint(base_url, provider=provider)
        payload = {
            "model": model,
            "input": input_items,
        }
        try:
            response = self.session.post(
                endpoint,
                json=payload,
                timeout=timeout or default_timeout,
            )
        except requests.RequestException as exc:
            raise AIServiceError(f"{provider} AI responses request failed: {exc}") from exc

        if response.status_code >= 400:
            raise AIServiceError(
                f"{provider} AI responses request failed: "
                f"HTTP {response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AIServiceError("AI responses payload is not valid JSON.") from exc

        content = parse_responses_output_text(payload)
        if not content:
            raise AIServiceError("AI responses payload does not contain readable output text.")

        usage = build_usage_metrics(self.settings, payload.get("usage"), profile=self.profile)
        return AIChatResult(text=content, usage=usage)

    def upload_file(
        self,
        file_path: Path,
        *,
        purpose: str = "file-extract",
        timeout: float | None = None,
        progress_callback: Callable[[AIUploadProgress], None] | None = None,
    ) -> AIUploadedFile:
        p = self.profile
        api_key = p.api_key if p else self.settings.ai_api_key
        if not api_key:
            raise AIServiceError("Missing AI API key for file upload.")

        base_url = p.base_url if p else self.settings.ai_base_url
        provider = p.provider if p else self.settings.ai_provider
        default_timeout = p.effective_timeout if p else self.settings.effective_ai_timeout
        endpoint = f"{base_url.rstrip('/')}/files"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": self.settings.user_agent,
        }
        total_bytes = file_path.stat().st_size
        with file_path.open("rb") as handle:
            upload_handle: BufferedReader | _UploadProgressReader = handle
            if progress_callback is not None:
                upload_handle = _UploadProgressReader(
                    handle,
                    total_bytes=total_bytes,
                    progress_callback=progress_callback,
                )
            files = {"file": (file_path.name, upload_handle, "application/pdf")}
            data = {"purpose": purpose}
            try:
                response = requests.post(
                    endpoint,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=timeout or default_timeout,
                )
            except requests.RequestException as exc:
                raise AIServiceError(f"{provider} file upload failed: {exc}") from exc

        if response.status_code >= 400:
            raise AIServiceError(
                f"{provider} file upload failed: HTTP {response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
            file_id = str(payload["id"])
            status = str(payload.get("status") or "unknown")
        except (ValueError, KeyError, TypeError) as exc:
            raise AIServiceError("File upload response is missing id/status.") from exc

        bytes_count = payload.get("bytes")
        filename = payload.get("filename")
        return AIUploadedFile(
            file_id=file_id,
            status=status,
            filename=str(filename) if filename else None,
            bytes_count=int(bytes_count) if isinstance(bytes_count, int) else None,
        )

    def get_file(
        self,
        file_id: str,
        *,
        timeout: float | None = None,
    ) -> AIUploadedFile:
        p = self.profile
        base_url = p.base_url if p else self.settings.ai_base_url
        provider = p.provider if p else self.settings.ai_provider
        default_timeout = p.effective_timeout if p else self.settings.effective_ai_timeout
        endpoint = f"{base_url.rstrip('/')}/files/{file_id}"
        try:
            response = self.session.get(endpoint, timeout=timeout or default_timeout)
        except requests.RequestException as exc:
            raise AIServiceError(f"{provider} file lookup failed: {exc}") from exc

        if response.status_code >= 400:
            raise AIServiceError(
                f"{provider} file lookup failed: HTTP {response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
            status = str(payload.get("status") or "unknown")
        except (ValueError, TypeError) as exc:
            raise AIServiceError("File lookup response is not valid JSON.") from exc

        bytes_count = payload.get("bytes")
        filename = payload.get("filename")
        return AIUploadedFile(
            file_id=file_id,
            status=status,
            filename=str(filename) if filename else None,
            bytes_count=int(bytes_count) if isinstance(bytes_count, int) else None,
        )

    def wait_for_file_processed(
        self,
        file_id: str,
        *,
        timeout: float | None = None,
        poll_interval: float | None = None,
        status_callback: Callable[[AIUploadedFile], None] | None = None,
    ) -> AIUploadedFile:
        effective_timeout = timeout or self.settings.ai_file_processing_timeout
        effective_interval = max(
            poll_interval or self.settings.ai_file_processing_poll_interval,
            0.2,
        )
        started_at = time.monotonic()
        last_state: AIUploadedFile | None = None

        while True:
            last_state = self.get_file(file_id)
            if status_callback is not None:
                status_callback(last_state)
            status = last_state.status.lower()
            if status in {"processed", "succeeded"}:
                return last_state
            if status in {"failed", "error", "cancelled"}:
                raise AIServiceError(f"Uploaded PDF file processing failed with status={status}.")
            if time.monotonic() - started_at >= effective_timeout:
                raise AIServiceError(
                    "Uploaded PDF file was not ready before timeout "
                    f"({effective_timeout:.1f}s); last status={status}."
                )
            time.sleep(effective_interval)

    def delete_file(
        self,
        file_id: str,
        *,
        timeout: float | None = None,
    ) -> None:
        p = self.profile
        base_url = p.base_url if p else self.settings.ai_base_url
        provider = p.provider if p else self.settings.ai_provider
        default_timeout = p.effective_timeout if p else self.settings.effective_ai_timeout
        endpoint = f"{base_url.rstrip('/')}/files/{file_id}"
        try:
            response = self.session.delete(endpoint, timeout=timeout or default_timeout)
        except requests.RequestException as exc:
            raise AIServiceError(f"{provider} file deletion failed: {exc}") from exc

        if response.status_code >= 400:
            raise AIServiceError(
                f"{provider} file deletion failed: HTTP {response.status_code}: {response.text}"
            )


class _UploadProgressReader:
    def __init__(
        self,
        handle: BufferedReader,
        *,
        total_bytes: int,
        progress_callback: Callable[[AIUploadProgress], None],
    ):
        self._handle = handle
        self._total_bytes = max(total_bytes, 0)
        self._progress_callback = progress_callback
        self._started_at = time.monotonic()
        self._bytes_sent = 0
        self._emit_progress()

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if chunk:
            self._bytes_sent += len(chunk)
        self._emit_progress()
        return chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def _emit_progress(self) -> None:
        elapsed = max(time.monotonic() - self._started_at, 1e-6)
        self._progress_callback(
            AIUploadProgress(
                bytes_sent=self._bytes_sent,
                total_bytes=self._total_bytes,
                speed_bps=self._bytes_sent / elapsed,
            )
        )


def parse_responses_output_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if not isinstance(output, list):
        return ""

    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
                continue
            if isinstance(part.get("content"), str) and part["content"].strip():
                parts.append(part["content"])
    return "\n".join(parts).strip()


def build_responses_endpoint(base_url: str, *, provider: str) -> str:
    normalized_base = base_url.rstrip("/")
    if provider.strip().lower() == "volcengine" and normalized_base.endswith("/api/coding/v3"):
        normalized_base = normalized_base[: -len("/api/coding/v3")] + "/api/v3"
    return f"{normalized_base}/responses"


class AnalysisService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile = resolve_ai_profile(settings, "analysis")
        self.client = OpenAICompatibleAIClient(settings, profile=self.profile)

    def analyze_record(
        self,
        record: PaperRecord,
        *,
        source_text: str | None = None,
    ) -> AnalysisArtifacts:
        resolved_source_text, source_basis = resolve_analysis_source(
            record,
            source_text=source_text,
        )
        chat_result = self.client.chat_text(
            build_analysis_messages(record, resolved_source_text, source_basis, self.profile)
        )
        payload = parse_json_object(chat_result.text)
        analysis = normalize_analysis_payload(payload, record, source_basis)
        return self.write_artifacts(
            record,
            analysis,
            source_text=resolved_source_text,
            usage=chat_result.usage,
        )

    def write_artifacts(
        self,
        record: PaperRecord,
        analysis: PaperAnalysis,
        *,
        source_text: str | None = None,
        usage: AIUsageMetrics | None = None,
    ) -> AnalysisArtifacts:
        output_base = build_analysis_output_base(self.settings.analysis_output_dir, record)
        output_base.parent.mkdir(parents=True, exist_ok=True)
        json_path = output_base.with_suffix(".json")

        p = self.profile
        json_payload: dict[str, Any] = {
            "doi": record.doi,
            "title": record.title,
            "provider": p.provider,
            "model": p.model,
            "profile": p.name,
            "generated_at": datetime.now(UTC).isoformat(),
            "analysis": analysis.model_dump(mode="json"),
        }
        if source_text:
            json_payload["source_text_excerpt"] = truncate_source_text(
                source_text,
                p.source_char_limit,
            )
        if usage:
            json_payload["usage"] = usage.to_dict()

        json_path.write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return AnalysisArtifacts(
            analysis=analysis,
            markdown_path=None,
            json_path=json_path,
            usage=usage,
        )


def build_analysis_messages(
    record: PaperRecord,
    source_text: str | None,
    source_basis: str,
    profile: AIProfileConfig,
) -> list[dict[str, str]]:
    metadata = {
        "title": record.title,
        "authors": [author.full_name for author in record.authors],
        "year": record.year,
        "document_type": record.document_type.value,
        "source_title": record.source_title,
        "publisher": record.publisher,
        "doi": record.doi,
        "abstract": record.abstract or "unknown",
        "publisher_url": record.publisher_url,
    }
    source_excerpt = truncate_source_text(source_text, profile.source_char_limit)
    user_payload = {
        "metadata": metadata,
        "source_text_excerpt": source_excerpt or "unknown",
        "task": {
            "language": "zh-CN",
            "strict_grounding": True,
            "fallback_token": "unknown",
        },
        "source_basis": source_basis,
        "required_keys": [
            "title",
            "source_basis",
            "research_problem",
            "power_system_context",
            "methods",
            "data_and_case_studies",
            "key_findings",
            "limitations",
            "relevance",
            "keywords",
            "evidence_items",
            "caution",
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "你是电力系统文献分析助手。"
                "你只能基于用户提供的元数据和文本抽取信息，不得补充外部事实。"
                "信息不足一律写 unknown。"
                "输出必须是单个 JSON 对象，不要使用 Markdown 代码块。"
                "methods、data_and_case_studies、key_findings、limitations、keywords "
                "必须是字符串数组；evidence_items 必须是对象数组，包含 claim 和 support。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
        },
    ]


def normalize_analysis_payload(
    payload: dict[str, Any],
    record: PaperRecord,
    source_basis: str,
) -> PaperAnalysis:
    reported_source_basis = payload.get("source_basis")
    if reported_source_basis in (None, "", "unknown"):
        reported_source_basis = source_basis
    normalized = {
        "title": payload.get("title") or record.title,
        "source_basis": reported_source_basis,
        "research_problem": payload.get("research_problem") or "unknown",
        "power_system_context": payload.get("power_system_context") or "unknown",
        "methods": normalize_string_list(payload.get("methods")),
        "data_and_case_studies": normalize_string_list(payload.get("data_and_case_studies")),
        "key_findings": normalize_string_list(payload.get("key_findings")),
        "limitations": normalize_string_list(payload.get("limitations")),
        "relevance": payload.get("relevance") or "unknown",
        "keywords": normalize_string_list(payload.get("keywords")),
        "evidence_items": normalize_evidence_items(payload.get("evidence_items")),
        "caution": payload.get("caution") or "unknown",
    }
    return PaperAnalysis.model_validate(normalized)


def normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        normalized = [str(item).strip() for item in value if str(item).strip()]
        return normalized or ["unknown"]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return ["unknown"]


def normalize_evidence_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return [{"claim": "unknown", "support": "unknown"}]
    normalized: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            claim = str(item.get("claim") or "").strip() or "unknown"
            support = str(item.get("support") or "").strip() or "unknown"
            normalized.append({"claim": claim, "support": support})
    return normalized or [{"claim": "unknown", "support": "unknown"}]


def resolve_analysis_source(
    record: PaperRecord,
    *,
    source_text: str | None = None,
) -> tuple[str | None, str]:
    if source_text:
        return source_text, "user_source_file"

    if record.parsed_json_path:
        payload = load_parsed_artifact_payload(Path(record.parsed_json_path))
        parsed_content = extract_parsed_source_text(payload)
        if parsed_content:
            return parsed_content, "parsed_json"

    if record.parsed_md_path:
        path = Path(record.parsed_md_path)
        if path.exists():
            return path.read_text(encoding="utf-8"), "parsed_markdown"

    return None, "metadata+abstract"


def load_parsed_artifact_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def extract_parsed_source_text(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    text_fields = (
        payload.get("content"),
        payload.get("normalized_text"),
        payload.get("source_text"),
    )
    for value in text_fields:
        if isinstance(value, str) and value.strip():
            return value
    return None


def truncate_source_text(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "\n...[truncated]"


def strip_code_fence(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def parse_json_object(content: str) -> dict[str, Any]:
    stripped = strip_code_fence(content)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise AIServiceError("AI 返回内容中没有找到 JSON 对象。")
    candidate = stripped[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AIServiceError(f"AI 返回 JSON 解析失败：{exc}") from exc
    if not isinstance(parsed, dict):
        raise AIServiceError("AI 返回的 JSON 根对象不是对象。")
    return parsed


def render_analysis_markdown(
    record: PaperRecord,
    analysis: PaperAnalysis,
    usage: AIUsageMetrics | None = None,
) -> str:
    lines = [
        f"# {record.title}",
        "",
        "## 元数据",
        "",
        f"- DOI: {record.doi or 'unknown'}",
        f"- 年份: {record.year or 'unknown'}",
        f"- 类型: {record.document_type.value}",
        f"- 来源: {record.source_title or 'unknown'}",
        f"- 分析依据: {analysis.source_basis}",
    ]
    if usage:
        cost_text = "unknown"
        if usage.estimated_cost is not None:
            cost_text = f"{usage.estimated_cost:.6f} {usage.currency}"
        lines.extend(
            [
                f"- Prompt Tokens: {usage.prompt_tokens}",
                f"- Completion Tokens: {usage.completion_tokens}",
                f"- 总 Tokens: {usage.total_tokens}",
                f"- 估算费用: {cost_text}",
            ]
        )
    lines.extend(
        [
            "",
            "## 研究问题",
            "",
            analysis.research_problem,
            "",
            "## 电力系统场景",
            "",
            analysis.power_system_context,
            "",
            "## 方法",
            "",
        ]
    )
    lines.extend(render_bullet_list(analysis.methods))
    lines.extend(["", "## 数据与算例", ""])
    lines.extend(render_bullet_list(analysis.data_and_case_studies))
    lines.extend(["", "## 关键发现", ""])
    lines.extend(render_bullet_list(analysis.key_findings))
    lines.extend(["", "## 局限性", ""])
    lines.extend(render_bullet_list(analysis.limitations))
    lines.extend(["", "## 相关性", "", analysis.relevance, "", "## 关键词", ""])
    lines.extend(render_bullet_list(analysis.keywords))
    lines.extend(["", "## 证据摘录", ""])
    for item in analysis.evidence_items:
        lines.append(f"- 结论: {item.claim}")
        lines.append(f"  支撑: {item.support}")
    lines.extend(["", "## 注意事项", "", analysis.caution, ""])
    return "\n".join(lines).rstrip() + "\n"


def render_bullet_list(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]
