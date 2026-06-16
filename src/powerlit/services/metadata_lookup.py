from __future__ import annotations

from pathlib import Path
from re import IGNORECASE, search, sub
from urllib.parse import quote

from pypdf import PdfReader

from powerlit.models import Author, DocumentType, PaperRecord
from powerlit.providers.base import ProviderError
from powerlit.providers.crossref import CrossrefProvider
from powerlit.services.library_layout import (
    doi_to_suffix,
    is_known_journal_doi,
    normalize_known_source_title,
    resolve_journal_short_name,
)
from powerlit.services.library_layout import (
    infer_source_title_from_doi as infer_known_source_title_from_doi,
)
from powerlit.settings import Settings


class MetadataLookupService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.crossref = CrossrefProvider(settings)

    def lookup_by_doi(
        self,
        doi: str,
        *,
        query_pack: str = "incoming_pdf_auto",
        pdf_path: Path | None = None,
        pdf_header_text: str | None = None,
    ) -> PaperRecord | None:
        normalized = doi.strip().lower()
        if not normalized:
            return None
        if self.settings.metadata_lookup_offline:
            fallback = build_fallback_record_from_pdf(
                pdf_path,
                normalized,
                query_pack,
                extracted_text=pdf_header_text,
            )
            return finalize_lookup_record(fallback, fallback_record=None)
        endpoint = f"{self.crossref.endpoint}/{quote(normalized, safe='')}"
        try:
            payload = self.crossref.get_json(endpoint)
        except ProviderError:
            fallback = build_fallback_record_from_pdf(
                pdf_path,
                normalized,
                query_pack,
                extracted_text=pdf_header_text,
            )
            return finalize_lookup_record(fallback, fallback_record=None)
        item = payload.get("message") or {}
        if not item:
            fallback = build_fallback_record_from_pdf(
                pdf_path,
                normalized,
                query_pack,
                extracted_text=pdf_header_text,
            )
            return finalize_lookup_record(fallback, fallback_record=None)
        record = self.crossref._parse_item(item, query_pack)  # noqa: SLF001
        fallback = build_fallback_record_from_pdf(
            pdf_path,
            normalized,
            query_pack,
            extracted_text=pdf_header_text,
        )
        return finalize_lookup_record(record, fallback_record=fallback)


def build_fallback_record_from_pdf(
    pdf_path: Path | None,
    doi: str,
    query_pack: str,
    *,
    extracted_text: str | None = None,
) -> PaperRecord | None:
    if extracted_text is None and pdf_path is not None and pdf_path.exists():
        extracted_text = extract_pdf_header_text(pdf_path)
    if extracted_text:
        record = build_fallback_record_from_text(extracted_text, doi=doi, query_pack=query_pack)
        if record is not None:
            return record
    if pdf_path is None or not pdf_path.exists():
        return None
    return build_fallback_record_from_filename(pdf_path, doi=doi, query_pack=query_pack)


def build_fallback_record_from_filename(
    pdf_path: Path,
    *,
    doi: str,
    query_pack: str,
) -> PaperRecord | None:
    title = extract_title_from_filename(pdf_path, doi=doi) or doi
    source_title = infer_known_source_title_from_doi(doi)
    year = infer_year_from_doi_or_filename(doi, pdf_path)
    return PaperRecord(
        title=title,
        authors=[],
        year=year,
        document_type=DocumentType.JOURNAL if is_known_journal_doi(doi) else DocumentType.UNKNOWN,
        source_title=normalize_known_source_title(source_title, doi=doi),
        doi=doi,
        query_pack=query_pack,
        source_providers=["pdf_filename_fallback"],
        raw={
            "metadata_fallback": "pdf_filename",
            "filename": pdf_path.name,
        },
    )


def extract_pdf_header_text(pdf_path: Path, *, max_pages: int = 2) -> str:
    try:
        reader = PdfReader(str(pdf_path))
    except Exception:  # pragma: no cover
        return ""

    chunks: list[str] = []
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # pragma: no cover
            continue
    return "\n".join(chunks)


def build_fallback_record_from_text(
    extracted_text: str,
    *,
    doi: str,
    query_pack: str,
) -> PaperRecord | None:
    normalized_text = normalize_extracted_text(extracted_text)
    if not normalized_text:
        return None

    lines = [line.strip() for line in normalized_text.splitlines() if line.strip()]
    if not lines:
        return None

    title, authors = extract_title_and_authors(lines)
    source_title = infer_known_source_title_from_doi(doi) or extract_source_title(
        normalized_text
    )
    volume = extract_volume(normalized_text)
    issue = extract_issue(normalized_text)
    year = extract_year(normalized_text)
    pages = extract_pages(normalized_text)

    if not title:
        title = doi

    return PaperRecord(
        title=title,
        authors=authors,
        year=year,
        document_type=DocumentType.JOURNAL,
        source_title=normalize_known_source_title(source_title, doi=doi),
        volume=volume,
        issue=issue,
        pages=pages,
        doi=doi,
        query_pack=query_pack,
        source_providers=["pdf_fallback"],
        raw={
            "metadata_fallback": "pdf_first_pages",
            "extracted_header_text": normalized_text[:4000],
        },
    )


def finalize_lookup_record(
    record: PaperRecord | None,
    *,
    fallback_record: PaperRecord | None,
) -> PaperRecord | None:
    if record is None:
        return fallback_record

    normalized_doi = record.doi
    record.source_title = normalize_known_source_title(record.source_title, doi=normalized_doi)
    if is_known_journal_doi(normalized_doi):
        record.document_type = DocumentType.JOURNAL

    if fallback_record is None:
        return record

    fallback_record.source_title = normalize_known_source_title(
        fallback_record.source_title,
        doi=fallback_record.doi,
    )
    if should_prefer_fallback_source_title(record, fallback_record):
        record.source_title = fallback_record.source_title

    if not record.volume:
        record.volume = fallback_record.volume
    if not record.issue:
        record.issue = fallback_record.issue
    if not record.year:
        record.year = fallback_record.year
    if record.published_date is None:
        record.published_date = fallback_record.published_date
    if not record.pages:
        record.pages = fallback_record.pages
    if not record.authors:
        record.authors = fallback_record.authors
    return record


def should_prefer_fallback_source_title(
    record: PaperRecord,
    fallback_record: PaperRecord,
) -> bool:
    fallback_source_title = fallback_record.source_title
    if not fallback_source_title:
        return False

    source_title = record.source_title
    if not source_title:
        return True
    if source_title == record.title:
        return True
    if resolve_journal_short_name(source_title) == "unknown_journal":
        return True
    return False


def normalize_extracted_text(value: str) -> str:
    collapsed_cjk = sub(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])", "", value)
    normalized_lines = [" ".join(line.split()) for line in collapsed_cjk.splitlines()]
    return "\n".join(line for line in normalized_lines if line).strip()


def infer_source_title_from_doi(doi: str) -> str | None:
    return infer_known_source_title_from_doi(doi)


def extract_title_from_filename(pdf_path: Path, *, doi: str) -> str | None:
    stem = pdf_path.stem
    doi_suffix = doi_to_suffix(doi)
    if "__" in stem:
        stem = stem.split("__", 1)[0]
    if doi_suffix:
        stem = sub(rf"(?:__|[-_\s]+)?{doi_suffix}$", "", stem, flags=IGNORECASE)
    stem = sub(r"\b10[-.]\d{4,9}[-._;()a-z0-9]+$", "", stem, flags=IGNORECASE)
    stem = sub(r"[_]+", " ", stem)
    stem = sub(r"\s*[-]+\s*", " ", stem)
    stem = sub(r"\s+", " ", stem).strip(" .-_")
    return stem or None


def infer_year_from_doi_or_filename(doi: str, pdf_path: Path) -> int | None:
    for candidate in (doi, pdf_path.stem):
        matched = search(r"(?:^|[^\d])((?:19|20)\d{2})(?:[^\d]|$)", candidate)
        if matched:
            return int(matched.group(1))
    return None


def extract_source_title(value: str) -> str | None:
    lower = value.lower()
    known_markers = (
        ("aeps-info.com", "电力系统自动化"),
        ("/aeps/", "电力系统自动化"),
        ("/dwjs/", "电网技术"),
        ("1000-3673.pst", "电网技术"),
        ("0258-8013.pcsee", "中国电机工程学报"),
        ("proceedings of the csee", "Proceedings of the CSEE"),
        ("journal of modern power systems and clean energy", "Journal of Modern Power Systems and Clean Energy"),
        ("ieee transactions on power systems", "IEEE Transactions on Power Systems"),
        ("ieee trans. power syst.", "IEEE Transactions on Power Systems"),
        ("power systems, ieee transactions on", "IEEE Transactions on Power Systems"),
        ("ieee transactions on smart grid", "IEEE Transactions on Smart Grid"),
        ("ieee trans. smart grid", "IEEE Transactions on Smart Grid"),
        ("smart grid, ieee transactions on", "IEEE Transactions on Smart Grid"),
        ("ieee transactions on power delivery", "IEEE Transactions on Power Delivery"),
        ("ieee trans. power delivery", "IEEE Transactions on Power Delivery"),
        ("power delivery, ieee transactions on", "IEEE Transactions on Power Delivery"),
        ("ieee transactions on sustainable energy", "IEEE Transactions on Sustainable Energy"),
        ("ieee trans. sustain. energy", "IEEE Transactions on Sustainable Energy"),
        ("sustainable energy, ieee transactions on", "IEEE Transactions on Sustainable Energy"),
        ("international journal of electrical power and energy systems", "International Journal of Electrical Power & Energy Systems"),
        ("international journal of electrical power & energy systems", "International Journal of Electrical Power & Energy Systems"),
    )
    for marker, title in known_markers:
        if marker in lower:
            return title

    first_lines = value.splitlines()[:4]
    for line in first_lines:
        cleaned = cleanup_metadata_text(line)
        if not cleaned or is_header_or_metadata_line(cleaned):
            continue
        lowered = cleaned.lower()
        if (
            "transactions on" in lowered
            or "journal of" in lowered
            or "ieee trans." in lowered
        ):
            return normalize_known_source_title(cleaned)
    return None


def extract_title_and_authors(lines: list[str]) -> tuple[str | None, list[Author]]:
    doi_index = next((index for index, line in enumerate(lines) if "doi" in line.lower()), -1)

    candidate_windows = [lines[:12]]
    if doi_index >= 0:
        candidate_windows.append(lines[doi_index + 1 : doi_index + 10])

    for window in candidate_windows:
        title, authors = extract_title_and_authors_from_window(window)
        if title:
            return title, authors

    fallback_lines = lines[doi_index + 1 :] if doi_index >= 0 else lines
    title_lines: list[str] = []
    authors: list[Author] = []
    for line in fallback_lines:
        cleaned = cleanup_metadata_text(line)
        if not cleaned:
            continue
        upper = cleaned.upper()
        if cleaned.startswith(("(", "（")) or upper.startswith("ABSTRACT") or cleaned.startswith("摘要"):
            break
        if title_lines and looks_like_author_line(cleaned):
            authors = parse_author_line(cleaned)
            break
        if is_title_line(cleaned):
            title_lines.append(cleaned)
            continue
        if title_lines:
            break

    title = "".join(title_lines).strip() or None
    return title, authors


def extract_title_and_authors_from_window(lines: list[str]) -> tuple[str | None, list[Author]]:
    title_lines: list[str] = []
    authors: list[Author] = []
    for line in lines:
        cleaned = cleanup_metadata_text(line)
        if not cleaned:
            continue
        if is_header_or_metadata_line(cleaned) or looks_like_institution_line(cleaned):
            if title_lines:
                break
            continue
        if title_lines and looks_like_author_line(cleaned):
            authors = parse_author_line(cleaned)
            break
        if is_title_line(cleaned):
            title_lines.append(cleaned)
            continue
        if title_lines:
            break

    title = "".join(title_lines).strip() or None
    if title and not authors:
        title, embedded_authors = split_embedded_author_tail(title)
        if embedded_authors:
            authors = embedded_authors
    return title, authors


def split_embedded_author_tail(value: str) -> tuple[str, list[Author]]:
    if count_cjk(value) < 8:
        return value, []
    tokens = value.split()
    if len(tokens) < 2:
        return split_compact_chinese_author_tail(value)

    tail: list[str] = []
    while tokens and looks_like_single_author_token(tokens[-1]):
        tail.insert(0, tokens.pop())
    if not tail or not tokens:
        return value, []

    title = " ".join(tokens).strip()
    if not title or not is_title_line(title):
        return split_compact_chinese_author_tail(value)
    return title, [Author(literal=item) for item in tail]


def looks_like_single_author_token(value: str) -> bool:
    cjk_count = count_cjk(value)
    if 2 <= cjk_count <= 4 and len(value) <= 6:
        return True
    return False


def split_compact_chinese_author_tail(value: str) -> tuple[str, list[Author]]:
    for length in (4, 3, 2):
        candidate = value[-length:]
        if not looks_like_compact_chinese_name(candidate):
            continue
        title = value[:-length].strip()
        if title and is_title_line(title):
            return title, [Author(literal=candidate)]
    return value, []


def looks_like_compact_chinese_name(value: str) -> bool:
    if len(value) < 2 or len(value) > 4:
        return False
    if any(not ("\u4e00" <= ch <= "\u9fff") for ch in value):
        return False
    common_surnames = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁刘"
    return value[0] in common_surnames


def is_title_line(value: str) -> bool:
    if is_header_or_metadata_line(value) or looks_like_institution_line(value):
        return False
    if "。" in value:
        return False
    if search(r"^\d+\s*[)\uFF09.]", value):
        return False
    if value.count("，") >= 3:
        return False
    if count_cjk(value) >= 8 and len(value) <= 80:
        return True
    if count_latin_letters(value) >= 20 and len(value) <= 180 and value.count(".") <= 1:
        return True
    return False


def looks_like_author_line(value: str) -> bool:
    if is_header_or_metadata_line(value) or looks_like_institution_line(value):
        return False
    parts = parse_author_candidates(value)
    if not parts or len(parts) > 8:
        return False
    blocked = ("大学", "学院", "研究", "实验室", "公司", "中心")
    return not any(any(mark in part for mark in blocked) for part in parts)


def parse_author_line(value: str) -> list[Author]:
    return [Author(literal=item) for item in parse_author_candidates(value)]


def parse_author_candidates(value: str) -> list[str]:
    normalized = sub(r"[、,，;；/·•]+", " ", value)
    parts = [cleanup_metadata_text(item) for item in normalized.split() if cleanup_metadata_text(item)]
    candidates: list[str] = []
    for part in parts:
        cjk_count = count_cjk(part)
        if 2 <= cjk_count <= 8 and len(part) <= 12:
            candidates.append(part)
            continue
        if count_latin_letters(part) >= 2 and len(part) <= 40:
            candidates.append(part)
    return candidates


def extract_volume(value: str) -> str | None:
    patterns = (
        r"\bVol\s*\.?\s*(\d+)\b",
        r"第\s*(\d+)\s*卷",
        r"V\s*(\d+)\b",
    )
    return extract_first_match(value, patterns)


def extract_issue(value: str) -> str | None:
    patterns = (
        r"\bNo\s*\.?\s*(\d+)\b",
        r"第\s*(\d+)\s*期",
        r"I\s*(\d+)\b",
    )
    return extract_first_match(value, patterns)


def extract_year(value: str) -> int | None:
    year_match = search(r"\b((?:19|20)\d{2})\b", value)
    if year_match:
        return int(year_match.group(1))
    return None


def extract_pages(value: str) -> str | None:
    article_match = search(r"\)\s*\d+\s*-\s*(\d{3,5})\s*-\s*(\d{1,3})", value)
    if article_match:
        start_page = int(article_match.group(1))
        page_count = int(article_match.group(2))
        end_page = start_page + max(page_count - 1, 0)
        return f"{start_page}-{end_page}" if end_page >= start_page else str(start_page)

    direct_range = search(r"\b(\d{3,5})\s*-\s*(\d{3,5})\b", value)
    if direct_range:
        return f"{direct_range.group(1)}-{direct_range.group(2)}"
    return None


def extract_first_match(value: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        matched = extract_match(value, pattern)
        if matched:
            return matched
    return None


def extract_match(value: str, pattern: str) -> str | None:
    match = search(pattern, value, flags=IGNORECASE)
    if not match:
        return None
    return cleanup_metadata_text(match.group(1))


def cleanup_metadata_text(value: str) -> str:
    return " ".join(value.replace("：", ":").split()).strip()


def count_cjk(value: str) -> int:
    return sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")


def count_latin_letters(value: str) -> int:
    return sum(1 for ch in value if "a" <= ch.lower() <= "z")


def is_header_or_metadata_line(value: str) -> bool:
    if looks_like_issue_header(value):
        return True
    lower = value.lower()
    markers = (
        "doi",
        "abstract",
        "keywords",
        "keyword",
        "摘要",
        "关键词",
        "收稿日期",
        "修回日期",
        "上网日期",
        "基金",
        "http",
        "www.",
        "aeps-info",
        "文章编号",
        "文献编号",
    )
    return any(marker in lower or marker in value for marker in markers)


def looks_like_issue_header(value: str) -> bool:
    lower = value.lower()
    if "vol" in lower and "no" in lower:
        return True
    if "卷" in value and "期" in value:
        return True
    if "年" in value and "月" in value and any(ch.isdigit() for ch in value):
        return True
    return False


def looks_like_institution_line(value: str) -> bool:
    if value.startswith(("(", "（")):
        return True
    lower = value.lower()
    markers = ("大学", "学院", "研究院", "实验室", "公司", "中心", "school", "university", "institute")
    return any(marker in value or marker in lower for marker in markers) and len(value) >= 8
