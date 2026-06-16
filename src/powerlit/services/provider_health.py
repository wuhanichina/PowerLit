from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import requests

from powerlit.models import QuerySpec
from powerlit.providers.base import ProviderError
from powerlit.services.search import build_provider_registry
from powerlit.services.status import provider_status
from powerlit.settings import Settings

DEFAULT_PROVIDER_HEALTH_QUERY = "power system stability"
DEFAULT_PROVIDER_NAMES = ("crossref", "openalex", "ieee", "elsevier")


def check_provider_connectivity(
    settings: Settings,
    *,
    provider_names: Sequence[str] = DEFAULT_PROVIDER_NAMES,
    query: str = DEFAULT_PROVIDER_HEALTH_QUERY,
    limit: int = 1,
) -> list[dict[str, str | int]]:
    status_lookup = {
        item["name"]: item
        for item in provider_status(settings)
        if item["kind"] == "metadata"
    }
    registry = build_provider_registry(settings)
    results: list[dict[str, str | int]] = []

    for name in provider_names:
        if name not in registry:
            raise ValueError(f"Unknown provider: {name}")

        base_status = status_lookup[name]
        result: dict[str, str | int] = {
            "name": name,
            "kind": str(base_status["kind"]),
            "config_status": str(base_status["status"]),
            "status": "pending",
            "detail": "",
            "result_count": 0,
        }

        if base_status["status"] != "ready":
            result["status"] = "needs_config"
            result["detail"] = str(base_status["detail"])
            results.append(result)
            continue

        provider = registry[name]
        spec = QuerySpec.model_validate(
            {
                "name": "provider-healthcheck",
                "query": query,
                "providers": [name],
                "limit": limit,
            }
        )

        try:
            records = provider.search(spec)
        except ProviderError as exc:
            result.update(classify_provider_error(name, str(exc)))
        except requests.RequestException as exc:
            result["status"] = "network_error"
            result["detail"] = f"{name} network error: {exc}"
        except Exception as exc:  # pragma: no cover - defensive branch
            result["status"] = "error"
            result["detail"] = f"{name} unexpected error: {exc}"
        else:
            result["status"] = "ok"
            result["result_count"] = len(records)
            result["detail"] = (
                f"{name} connectivity check passed with {len(records)} result(s)."
            )

        results.append(result)

    return results


def classify_provider_error(name: str, message: str) -> dict[str, str]:
    matched = re.search(r"HTTP (\d+)", message)
    if matched:
        status_code = int(matched.group(1))
        if status_code in {401, 403}:
            return {
                "status": "auth_error",
                "detail": f"{name} authentication failed: {message}",
            }
        return {
            "status": "http_error",
            "detail": f"{name} request failed: {message}",
        }
    return {
        "status": "provider_error",
        "detail": f"{name} provider error: {message}",
    }


def render_provider_check_line(item: dict[str, str | int]) -> str:
    return f"{item['name']}: {item['status']} ({item['detail']})"


def emit_provider_check_report(
    results: Sequence[dict[str, str | int]],
    printer: Callable[[str], None],
) -> None:
    for item in results:
        printer(render_provider_check_line(item))
