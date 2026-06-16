from __future__ import annotations

from datetime import date

from powerlit.models import Author, DocumentType, JournalSpec, PaperRecord, QuerySpec
from powerlit.providers.base import BaseProvider
from powerlit.services.library_layout import is_known_journal_doi, normalize_known_source_title
from powerlit.services.publisher_links import resolve_publisher_url


class CrossrefProvider(BaseProvider):
    name = "crossref"
    endpoint = "https://api.crossref.org/works"
    journal_endpoint = "https://api.crossref.org/journals/{issn}/works"

    def search(self, spec: QuerySpec) -> list[PaperRecord]:
        records: list[PaperRecord] = []
        seen: set[str] = set()
        for content_type in ("journal-article", "proceedings-article"):
            params = self._build_params(spec, content_type)
            payload = self.get_json(self.endpoint, params=params)
            items = payload.get("message", {}).get("items", [])
            for item in items:
                record = self._parse_item(item, spec.name)
                if record.document_type == DocumentType.UNKNOWN:
                    continue
                if record.dedupe_key in seen:
                    continue
                seen.add(record.dedupe_key)
                records.append(record)
                if len(records) >= spec.limit:
                    return records
        return records

    def _build_params(self, spec: QuerySpec, content_type: str) -> dict[str, str | int]:
        filters = [f"type:{content_type}"]
        if spec.from_date:
            filters.append(f"from-pub-date:{spec.from_date.isoformat()}")
        if spec.until_date:
            filters.append(f"until-pub-date:{spec.until_date.isoformat()}")

        params: dict[str, str | int] = {
            "query.bibliographic": spec.query,
            "rows": min(spec.limit, 100),
            "sort": "relevance",
            "filter": ",".join(filters),
            "select": ",".join(
                [
                    "DOI",
                    "URL",
                    "author",
                    "publisher",
                    "title",
                    "container-title",
                    "published",
                    "published-print",
                    "published-online",
                    "type",
                    "volume",
                    "issue",
                    "page",
                    "abstract",
                ]
            ),
        }
        if self.settings.crossref_mailto:
            params["mailto"] = self.settings.crossref_mailto
        return params

    def search_journal(self, spec: JournalSpec) -> list[PaperRecord]:
        if not spec.issns:
            raise ValueError(f"Crossref journal sync requires ISSN: {spec.short_name}")

        records: list[PaperRecord] = []
        seen: set[str] = set()
        remaining = spec.limit
        for issn in spec.issns:
            offset = 0
            page_size = min(100, remaining)
            while remaining > 0:
                params = self._build_journal_params(spec, rows=page_size, offset=offset)
                payload = self.get_json(self.journal_endpoint.format(issn=issn), params=params)
                items = payload.get("message", {}).get("items", [])
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
                offset += page_size
                page_size = min(100, remaining)
        return records

    def _build_journal_params(
        self,
        spec: JournalSpec,
        *,
        rows: int,
        offset: int,
    ) -> dict[str, str | int]:
        filters = ["type:journal-article"]
        if spec.from_year:
            filters.append(f"from-pub-date:{spec.from_year}-01-01")
        if spec.until_year:
            filters.append(f"until-pub-date:{spec.until_year}-12-31")
        params: dict[str, str | int] = {
            "rows": rows,
            "offset": offset,
            "sort": "published",
            "order": "desc",
            "filter": ",".join(filters),
            "select": ",".join(
                [
                    "DOI",
                    "URL",
                    "author",
                    "publisher",
                    "title",
                    "container-title",
                    "published",
                    "published-print",
                    "published-online",
                    "type",
                    "volume",
                    "issue",
                    "page",
                    "abstract",
                ]
            ),
        }
        if self.settings.crossref_mailto:
            params["mailto"] = self.settings.crossref_mailto
        return params

    def _parse_item(self, item: dict, query_pack: str) -> PaperRecord:
        title = " ".join(item.get("title") or ["[题名缺失]"])
        container = " ".join(item.get("container-title") or [])
        doi = item.get("DOI")
        published = (
            item.get("published-print")
            or item.get("published-online")
            or item.get("published")
            or {}
        )
        published_date = parse_crossref_date(published.get("date-parts"))
        year = published_date.year if published_date else None
        return PaperRecord(
            title=title,
            authors=[
                Author(
                    given=author.get("given"),
                    family=author.get("family"),
                    literal=author.get("name"),
                )
                for author in item.get("author", [])
            ],
            year=year,
            published_date=published_date,
            document_type=map_crossref_type(item.get("type"), doi=doi),
            source_title=normalize_known_source_title(container or None, doi=doi),
            publisher=item.get("publisher"),
            volume=item.get("volume"),
            issue=item.get("issue"),
            pages=item.get("page"),
            doi=doi,
            abstract=item.get("abstract"),
            publisher_url=resolve_publisher_url(doi, item.get("URL")),
            query_pack=query_pack,
            source_providers=[self.name],
            raw=item,
        )


def map_crossref_type(value: str | None, *, doi: str | None = None) -> DocumentType:
    if is_known_journal_doi(doi):
        return DocumentType.JOURNAL
    if value == "proceedings-article":
        return DocumentType.CONFERENCE
    if value == "journal-article":
        return DocumentType.JOURNAL
    return DocumentType.UNKNOWN


def parse_crossref_date(date_parts: list[list[int]] | None) -> date | None:
    if not date_parts:
        return None
    parts = date_parts[0]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    day = parts[2] if len(parts) > 2 else 1
    return date(year, month, day)


def normalize_source_title(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.lower().split())
