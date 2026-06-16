from __future__ import annotations

from powerlit.models import Author, DocumentType, PaperRecord, QuerySpec
from powerlit.providers.base import BaseProvider, ProviderError
from powerlit.services.publisher_links import resolve_publisher_url


class IEEEProvider(BaseProvider):
    name = "ieee"
    endpoint = "https://ieeexploreapi.ieee.org/api/v1/search/articles"

    def search(self, spec: QuerySpec) -> list[PaperRecord]:
        if not self.settings.ieee_api_key:
            raise ProviderError("IEEE provider requires POWERLIT_IEEE_API_KEY.")

        params = {
            "apikey": self.settings.ieee_api_key,
            "format": "json",
            "querytext": spec.query,
            "max_records": min(spec.limit, 200),
            "start_record": 1,
        }
        payload = self.get_json(self.endpoint, params=params)
        items = payload.get("articles", [])
        return [self._parse_item(item, spec.name) for item in items]

    def _parse_item(self, item: dict, query_pack: str) -> PaperRecord:
        authors_block = item.get("authors") or {}
        authors = [
            Author(
                given=extract_given_name(author.get("full_name")),
                family=extract_family_name(author.get("full_name")),
                literal=author.get("full_name"),
            )
            for author in authors_block.get("authors", [])
        ]
        pages = None
        if item.get("start_page") and item.get("end_page"):
            pages = f"{item['start_page']}-{item['end_page']}"
        elif item.get("start_page"):
            pages = str(item["start_page"])

        content_type = (item.get("content_type") or "").lower()
        return PaperRecord(
            title=item.get("article_title") or "[题名缺失]",
            authors=authors,
            year=coerce_int(item.get("publication_year")),
            document_type=map_ieee_type(content_type),
            source_title=item.get("publication_title"),
            publisher="IEEE",
            volume=item.get("volume"),
            issue=item.get("issue"),
            pages=pages,
            article_number=str(item.get("article_number")) if item.get("article_number") else None,
            doi=item.get("doi"),
            abstract=item.get("abstract"),
            publisher_url=resolve_publisher_url(
                item.get("doi"),
                item.get("html_url") or item.get("abstract_url"),
            ),
            query_pack=query_pack,
            source_providers=[self.name],
            raw=item,
        )


def map_ieee_type(value: str) -> DocumentType:
    if "conference" in value:
        return DocumentType.CONFERENCE
    if "journal" in value or "article" in value:
        return DocumentType.JOURNAL
    return DocumentType.UNKNOWN


def coerce_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    return int(value)


def extract_given_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    parts = full_name.split()
    if len(parts) <= 1:
        return None
    return " ".join(parts[:-1])


def extract_family_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    parts = full_name.split()
    if not parts:
        return None
    return parts[-1]
