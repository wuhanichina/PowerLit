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
    currency: str
    input_price_per_mtokens: float | None = None
    output_price_per_mtokens: float | None = None
    estimated_cost: float | None = None

    def to_dict(self) -> dict[str, int | float | str | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "currency": self.currency,
            "input_price_per_mtokens": self.input_price_per_mtokens,
            "output_price_per_mtokens": self.output_price_per_mtokens,
            "estimated_cost": self.estimated_cost,
        }


DEFAULT_PRICING: dict[tuple[str, str], tuple[float, float, str]] = {
    ("siliconflow", "Qwen/Qwen2.5-72B-Instruct"): (4.13, 4.13, "CNY"),
}


def resolve_pricing(
    settings: Settings,
    *,
    profile: AIProfileConfig | None = None,
) -> tuple[float | None, float | None, str]:
    if profile is not None:
        if (
            profile.input_price_per_mtokens is not None
            and profile.output_price_per_mtokens is not None
        ):
            return (
                profile.input_price_per_mtokens,
                profile.output_price_per_mtokens,
                profile.currency,
            )
        return DEFAULT_PRICING.get(
            (profile.provider.lower(), profile.model),
            (None, None, profile.currency),
        )

    if (
        settings.ai_input_price_per_mtokens is not None
        and settings.ai_output_price_per_mtokens is not None
    ):
        return (
            settings.ai_input_price_per_mtokens,
            settings.ai_output_price_per_mtokens,
            settings.ai_currency,
        )
    return DEFAULT_PRICING.get(
        (settings.ai_provider.lower(), settings.ai_model),
        (None, None, settings.ai_currency),
    )


def build_usage_metrics(
    settings: Settings,
    usage_payload: dict | None,
    *,
    profile: AIProfileConfig | None = None,
) -> AIUsageMetrics | None:
    if not isinstance(usage_payload, dict):
        return None
    prompt_tokens = int(
        usage_payload.get("prompt_tokens") or usage_payload.get("input_tokens") or 0
    )
    completion_tokens = int(
        usage_payload.get("completion_tokens") or usage_payload.get("output_tokens") or 0
    )
    total_tokens = int(usage_payload.get("total_tokens") or (prompt_tokens + completion_tokens))
    input_price, output_price, currency = resolve_pricing(settings, profile=profile)
    estimated_cost = None
    if input_price is not None and output_price is not None:
        estimated_cost = (
            prompt_tokens * input_price + completion_tokens * output_price
        ) / 1_000_000
    return AIUsageMetrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        currency=currency,
        input_price_per_mtokens=input_price,
        output_price_per_mtokens=output_price,
        estimated_cost=estimated_cost,
    )


def merge_usage_metrics(*items: AIUsageMetrics | None) -> AIUsageMetrics | None:
    """Sum token counts and costs from multiple API calls (e.g. per-page transcription)."""
    parts = [u for u in items if u is not None]
    if not parts:
        return None
    prompt_tokens = sum(p.prompt_tokens for p in parts)
    completion_tokens = sum(p.completion_tokens for p in parts)
    total_tokens = sum(p.total_tokens for p in parts)
    currency = parts[0].currency
    in_p = parts[0].input_price_per_mtokens
    out_p = parts[0].output_price_per_mtokens
    estimated: float | None = None
    if any(p.estimated_cost is not None for p in parts):
        estimated = sum((p.estimated_cost or 0.0) for p in parts)
    return AIUsageMetrics(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        currency=currency,
        input_price_per_mtokens=in_p,
        output_price_per_mtokens=out_p,
        estimated_cost=estimated,
    )
