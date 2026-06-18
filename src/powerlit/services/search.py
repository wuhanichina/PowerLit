from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from powerlit.models import JournalBundle, JournalSpec, PaperRecord, QueryBundle, QuerySpec
from powerlit.providers.base import BaseProvider
from powerlit.providers.crossref import CrossrefProvider
from powerlit.providers.elsevier import ElsevierScopusProvider
from powerlit.providers.ieee import IEEEProvider
from powerlit.providers.openalex import OpenAlexProvider
from powerlit.services.cas_whitelist import CASWhitelistService
from powerlit.services.researchgate import ResearchGateService
from powerlit.settings import Settings


class SearchService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.providers = build_provider_registry(settings)
        self.cas_whitelist = CASWhitelistService(settings)
        self.researchgate = ResearchGateService(settings)

    def search(self, spec: QuerySpec) -> list[PaperRecord]:
        records: list[PaperRecord] = []
        for provider_name in spec.providers:
            try:
                provider = self.providers[provider_name]
            except KeyError as exc:
                raise ValueError(f"Unknown provider: {provider_name}") from exc
            provider_records = provider.search(spec)
            records.extend(provider_records)
        deduped = dedupe_records(records)
        filtered = self.cas_whitelist.filter_records(deduped)
        return self.researchgate.annotate(filtered)

    def batch_search(self, bundle: QueryBundle) -> dict[str, list[PaperRecord]]:
        return {query.name: self.search(query) for query in bundle.queries}

    def search_journal(self, spec: JournalSpec) -> list[PaperRecord]:
        records: list[PaperRecord] = []
        for provider_name in spec.providers:
            try:
                provider = self.providers[provider_name]
            except KeyError as exc:
                raise ValueError(f"Unknown provider: {provider_name}") from exc
            provider_records = provider.search_journal(spec)
            records.extend(provider_records)
        deduped = dedupe_records(records)
        filtered = self.cas_whitelist.filter_records(deduped)
        enriched = self.researchgate.annotate(filtered)
        return sorted(enriched, key=sort_key)[: spec.limit]


def build_provider_registry(settings: Settings) -> dict[str, BaseProvider]:
    return {
        "crossref": CrossrefProvider(settings),
        "openalex": OpenAlexProvider(settings),
        "ieee": IEEEProvider(settings),
        "elsevier": ElsevierScopusProvider(settings),
    }


def load_query_bundle(path: Path) -> QueryBundle:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults_payload = payload.get("defaults", {})
    queries: list[QuerySpec] = []
    for item in payload.get("queries", []):
        merged = {**defaults_payload, **item}
        queries.append(QuerySpec.model_validate(merged))
    defaults = QuerySpec.model_validate({"name": "defaults", "query": "", **defaults_payload})
    return QueryBundle(defaults=defaults, queries=queries)


def load_journal_bundle(path: Path) -> JournalBundle:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    journals = [JournalSpec.model_validate(item) for item in payload.get("journals", [])]
    return JournalBundle(journals=journals)


def dedupe_records(records: Iterable[PaperRecord]) -> list[PaperRecord]:
    deduped: dict[str, PaperRecord] = {}
    for record in records:
        existing = deduped.get(record.dedupe_key)
        if existing is None:
            deduped[record.dedupe_key] = record
            continue
        deduped[record.dedupe_key] = merge_records(existing, record)
    return list(deduped.values())


def merge_records(left: PaperRecord, right: PaperRecord) -> PaperRecord:
    payload: dict[str, Any] = left.model_dump()
    for field_name, value in right.model_dump().items():
        if field_name == "source_providers":
            payload[field_name] = sorted(set((payload.get(field_name) or []) + (value or [])))
            continue
        if field_name == "raw":
            payload[field_name] = {**(payload.get("raw") or {}), **(value or {})}
            continue
        current = payload.get(field_name)
        if should_replace(field_name, current, value):
            payload[field_name] = value
    return PaperRecord.model_validate(payload)


def should_replace(field_name: str, current: Any, incoming: Any) -> bool:
    if incoming in (None, "", [], {}):
        return False
    if current in (None, "", [], {}):
        return True
    if field_name == "abstract":
        return len(str(incoming)) > len(str(current))
    if field_name == "authors":
        return len(incoming) > len(current)
    return False


def sort_key(record: PaperRecord) -> tuple[int, str]:
    year = record.year or 0
    return (-year, record.title.lower())
