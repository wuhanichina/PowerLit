from __future__ import annotations

from powerlit.services.ai_config import KNOWN_TASKS, resolve_ai_profile
from powerlit.settings import Settings

TASK_LABELS = {
    "note": "转写",
    "review": "校对",
    "analysis": "分析",
}


def provider_status(settings: Settings) -> list[dict[str, str]]:
    items: list[dict[str, str]] = [
        {
            "name": "crossref",
            "kind": "metadata",
            "status": "ready",
            "detail": "Public metadata API.",
        },
        {
            "name": "openalex",
            "kind": "metadata",
            "status": "ready",
            "detail": "Public metadata API.",
        },
        {
            "name": "ieee",
            "kind": "metadata",
            "status": "ready" if settings.ieee_api_key else "needs_config",
            "detail": (
                "POWERLIT_IEEE_API_KEY detected."
                if settings.ieee_api_key
                else "Missing POWERLIT_IEEE_API_KEY."
            ),
        },
        {
            "name": "elsevier",
            "kind": "metadata",
            "status": "ready" if settings.elsevier_api_key else "needs_config",
            "detail": (
                "POWERLIT_ELSEVIER_API_KEY detected."
                if settings.elsevier_api_key
                else "Missing POWERLIT_ELSEVIER_API_KEY."
            ),
        },
        {
            "name": "researchgate_exact_link",
            "kind": "enrichment",
            "status": "ready" if settings.serpapi_api_key else "optional",
            "detail": (
                "POWERLIT_SERPAPI_API_KEY detected."
                if settings.serpapi_api_key
                else "Fallback lookup URLs only; exact links disabled."
            ),
        },
    ]

    for task in KNOWN_TASKS:
        profile = resolve_ai_profile(settings, task)
        label = TASK_LABELS.get(task, task)
        has_key = bool(profile.api_key)
        items.append(
            {
                "name": f"{profile.provider}({label})",
                "kind": "ai",
                "status": "ready" if has_key else "needs_config",
                "detail": (
                    f"[{profile.name}] {profile.provider} ready: {profile.model}"
                    if has_key
                    else f"[{profile.name}] Missing API key for {label} task."
                ),
            }
        )

    return items
