from __future__ import annotations

from datetime import date
from urllib.parse import quote

from powerlit.models import Author, DocumentType, JournalSpec, PaperRecord, QuerySpec
from powerlit.providers.base import BaseProvider
from powerlit.services.library_layout import is_known_journal_doi, normalize_known_source_title
from powerlit.services.publisher_links import resolve_publisher_url


class OpenAlexProvider(BaseProvider):
    name = "openalex"
    endpoint = "https://api.openalex.org/works"

    def search(self, spec: QuerySpec) -> list[PaperRecord]:
        filters = ["type:article|proceedings-article", "is_paratext:false"]
        if spec.from_date:
            filters.append(f"from_publication_date:{spec.from_date.isoformat()}")
        if spec.until_date:
            filters.append(f"to_publication_date:{spec.until_date.isoformat()}")

        params = {
            "search": spec.query,
            "per-page": min(spec.limit, 100),
            "filter": ",".join(filters),
            "sort": "relevance_score:desc",
        }
        if self.settings.crossref_mailto:
            params["mailto"] = self.settings.crossref_mailto

        payload = self.get_json(self.endpoint, params=params)
        items = payload.get("results", [])
        return [self._parse_item(item, spec.name) for item in items]

    def search_journal(self, spec: JournalSpec) -> list[PaperRecord]:
        if not spec.issns:
            raise ValueError(f"OpenAlex journal sync requires ISSN: {spec.short_name}")

        records: list[PaperRecord] = []
        seen: set[str] = set()
        remaining = spec.limit
        for issn in spec.issns:
            page = 1
            while remaining > 0:
                filters = [
                    "type:article",
                    "is_paratext:false",
                    f"locations.source.issn:{issn}",
                ]
                if spec.from_year:
                    filters.append(f"from_publication_date:{spec.from_year}-01-01")
                if spec.until_year:
                    filters.append(f"to_publication_date:{spec.until_year}-12-31")

                page_size = min(100, remaining)
                params = {
                    "per-page": page_size,
                    "page": page,
                    "filter": ",".join(filters),
                    "sort": "publication_date:desc",
                }
                if self.settings.crossref_mailto:
                    params["mailto"] = self.settings.crossref_mailto

                payload = self.get_json(self.endpoint, params=params)
                items = payload.get("results", [])
                if not items:
                    break
                added_this_page = 0
                for item in items:
                    record = self._parse_item(item, spec.short_name)
                    if record.document_type != DocumentType.JOURNAL:
                        continue
                    if normalize_source_title(record.source_title) != normalize_source_title(
                        spec.title
                    ):
                        continue
                    if record.dedupe_key in seen:
                        continue
                    seen.add(record.dedupe_key)
                    records.append(record)
                    remaining -= 1
                    added_this_page += 1
                    if remaining <= 0:
                        break
                if len(items) < page_size or added_this_page == 0:
                    break
                page += 1
        return records

    def lookup_by_doi(self, doi: str, query_pack: str) -> PaperRecord | None:
        normalized = normalize_doi(doi)
        if not normalized:
            return None
        endpoint = f"{self.endpoint}/{quote(f'doi:{normalized}', safe=':')}"
        params = {"mailto": self.settings.crossref_mailto} if self.settings.crossref_mailto else None
        item = self.get_json(endpoint, params=params)
        if not item:
            return None
        return self._parse_item(item, query_pack)

    def _parse_item(self, item: dict, query_pack: str) -> PaperRecord:
        biblio = item.get("biblio") or {}
        primary_location = item.get("primary_location") or {}
        source = (primary_location.get("source") or {}).get("display_name")
        landing_page = primary_location.get("landing_page_url")
        doi = item.get("doi") or (item.get("ids") or {}).get("doi")
        publication_date = parse_iso_date(item.get("publication_date"))
        year = item.get("publication_year") or (publication_date.year if publication_date else None)
        first_page = biblio.get("first_page")
        last_page = biblio.get("last_page")
        pages = None
        if first_page and last_page:
            pages = f"{first_page}-{last_page}"
        elif first_page:
            pages = str(first_page)

        return PaperRecord(
            title=item.get("display_name") or "[题名缺失]",
            authors=[
                Author(literal=(authorship.get("author") or {}).get("display_name"))
                for authorship in item.get("authorships", [])
                if (authorship.get("author") or {}).get("display_name")
            ],
            year=year,
            published_date=publication_date,
            document_type=map_openalex_type(item.get("type"), doi=doi),
            source_title=normalize_known_source_title(source, doi=doi),
            publisher=(primary_location.get("source") or {}).get("host_organization_name"),
            volume=str(biblio.get("volume")) if biblio.get("volume") else None,
            issue=str(biblio.get("issue")) if biblio.get("issue") else None,
            pages=pages,
            doi=doi,
            abstract=None,
            publisher_url=resolve_publisher_url(doi, landing_page),
            query_pack=query_pack,
            source_providers=[self.name],
            raw=item,
        )


def map_openalex_type(value: str | None, *, doi: str | None = None) -> DocumentType:
    if is_known_journal_doi(doi):
        return DocumentType.JOURNAL
    if value == "proceedings-article":
        return DocumentType.CONFERENCE
    if value == "article":
        return DocumentType.JOURNAL
    return DocumentType.UNKNOWN


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def normalize_source_title(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.lower().split())


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    normalized = normalized.removeprefix("doi:")
    normalized = normalized.strip()
    return normalized.lower() or None
