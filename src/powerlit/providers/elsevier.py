from __future__ import annotations

from datetime import date

from powerlit.models import Author, DocumentType, PaperRecord, QuerySpec
from powerlit.providers.base import BaseProvider, ProviderError
from powerlit.services.publisher_links import resolve_publisher_url


class ElsevierScopusProvider(BaseProvider):
    name = "elsevier"
    endpoint = "https://api.elsevier.com/content/search/scopus"

    def search(self, spec: QuerySpec) -> list[PaperRecord]:
        if not self.settings.elsevier_api_key:
            raise ProviderError("Elsevier provider requires POWERLIT_ELSEVIER_API_KEY.")

        query = f"TITLE-ABS-KEY({spec.query}) AND (DOCTYPE(ar) OR DOCTYPE(cp))"
        if spec.from_date:
            query += f" AND PUBYEAR > {spec.from_date.year - 1}"
        if spec.until_date:
            query += f" AND PUBYEAR < {spec.until_date.year + 1}"

        headers = {
            "Accept": "application/json",
            "X-ELS-APIKey": self.settings.elsevier_api_key,
        }
        if self.settings.elsevier_insttoken:
            headers["X-ELS-Insttoken"] = self.settings.elsevier_insttoken

        params = {
            "query": query,
            "count": min(spec.limit, 25),
            "sort": "-coverDate",
        }
        payload = self.get_json(self.endpoint, params=params, headers=headers)
        items = (payload.get("search-results") or {}).get("entry", [])
        return [self._parse_item(item, spec.name) for item in items]

    def _parse_item(self, item: dict, query_pack: str) -> PaperRecord:
        cover_date = parse_cover_date(item.get("prism:coverDate"))
        year = cover_date.year if cover_date else None
        subtype = (item.get("subtypeDescription") or "").lower()
        link = item.get("prism:url")

        return PaperRecord(
            title=item.get("dc:title") or "[题名缺失]",
            authors=[Author(literal=item.get("dc:creator"))] if item.get("dc:creator") else [],
            year=year,
            published_date=cover_date,
            document_type=map_elsevier_type(subtype),
            source_title=item.get("prism:publicationName"),
            publisher="Elsevier",
            volume=item.get("prism:volume"),
            issue=item.get("prism:issueIdentifier"),
            pages=item.get("prism:pageRange"),
            doi=item.get("prism:doi"),
            abstract=item.get("dc:description"),
            publisher_url=resolve_publisher_url(item.get("prism:doi"), link),
            query_pack=query_pack,
            source_providers=[self.name],
            raw=item,
        )


def map_elsevier_type(value: str) -> DocumentType:
    if "conference" in value:
        return DocumentType.CONFERENCE
    if "article" in value:
        return DocumentType.JOURNAL
    return DocumentType.UNKNOWN


def parse_cover_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
