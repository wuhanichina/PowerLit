from __future__ import annotations

from urllib.parse import quote, urlparse

import requests

from powerlit.models import (
    AcquisitionMethod,
    AcquisitionStage,
    FullTextCandidate,
    FullTextFormat,
    PaperRecord,
)
from powerlit.settings import Settings


class FullTextResolver:
    """Resolve actionable full-text candidates without downloading files."""

    unpaywall_endpoint = "https://api.unpaywall.org/v2/{doi}"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})

    def resolve_records(self, records: list[PaperRecord]) -> list[PaperRecord]:
        return [self.resolve_record(record) for record in records]

    def resolve_record(self, record: PaperRecord) -> PaperRecord:
        candidates = self.resolve_candidates(record)
        payload = record.model_dump()
        payload["raw"] = {
            **(record.raw or {}),
            "fulltext_candidates": self._serialize_candidates(candidates),
        }

        if candidates:
            best = candidates[0]
            payload["acquisition_method"] = best.method
            payload["acquisition_source_url"] = best.url
            payload["acquisition_stage"] = self._resolved_stage(record)

        return PaperRecord.model_validate(payload)

    def resolve_candidates(self, record: PaperRecord) -> list[FullTextCandidate]:
        openalex_candidates = self._openalex_candidates(record)
        if openalex_candidates:
            return self._rank_candidates(openalex_candidates)

        unpaywall_candidates = self._unpaywall_candidates(record)
        if unpaywall_candidates:
            return self._rank_candidates(unpaywall_candidates)

        publisher_candidate = self._publisher_candidate(record)
        if publisher_candidate:
            return [publisher_candidate]
        return []

    def _openalex_candidates(self, record: PaperRecord) -> list[FullTextCandidate]:
        raw = record.raw or {}
        candidates: list[FullTextCandidate] = []
        content_url = raw.get("content_url")
        has_content = raw.get("has_content") or {}
        best_location = raw.get("best_oa_location") or {}
        primary_location = raw.get("primary_location") or {}
        open_access = raw.get("open_access") or {}
        landing_page = (
            best_location.get("landing_page_url")
            or primary_location.get("landing_page_url")
            or record.publisher_url
        )

        if content_url and has_content.get("grobid_xml"):
            candidates.append(
                FullTextCandidate(
                    source="openalex",
                    method=AcquisitionMethod.OPENALEX_TEI,
                    format=FullTextFormat.TEI,
                    url=f"{content_url}.grobid-xml",
                    landing_page_url=landing_page,
                    evidence="OpenAlex content_url with grobid_xml availability",
                )
            )
        if content_url and has_content.get("pdf"):
            candidates.append(
                FullTextCandidate(
                    source="openalex",
                    method=AcquisitionMethod.OPENALEX_PDF,
                    format=FullTextFormat.PDF,
                    url=f"{content_url}.pdf",
                    landing_page_url=landing_page,
                    evidence="OpenAlex content_url with pdf availability",
                )
            )

        direct_pdf = best_location.get("pdf_url") or primary_location.get("pdf_url")
        if direct_pdf:
            candidates.append(
                FullTextCandidate(
                    source="openalex",
                    method=AcquisitionMethod.OPENALEX_PDF,
                    format=FullTextFormat.PDF,
                    url=direct_pdf,
                    landing_page_url=landing_page,
                    evidence="OpenAlex location pdf_url",
                )
            )

        oa_url = open_access.get("oa_url")
        if oa_url and self._looks_like_pdf(oa_url):
            candidates.append(
                FullTextCandidate(
                    source="openalex",
                    method=AcquisitionMethod.OPENALEX_PDF,
                    format=FullTextFormat.PDF,
                    url=oa_url,
                    landing_page_url=landing_page,
                    evidence="OpenAlex oa_url appears to be a PDF",
                )
            )

        return self._dedupe_candidates(candidates)

    def _unpaywall_candidates(self, record: PaperRecord) -> list[FullTextCandidate]:
        if not record.doi or not self.settings.unpaywall_contact_email:
            return []

        payload = self._get_unpaywall_payload(record.doi)
        if not payload:
            return []

        candidates: list[FullTextCandidate] = []
        seen_locations: list[dict] = []
        best_location = payload.get("best_oa_location")
        if best_location:
            seen_locations.append(best_location)
        seen_locations.extend(payload.get("oa_locations") or [])

        for location in seen_locations:
            landing_page = location.get("url")
            pdf_url = location.get("url_for_pdf")
            license_name = location.get("license")
            host_type = location.get("host_type")
            if pdf_url:
                candidates.append(
                    FullTextCandidate(
                        source="unpaywall",
                        method=AcquisitionMethod.UNPAYWALL_PDF,
                        format=FullTextFormat.PDF,
                        url=pdf_url,
                        landing_page_url=landing_page,
                        license=license_name,
                        host_type=host_type,
                        evidence="Unpaywall url_for_pdf",
                    )
                )
                continue
            if landing_page:
                candidates.append(
                    FullTextCandidate(
                        source="unpaywall",
                        method=AcquisitionMethod.PUBLISHER_DIRECT,
                        format=FullTextFormat.HTML,
                        url=landing_page,
                        landing_page_url=landing_page,
                        license=license_name,
                        host_type=host_type,
                        evidence="Unpaywall OA landing page without direct PDF",
                    )
                )

        return self._dedupe_candidates(candidates)

    def _publisher_candidate(self, record: PaperRecord) -> FullTextCandidate | None:
        publisher_url = record.publisher_url
        if not publisher_url:
            return None
        return FullTextCandidate(
            source="publisher",
            method=AcquisitionMethod.PUBLISHER_DIRECT,
            format=FullTextFormat.HTML,
            url=publisher_url,
            landing_page_url=publisher_url,
            evidence="Indexed publisher landing page",
        )

    def _get_unpaywall_payload(self, doi: str) -> dict | None:
        response = self.session.get(
            self.unpaywall_endpoint.format(doi=quote(doi, safe="")),
            params={"email": self.settings.unpaywall_contact_email},
            timeout=self.settings.request_timeout,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def _dedupe_candidates(self, candidates: list[FullTextCandidate]) -> list[FullTextCandidate]:
        deduped: dict[str, FullTextCandidate] = {}
        for candidate in candidates:
            existing = deduped.get(candidate.url)
            if existing is None or self._candidate_rank(candidate) < self._candidate_rank(existing):
                deduped[candidate.url] = candidate
        return list(deduped.values())

    def _rank_candidates(self, candidates: list[FullTextCandidate]) -> list[FullTextCandidate]:
        return sorted(candidates, key=self._candidate_rank)

    def _candidate_rank(self, candidate: FullTextCandidate) -> tuple[int, int, int, str]:
        method_rank = {
            AcquisitionMethod.OPENALEX_TEI: 0,
            AcquisitionMethod.OPENALEX_PDF: 1,
            AcquisitionMethod.UNPAYWALL_PDF: 2,
            AcquisitionMethod.PUBLISHER_DIRECT: 3,
            AcquisitionMethod.BROWSER_AGENT: 4,
            AcquisitionMethod.MANUAL: 5,
        }
        format_rank = {
            FullTextFormat.TEI: 0,
            FullTextFormat.XML: 1,
            FullTextFormat.PDF: 2,
            FullTextFormat.HTML: 3,
        }
        return (
            method_rank.get(candidate.method, 99),
            format_rank.get(candidate.format, 99),
            1 if candidate.requires_browser else 0,
            candidate.url,
        )

    def _resolved_stage(self, record: PaperRecord) -> AcquisitionStage:
        if record.acquisition_stage in {
            AcquisitionStage.DOWNLOADED,
            AcquisitionStage.PARSED,
            AcquisitionStage.MARKDOWN_BUILT,
            AcquisitionStage.ANALYZED,
        }:
            return record.acquisition_stage
        return AcquisitionStage.FULLTEXT_RESOLVED

    def _serialize_candidates(
        self,
        candidates: list[FullTextCandidate],
    ) -> list[dict[str, str | bool | None]]:
        return [candidate.model_dump(mode="json") for candidate in candidates]

    def _looks_like_pdf(self, url: str) -> bool:
        lowered = url.lower()
        parsed = urlparse(lowered)
        return ".pdf" in lowered or parsed.path.endswith("/pdf")
