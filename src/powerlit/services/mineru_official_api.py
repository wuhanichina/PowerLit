from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import requests
from pypdf import PdfReader

from powerlit.models import PaperRecord
from powerlit.services.library_layout import doi_to_suffix
from powerlit.services.obsidian_notes import render_obsidian_note
from powerlit.services.pdf_content_cleaner import clean_mineru_markdown
from powerlit.services.pdf_parser import build_output_paths, build_parsed_payload, write_parsed_json
from powerlit.settings import Settings

TERMINAL_BATCH_STATES = {"done", "success", "failed", "error"}
TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}
CONTENT_LIST_BASENAMES = {
    "content_list.json",
    "content_list_v2.json",
}
NETWORK_RETRY_ATTEMPTS = 5
NETWORK_CHUNK_SIZE = 1024 * 1024
CURL_RETRYABLE_EXIT_CODES = {18, 28, 35, 52, 55, 56, 92}


class MineruOfficialAPIError(RuntimeError):
    """Raised when the official MinerU batch API cannot complete a request."""


class MineruStorageUnavailableError(RuntimeError):
    """Raised when NAS-backed input/output paths are not currently available."""


class MineruCurlError(MineruOfficialAPIError):
    """Raised when a curl subprocess fails while transferring a file."""

    def __init__(self, *, context: str, returncode: int, stderr: str):
        self.context = context
        self.returncode = returncode
        self.stderr = stderr.strip()
        super().__init__(
            f"MinerU {context} failed with curl exit {returncode}: {self.stderr[:300]}"
        )

    @property
    def is_retryable(self) -> bool:
        return self.returncode in CURL_RETRYABLE_EXIT_CODES


@dataclass(slots=True)
class MineruBatchFile:
    record: PaperRecord
    pdf_path: Path
    data_id: str
    file_name: str


@dataclass(slots=True)
class MineruUploadBatch:
    batch_id: str
    upload_urls: list[str]


@dataclass(slots=True)
class MineruBatchResult:
    batch_id: str
    state: str
    data_id: str | None
    file_name: str | None
    full_zip_url: str | None
    err_msg: str | None
    payload: dict[str, object]

    @property
    def normalized_state(self) -> str:
        return self.state.strip().lower()

    @property
    def is_terminal(self) -> bool:
        return self.normalized_state in TERMINAL_BATCH_STATES

    @property
    def is_success(self) -> bool:
        return self.normalized_state in {"done", "success"}


@dataclass(slots=True)
class MineruArchiveContents:
    raw_markdown: str
    raw_markdown_member: str
    content_list_member: str | None = None
    content_list_payload: object | None = None


@dataclass(slots=True)
class MineruParsedArtifact:
    json_path: Path
    generation_mode: str
    page_count: int
    raw_markdown_member: str
    content_list_member: str | None = None


@dataclass(slots=True)
class MineruTransferFailure:
    batch_file: MineruBatchFile
    error: str


class MineruOfficialBatchAPIService:
    def __init__(
        self,
        settings: Settings,
        *,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self.session = session or requests.Session()

    def build_batch_file(self, record: PaperRecord) -> MineruBatchFile:
        if not record.doi:
            raise MineruOfficialAPIError("Official MinerU batch API requires a DOI-backed record.")
        if not record.local_pdf_path:
            raise MineruOfficialAPIError("Official MinerU batch API requires an attached PDF.")
        data_id = doi_to_suffix(record.doi)
        return MineruBatchFile(
            record=record,
            pdf_path=Path(record.local_pdf_path).resolve(),
            data_id=data_id,
            file_name=Path(record.local_pdf_path).name,
        )

    def ensure_storage_available(self) -> None:
        required_paths = [
            self.settings.reference_dir,
            self.settings.db_path,
        ]
        for path in required_paths:
            if not path.exists():
                raise MineruStorageUnavailableError(f"Required path is unavailable: {path}")
        try:
            self.settings.parsed_output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            raise MineruStorageUnavailableError(
                f"Parsed output directory is unavailable: {self.settings.parsed_output_dir}"
            ) from exc

    def create_upload_batch(self, files: list[MineruBatchFile]) -> MineruUploadBatch:
        if not files:
            raise ValueError("At least one file is required to create an upload batch.")
        payload = {
            "enable_formula": self.settings.mineru_api_enable_formula,
            "enable_table": self.settings.mineru_api_enable_table,
            "language": self.settings.mineru_api_language,
            "model_version": self.settings.mineru_api_model_version,
            "files": [
                {
                    "name": item.file_name,
                    "is_ocr": self.settings.mineru_api_is_ocr,
                    "data_id": item.data_id,
                }
                for item in files
            ],
        }
        response = self._request_json(
            "post",
            f"{self.settings.mineru_api_base_url.rstrip('/')}/file-urls/batch",
            json_payload=payload,
            timeout=self.settings.mineru_api_request_timeout,
        )
        data = self._extract_data(response, context="create upload batch")
        batch_id = str(data.get("batch_id") or "").strip()
        upload_urls = [str(item) for item in data.get("file_urls") or [] if str(item).strip()]
        if not batch_id:
            raise MineruOfficialAPIError("MinerU batch response did not include batch_id.")
        if len(upload_urls) != len(files):
            raise MineruOfficialAPIError(
                f"MinerU returned {len(upload_urls)} upload URLs for {len(files)} files."
            )
        return MineruUploadBatch(
            batch_id=batch_id,
            upload_urls=upload_urls,
        )

    def upload_batch_files(
        self,
        upload_batch: MineruUploadBatch,
        files: list[MineruBatchFile],
    ) -> tuple[list[MineruBatchFile], list[MineruTransferFailure]]:
        if len(upload_batch.upload_urls) != len(files):
            raise ValueError("Upload URL count does not match file count.")
        uploaded: list[MineruBatchFile] = []
        failures: list[MineruTransferFailure] = []
        for item, upload_url in zip(files, upload_batch.upload_urls, strict=True):
            try:
                self._upload_file(item.pdf_path, upload_url)
            except MineruOfficialAPIError as exc:
                failures.append(
                    MineruTransferFailure(
                        batch_file=item,
                        error=str(exc),
                    )
                )
                continue
            uploaded.append(item)
        return uploaded, failures

    def wait_for_batch_results(
        self,
        batch_id: str,
        *,
        expected_count: int | None = None,
        expected_data_ids: set[str] | None = None,
    ) -> list[MineruBatchResult]:
        deadline = time.monotonic() + self.settings.mineru_api_batch_timeout
        while True:
            results = self.fetch_batch_results(batch_id)
            relevant = results
            if expected_data_ids is not None:
                relevant = [
                    result
                    for result in results
                    if result.data_id is not None and result.data_id in expected_data_ids
                ]
                seen_data_ids = {result.data_id for result in relevant if result.data_id is not None}
                if expected_data_ids and expected_data_ids.issubset(seen_data_ids) and all(
                    result.is_terminal for result in relevant
                ):
                    return results
            elif expected_count is not None and (
                len(results) >= expected_count
                and all(result.is_terminal for result in results)
            ):
                return results
            if time.monotonic() >= deadline:
                raise MineruOfficialAPIError(
                    f"Timed out while waiting for MinerU batch {batch_id} results."
                )
            time.sleep(self.settings.mineru_api_poll_interval)

    def fetch_batch_results(self, batch_id: str) -> list[MineruBatchResult]:
        response = self._request_json(
            "get",
            f"{self.settings.mineru_api_base_url.rstrip('/')}/extract-results/batch/{batch_id}",
            timeout=self.settings.mineru_api_request_timeout,
        )
        data = self._extract_data(response, context="fetch batch results")
        results_payload = (
            data.get("extract_result")
            or data.get("extract_results")
            or data.get("files")
            or []
        )
        if isinstance(results_payload, dict):
            results_payload = [results_payload]
        results: list[MineruBatchResult] = []
        for raw in results_payload:
            if not isinstance(raw, dict):
                continue
            results.append(
                MineruBatchResult(
                    batch_id=batch_id,
                    state=str(raw.get("state") or raw.get("status") or "").strip(),
                    data_id=_optional_str(raw.get("data_id")),
                    file_name=_optional_str(raw.get("file_name") or raw.get("name")),
                    full_zip_url=_optional_str(raw.get("full_zip_url")),
                    err_msg=_optional_str(raw.get("err_msg") or raw.get("message")),
                    payload=raw,
                )
            )
        return results

    def write_parsed_artifact(
        self,
        batch_file: MineruBatchFile,
        *,
        zip_bytes: bytes,
    ) -> MineruParsedArtifact:
        archive = extract_mineru_archive(zip_bytes)
        cleaned_body = clean_mineru_markdown(
            archive.raw_markdown,
            source_title=batch_file.record.source_title,
        )
        note_markdown = render_obsidian_note(
            batch_file.record,
            body=cleaned_body,
        )
        output_paths = build_output_paths(self.settings, batch_file.record)
        output_paths.output_base.parent.mkdir(parents=True, exist_ok=True)
        page_count = safe_pdf_page_count(
            batch_file.pdf_path,
            fallback=infer_page_count_from_content_list(archive.content_list_payload),
        )
        payload = build_parsed_payload(
            batch_file.record,
            note_content=note_markdown,
            generation_mode=f"mineru_official_batch_api_{self.settings.mineru_api_model_version}",
            page_count=page_count,
        )
        payload["mineru_api"] = {
            "model_version": self.settings.mineru_api_model_version,
            "language": self.settings.mineru_api_language,
            "enable_formula": self.settings.mineru_api_enable_formula,
            "enable_table": self.settings.mineru_api_enable_table,
            "is_ocr": self.settings.mineru_api_is_ocr,
            "raw_markdown_member": archive.raw_markdown_member,
            "content_list_member": archive.content_list_member,
        }
        write_parsed_json(output_paths.json_path, payload)
        return MineruParsedArtifact(
            json_path=output_paths.json_path,
            generation_mode=f"mineru_official_batch_api_{self.settings.mineru_api_model_version}",
            page_count=page_count,
            raw_markdown_member=archive.raw_markdown_member,
            content_list_member=archive.content_list_member,
        )

    def download_result_archive(self, result: MineruBatchResult) -> bytes:
        if not result.full_zip_url:
            raise MineruOfficialAPIError(
                f"MinerU batch result for {result.data_id or result.file_name} has no full_zip_url."
            )
        if shutil.which("curl"):
            return self._download_bytes_with_curl(
                result.full_zip_url,
                timeout=self.settings.mineru_api_download_timeout,
            )
        last_error: Exception | None = None
        for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
            try:
                payload = self._download_bytes(
                    result.full_zip_url,
                    timeout=self.settings.mineru_api_download_timeout,
                )
                validate_zip_payload(payload)
                return payload
            except (requests.RequestException, OSError, BadZipFile, MineruOfficialAPIError) as exc:
                last_error = exc
                if not _is_retryable_transfer_error(exc) or attempt >= NETWORK_RETRY_ATTEMPTS:
                    break
                time.sleep(_retry_delay_seconds(attempt))
        raise MineruOfficialAPIError(
            f"MinerU download failed for {result.data_id or result.file_name}: {last_error}"
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_payload: dict[str, object] | None = None,
        timeout: float,
    ) -> dict[str, object]:
        response = self._request(
            method,
            url,
            json_payload=json_payload,
            timeout=timeout,
            needs_auth=True,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise MineruOfficialAPIError(
                f"MinerU returned non-JSON response for {url}: {response.text[:200]}"
            ) from exc
        if not isinstance(payload, dict):
            raise MineruOfficialAPIError(f"MinerU returned unexpected payload for {url}.")
        code = payload.get("code")
        if code not in (None, 0, "0"):
            raise MineruOfficialAPIError(
                f"MinerU API error for {url}: code={code}, msg={payload.get('msg') or payload.get('message')}"
            )
        return payload

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_payload: dict[str, object] | None = None,
        timeout: float,
        needs_auth: bool,
    ) -> requests.Response:
        headers = {}
        if needs_auth:
            token = (self.settings.mineru_api_token or "").strip()
            if not token:
                raise MineruOfficialAPIError("POWERLIT_MINERU_API_TOKEN is required.")
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers or None,
                    json=json_payload,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt >= NETWORK_RETRY_ATTEMPTS:
                    raise MineruOfficialAPIError(
                        f"MinerU request failed: {method.upper()} {url} -> {exc}"
                    ) from exc
                time.sleep(_retry_delay_seconds(attempt))
                continue
            if response.status_code < 400:
                return response
            if response.status_code not in TRANSIENT_HTTP_STATUS or attempt >= NETWORK_RETRY_ATTEMPTS:
                raise MineruOfficialAPIError(
                    f"MinerU request failed: {method.upper()} {url} -> {response.status_code} {response.text[:300]}"
                )
            time.sleep(_retry_delay_seconds(attempt))
        raise RuntimeError("unreachable")

    def _upload_file(self, pdf_path: Path, upload_url: str) -> None:
        if shutil.which("curl"):
            self._upload_file_with_curl(pdf_path, upload_url)
            return
        last_error: Exception | None = None
        for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
            try:
                with pdf_path.open("rb") as handle:
                    response = self.session.put(
                        upload_url,
                        data=handle,
                        timeout=self.settings.mineru_api_upload_timeout,
                    )
                if response.status_code < 400:
                    return
                last_error = MineruOfficialAPIError(
                    f"MinerU upload failed for {pdf_path.name}: {response.status_code} {response.text[:300]}"
                )
                if response.status_code not in TRANSIENT_HTTP_STATUS or attempt >= NETWORK_RETRY_ATTEMPTS:
                    raise last_error
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= NETWORK_RETRY_ATTEMPTS:
                    raise MineruOfficialAPIError(
                        f"MinerU upload failed for {pdf_path.name}: {exc}"
                    ) from exc
            time.sleep(_retry_delay_seconds(attempt))
        raise MineruOfficialAPIError(f"MinerU upload failed for {pdf_path.name}: {last_error}")

    def _upload_file_with_curl(self, pdf_path: Path, upload_url: str) -> None:
        last_error: MineruOfficialAPIError | None = None
        for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
            try:
                self._run_curl(
                    [
                        "--request",
                        "PUT",
                        "--header",
                        "Expect:",
                        "--upload-file",
                        str(pdf_path),
                        upload_url,
                    ],
                    timeout=self.settings.mineru_api_upload_timeout,
                    context=f"upload {pdf_path.name}",
                )
                return
            except MineruOfficialAPIError as exc:
                last_error = exc
                if attempt >= NETWORK_RETRY_ATTEMPTS:
                    break
                time.sleep(_retry_delay_seconds(attempt))
        raise last_error or MineruOfficialAPIError(f"MinerU upload failed for {pdf_path.name}.")

    @staticmethod
    def _extract_data(payload: dict[str, object], *, context: str) -> dict[str, object]:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        raise MineruOfficialAPIError(f"MinerU payload missing data block for {context}.")

    def _download_bytes(self, url: str, *, timeout: float) -> bytes:
        with self.session.get(url, timeout=timeout, stream=True) as response:
            if response.status_code >= 400:
                raise MineruOfficialAPIError(
                    f"MinerU download failed: GET {url} -> {response.status_code} {response.text[:300]}"
                )
            buffer = io.BytesIO()
            for chunk in response.iter_content(chunk_size=NETWORK_CHUNK_SIZE):
                if not chunk:
                    continue
                buffer.write(chunk)
            return buffer.getvalue()

    def _download_bytes_with_curl(self, url: str, *, timeout: float) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            temp_path = Path(handle.name)
        last_error: Exception | None = None
        try:
            for attempt in range(1, NETWORK_RETRY_ATTEMPTS + 1):
                try:
                    command = ["--location"]
                    if temp_path.exists() and temp_path.stat().st_size > 0:
                        command.extend(["--continue-at", "-"])
                    command.extend(
                        [
                            "--output",
                            str(temp_path),
                            url,
                        ]
                    )
                    self._run_curl(
                        command,
                        timeout=timeout,
                        context=f"download {url}",
                    )
                    payload = temp_path.read_bytes()
                    validate_zip_payload(payload)
                    return payload
                except (MineruCurlError, OSError, BadZipFile) as exc:
                    last_error = exc
                    if isinstance(exc, MineruCurlError) and exc.returncode in {33, 36}:
                        temp_path.unlink(missing_ok=True)
                    if not _is_retryable_transfer_error(exc) or attempt >= NETWORK_RETRY_ATTEMPTS:
                        break
                    time.sleep(_retry_delay_seconds(attempt))
            raise MineruOfficialAPIError(f"MinerU download failed for {url}: {last_error}")
        finally:
            temp_path.unlink(missing_ok=True)

    def _run_curl(
        self,
        args: list[str],
        *,
        timeout: float,
        context: str,
    ) -> None:
        command = [
            "curl",
            "--fail",
            "--globoff",
            "--http1.1",
            "--silent",
            "--show-error",
            "--connect-timeout",
            "30",
            "--max-time",
            str(int(timeout)),
            *args,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
        except subprocess.TimeoutExpired as exc:
            raise MineruOfficialAPIError(f"MinerU {context} timed out after {timeout} seconds.") from exc
        if completed.returncode == 0:
            return
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise MineruCurlError(
            context=context,
            returncode=completed.returncode,
            stderr=stderr,
        )


def extract_mineru_archive(zip_bytes: bytes) -> MineruArchiveContents:
    with ZipFile(io.BytesIO(zip_bytes)) as archive:
        members = [
            name
            for name in archive.namelist()
            if name and not name.endswith("/")
        ]
        markdown_member = select_archive_member(members, preferred_basenames=["full.md"])
        if markdown_member is None:
            markdown_member = select_archive_member(
                members,
                preferred_suffixes=[".md"],
            )
        if markdown_member is None:
            raise MineruOfficialAPIError("MinerU result archive does not contain Markdown output.")
        raw_markdown = archive.read(markdown_member).decode("utf-8", errors="replace")

        content_list_member = select_archive_member(
            members,
            preferred_basenames=sorted(CONTENT_LIST_BASENAMES),
        )
        content_list_payload = None
        if content_list_member is not None:
            try:
                content_list_payload = json.loads(
                    archive.read(content_list_member).decode("utf-8", errors="replace")
                )
            except ValueError:
                content_list_payload = None

    return MineruArchiveContents(
        raw_markdown=raw_markdown,
        raw_markdown_member=markdown_member,
        content_list_member=content_list_member,
        content_list_payload=content_list_payload,
    )


def select_archive_member(
    members: list[str],
    *,
    preferred_basenames: list[str] | None = None,
    preferred_suffixes: list[str] | None = None,
) -> str | None:
    if preferred_basenames:
        for basename in preferred_basenames:
            for member in members:
                if Path(member).name == basename:
                    return member
    if preferred_suffixes:
        for suffix in preferred_suffixes:
            for member in members:
                if member.lower().endswith(suffix.lower()):
                    return member
    return None


def infer_page_count_from_content_list(content_list_payload: object | None) -> int | None:
    if isinstance(content_list_payload, dict):
        pages = content_list_payload.get("pages")
        if isinstance(pages, list) and pages:
            return len(pages)
        content_list_payload = content_list_payload.get("content_list") or content_list_payload

    if not isinstance(content_list_payload, list):
        return None

    page_numbers: set[int] = set()
    for item in content_list_payload:
        if not isinstance(item, dict):
            continue
        for key in ("page_idx", "page_index", "page_no", "page_num", "page"):
            value = item.get(key)
            if isinstance(value, int):
                page_numbers.add(value)
                break
    if not page_numbers:
        return None
    if 0 in page_numbers:
        return max(page_numbers) + 1
    return max(page_numbers)


def safe_pdf_page_count(pdf_path: Path, *, fallback: int | None = None) -> int:
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception:
        return fallback or 0


def validate_zip_payload(payload: bytes) -> None:
    with ZipFile(io.BytesIO(payload)) as archive:
        archive.namelist()


def _retry_delay_seconds(attempt: int) -> float:
    return float(min(30, 2** (attempt - 1)))


def _is_retryable_transfer_error(exc: Exception) -> bool:
    if isinstance(exc, MineruCurlError):
        return exc.is_retryable
    if isinstance(exc, (requests.RequestException, OSError, BadZipFile)):
        return True
    if isinstance(exc, MineruOfficialAPIError):
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                " timed out",
                " timeout",
                "ssl connection timeout",
                "connection aborted",
                "incomplete",
                "partial file",
                " 408 ",
                " 425 ",
                " 429 ",
                " 500 ",
                " 502 ",
                " 503 ",
                " 504 ",
            )
        )
    return False


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
