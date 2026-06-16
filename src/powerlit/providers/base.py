from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import requests

from powerlit.models import JournalSpec, PaperRecord, QuerySpec
from powerlit.settings import Settings


class ProviderError(RuntimeError):
    """Raised when a provider request fails."""


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
        try:
            response = self.session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.settings.request_timeout,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"{self.name} request failed: {exc}") from exc
        if response.status_code >= 400:
            message = (
                f"{self.name} request failed with HTTP {response.status_code}: "
                f"{response.text}"
            )
            raise ProviderError(message)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.name} returned a non-JSON response.") from exc
