from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from powerlit.services.ai_config import AIProfileConfig

from powerlit.settings import Settings


@dataclass(slots=True)
class AIUsageMetrics:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def build_usage_metrics(
    settings: Settings,
    usage_payload: dict | None,
    *,
    profile: AIProfileConfig | None = None,
) -> AIUsageMetrics | None:
    _ = settings, profile
    if not isinstance(usage_payload, dict):
        return None
    prompt_tokens = int(
        usage_payload.get("prompt_tokens") or usage_payload.get("input_tokens") or 0
    )
    completion_tokens = int(
        usage_payload.get("completion_tokens") or usage_payload.get("output_tokens") or 0
    )
    total_tokens = int(usage_payload.get("total_tokens") or (prompt_tokens + completion_tokens))
    return AIUsageMetrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def merge_usage_metrics(*items: AIUsageMetrics | None) -> AIUsageMetrics | None:
    """Sum token counts from multiple API calls."""
    parts = [u for u in items if u is not None]
    if not parts:
        return None
    return AIUsageMetrics(
        prompt_tokens=sum(p.prompt_tokens for p in parts),
        completion_tokens=sum(p.completion_tokens for p in parts),
        total_tokens=sum(p.total_tokens for p in parts),
    )
