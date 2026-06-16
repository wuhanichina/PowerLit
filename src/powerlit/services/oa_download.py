from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from pathlib import Path
from re import finditer, IGNORECASE
from urllib.parse import urljoin, urlparse

import requests

from powerlit.models import AcquisitionMethod, FullTextFormat, PaperRecord
from powerlit.services.library_layout import (
    build_reference_pdf_path,
    doi_to_suffix,
    sanitize_filename,
)
from powerlit.settings import Settings


class OADownloadError(RuntimeError):
    """Raised when an OA PDF download fails."""


@dataclass(slots=True)
class OADownloadResult:
    doi: str
    path: Path
    source_url: str
    downloaded: bool


class OADownloadService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})

    def download_record(self, record: PaperRecord) -> OADownloadResult | None:
        candidate = select_pdf_candidate(record)
        if candidate is None or not record.doi:
            return None

        target_path = build_reference_pdf_path(
            self.settings.reference_dir,
            title=record.title,
            doi=record.doi,
            source_title=record.source_title,
            year=record.year,
            volume=record.volume,
            issue=record.issue,
            original_filename=filename_from_url(candidate["url"]),
        )
        return self.download_record_to_path(record, target_path=target_path)

    def download_record_to_directory(
        self,
        record: PaperRecord,
        target_dir: Path,
    ) -> OADownloadResult | None:
        if not record.doi:
            return None
        candidate = select_pdf_candidate(record)
        original_filename = filename_from_url(candidate["url"]) if candidate else None
        target_path = build_incoming_pdf_path(
            target_dir,
            record=record,
            original_filename=original_filename,
        )
        return self.download_record_to_path(record, target_path=target_path)

    def download_record_to_path(
        self,
        record: PaperRecord,
        *,
        target_path: Path,
    ) -> OADownloadResult | None:
        candidate = select_pdf_candidate(record)
        if record.doi is None:
            return None
        if candidate is not None:
            return self.download_pdf_url(record.doi, candidate["url"], target_path)
        for landing_url in select_landing_page_candidates(record):
            result = self.download_pdf_from_landing_page(
                doi=record.doi,
                landing_url=landing_url,
                target_path=target_path,
            )
            if result is not None:
                return result
        return None

    def download_pdf_url(
        self,
        doi: str,
        pdf_url: str,
        target_path: Path,
    ) -> OADownloadResult:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists() and target_path.stat().st_size > 0:
            return OADownloadResult(
                doi=doi,
                path=target_path,
                source_url=pdf_url,
                downloaded=False,
            )

        response = self.session.get(pdf_url, timeout=self.settings.request_timeout)
        if response.status_code >= 400:
            raise OADownloadError(f"OA download failed for {doi}: HTTP {response.status_code}")
        content_type = response.headers.get("Content-Type", "").lower()
        if "pdf" not in content_type:
            nested_candidates = extract_pdf_urls_from_html(response.text, base_url=response.url)
            for nested_url in nested_candidates:
                if nested_url == pdf_url:
                    continue
                try:
                    return self.download_pdf_url(doi, nested_url, target_path)
                except OADownloadError:
                    continue
            raise OADownloadError(f"OA candidate is not a PDF for {doi}: {pdf_url}")
        target_path.write_bytes(response.content)
        return OADownloadResult(
            doi=doi,
            path=target_path,
            source_url=pdf_url,
            downloaded=True,
        )

    def download_pdf_from_landing_page(
        self,
        *,
        doi: str,
        landing_url: str,
        target_path: Path,
    ) -> OADownloadResult | None:
        response = self.session.get(
            landing_url,
            timeout=self.settings.request_timeout,
            headers={"Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8"},
        )
        if response.status_code >= 400:
            raise OADownloadError(
                f"OA landing page lookup failed for {doi}: HTTP {response.status_code}"
            )
        content_type = response.headers.get("Content-Type", "").lower()
        if "pdf" in content_type:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(response.content)
            return OADownloadResult(
                doi=doi,
                path=target_path,
                source_url=response.url,
                downloaded=True,
            )
        candidate_urls = extract_pdf_urls_from_html(response.text, base_url=response.url)
        for candidate_url in candidate_urls:
            try:
                return self.download_pdf_url(doi, candidate_url, target_path)
            except OADownloadError:
                continue
        return None


def select_pdf_candidate(record: PaperRecord) -> dict[str, str] | None:
    raw_candidates = (record.raw or {}).get("fulltext_candidates") or []
    pdf_candidates: list[dict[str, str]] = []
    for candidate in raw_candidates:
        method = candidate.get("method")
        fmt = candidate.get("format")
        url = candidate.get("url")
        if method not in {
            AcquisitionMethod.OPENALEX_PDF.value,
            AcquisitionMethod.UNPAYWALL_PDF.value,
        }:
            continue
        if fmt != FullTextFormat.PDF.value or not url:
            continue
        pdf_candidates.append(candidate)
    if pdf_candidates:
        return min(pdf_candidates, key=pdf_candidate_rank)

    if (
        record.acquisition_method
        in {
            AcquisitionMethod.OPENALEX_PDF,
            AcquisitionMethod.UNPAYWALL_PDF,
        }
        and record.acquisition_source_url
    ):
        return {"url": record.acquisition_source_url}
    return None


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = Path(parsed.path).name
    return name or "paper.pdf"


def build_incoming_pdf_path(
    target_dir: Path,
    *,
    record: PaperRecord,
    original_filename: str | None = None,
) -> Path:
    suffix = doi_to_suffix(record.doi)
    stem = sanitize_filename(record.title or "paper")
    if suffix not in stem.lower():
        stem = f"{stem}__{suffix}"
    return target_dir / f"{stem}.pdf"


def pdf_candidate_rank(candidate: dict[str, str]) -> tuple[int, str]:
    method = candidate.get("method") or ""
    url = candidate.get("url") or ""
    is_openalex_content = "content.openalex.org" in url.lower()
    method_rank = {
        AcquisitionMethod.UNPAYWALL_PDF.value: 0,
        AcquisitionMethod.OPENALEX_PDF.value: 1,
    }
    return (
        method_rank.get(method, 99),
        1 if is_openalex_content else 0,
        url,
    )


def select_landing_page_candidates(record: PaperRecord) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for url in (
        record.acquisition_source_url,
        record.publisher_url,
    ):
        if not url or url in seen:
            continue
        seen.add(url)
        candidates.append(url)
    raw_candidates = (record.raw or {}).get("fulltext_candidates") or []
    for candidate in raw_candidates:
        if candidate.get("format") != FullTextFormat.HTML.value:
            continue
        url = candidate.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        candidates.append(url)
    return candidates


def extract_pdf_urls_from_html(html: str, *, base_url: str) -> list[str]:
    patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        r'<link[^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)["\']',
        r'"pdfPath"\s*:\s*"([^"]+)"',
        r'"pdfUrl"\s*:\s*"([^"]+)"',
        r'"pdf_url"\s*:\s*"([^"]+)"',
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
        r'href=["\']([^"\']*(?:stamp/stamp\.jsp|stampPDF/getPDF\.jsp|xpl/articleDetails\.jsp\?arnumber=|xplPDF\.jsp)[^"\']*)["\']',
        r'src=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    ]
    candidates: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in finditer(pattern, html, flags=IGNORECASE):
            raw_url = unescape(match.group(1)).replace("\\/", "/")
            candidate_url = urljoin(base_url, raw_url)
            if candidate_url in seen:
                continue
            seen.add(candidate_url)
            candidates.append(candidate_url)
    return candidates
