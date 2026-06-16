from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from powerlit.models import JournalSpec, PaperRecord
from powerlit.services.incoming_processor import (
    extract_doi_from_pdf,
    identify_doi_from_filename,
    iter_incoming_pdfs,
)
from powerlit.services.index import IndexStore
from powerlit.services.journal_issue_catalog import (
    ISSUE_CATALOG_JSON_FILENAME,
    JournalIssueCatalogService,
    clean_text,
    normalize_title_lookup_key,
    parse_cnki_navi_issue_refs,
    resolve_cnki_navi_detail_url,
)
from powerlit.services.metadata_lookup import MetadataLookupService
from powerlit.services.library_layout import doi_to_suffix, resolve_journal_short_name, sanitize_filename
from powerlit.settings import Settings

CSEE_PORTAL_BASE_URL = "http://csee.publish.founderss.cn"
CSEE_GET_ARTICLE_BY_DOI_URL = f"{CSEE_PORTAL_BASE_URL}/rc-pub/front/front-article/getArticleByDoi"
CSEE_GET_ATTACH_TYPES_URL = (
    f"{CSEE_PORTAL_BASE_URL}/rc-pub/front/front-article/getAttachTypesById"
)
CSEE_DOWNLOAD_URL = f"{CSEE_PORTAL_BASE_URL}/rc-pub/front/front-article/download"
AEPS_ISSUE_URL_TEMPLATE = "http://aeps-info.com/aeps/article/issue/{year}_{volume}_{issue}"
PST_ISSUE_URL_TEMPLATE = "http://ntps.epri.sgcc.com.cn/dwjs/CN/Y{year}/V{volume}/I{issue}"
CNKI_COOKIE_HOST_KEYWORDS = ("cnki", "utuvpn", "utuedu", "mdjsf")
CHROMIUM_BROWSER_ROOTS = (
    ("edge", Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"),
    ("chrome", Path(os.getenv("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"),
)
OFFICIAL_VOLUME_BASE_YEAR = {
    "aeps": 1977,
    "pst": 1977,
}
PST_OFFICIAL_MIN_YEAR = 2005
BROWSER_DOWNLOAD_TIMEOUT_SECONDS = 120.0
BROWSER_DOWNLOAD_POLL_SECONDS = 0.5
CNKI_BROWSER_LOGIN_TIMEOUT_SECONDS = 300.0
CNKI_BROWSER_RETRY_SECONDS = 8.0
CNKI_BROWSER_STATUS_INTERVAL_SECONDS = 15.0
PST_ROW_WAIT_TIMEOUT_SECONDS = 45.0
LEGACY_HTTP_ONLY_HOSTS = (
    "ntps.epri.sgcc.com.cn",
    "www.dwjs.com.cn",
)
EDGE_BINARY_CANDIDATES = (
    Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe",
    Path(os.getenv("PROGRAMFILES", r"C:\Program Files"))
    / "Microsoft"
    / "Edge"
    / "Application"
    / "msedge.exe",
)
DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)


class AssistedDownloadError(RuntimeError):
    """Raised when a candidate could not be downloaded by the assisted pipeline."""


@dataclass(slots=True)
class AssistedIncomingCandidate:
    title: str
    doi: str | None
    source_title: str | None
    journal_short_name: str
    year: int | None
    volume: str | None
    issue: str | None
    provider: str | None
    publisher_url: str | None
    issue_source_url: str | None
    pages: str | None
    issue_catalog_path: Path


@dataclass(slots=True)
class AssistedDownloadTarget:
    method: str
    title: str
    doi: str | None
    download_url: str
    source_url: str | None = None
    referer: str | None = None


@dataclass(slots=True)
class AssistedIncomingDownloadOutcome:
    candidate: AssistedIncomingCandidate
    status: str
    method: str | None = None
    path: Path | None = None
    source_url: str | None = None
    downloaded: bool = False
    message: str | None = None


@dataclass(slots=True)
class IssuePageArticle:
    title: str
    doi: str | None
    download_url: str | None
    article_url: str | None


@dataclass(slots=True)
class BrowserCookieSession:
    browser_name: str
    profile_name: str
    session: requests.Session
    cookie_count: int


@dataclass(slots=True)
class TitleFingerprintMatch:
    signature: str
    local_pdf_path: Path | None = None


class OfficialBrowserDownloadSession:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.download_dir = Path(tempfile.mkdtemp(prefix="powerlit-assisted-download-"))
        self._driver = None
        self._current_issue_url: str | None = None
        self._pst_issue_cache: dict[str, list[IssuePageArticle]] = {}
        self._aeps_issue_cache: dict[str, list[IssuePageArticle]] = {}
        self._cnki_login_announced = False
        self._ieee_login_announced = False
        self._elsevier_login_announced = False

    def close(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        shutil.rmtree(self.download_dir, ignore_errors=True)

    def load_pst_issue_articles(self, issue_url: str) -> list[IssuePageArticle]:
        cached = self._pst_issue_cache.get(issue_url)
        if cached is not None:
            return cached
        driver = self._ensure_driver()
        self._load_issue_page(
            issue_url,
            article_selector="ul.article-list > li",
            error_message=f"PST issue page did not render article rows in time: {issue_url}",
        )
        articles = parse_pst_issue_articles(driver.page_source, base_url=issue_url)
        if not articles:
            raise AssistedDownloadError(f"PST issue page did not expose any article rows: {issue_url}")
        self._pst_issue_cache[issue_url] = articles
        return articles

    def load_aeps_issue_articles(self, issue_url: str) -> list[IssuePageArticle]:
        cached = self._aeps_issue_cache.get(issue_url)
        if cached is not None:
            return cached
        driver = self._ensure_driver()
        self._load_issue_page(
            issue_url,
            article_selector="li.article_line",
            error_message=f"AEPS issue page did not render article rows in time: {issue_url}",
        )
        articles = parse_aeps_issue_articles(driver.page_source, base_url=issue_url)
        if not articles:
            raise AssistedDownloadError(f"AEPS issue page did not expose any article rows: {issue_url}")
        self._aeps_issue_cache[issue_url] = articles
        return articles

    def download_pst_pdf(
        self,
        *,
        issue_url: str,
        article_url: str | None,
        title: str,
        doi: str | None,
        target_path: Path,
    ) -> None:
        bindings = load_selenium_bindings()
        existing_names = {path.name for path in self.download_dir.iterdir()}
        detail_url = string_or_none(article_url)
        last_error: AssistedDownloadError | None = None
        if detail_url:
            try:
                self._load_pst_article_page(detail_url)
                pdf_action = self._find_pst_detail_pdf_action(by=bindings["By"])
                if pdf_action is None:
                    raise AssistedDownloadError(
                        f"PST article page did not expose a visible PDF download action for {doi or title}"
                    )
                self._click_pst_detail_pdf_action(
                    pdf_action,
                    title=title,
                    doi=doi,
                    action_chains=bindings["ActionChains"],
                )
            except AssistedDownloadError as exc:
                last_error = exc
            else:
                downloaded_path = self._wait_for_download(existing_names)
                self._close_secondary_download_windows()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(downloaded_path), str(target_path))
                return
        detail_url = self._resolve_pst_article_url_from_issue(
            issue_url=issue_url,
            title=title,
            doi=doi,
            by=bindings["By"],
        )
        if not detail_url:
            raise last_error or AssistedDownloadError(
                f"PST issue page did not contain a matching article detail page for {doi or title}: {issue_url}"
            )
        self._load_pst_article_page(detail_url)
        pdf_action = self._find_pst_detail_pdf_action(by=bindings["By"])
        if pdf_action is None:
            raise AssistedDownloadError(
                f"PST article page did not expose a visible PDF download action for {doi or title}"
            )
        self._click_pst_detail_pdf_action(
            pdf_action,
            title=title,
            doi=doi,
            action_chains=bindings["ActionChains"],
        )
        downloaded_path = self._wait_for_download(existing_names)
        self._close_secondary_download_windows()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded_path), str(target_path))

    def download_cnki_pdf(
        self,
        *,
        detail_url: str | None,
        article_url: str | None,
        download_url: str,
        target_path: Path,
    ) -> None:
        driver = self._ensure_driver()
        start_url = article_url or detail_url or download_url
        if not self._has_cnki_browser_session():
            self._await_manual_cnki_login(start_url)
        existing_names = {path.name for path in self.download_dir.iterdir()}
        clicked = False
        for page_url in (article_url, detail_url):
            if not page_url:
                continue
            if self._click_cnki_download_from_page(page_url, expected_download_url=download_url):
                clicked = True
                break
        if not clicked:
            try:
                if detail_url:
                    driver.get(detail_url)
                driver.get(download_url)
            except Exception as exc:
                raise AssistedDownloadError(
                    f"Failed to open the CNKI download page in the assisted browser: {download_url}"
                ) from exc
        downloaded_path = self._wait_for_download(existing_names)
        self._close_secondary_download_windows()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded_path), str(target_path))

    def download_ieee_pdf(
        self,
        *,
        landing_url: str,
        title: str,
        doi: str | None,
        target_path: Path,
    ) -> None:
        bindings = load_selenium_bindings()
        driver = self._ensure_driver()
        self._load_ieee_document_page(landing_url)
        pdf_link = self._find_ieee_pdf_link(by=bindings["By"])
        if pdf_link is None:
            raise AssistedDownloadError(f"IEEE Xplore page did not expose a PDF action for {doi or title}")
        if self._ieee_pdf_requires_login(pdf_link):
            self._await_manual_ieee_login(landing_url, title=title, doi=doi, by=bindings["By"])
            self._load_ieee_document_page(landing_url)
            pdf_link = self._find_ieee_pdf_link(by=bindings["By"])
            if pdf_link is None or self._ieee_pdf_requires_login(pdf_link):
                raise AssistedDownloadError(
                    f"IEEE login or subscription access is still unavailable for {doi or title}"
                )
        existing_names = {path.name for path in self.download_dir.iterdir()}
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", pdf_link)
            pdf_link.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", pdf_link)
            except Exception as exc:
                raise AssistedDownloadError(
                    f"Failed to click the IEEE PDF button for {doi or title}"
                ) from exc
        downloaded_path = self._wait_for_download(existing_names)
        self._close_secondary_download_windows()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded_path), str(target_path))

    def download_elsevier_pdf(
        self,
        *,
        landing_url: str,
        title: str,
        doi: str | None,
        target_path: Path,
    ) -> None:
        bindings = load_selenium_bindings()
        driver = self._ensure_driver()
        self._load_elsevier_article_page(landing_url)
        pdf_action = self._find_elsevier_pdf_action(by=bindings["By"])
        if pdf_action is None or self._elsevier_pdf_requires_access(pdf_action):
            self._await_manual_elsevier_access(landing_url, title=title, doi=doi, by=bindings["By"])
            self._load_elsevier_article_page(landing_url)
            pdf_action = self._find_elsevier_pdf_action(by=bindings["By"])
            if pdf_action is None or self._elsevier_pdf_requires_access(pdf_action):
                raise AssistedDownloadError(
                    f"Elsevier login, remote access, or PDF availability is still unavailable for {doi or title}"
                )
        existing_names = {path.name for path in self.download_dir.iterdir()}
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", pdf_action)
            pdf_action.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", pdf_action)
            except Exception as exc:
                raise AssistedDownloadError(
                    f"Failed to click the Elsevier PDF button for {doi or title}"
                ) from exc
        downloaded_path = self._wait_for_download(existing_names)
        self._close_secondary_download_windows()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(downloaded_path), str(target_path))

    def _ensure_driver(self):
        if self._driver is not None:
            return self._driver
        bindings = load_selenium_bindings()
        options = bindings["webdriver"].EdgeOptions()
        binary_path = next((path for path in EDGE_BINARY_CANDIDATES if path.exists()), None)
        if binary_path is not None:
            options.binary_location = str(binary_path)
        try:
            options.page_load_strategy = "eager"
        except Exception:
            pass
        options.add_argument(
            "--disable-features=HttpsUpgrades,HttpsFirstBalancedModeAutoEnable,HttpsOnlyMode"
        )
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--disable-popup-blocking")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--ignore-ssl-errors")
        options.add_argument("--lang=zh-CN")
        options.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        options.add_experimental_option(
            "prefs",
            {
                "download.default_directory": str(self.download_dir),
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "plugins.always_open_pdf_externally": True,
            },
        )
        try:
            driver = bindings["webdriver"].Edge(options=options)
        except Exception as exc:
            raise AssistedDownloadError(
                "Unable to launch Microsoft Edge for official-site assisted downloads"
            ) from exc
        driver.set_page_load_timeout(int(max(self.settings.request_timeout, 60.0)))
        try:
            driver.execute_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:
            pass
        self._driver = driver
        return driver

    def _load_issue_page(
        self,
        issue_url: str,
        *,
        article_selector: str,
        error_message: str,
    ) -> None:
        bindings = load_selenium_bindings()
        driver = self._ensure_driver()
        issue_url = normalize_legacy_http_url(issue_url)
        if self._current_issue_url != issue_url:
            try:
                driver.get(issue_url)
            except bindings["TimeoutException"] as exc:
                raise AssistedDownloadError(f"Timed out while opening official issue page: {issue_url}") from exc
            except Exception as exc:
                raise AssistedDownloadError(f"Failed to open official issue page: {issue_url}") from exc
            self._current_issue_url = issue_url
        try:
            bindings["WebDriverWait"](driver, max(self.settings.request_timeout, 30.0)).until(
                lambda current_driver: len(
                    current_driver.find_elements(bindings["By"].CSS_SELECTOR, article_selector)
                )
                > 0
            )
        except bindings["TimeoutException"] as exc:
            raise AssistedDownloadError(error_message) from exc

    def _load_pst_article_page(self, article_url: str) -> None:
        bindings = load_selenium_bindings()
        driver = self._ensure_driver()
        article_url = normalize_legacy_http_url(article_url)
        if self._current_issue_url != article_url:
            try:
                driver.get(article_url)
            except bindings["TimeoutException"] as exc:
                raise AssistedDownloadError(
                    f"Timed out while opening the PST article page: {article_url}"
                ) from exc
            except Exception as exc:
                raise AssistedDownloadError(
                    f"Failed to open the PST article page: {article_url}"
                ) from exc
            self._current_issue_url = article_url
        try:
            bindings["WebDriverWait"](driver, max(self.settings.request_timeout, 30.0)).until(
                lambda current_driver: any(
                    element.is_displayed()
                    for element in current_driver.find_elements(
                        bindings["By"].CSS_SELECTOR,
                        ".main_content_center_right_pdf_i, .main_content_center_right_pdf_b, .main_content_center_right_pdf",
                    )
                )
            )
        except bindings["TimeoutException"] as exc:
            raise AssistedDownloadError(
                f"PST article page did not render a visible PDF action in time: {article_url}"
            ) from exc

    def _find_pst_detail_pdf_action(self, *, by):
        if self._driver is None:
            return None
        icon_elements = self._driver.find_elements(
            by.CSS_SELECTOR,
            'i.main_content_center_right_pdf_i[wm_ev_click="1"]',
        )
        if icon_elements:
            return icon_elements[-1]
        selectors = (
            "i.main_content_center_right_pdf_i",
            "b.main_content_center_right_pdf_b",
            "div.main_content_center_right_pdf",
        )
        for selector in selectors:
            elements = self._driver.find_elements(by.CSS_SELECTOR, selector)
            for element in elements:
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
        return None

    def _click_pst_detail_pdf_action(self, element, *, title: str, doi: str | None, action_chains) -> None:
        driver = self._ensure_driver()
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(1.0)
        except Exception:
            pass
        try:
            element.click()
            return
        except Exception:
            pass
        try:
            action_chains(driver).move_to_element(element).click().perform()
            return
        except Exception:
            pass
        try:
            driver.execute_script("arguments[0].click();", element)
            return
        except Exception as inner_exc:
            raise AssistedDownloadError(
                f"Failed to click the PST article PDF action for {doi or title}"
            ) from inner_exc

    def _resolve_pst_article_url_from_issue(
        self,
        *,
        issue_url: str,
        title: str,
        doi: str | None,
        by,
    ) -> str | None:
        self._load_issue_page(
            issue_url,
            article_selector="ul.article-list > li",
            error_message=f"PST issue page did not render article rows in time: {issue_url}",
        )
        row = self._wait_for_pst_row(
            title=title,
            doi=doi,
            by=by,
        )
        if row is None:
            return None
        try:
            title_link = row.find_element(by.CSS_SELECTOR, ".j-title-1 a")
        except Exception:
            return None
        article_url = string_or_none(title_link.get_attribute("href"))
        if not article_url:
            return None
        return normalize_legacy_http_url(article_url)

    def _find_pst_row(self, *, title: str, doi: str | None, by):
        normalized_doi = normalize_doi(doi)
        normalized_title = normalize_title_lookup_key(title)
        title_matches = []
        for row in self._driver.find_elements(by.CSS_SELECTOR, "ul.article-list > li"):
            title_node = row.find_element(by.CSS_SELECTOR, ".j-title-1 a")
            row_title = clean_text(title_node.text)
            row_doi = None
            doi_nodes = row.find_elements(by.CSS_SELECTOR, "a.j-doi")
            if doi_nodes:
                row_doi = normalize_doi(doi_nodes[0].text)
            if normalized_doi and row_doi == normalized_doi:
                return row
            if normalize_title_lookup_key(row_title) == normalized_title:
                title_matches.append(row)
        if len(title_matches) == 1:
            return title_matches[0]
        return None

    def _wait_for_pst_row(self, *, title: str, doi: str | None, by):
        deadline = time.monotonic() + PST_ROW_WAIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            row = self._find_pst_row(title=title, doi=doi, by=by)
            if row is not None:
                return row
            time.sleep(1.0)
        return None

    def _wait_for_download(self, existing_names: set[str]) -> Path:
        deadline = time.monotonic() + BROWSER_DOWNLOAD_TIMEOUT_SECONDS
        newest_completed: Path | None = None
        while time.monotonic() < deadline:
            current_files = [path for path in self.download_dir.iterdir() if path.name not in existing_names]
            in_progress = [
                path
                for path in current_files
                if path.name.endswith(".crdownload") or path.name.endswith(".tmp")
            ]
            completed = [path for path in current_files if path.is_file() and path.suffix.casefold() == ".pdf"]
            if completed and not in_progress:
                newest_completed = max(completed, key=lambda path: path.stat().st_mtime)
                if newest_completed.stat().st_size > 0:
                    break
            time.sleep(BROWSER_DOWNLOAD_POLL_SECONDS)
        if newest_completed is None or not newest_completed.exists():
            raise AssistedDownloadError("Timed out while waiting for the official-site PDF download to finish")
        return newest_completed

    def _load_ieee_document_page(self, landing_url: str) -> None:
        bindings = load_selenium_bindings()
        driver = self._ensure_driver()
        landing_url = normalize_legacy_http_url(landing_url)
        if self._current_issue_url != landing_url:
            try:
                driver.get(landing_url)
            except bindings["TimeoutException"] as exc:
                raise AssistedDownloadError(
                    f"Timed out while opening the IEEE Xplore document page: {landing_url}"
                ) from exc
            except Exception as exc:
                raise AssistedDownloadError(
                    f"Failed to open the IEEE Xplore document page: {landing_url}"
                ) from exc
            self._current_issue_url = landing_url
        try:
            bindings["WebDriverWait"](driver, max(self.settings.request_timeout, 30.0)).until(
                lambda current_driver: len(
                    current_driver.find_elements(
                        bindings["By"].CSS_SELECTOR,
                        'a[data-analytics_identifier="document-lh-action-downloadpdf"]',
                    )
                )
                > 0
            )
        except bindings["TimeoutException"] as exc:
            raise AssistedDownloadError(
                f"IEEE Xplore page did not render a PDF action in time: {landing_url}"
            ) from exc

    def _find_ieee_pdf_link(self, *, by):
        if self._driver is None:
            return None
        links = self._driver.find_elements(
            by.CSS_SELECTOR,
            'a[data-analytics_identifier="document-lh-action-downloadpdf"]',
        )
        for link in links:
            try:
                if link.is_displayed():
                    return link
            except Exception:
                continue
        return links[0] if links else None

    def _ieee_pdf_requires_login(self, link) -> bool:  # noqa: ANN001
        try:
            title = (link.get_attribute("title") or "").strip().lower()
            aria_label = (link.get_attribute("aria-label") or "").strip().lower()
        except Exception:
            return True
        combined = f"{title} {aria_label}"
        blocked_markers = (
            "do not have access",
            "purchase",
            "sign in",
            "subscribe",
        )
        return any(marker in combined for marker in blocked_markers)

    def _await_manual_ieee_login(self, landing_url: str, *, title: str, doi: str | None, by) -> None:
        if not self._ieee_login_announced:
            print(
                "IEEE full-text access is not yet active in the assisted Edge window. "
                "The first PDF page has been opened there; sign in to IEEE or institutional access once, "
                "then the downloader will continue automatically."
            )
            self._ieee_login_announced = True
        deadline = time.monotonic() + CNKI_BROWSER_LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            link = self._find_ieee_pdf_link(by=by)
            if link is not None and not self._ieee_pdf_requires_login(link):
                return
            time.sleep(1.0)
        raise AssistedDownloadError(
            f"IEEE login or subscription access was not completed in the assisted Edge window for {doi or title}"
        )

    def _load_elsevier_article_page(self, landing_url: str) -> None:
        bindings = load_selenium_bindings()
        driver = self._ensure_driver()
        landing_url = normalize_legacy_http_url(landing_url)
        if self._current_issue_url != landing_url:
            try:
                driver.get(landing_url)
            except bindings["TimeoutException"] as exc:
                raise AssistedDownloadError(
                    f"Timed out while opening the Elsevier article page: {landing_url}"
                ) from exc
            except Exception as exc:
                raise AssistedDownloadError(
                    f"Failed to open the Elsevier article page: {landing_url}"
                ) from exc
            self._current_issue_url = landing_url
        time.sleep(2.0)

    def _find_elsevier_pdf_action(self, *, by):
        if self._driver is None:
            return None
        selectors = [
            'a[data-aa-name*="pdf"]',
            'button[data-aa-name*="pdf"]',
            'a[href*="/science/article/pii/"][href*="/pdf"]',
            'a[href*="/science/article/pii/"][href*="/pdfft"]',
            'a[aria-label*="PDF" i]',
            'button[aria-label*="PDF" i]',
        ]
        for selector in selectors:
            elements = self._driver.find_elements(by.CSS_SELECTOR, selector)
            for element in elements:
                try:
                    if element.is_displayed():
                        return element
                except Exception:
                    continue
        text_queries = ("view pdf", "download pdf", "pdf")
        for selector in ("a", "button"):
            for element in self._driver.find_elements(by.CSS_SELECTOR, selector):
                try:
                    if not element.is_displayed():
                        continue
                    label = " ".join(
                        part
                        for part in (
                            element.text,
                            element.get_attribute("title"),
                            element.get_attribute("aria-label"),
                            element.get_attribute("data-aa-name"),
                        )
                        if part
                    ).strip().lower()
                except Exception:
                    continue
                if any(query in label for query in text_queries):
                    return element
        return None

    def _elsevier_pdf_requires_access(self, element) -> bool:  # noqa: ANN001
        try:
            label = " ".join(
                part
                for part in (
                    element.text,
                    element.get_attribute("title"),
                    element.get_attribute("aria-label"),
                    element.get_attribute("data-aa-name"),
                )
                if part
            ).strip().lower()
            href = (element.get_attribute("href") or "").strip().lower()
        except Exception:
            return True
        blocked_markers = (
            "just a moment",
            "remote access",
            "sign in",
            "get access",
            "purchase",
        )
        if any(marker in label for marker in blocked_markers):
            return True
        return not any(token in label or token in href for token in ("pdf", "/pdf", "/pdfft"))

    def _await_manual_elsevier_access(self, landing_url: str, *, title: str, doi: str | None, by) -> None:
        if not self._elsevier_login_announced:
            print(
                "Elsevier full-text access is not yet active in the assisted Edge window. "
                "The first ScienceDirect page has been opened there; complete institutional login or remote access once, "
                "then the downloader will continue automatically."
            )
            self._elsevier_login_announced = True
        deadline = time.monotonic() + CNKI_BROWSER_LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            action = self._find_elsevier_pdf_action(by=by)
            if action is not None and not self._elsevier_pdf_requires_access(action):
                return
            time.sleep(1.0)
        raise AssistedDownloadError(
            f"Elsevier login or remote access was not completed in the assisted Edge window for {doi or title}"
        )

    def _await_manual_cnki_login(self, start_url: str) -> None:
        driver = self._ensure_driver()
        if not self._cnki_login_announced:
            print(
                "CNKI login was not reusable from local browser cookies. "
                "An assisted Edge window has been opened; complete CNKI/VPN login there once, "
                "then downloads will continue automatically."
            )
            self._cnki_login_announced = True
        if not self._open_cnki_start_page(start_url):
            raise AssistedDownloadError(
                f"Failed to open the CNKI login page in the assisted browser: {start_url}"
            )
        deadline = time.monotonic() + CNKI_BROWSER_LOGIN_TIMEOUT_SECONDS
        next_retry_at = time.monotonic() + CNKI_BROWSER_RETRY_SECONDS
        next_status_at = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            page_state = self._get_cnki_page_state()
            if page_state == "ready" or self._has_cnki_browser_session():
                return
            now = time.monotonic()
            if now >= next_status_at:
                remaining_seconds = max(int(deadline - now), 0)
                print(self._format_cnki_wait_status(page_state, remaining_seconds))
                next_status_at = now + CNKI_BROWSER_STATUS_INTERVAL_SECONDS
            if page_state == "unknown" and time.monotonic() >= next_retry_at:
                self._open_cnki_start_page(start_url)
                next_retry_at = time.monotonic() + CNKI_BROWSER_RETRY_SECONDS
            time.sleep(1.0)
        raise AssistedDownloadError(
            "browser login session was not completed in the assisted Edge window within 300 seconds"
        )

    def _open_cnki_start_page(self, start_url: str) -> bool:
        driver = self._ensure_driver()
        bindings = load_selenium_bindings()
        for candidate_url in build_cnki_start_url_candidates(start_url):
            try:
                driver.get(candidate_url)
                if self._get_cnki_page_state() != "unknown":
                    return True
                current_url = (driver.current_url or "").strip().lower()
                if any(keyword in current_url for keyword in CNKI_COOKIE_HOST_KEYWORDS):
                    return True
            except bindings["TimeoutException"]:
                if self._get_cnki_page_state() != "unknown":
                    return True
                try:
                    current_url = (driver.current_url or "").strip().lower()
                except Exception:
                    current_url = ""
                if any(keyword in current_url for keyword in CNKI_COOKIE_HOST_KEYWORDS):
                    return True
            except Exception:
                continue
        return False

    def _click_cnki_download_from_page(self, page_url: str, *, expected_download_url: str) -> bool:
        driver = self._ensure_driver()
        bindings = load_selenium_bindings()
        try:
            driver.get(page_url)
        except Exception:
            return False
        time.sleep(1.0)
        download_action = self._find_cnki_download_action(
            by=bindings["By"],
            expected_download_url=expected_download_url,
        )
        if download_action is None:
            return False
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", download_action)
            download_action.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", download_action)
            except Exception:
                return False
        return True

    def _find_cnki_download_action(self, *, by, expected_download_url: str):
        if self._driver is None:
            return None
        selectors = [
            'a[href*="/bar/download/order"]',
            'a[href*="download"]',
            'a[onclick*="download"]',
            'button[onclick*="download"]',
        ]
        expected_url_lower = expected_download_url.strip().lower()
        for selector in selectors:
            elements = self._driver.find_elements(by.CSS_SELECTOR, selector)
            best_match = None
            best_score = -1
            for element in elements:
                try:
                    if not element.is_displayed():
                        continue
                    href = (element.get_attribute("href") or "").strip().lower()
                    onclick = (element.get_attribute("onclick") or "").strip().lower()
                    label = " ".join(
                        part
                        for part in (
                            element.text,
                            element.get_attribute("title"),
                            element.get_attribute("aria-label"),
                        )
                        if part
                    ).strip().lower()
                except Exception:
                    continue
                score = score_cnki_article_download_candidate(href=href, label=label, onclick=onclick)
                if score is None:
                    continue
                if expected_url_lower and (
                    expected_url_lower in href
                    or href in expected_url_lower
                    or expected_url_lower in onclick
                ):
                    score += 30
                if score > best_score:
                    best_match = element
                    best_score = score
            if best_match is not None:
                return best_match
        text_queries = ("pdf", "全文", "下载", "下载pdf", "整本下载")
        best_match = None
        best_score = -1
        for selector in ("a", "button"):
            for element in self._driver.find_elements(by.CSS_SELECTOR, selector):
                try:
                    if not element.is_displayed():
                        continue
                    label = " ".join(
                        part
                        for part in (
                            element.text,
                            element.get_attribute("title"),
                            element.get_attribute("aria-label"),
                        )
                        if part
                    ).strip().lower()
                    href = (element.get_attribute("href") or "").strip().lower()
                    onclick = (element.get_attribute("onclick") or "").strip().lower()
                except Exception:
                    continue
                score = score_cnki_article_download_candidate(href=href, label=label, onclick=onclick)
                if score is None:
                    continue
                if any(query in label for query in text_queries):
                    score += 20
                if expected_url_lower and (
                    expected_url_lower in href
                    or href in expected_url_lower
                    or expected_url_lower in onclick
                ):
                    score += 30
                if score > best_score:
                    best_match = element
                    best_score = score
        return best_match

    def _has_cnki_browser_session(self) -> bool:
        if self._driver is None:
            return False
        page_state = self._get_cnki_page_state()
        if page_state == "ready":
            return True
        if page_state in {"login", "captcha"}:
            return False
        try:
            cookies = self._driver.get_cookies()
        except Exception:
            return False
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lower()
            if any(keyword in domain for keyword in CNKI_COOKIE_HOST_KEYWORDS):
                return True
        return False

    def _get_cnki_page_state(self) -> str:
        if self._driver is None:
            return "unknown"
        try:
            current_url = (self._driver.current_url or "").strip().lower()
        except Exception:
            current_url = ""
        try:
            title = (self._driver.title or "").strip().lower()
        except Exception:
            title = ""
        try:
            page_source = (self._driver.page_source or "")[:12000].lower()
        except Exception:
            page_source = ""

        combined = "\n".join(part for part in (current_url, title, page_source) if part)
        login_markers = (
            "login",
            "sign in",
            "统一身份认证",
            "用户登录",
            "校园vpn",
            "校园网登录",
            "身份认证",
            "sso",
        )
        captcha_markers = (
            "tjcpatcha",
            "tjcaptcha",
            "验证码",
            "just a moment",
            "security check",
            "verify you are human",
        )
        ready_markers = (
            "knavi/detail",
            "/knavi/detail",
            "/bar/download/order",
            "pdf全文",
            "整本下载",
            "篇名",
            "参考文献",
            "download",
            "data-curpage",
            "briefBox",
            "issue-item",
        )

        if any(marker in combined for marker in captcha_markers):
            return "captcha"
        if any(marker in combined for marker in login_markers):
            return "login"
        if any(keyword in current_url for keyword in CNKI_COOKIE_HOST_KEYWORDS) and any(
            marker in combined for marker in ready_markers
        ):
            return "ready"
        return "unknown"

    def _format_cnki_wait_status(self, page_state: str, remaining_seconds: int) -> str:
        label_map = {
            "ready": "content page ready",
            "login": "login page detected",
            "captcha": "captcha or security check detected",
            "unknown": "waiting for CNKI page to stabilize",
        }
        state_label = label_map.get(page_state, page_state)
        current_url = ""
        if self._driver is not None:
            try:
                current_url = string_or_none(self._driver.current_url) or ""
            except Exception:
                current_url = ""
        if len(current_url) > 140:
            current_url = f"{current_url[:137]}..."
        if current_url:
            return (
                "Waiting for CNKI login in the assisted Edge window: "
                f"{state_label}. {remaining_seconds}s remaining. Current page: {current_url}"
            )
        return (
            "Waiting for CNKI login in the assisted Edge window: "
            f"{state_label}. {remaining_seconds}s remaining."
        )

    def _close_secondary_download_windows(self) -> None:
        if self._driver is None:
            return
        try:
            primary_handle = self._driver.current_window_handle
        except Exception:
            return
        for handle in list(self._driver.window_handles):
            if handle == primary_handle:
                continue
            try:
                self._driver.switch_to.window(handle)
                if self._driver.current_url.startswith("edge://downloads"):
                    self._driver.close()
            except Exception:
                continue
            finally:
                try:
                    self._driver.switch_to.window(primary_handle)
                except Exception:
                    pass


class AssistedIncomingDownloadService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = IndexStore(settings)
        self.catalog_service = JournalIssueCatalogService(settings)
        self.lookup = MetadataLookupService(settings)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": settings.user_agent})
        self._aeps_issue_cache: dict[tuple[int, str, str], list[IssuePageArticle]] = {}
        self._cnki_context_cache: dict[str, object] = {}
        self._cnki_issue_cache: dict[tuple[str, int, str], tuple[str, object]] = {}
        self._browser_cookie_session: BrowserCookieSession | None = None
        self._official_browser_session: OfficialBrowserDownloadSession | None = None
        self._library_title_fingerprint_map: dict[str, Path | None] | None = None

    def close(self) -> None:
        if self._browser_cookie_session is not None:
            self._browser_cookie_session.session.close()
        if self._official_browser_session is not None:
            self._official_browser_session.close()
            self._official_browser_session = None
        self.session.close()

    def discover_issue_catalog_candidates(
        self,
        *,
        journals: list[str] | None = None,
        from_year: int | None = None,
        until_year: int | None = None,
        doi: str | None = None,
        limit: int | None = None,
    ) -> list[AssistedIncomingCandidate]:
        normalized_doi = normalize_doi(doi)
        journal_filters = {normalize_journal_filter(item) for item in (journals or []) if item}
        candidates: list[AssistedIncomingCandidate] = []
        seen_keys: set[str] = set()
        for path, payload in self.iter_issue_catalog_payloads(journal_filters):
            source_title = string_or_none(payload.get("source_title"))
            journal_short_name = string_or_none(payload.get("journal_short_name")) or resolve_journal_short_name(
                source_title
            )
            if journal_filters and not matches_journal_filter(
                journal_short_name=journal_short_name,
                source_title=source_title,
                journal_filters=journal_filters,
            ):
                continue
            year = parse_year(payload.get("year"))
            if from_year is not None and (year is None or year < from_year):
                continue
            if until_year is not None and (year is None or year > until_year):
                continue
            provider = string_or_none(payload.get("provider"))
            issue_source_url = string_or_none(payload.get("issue_source_url"))
            volume = string_or_none(payload.get("volume"))
            issue = string_or_none(payload.get("issue"))
            for article in payload.get("articles") or []:
                title = string_or_none(article.get("title"))
                if not title:
                    continue
                article_doi = normalize_doi(article.get("doi"))
                if normalized_doi and article_doi != normalized_doi:
                    continue
                candidate = AssistedIncomingCandidate(
                    title=title,
                    doi=article_doi,
                    source_title=source_title,
                    journal_short_name=journal_short_name,
                    year=year,
                    volume=volume,
                    issue=issue,
                    provider=provider,
                    publisher_url=string_or_none(article.get("publisher_url")),
                    issue_source_url=issue_source_url,
                    pages=string_or_none(article.get("pages")),
                    issue_catalog_path=path,
                )
                if not self.supports_candidate(candidate):
                    continue
                if self.is_candidate_already_downloaded(candidate):
                    continue
                dedupe_key = article_doi or build_candidate_fallback_key(candidate)
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                candidates.append(candidate)
        if normalized_doi and not candidates:
            direct_candidate = self.discover_direct_doi_candidate(
                normalized_doi=normalized_doi,
                journal_filters=journal_filters,
            )
            if direct_candidate is not None:
                candidates.append(direct_candidate)
        candidates.sort(key=assisted_candidate_sort_key)
        if limit is not None:
            return candidates[:limit]
        return candidates

    def is_candidate_already_downloaded(self, candidate: AssistedIncomingCandidate) -> bool:
        normalized_doi = normalize_doi(candidate.doi)
        if normalized_doi:
            existing_row = self.store.get_paper_by_doi(normalized_doi)
            if existing_row and existing_row.get("local_pdf_path"):
                local_pdf_path = Path(str(existing_row["local_pdf_path"]))
                if local_pdf_path.exists():
                    return True
        signature = build_title_signature(
            title=candidate.title,
            source_title=candidate.source_title or candidate.journal_short_name,
            year=candidate.year,
            volume=candidate.volume,
            issue=candidate.issue,
            pages=candidate.pages,
        )
        return signature in self.get_library_title_fingerprint_map()

    def discover_direct_doi_candidate(
        self,
        *,
        normalized_doi: str,
        journal_filters: set[str],
    ) -> AssistedIncomingCandidate | None:
        existing_records = self.store.load_paper_records(
            limit=1,
            doi=normalized_doi,
            unresolved_only=False,
        )
        record = existing_records[0] if existing_records else self.lookup.lookup_by_doi(
            normalized_doi,
            query_pack="direct_doi_lookup",
        )
        if record is None:
            return None
        return self.build_direct_doi_candidate(
            record=record,
            normalized_doi=normalized_doi,
            journal_filters=journal_filters,
        )

    def build_direct_doi_candidate(
        self,
        *,
        record: PaperRecord,
        normalized_doi: str,
        journal_filters: set[str],
    ) -> AssistedIncomingCandidate | None:
        source_title = string_or_none(record.source_title)
        query_pack = normalize_journal_filter(record.query_pack)
        journal_short_name = (
            resolve_journal_short_name(source_title)
            if source_title
            else (query_pack or "unknown_journal")
        )
        if journal_filters and not matches_journal_filter(
            journal_short_name=journal_short_name,
            source_title=source_title,
            journal_filters=journal_filters,
        ):
            return None
        candidate = AssistedIncomingCandidate(
            title=record.title or normalized_doi,
            doi=normalize_doi(record.doi) or normalized_doi,
            source_title=source_title,
            journal_short_name=journal_short_name,
            year=record.year,
            volume=string_or_none(record.volume),
            issue=string_or_none(record.issue),
            provider="direct_doi_lookup",
            publisher_url=string_or_none(record.publisher_url) or f"https://doi.org/{normalized_doi}",
            issue_source_url=string_or_none(record.acquisition_source_url),
            pages=string_or_none(record.pages),
            issue_catalog_path=Path(f"_direct_doi/{doi_to_suffix(normalized_doi)}.json"),
        )
        if not self.supports_candidate(candidate):
            return None
        return candidate

    def iter_issue_catalog_payloads(
        self,
        journal_filters: set[str],
    ) -> list[tuple[Path, dict]]:
        payloads: list[tuple[Path, dict]] = []
        seen_paths: set[Path] = set()
        for journal_dir in sorted(path for path in self.settings.reference_dir.iterdir() if path.is_dir()):
            for issue_path in sorted(journal_dir.rglob(ISSUE_CATALOG_JSON_FILENAME)):
                if issue_path in seen_paths:
                    continue
                try:
                    payload = json.loads(issue_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                source_title = string_or_none(payload.get("source_title"))
                journal_short_name = (
                    string_or_none(payload.get("journal_short_name")) or journal_dir.name
                )
                if journal_filters and not matches_journal_filter(
                    journal_short_name=journal_short_name,
                    source_title=source_title,
                    journal_filters=journal_filters,
                ):
                    continue
                seen_paths.add(issue_path)
                payloads.append((issue_path, payload))
        return payloads

    def supports_candidate(self, candidate: AssistedIncomingCandidate) -> bool:
        if is_pcsee_journal(candidate.journal_short_name, candidate.source_title):
            return True
        if is_aeps_journal(candidate.journal_short_name, candidate.source_title):
            return True
        if is_pst_journal(candidate.journal_short_name, candidate.source_title):
            return True
        if is_ieee_assisted_journal(candidate.journal_short_name, candidate.source_title):
            return bool(candidate.publisher_url or candidate.doi)
        if is_elsevier_assisted_journal(candidate.journal_short_name, candidate.source_title):
            return bool(candidate.publisher_url or candidate.doi)
        return candidate.provider == "cnki_navi"

    def collect_existing_incoming_dois(self) -> set[str]:
        existing_dois: set[str] = set()
        for pdf_path in iter_incoming_pdfs(self.settings.incoming_pdf_dir):
            doi = identify_doi_from_filename(pdf_path, self.store) or extract_doi_from_pdf(pdf_path)
            if doi:
                existing_dois.add(doi.strip().lower())
        return existing_dois

    def download_candidate(
        self,
        candidate: AssistedIncomingCandidate,
        *,
        existing_incoming_dois: set[str],
    ) -> AssistedIncomingDownloadOutcome:
        targets = self.resolve_download_targets(candidate)
        if not targets:
            return AssistedIncomingDownloadOutcome(
                candidate=candidate,
                status="no_supported_route",
            )

        last_error: str | None = None
        saw_login_required = False
        for target in targets:
            effective_doi = normalize_doi(target.doi or candidate.doi)
            target_path: Path
            if effective_doi:
                duplicate_outcome = self.check_duplicate_target(
                    candidate,
                    doi=effective_doi,
                    existing_incoming_dois=existing_incoming_dois,
                    method=target.method,
                    source_url=target.source_url or target.download_url,
                )
                if duplicate_outcome is not None:
                    return duplicate_outcome
                target_path = build_incoming_pdf_path(
                    self.settings.incoming_pdf_dir,
                    title=target.title or candidate.title,
                    doi=effective_doi,
                )
            else:
                title_match = self.check_duplicate_title_target(
                    candidate,
                    target=target,
                    method=target.method,
                    source_url=target.source_url or target.download_url,
                )
                if title_match is not None:
                    return AssistedIncomingDownloadOutcome(
                        candidate=candidate,
                        status="already_in_library" if title_match.local_pdf_path else "already_in_incoming",
                        method=target.method,
                        path=title_match.local_pdf_path,
                        source_url=target.source_url or target.download_url,
                    )
                target_path = build_non_doi_incoming_pdf_path(
                    self.settings.incoming_pdf_dir,
                    candidate=candidate,
                    target=target,
                )
                if target_path.exists() and target_path.stat().st_size > 0:
                    return AssistedIncomingDownloadOutcome(
                        candidate=candidate,
                        status="already_in_incoming",
                        method=target.method,
                        path=target_path,
                        source_url=target.source_url or target.download_url,
                    )
            try:
                downloaded = self.download_target(target, target_path=target_path)
            except AssistedDownloadError as exc:
                last_error = str(exc)
                if target.method == "cnki_browser":
                    saw_login_required = "login" in str(exc).lower() or "captcha" in str(exc).lower()
                continue

            if effective_doi:
                existing_incoming_dois.add(effective_doi)
            return AssistedIncomingDownloadOutcome(
                candidate=candidate,
                status="downloaded" if downloaded else "already_in_incoming",
                method=target.method,
                path=target_path,
                source_url=target.source_url or target.download_url,
                downloaded=downloaded,
            )

        return AssistedIncomingDownloadOutcome(
            candidate=candidate,
            status="login_required" if saw_login_required else "failed",
            message=last_error or "all assisted download routes failed",
        )

    def check_duplicate_target(
        self,
        candidate: AssistedIncomingCandidate,
        *,
        doi: str,
        existing_incoming_dois: set[str],
        method: str,
        source_url: str | None,
    ) -> AssistedIncomingDownloadOutcome | None:
        if doi in existing_incoming_dois:
            return AssistedIncomingDownloadOutcome(
                candidate=candidate,
                status="already_in_incoming",
                method=method,
                source_url=source_url,
            )
        existing_row = self.store.get_paper_by_doi(doi)
        if existing_row and existing_row.get("local_pdf_path"):
            local_pdf_path = Path(str(existing_row["local_pdf_path"]))
            if local_pdf_path.exists():
                return AssistedIncomingDownloadOutcome(
                    candidate=candidate,
                    status="already_in_library",
                    method=method,
                    path=local_pdf_path,
                    source_url=source_url,
                )
        return None

    def check_duplicate_title_target(
        self,
        candidate: AssistedIncomingCandidate,
        *,
        target: AssistedDownloadTarget,
        method: str,
        source_url: str | None,
    ) -> TitleFingerprintMatch | None:
        signature = build_title_signature(
            title=target.title or candidate.title,
            source_title=candidate.source_title or candidate.journal_short_name,
            year=candidate.year,
            volume=candidate.volume,
            issue=candidate.issue,
            pages=candidate.pages,
        )
        library_map = self.get_library_title_fingerprint_map()
        if signature in library_map:
            return TitleFingerprintMatch(signature=signature, local_pdf_path=library_map[signature])
        alt_path = build_non_doi_incoming_pdf_path(
            self.settings.incoming_pdf_dir,
            candidate=candidate,
            target=target,
        )
        if alt_path.exists() and alt_path.stat().st_size > 0:
            return TitleFingerprintMatch(signature=signature, local_pdf_path=None)
        return None

    def resolve_download_targets(
        self,
        candidate: AssistedIncomingCandidate,
    ) -> list[AssistedDownloadTarget]:
        targets: list[AssistedDownloadTarget] = []
        if is_pcsee_journal(candidate.journal_short_name, candidate.source_title) and candidate.doi:
            target = self.try_resolve_target(lambda: self.resolve_csee_target(candidate))
            if target is not None:
                targets.append(target)
        if is_aeps_journal(candidate.journal_short_name, candidate.source_title):
            target = self.try_resolve_target(lambda: self.resolve_aeps_target(candidate))
            if target is not None:
                targets.append(target)
        if should_use_pst_official_route(candidate):
            target = self.try_resolve_target(lambda: self.resolve_pst_target(candidate))
            if target is not None:
                targets.append(target)
        if is_ieee_assisted_journal(candidate.journal_short_name, candidate.source_title):
            target = self.try_resolve_target(lambda: self.resolve_ieee_target(candidate))
            if target is not None:
                targets.append(target)
        if is_elsevier_assisted_journal(candidate.journal_short_name, candidate.source_title):
            target = self.try_resolve_target(lambda: self.resolve_elsevier_target(candidate))
            if target is not None:
                targets.append(target)
        if resolve_cnki_short_name(candidate) is not None:
            target = self.try_resolve_target(lambda: self.resolve_cnki_target(candidate))
            if target is not None:
                targets.append(target)
        return targets

    def try_resolve_target(
        self,
        resolver,
    ) -> AssistedDownloadTarget | None:  # noqa: ANN001
        try:
            return resolver()
        except (AssistedDownloadError, requests.RequestException, Exception):
            return None

    def resolve_csee_target(
        self,
        candidate: AssistedIncomingCandidate,
    ) -> AssistedDownloadTarget | None:
        response = self.session.get(
            CSEE_GET_ARTICLE_BY_DOI_URL,
            params={"doi": candidate.doi},
            headers=build_csee_headers(),
            timeout=self.settings.request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        article = payload.get("data") or {}
        article_id = article.get("id")
        if not article_id:
            return None
        site_id = string_or_none(article.get("siteId")) or "964"
        attach_response = self.session.get(
            CSEE_GET_ATTACH_TYPES_URL,
            params={"id": article_id},
            headers=build_csee_headers(site_id=site_id),
            timeout=self.settings.request_timeout,
        )
        attach_response.raise_for_status()
        attach_payload = attach_response.json()
        attach_type = choose_csee_attach_type(attach_payload.get("data") or [])
        if not attach_type:
            return None
        download_url = (
            f"{CSEE_DOWNLOAD_URL}?id={article_id}&attachType={requests.utils.quote(attach_type)}"
        )
        resolved_doi = normalize_doi(article.get("doi")) or candidate.doi
        article_url = (
            f"https://epjournal.csee.org.cn/zh/article/doi/{resolved_doi}/"
            if resolved_doi
            else None
        )
        return AssistedDownloadTarget(
            method="csee_portal",
            title=string_or_none(article.get("resName")) or candidate.title,
            doi=resolved_doi,
            download_url=download_url,
            source_url=article_url,
            referer=article_url,
        )

    def resolve_aeps_target(
        self,
        candidate: AssistedIncomingCandidate,
    ) -> AssistedDownloadTarget | None:
        issue_url = build_aeps_issue_url(candidate.year, candidate.volume, candidate.issue)
        if issue_url is None:
            return None
        cache_key = (
            candidate.year or 0,
            normalize_issue_folder_value(candidate.volume),
            normalize_issue_folder_value(candidate.issue),
        )
        articles = self._aeps_issue_cache.get(cache_key)
        if articles is None:
            articles = self.load_aeps_issue_articles(issue_url)
            self._aeps_issue_cache[cache_key] = articles
        article = select_issue_page_article(
            articles,
            title=candidate.title,
            doi=candidate.doi,
        )
        if article is None or not article.download_url:
            return None
        return AssistedDownloadTarget(
            method="aeps_official",
            title=article.title,
            doi=article.doi or candidate.doi,
            download_url=article.download_url,
            source_url=article.article_url or issue_url,
            referer=issue_url,
        )

    def load_aeps_issue_articles(self, issue_url: str) -> list[IssuePageArticle]:
        try:
            response = self.session.get(
                issue_url,
                headers=build_browser_like_headers(referer="http://aeps-info.com/aeps/home"),
                timeout=self.settings.request_timeout,
            )
            response.raise_for_status()
            articles = parse_aeps_issue_articles(response.text, base_url=issue_url)
            if articles:
                return articles
        except requests.RequestException:
            pass
        return self.get_official_browser_session().load_aeps_issue_articles(issue_url)

    def resolve_cnki_target(
        self,
        candidate: AssistedIncomingCandidate,
    ) -> AssistedDownloadTarget | None:
        if candidate.year is None or not candidate.issue:
            return None
        short_name = resolve_cnki_short_name(candidate)
        if short_name is None:
            return None
        cache_key = (short_name, candidate.year, candidate.issue)
        cached = self._cnki_issue_cache.get(cache_key)
        if cached is None:
            spec = JournalSpec.model_validate(
                {
                    "short_name": short_name,
                    "title": candidate.source_title or candidate.journal_short_name,
                    "providers": ["cnki_navi"],
                }
            )
            detail_url = resolve_cnki_navi_detail_url(spec)
            context = self._cnki_context_cache.get(short_name)
            if context is None:
                context = self.catalog_service.fetch_cnki_navi_context(detail_url)
                self._cnki_context_cache[short_name] = context
            year_list_html = self.catalog_service.fetch_cnki_navi_year_list_html(context)
            refs = parse_cnki_navi_issue_refs(
                year_list_html,
                from_year=candidate.year,
                until_year=candidate.year,
            )
            issue_ref = next(
                (
                    item
                    for item in refs
                    if normalize_issue_folder_value(item.issue)
                    == normalize_issue_folder_value(candidate.issue)
                ),
                None,
            )
            if issue_ref is None:
                return None
            issue_html = self.catalog_service.fetch_cnki_navi_issue_html(
                context,
                year_issue_token=issue_ref.year_issue_token,
            )
            cached = (issue_html, context)
            self._cnki_issue_cache[cache_key] = cached
        issue_html, context = cached
        articles = parse_cnki_issue_articles(issue_html, base_url=context.base_url)
        article = select_issue_page_article(
            articles,
            title=candidate.title,
            doi=candidate.doi,
        )
        if article is None or not article.download_url:
            return None
        return AssistedDownloadTarget(
            method="cnki_browser",
            title=article.title,
            doi=article.doi or candidate.doi,
            download_url=article.download_url,
            source_url=article.article_url,
            referer=context.detail_url,
        )

    def resolve_pst_target(
        self,
        candidate: AssistedIncomingCandidate,
    ) -> AssistedDownloadTarget | None:
        issue_url = build_pst_issue_url(candidate.year, candidate.volume, candidate.issue)
        if issue_url is None:
            return None
        article = None
        try:
            browser_session = self.get_official_browser_session()
            articles = browser_session.load_pst_issue_articles(issue_url)
            article = select_issue_page_article(
                articles,
                title=candidate.title,
                doi=candidate.doi,
            )
        except Exception:
            article = None
        resolved_title = article.title if article is not None else candidate.title
        resolved_doi = (article.doi if article is not None else None) or candidate.doi
        source_url = (
            article.article_url
            if article is not None and article.article_url
            else build_pst_article_url(resolved_doi) or issue_url
        )
        if not resolved_title:
            return None
        return AssistedDownloadTarget(
            method="pst_official_browser",
            title=resolved_title,
            doi=resolved_doi,
            download_url=issue_url,
            source_url=source_url,
            referer=issue_url,
        )

    def resolve_ieee_target(
        self,
        candidate: AssistedIncomingCandidate,
    ) -> AssistedDownloadTarget | None:
        landing_url = string_or_none(candidate.publisher_url)
        if candidate.doi and (landing_url is None or "doi.org" in landing_url):
            landing_url = f"https://doi.org/{candidate.doi}"
        if not landing_url:
            return None
        return AssistedDownloadTarget(
            method="ieee_xplore_browser",
            title=candidate.title,
            doi=candidate.doi,
            download_url=landing_url,
            source_url=landing_url,
            referer=landing_url,
        )

    def resolve_elsevier_target(
        self,
        candidate: AssistedIncomingCandidate,
    ) -> AssistedDownloadTarget | None:
        landing_url = string_or_none(candidate.publisher_url)
        if candidate.doi and (
            landing_url is None
            or "doi.org/" in landing_url.casefold()
            or "dx.doi.org/" in landing_url.casefold()
        ):
            landing_url = f"https://doi.org/{candidate.doi}"
        if not landing_url:
            return None
        return AssistedDownloadTarget(
            method="elsevier_sciencedirect_browser",
            title=candidate.title,
            doi=candidate.doi,
            download_url=landing_url,
            source_url=landing_url,
            referer=landing_url,
        )

    def download_target(self, target: AssistedDownloadTarget, *, target_path: Path) -> bool:
        if target_path.exists() and target_path.stat().st_size > 0:
            return False
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target.method == "pst_official_browser":
            issue_url = target.referer or target.download_url
            self.get_official_browser_session().download_pst_pdf(
                issue_url=issue_url,
                article_url=target.source_url,
                title=target.title,
                doi=target.doi,
                target_path=target_path,
            )
            return True
        if target.method == "ieee_xplore_browser":
            self.get_official_browser_session().download_ieee_pdf(
                landing_url=target.referer or target.download_url,
                title=target.title,
                doi=target.doi,
                target_path=target_path,
            )
            return True
        if target.method == "elsevier_sciencedirect_browser":
            self.get_official_browser_session().download_elsevier_pdf(
                landing_url=target.referer or target.download_url,
                title=target.title,
                doi=target.doi,
                target_path=target_path,
            )
            return True
        if target.method == "cnki_browser":
            browser_session = self.get_browser_cookie_session()
            if browser_session is None:
                self.get_official_browser_session().download_cnki_pdf(
                    detail_url=target.referer,
                    article_url=target.source_url,
                    download_url=target.download_url,
                    target_path=target_path,
                )
                return True
            request_session = browser_session.session
        else:
            request_session = self.session
        if target.referer:
            try:
                request_session.get(
                    target.referer,
                    headers=build_browser_like_headers(referer=target.referer),
                    timeout=self.settings.request_timeout,
                )
            except requests.RequestException:
                pass
        response = request_session.get(
            target.download_url,
            headers=build_download_headers(target.referer),
            timeout=max(self.settings.request_timeout, 60.0),
            allow_redirects=True,
        )
        if response.status_code >= 400:
            raise AssistedDownloadError(
                f"{target.method} download failed with HTTP {response.status_code}: {target.download_url}"
            )
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "pdf" not in content_type and not response.content.startswith(b"%PDF"):
            response_text = response.text[:500] if response.text else ""
            if "login" in response.url.lower() or "captcha" in response_text.lower():
                raise AssistedDownloadError("login or captcha is still required for the selected route")
            raise AssistedDownloadError(
                f"{target.method} route returned non-PDF content from {response.url}"
            )
        target_path.write_bytes(response.content)
        return True

    def get_browser_cookie_session(self) -> BrowserCookieSession | None:
        if self._browser_cookie_session is not None:
            return self._browser_cookie_session
        browser_session = load_best_chromium_cookie_session(self.settings)
        if browser_session is not None:
            self._browser_cookie_session = browser_session
        return browser_session

    def get_official_browser_session(self) -> OfficialBrowserDownloadSession:
        if self._official_browser_session is None:
            self._official_browser_session = OfficialBrowserDownloadSession(self.settings)
        return self._official_browser_session

    def get_library_title_fingerprint_map(self) -> dict[str, Path | None]:
        if self._library_title_fingerprint_map is not None:
            return self._library_title_fingerprint_map
        mapping: dict[str, Path | None] = {}
        for record in self.store.load_paper_records(limit=50000, unresolved_only=False):
            if not record.local_pdf_path or not Path(record.local_pdf_path).exists():
                continue
            signature = build_title_signature(
                title=record.title,
                source_title=record.source_title,
                year=record.year,
                volume=record.volume,
                issue=record.issue,
                pages=record.pages,
            )
            local_pdf_path = Path(record.local_pdf_path)
            if signature not in mapping:
                mapping[signature] = local_pdf_path
        self._library_title_fingerprint_map = mapping
        return mapping


def parse_aeps_issue_articles(html: str, *, base_url: str) -> list[IssuePageArticle]:
    soup = BeautifulSoup(html, "html.parser")
    articles: list[IssuePageArticle] = []
    for node in soup.select("li.article_line"):
        title_link = node.select_one(".article_title a")
        title = clean_text(title_link.get_text(" ", strip=True) if title_link else None)
        if not title:
            continue
        doi_link = node.select_one(".article_position a[href*='doi.org']")
        pdf_link = node.select_one("a.btn_pdf")
        articles.append(
            IssuePageArticle(
                title=title,
                doi=normalize_doi(doi_link.get_text(" ", strip=True) if doi_link else None),
                download_url=resolve_issue_relative_url(base_url, pdf_link.get("href")) if pdf_link else None,
                article_url=resolve_issue_relative_url(base_url, title_link.get("href")) if title_link else None,
            )
        )
    return articles


def parse_pst_issue_articles(html: str, *, base_url: str) -> list[IssuePageArticle]:
    soup = BeautifulSoup(html, "html.parser")
    articles: list[IssuePageArticle] = []
    for node in soup.select("ul.article-list > li"):
        title_link = node.select_one(".j-title-1 a")
        title = clean_text(title_link.get_text(" ", strip=True) if title_link else None)
        if not title:
            continue
        doi_link = node.select_one("a.j-doi")
        pdf_link = node.select_one("a.j-pdf")
        articles.append(
            IssuePageArticle(
                title=title,
                doi=normalize_doi(doi_link.get_text(" ", strip=True) if doi_link else None),
                download_url=resolve_issue_relative_url(base_url, pdf_link.get("href")) if pdf_link else None,
                article_url=resolve_issue_relative_url(base_url, title_link.get("href")) if title_link else None,
            )
        )
    return articles


def parse_cnki_issue_articles(html: str, *, base_url: str) -> list[IssuePageArticle]:
    soup = BeautifulSoup(html, "html.parser")
    articles: list[IssuePageArticle] = []
    for node in soup.select("dd.row"):
        title_link = node.select_one("span.name a")
        title = clean_text(title_link.get_text(" ", strip=True) if title_link else None)
        if not title:
            continue
        download_link = None
        best_download_score = -1
        for anchor in node.find_all("a", href=True):
            href = anchor.get("href") or ""
            text = clean_text(anchor.get_text(" ", strip=True)) or ""
            onclick = anchor.get("onclick") or ""
            score = score_cnki_article_download_candidate(
                href=href.casefold(),
                label=text.casefold(),
                onclick=onclick.casefold(),
            )
            if score is None:
                continue
            if score > best_download_score:
                download_link = anchor
                best_download_score = score
        articles.append(
            IssuePageArticle(
                title=title,
                doi=None,
                download_url=resolve_issue_relative_url(base_url, download_link.get("href")) if download_link else None,
                article_url=resolve_issue_relative_url(base_url, title_link.get("href")) if title_link else None,
            )
        )
    return articles


def select_issue_page_article(
    articles: list[IssuePageArticle],
    *,
    title: str,
    doi: str | None,
) -> IssuePageArticle | None:
    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        exact_doi = next((item for item in articles if item.doi == normalized_doi), None)
        if exact_doi is not None:
            return exact_doi
    normalized_title = normalize_title_lookup_key(title)
    matches = [item for item in articles if normalize_title_lookup_key(item.title) == normalized_title]
    if len(matches) == 1:
        return matches[0]
    return None


def choose_csee_attach_type(rows: list[dict]) -> str | None:
    preferred_codes = ("lowqualitypdf", "pdf")
    normalized_rows = [row for row in rows if isinstance(row, dict)]
    for code in preferred_codes:
        match = next((row for row in normalized_rows if string_or_none(row.get("code")) == code), None)
        if match is not None:
            return code
    for row in normalized_rows:
        code = string_or_none(row.get("code"))
        if code and "pdf" in code.casefold():
            return code
    return None


def build_incoming_pdf_path(target_dir: Path, *, title: str, doi: str) -> Path:
    stem = sanitize_filename(title or "paper")
    suffix = doi_to_suffix(doi)
    if suffix not in stem.casefold():
        stem = f"{stem}__{suffix}"
    return target_dir / f"{stem}.pdf"


def build_non_doi_incoming_pdf_path(
    target_dir: Path,
    *,
    candidate: AssistedIncomingCandidate,
    target: AssistedDownloadTarget,
) -> Path:
    stem = sanitize_filename(target.title or candidate.title or "paper")
    signature = build_title_signature(
        title=target.title or candidate.title,
        source_title=candidate.source_title or candidate.journal_short_name,
        year=candidate.year,
        volume=candidate.volume,
        issue=candidate.issue,
        pages=candidate.pages,
    )
    short_hash = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:10]
    suffix_parts = ["sig", short_hash]
    if candidate.year is not None:
        suffix_parts.insert(0, f"year-{candidate.year}")
    return target_dir / f"{stem}__{'-'.join(suffix_parts)}.pdf"


def build_candidate_fallback_key(candidate: AssistedIncomingCandidate) -> str:
    return "|".join(
        [
            normalize_journal_filter(candidate.journal_short_name),
            str(candidate.year or ""),
            normalize_issue_folder_value(candidate.volume),
            normalize_issue_folder_value(candidate.issue),
            normalize_title_lookup_key(candidate.title),
        ]
    )


def assisted_candidate_sort_key(candidate: AssistedIncomingCandidate) -> tuple[int, int, int, int, str]:
    return (
        assisted_candidate_priority(candidate),
        -(candidate.year or 0),
        -parse_numeric_token(candidate.volume),
        -parse_numeric_token(candidate.issue),
        normalize_title_lookup_key(candidate.title),
    )


def assisted_candidate_priority(candidate: AssistedIncomingCandidate) -> int:
    if is_pcsee_journal(candidate.journal_short_name, candidate.source_title):
        return 0 if candidate.doi else 3
    if should_use_pst_official_route(candidate):
        return 1 if candidate.doi else 2
    if is_ieee_assisted_journal(candidate.journal_short_name, candidate.source_title):
        return 1 if candidate.doi else 4
    if is_elsevier_assisted_journal(candidate.journal_short_name, candidate.source_title):
        return 1 if candidate.doi else 4
    if is_aeps_journal(candidate.journal_short_name, candidate.source_title):
        return 1 if candidate.doi else 4
    if candidate.provider == "cnki_navi":
        return 2 if candidate.doi else 5
    return 6


def build_title_signature(
    *,
    title: str | None,
    source_title: str | None,
    year: int | None,
    volume: str | None,
    issue: str | None,
    pages: str | None,
) -> str:
    return "|".join(
        [
            normalize_title_lookup_key(title or ""),
            normalize_journal_filter(source_title),
            str(year or ""),
            normalize_issue_folder_value(volume),
            normalize_issue_folder_value(issue),
            normalize_pages_value(pages),
        ]
    )


def build_csee_headers(*, site_id: str = "964") -> dict[str, str]:
    headers = build_browser_like_headers(referer="https://epjournal.csee.org.cn/zh/volumn/home")
    headers["siteId"] = site_id
    headers["language"] = "zh"
    return headers


def build_browser_like_headers(referer: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def build_download_headers(referer: str | None = None) -> dict[str, str]:
    headers = build_browser_like_headers(referer=referer)
    headers["Accept"] = "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8"
    return headers


def build_aeps_issue_url(year: int | None, volume: str | None, issue: str | None) -> str | None:
    if year is None or not issue:
        return None
    resolved_volume = volume or infer_official_volume("aeps", year)
    if not resolved_volume:
        return None
    issue_token = str(parse_numeric_token(issue))
    volume_token = str(parse_numeric_token(resolved_volume))
    return AEPS_ISSUE_URL_TEMPLATE.format(
        year=year,
        volume=volume_token,
        issue=issue_token,
    )


def build_pst_issue_url(year: int | None, volume: str | None, issue: str | None) -> str | None:
    if year is None or not issue:
        return None
    resolved_volume = volume or infer_official_volume("pst", year)
    if not resolved_volume:
        return None
    issue_token = str(parse_numeric_token(issue))
    volume_token = str(parse_numeric_token(resolved_volume))
    return PST_ISSUE_URL_TEMPLATE.format(
        year=year,
        volume=volume_token,
        issue=issue_token,
    )


def build_pst_article_url(doi: str | None) -> str | None:
    normalized_doi = normalize_doi(doi)
    if not normalized_doi:
        return None
    return f"http://ntps.epri.sgcc.com.cn/dwjs/CN/{normalized_doi}"


def infer_official_volume(journal_key: str, year: int | None) -> str | None:
    if year is None:
        return None
    base_year = OFFICIAL_VOLUME_BASE_YEAR.get(journal_key)
    if base_year is None or year < base_year:
        return None
    return str(year - base_year + 1)


def resolve_issue_relative_url(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith(("http://", "https://")):
        return normalize_legacy_http_url(href)
    if href.startswith("/"):
        return normalize_legacy_http_url(urljoin(base_url, href))
    if "://" in base_url:
        origin = base_url.split("/", 3)[:3]
        if href.startswith(("aeps/", "dwjs/", "djgcxb/")):
            return normalize_legacy_http_url("/".join(origin) + "/" + href)
    return normalize_legacy_http_url(urljoin(base_url, href))


def load_best_chromium_cookie_session(settings: Settings) -> BrowserCookieSession | None:
    best_session: BrowserCookieSession | None = None
    for browser_name, root in CHROMIUM_BROWSER_ROOTS:
        if not root.exists():
            continue
        local_state_path = root / "Local State"
        master_key = load_chromium_master_key(local_state_path)
        for profile_dir in iter_chromium_profiles(root):
            cookies_path = resolve_chromium_cookies_path(profile_dir)
            if cookies_path is None:
                continue
            cookie_rows = read_chromium_cookie_rows(cookies_path)
            if not cookie_rows:
                continue
            session = requests.Session()
            session.headers.update({"User-Agent": settings.user_agent})
            cookie_count = 0
            for row in cookie_rows:
                value = decrypt_chromium_cookie_value(row["value"], row["encrypted_value"], master_key)
                if not value:
                    continue
                session.cookies.set(
                    row["name"],
                    value,
                    domain=row["host_key"],
                    path=row["path"] or "/",
                    secure=bool(row["is_secure"]),
                )
                cookie_count += 1
            if cookie_count == 0:
                continue
            candidate = BrowserCookieSession(
                browser_name=browser_name,
                profile_name=profile_dir.name,
                session=session,
                cookie_count=cookie_count,
            )
            if best_session is None or candidate.cookie_count > best_session.cookie_count:
                best_session = candidate
    return best_session


def iter_chromium_profiles(root: Path) -> list[Path]:
    profiles: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "Default" or child.name.startswith("Profile "):
            profiles.append(child)
    return profiles


def resolve_chromium_cookies_path(profile_dir: Path) -> Path | None:
    for relative in (Path("Network") / "Cookies", Path("Cookies")):
        candidate = profile_dir / relative
        if candidate.exists():
            return candidate
    return None


def read_chromium_cookie_rows(cookies_path: Path) -> list[sqlite3.Row]:
    patterns = [f"%{item}%" for item in CNKI_COOKIE_HOST_KEYWORDS]
    query = (
        "SELECT host_key, name, value, encrypted_value, path, is_secure "
        "FROM cookies WHERE " + " OR ".join("host_key LIKE ?" for _ in patterns)
    )
    try:
        connection = sqlite3.connect(f"file:{cookies_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute(query, patterns))
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    with tempfile.TemporaryDirectory() as tmpdir:
        copied_path = Path(tmpdir) / "Cookies"
        try:
            with cookies_path.open("rb") as source_handle, copied_path.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
        except OSError:
            return []
        connection = sqlite3.connect(copied_path)
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute(query, patterns))
        except sqlite3.Error:
            return []
        finally:
            connection.close()


def load_chromium_master_key(local_state_path: Path) -> bytes | None:
    if not local_state_path.exists():
        return None
    payload = json.loads(local_state_path.read_text(encoding="utf-8"))
    os_crypt = payload.get("os_crypt") or {}
    encrypted_key_b64 = os_crypt.get("encrypted_key")
    if not encrypted_key_b64:
        return None
    encrypted_key = base64.b64decode(encrypted_key_b64)
    if encrypted_key.startswith(b"DPAPI"):
        encrypted_key = encrypted_key[5:]
    try:
        return dpapi_decrypt(encrypted_key)
    except OSError:
        return None


def decrypt_chromium_cookie_value(
    value: str,
    encrypted_value: bytes,
    master_key: bytes | None,
) -> str | None:
    if value:
        return string_or_none(value)
    if not encrypted_value:
        return None
    if encrypted_value.startswith((b"v10", b"v11", b"v20")):
        if master_key is None:
            return None
        nonce = encrypted_value[3:15]
        ciphertext = encrypted_value[15:]
        try:
            plaintext = AESGCM(master_key).decrypt(nonce, ciphertext, None)
        except Exception:
            return None
        return string_or_none(plaintext.decode("utf-8", errors="ignore"))
    try:
        plaintext = dpapi_decrypt(encrypted_value)
    except OSError:
        return None
    return string_or_none(plaintext.decode("utf-8", errors="ignore"))


class DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def dpapi_decrypt(ciphertext: bytes) -> bytes:
    if not ciphertext:
        return b""
    buffer = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    blob_in = DataBlob(len(ciphertext), buffer)
    blob_out = DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def matches_journal_filter(
    *,
    journal_short_name: str | None,
    source_title: str | None,
    journal_filters: set[str],
) -> bool:
    aliases = {
        normalize_journal_filter(journal_short_name),
        normalize_journal_filter(source_title),
    }
    if is_pcsee_journal(journal_short_name, source_title):
        aliases.add("pcsee")
    if is_aeps_journal(journal_short_name, source_title):
        aliases.add("aeps")
    if is_pst_journal(journal_short_name, source_title):
        aliases.add("pst")
    aliases.discard("")
    return any(alias in journal_filters for alias in aliases)


def normalize_journal_filter(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    normalized = normalized.removeprefix("doi:")
    normalized = normalized.strip()
    return normalized.lower() or None


def string_or_none(value) -> str | None:  # noqa: ANN001
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def parse_year(value) -> int | None:  # noqa: ANN001
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_numeric_token(value: str | None) -> int:
    if not value:
        return 0
    digits = "".join(char for char in str(value) if char.isdigit())
    if digits:
        return int(digits)
    try:
        return int(str(value))
    except ValueError:
        return 0


def normalize_issue_folder_value(value: str | None) -> str:
    if not value:
        return ""
    number = parse_numeric_token(value)
    if number:
        return f"{number:02d}"
    return value.strip().casefold()


def normalize_pages_value(value: str | None) -> str:
    if not value:
        return ""
    digits = "".join(char for char in value if char.isdigit() or char == "-")
    return digits or value.strip().casefold()


def load_selenium_bindings() -> dict[str, object]:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:
        raise AssistedDownloadError(
            "selenium is not installed; install the project dependencies before using official browser downloads"
        ) from exc
    return {
        "webdriver": webdriver,
        "ActionChains": ActionChains,
        "By": By,
        "TimeoutException": TimeoutException,
        "WebDriverWait": WebDriverWait,
    }


def resolve_cnki_short_name(candidate: AssistedIncomingCandidate) -> str | None:
    normalized = normalize_journal_filter(candidate.journal_short_name)
    normalized_title = normalize_journal_filter(candidate.source_title)
    if is_pcsee_journal(candidate.journal_short_name, candidate.source_title):
        return "pcsee"
    if normalized in {"aeps", "电力系统自动化"} or normalized_title in {
        "aeps",
        "automation of electric power systems",
        "电力系统自动化",
    }:
        return "aeps"
    if normalized in {"pst", "电网技术"} or normalized_title in {
        "pst",
        "power system technology",
        "电网技术",
    }:
        return "pst"
    return None


def build_cnki_start_url_candidates(start_url: str) -> list[str]:
    candidates: list[str] = []

    def add(url: str | None) -> None:
        normalized = string_or_none(url)
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(start_url)
    parsed = urlsplit(start_url)
    if parsed.scheme and parsed.netloc:
        add(urlunsplit((parsed.scheme, parsed.netloc, "/", "", "")))
        add(urlunsplit((parsed.scheme, parsed.netloc, "/knavi/detail", parsed.query, "")))
        add(urlunsplit((parsed.scheme, parsed.netloc, "/knavi/", "", "")))
    return candidates


def normalize_legacy_http_url(url: str | None) -> str | None:
    normalized = string_or_none(url)
    if not normalized:
        return None
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and hostname in LEGACY_HTTP_ONLY_HOSTS:
        return urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return normalized


def score_cnki_article_download_candidate(*, href: str, label: str, onclick: str) -> int | None:
    combined = " ".join(part for part in (href, label, onclick) if part)
    blocked_markers = (
        "software/xzydq",
        "/software/",
        "cajviewer",
        "readerdownload",
        "下载阅读器",
        "软件下载",
        "xzydq.htm",
        "xzydq",
    )
    if any(marker in combined for marker in blocked_markers):
        return None
    attachment_markers = (
        "附件",
        "附录",
        "补充",
        "补充材料",
        "支撑材料",
        "supplement",
        "supplementary",
        "appendix",
        "attachment",
        "supporting information",
        "supportinginformation",
    )
    if any(marker in combined for marker in attachment_markers):
        return None
    score = 0
    if "/bar/download/order" in combined or "/download/order" in combined:
        score += 60
    if "displaypdf=true" in combined:
        score += 80
    if "pdf全文" in label:
        score += 120
    if "pdf全文下载" in label:
        score += 120
    if "全文下载" in label:
        score += 100
    if "整本下载" in label:
        score += 100
    if "pdf" in label:
        score += 50
    if "全文" in label:
        score += 50
    if "整本" in label:
        score += 50
    if "下载" in label:
        score += 20
    if "pdf" in combined:
        score += 20
    if score <= 0 and "下载" in combined and ("全文" in combined or "pdf" in combined or "整本" in combined):
        score = 10
    return score if score > 0 else None


def is_cnki_article_download_candidate(*, href: str, label: str, onclick: str) -> bool:
    return score_cnki_article_download_candidate(href=href, label=label, onclick=onclick) is not None


def is_pcsee_journal(journal_short_name: str | None, source_title: str | None) -> bool:
    return normalize_journal_filter(journal_short_name) in {
        "pcsee",
        "中国电机工程学报",
    } or normalize_journal_filter(source_title) in {
        "proceedings of the csee",
        "中国电机工程学报",
    }


def is_ieee_assisted_journal(journal_short_name: str | None, source_title: str | None) -> bool:
    normalized_short_name = normalize_journal_filter(journal_short_name)
    normalized_source_title = normalize_journal_filter(source_title)
    return normalized_short_name.startswith("ieee_") or normalized_source_title.startswith("ieee ")


def is_elsevier_assisted_journal(journal_short_name: str | None, source_title: str | None) -> bool:
    normalized_short_name = normalize_journal_filter(journal_short_name)
    normalized_source_title = normalize_journal_filter(source_title)
    return normalized_short_name in {
        "applied_energy",
        "energy",
    } or normalized_source_title in {
        "applied energy",
        "energy",
    }


def is_aeps_journal(journal_short_name: str | None, source_title: str | None) -> bool:
    return normalize_journal_filter(journal_short_name) in {
        "aeps",
        "电力系统自动化",
    } or normalize_journal_filter(source_title) in {
        "automation of electric power systems",
        "电力系统自动化",
    }


def is_pst_journal(journal_short_name: str | None, source_title: str | None) -> bool:
    return normalize_journal_filter(journal_short_name) in {
        "pst",
        "电网技术",
    } or normalize_journal_filter(source_title) in {
        "power system technology",
        "电网技术",
    }


def should_use_pst_official_route(candidate: AssistedIncomingCandidate) -> bool:
    if not is_pst_journal(candidate.journal_short_name, candidate.source_title):
        return False
    if candidate.year is None:
        return False
    return candidate.year >= PST_OFFICIAL_MIN_YEAR
