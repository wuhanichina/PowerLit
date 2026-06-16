from __future__ import annotations

from urllib.parse import quote_plus, urlparse

from powerlit.models import PaperRecord
from powerlit.settings import Settings

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


class ResearchGateService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def annotate(self, records: list[PaperRecord]) -> list[PaperRecord]:
        for record in records:
            record.researchgate_lookup_url = build_lookup_url(record)
            if self.settings.serpapi_api_key and not record.researchgate_url:
                self._try_resolve_exact_url(record)
            if not record.researchgate_match_status:
                record.researchgate_match_status = "lookup_query"
        return records

    def _try_resolve_exact_url(self, record: PaperRecord) -> None:
        if requests is None:  # pragma: no cover
            return

        params = {
            "engine": "google",
            "api_key": self.settings.serpapi_api_key,
            "q": build_lookup_query(record),
            "num": 5,
        }
        response = requests.get("https://serpapi.com/search.json", params=params, timeout=30)
        if response.status_code >= 400:
            return

        for item in response.json().get("organic_results", []):
            link = item.get("link") or ""
            if not is_researchgate_publication_url(link):
                continue
            title = (item.get("title") or "").lower()
            snippet = (item.get("snippet") or "").lower()
            if record.doi and record.doi.lower() in snippet:
                record.researchgate_url = link
                record.researchgate_match_status = "exact_search_result"
                return
            if record.title.lower() in title or record.title.lower() in snippet:
                record.researchgate_url = link
                record.researchgate_match_status = "title_search_result"
                return


def build_lookup_query(record: PaperRecord) -> str:
    if record.doi:
        return f'site:researchgate.net/publication "{record.doi}"'
    return f'site:researchgate.net/publication "{record.title}"'


def build_lookup_url(record: PaperRecord) -> str:
    return f"https://www.google.com/search?q={quote_plus(build_lookup_query(record))}"


def is_researchgate_publication_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.endswith("researchgate.net")
        and "/publication/" in parsed.path
    )
