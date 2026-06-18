from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from powerlit.settings import Settings

KNOWN_TASKS = ("note", "review", "analysis")


class AIProfileConfig(BaseModel):
    """A single named AI profile with all parameters needed to call an LLM."""

    name: str = ""
    provider: str = "siliconflow"
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str | None = None
    model: str = "Qwen/Qwen2.5-72B-Instruct"
    temperature: float = 0.1
    timeout: float | None = None
    note_timeout: float | None = 600.0
    source_char_limit: int = 16000
    note_source_char_limit: int = 90000
    note_chunk_char_limit: int = 6000

    @field_validator("provider", "base_url", "model")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @property
    def effective_timeout(self) -> float:
        return self.timeout or 30.0

    @property
    def effective_note_timeout(self) -> float:
        return self.note_timeout or max(self.effective_timeout, 180.0)


class AIConfig(BaseModel):
    """Top-level AI configuration holding named profiles and task assignments."""

    defaults: dict[str, object] = Field(default_factory=dict)
    profiles: dict[str, AIProfileConfig] = Field(default_factory=dict)
    default: str = ""
    tasks: dict[str, str] = Field(default_factory=dict)

    def resolve_profile(self, task: str) -> AIProfileConfig:
        profile_name = self.tasks.get(task, self.default)
        if profile_name and profile_name in self.profiles:
            return self.profiles[profile_name]
        if self.default and self.default in self.profiles:
            return self.profiles[self.default]
        if self.profiles:
            return next(iter(self.profiles.values()))
        return AIProfileConfig(name="builtin-default")


def load_ai_config(path: Path) -> AIConfig:
    """Load AI configuration from a YAML file."""
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults_payload = payload.get("defaults") or {}

    raw_profiles = payload.get("profiles") or {}
    profiles: dict[str, AIProfileConfig] = {}
    for profile_name, profile_data in raw_profiles.items():
        merged = {**defaults_payload, **profile_data, "name": profile_name}
        profiles[profile_name] = AIProfileConfig.model_validate(merged)

    return AIConfig(
        defaults=defaults_payload,
        profiles=profiles,
        default=payload.get("default") or "",
        tasks=payload.get("tasks") or {},
    )


def build_fallback_config(settings: Settings) -> AIConfig:
    """Build an AIConfig from legacy .env-based settings fields."""
    profile = AIProfileConfig(
        name="env-default",
        provider=settings.ai_provider,
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        temperature=settings.ai_temperature,
        timeout=settings.ai_timeout,
        note_timeout=settings.ai_note_timeout,
        source_char_limit=settings.ai_source_char_limit,
        note_source_char_limit=settings.ai_note_source_char_limit,
        note_chunk_char_limit=settings.ai_note_chunk_char_limit,
    )
    return AIConfig(
        profiles={"env-default": profile},
        default="env-default",
    )


@lru_cache(maxsize=1)
def _cached_ai_config(config_path: str, mtime_ns: int) -> AIConfig:
    """Cache parsed config keyed by path + mtime to avoid re-reading on every call."""
    return load_ai_config(Path(config_path))


def _get_ai_config(settings: Settings) -> AIConfig:
    config_path = settings.ai_config_path
    if config_path.exists():
        mtime_ns = config_path.stat().st_mtime_ns
        return _cached_ai_config(str(config_path), mtime_ns)
    return build_fallback_config(settings)


def resolve_ai_profile(settings: Settings, task: str = "default") -> AIProfileConfig:
    """Resolve the AI profile for a given task.

    If config/ai.yml exists, loads it and returns the profile assigned to *task*.
    Otherwise builds a fallback profile from the .env-based settings fields.

    When the resolved profile has no api_key, falls back to settings.ai_api_key.
    """
    config = _get_ai_config(settings)
    profile = config.resolve_profile(task)
    provider = profile.provider.strip().lower()
    if provider in {"aliyun", "aliyun-bailian", "dashscope"}:
        if not profile.api_key and settings.dashscope_api_key:
            profile = profile.model_copy(update={"api_key": settings.dashscope_api_key})
        return profile
    if provider == "openai":
        openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not profile.api_key and openai_api_key:
            profile = profile.model_copy(update={"api_key": openai_api_key})
        elif not profile.api_key and settings.ai_api_key:
            profile = profile.model_copy(update={"api_key": settings.ai_api_key})
        return profile
    if not profile.api_key and settings.ai_api_key:
        profile = profile.model_copy(update={"api_key": settings.ai_api_key})
    return profile
