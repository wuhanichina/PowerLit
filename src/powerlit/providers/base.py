from __future__ import annotations

from abc import ABC, abstractmethod
from email.utils import parsedate_to_datetime
import time
from typing import Any

import requests

from powerlit.models import JournalSpec, PaperRecord, QuerySpec
from powerlit.settings import Settings


class ProviderError(RuntimeError):
    """Raised when a provider request fails."""


TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_RETRY_ATTEMPTS = 3


class BaseProvider(ABC):
    name: str

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})

    @abstractmethod
    def search(self, spec: QuerySpec) -> list[PaperRecord]:
        raise NotImplementedError

    def search_journal(self, spec: JournalSpec) -> list[PaperRecord]:
        raise NotImplementedError(f"{self.name} does not implement journal sync")

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, DEFAULT_RETRY_ATTEMPTS + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.settings.request_timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= DEFAULT_RETRY_ATTEMPTS:
                    raise ProviderError(f"{self.name} request failed: {exc}") from exc
                time.sleep(retry_delay_seconds(attempt))
                continue

            if response.status_code < 400:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError(f"{self.name} returned a non-JSON response.") from exc

            message = (
                f"{self.name} request failed with HTTP {response.status_code}: "
                f"{response.text}"
            )
            if response.status_code not in TRANSIENT_HTTP_STATUS or attempt >= DEFAULT_RETRY_ATTEMPTS:
                raise ProviderError(message)
            last_error = ProviderError(message)
            time.sleep(retry_delay_seconds(attempt, response=response))

        raise ProviderError(f"{self.name} request failed: {last_error}")


def retry_delay_seconds(attempt: int, *, response: requests.Response | None = None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        parsed = parse_retry_after_seconds(retry_after)
        if parsed is not None:
            return parsed
    return float(min(30, 2 ** (attempt - 1)))


def parse_retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return min(float(stripped), 120.0)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    now = time.time()
    delay = retry_at.timestamp() - now
    return min(max(delay, 0.0), 120.0)
