from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from pathlib import Path
from re import IGNORECASE, search, sub

from powerlit.models import PaperRecord

JOURNAL_SHORT_NAMES = {
    "ieee transactions on smart grid": "ieee_tsg",
    "ieee transactions on power systems": "ieee_tpwrs",
    "ieee transactions on power delivery": "ieee_tpwrd",
    "ieee transactions on sustainable energy": "ieee_tste",
    "ieee access": "ieee_access",
    "applied energy": "applied_energy",
    "energy": "energy",
    "electric power systems research": "epsr",
    "international journal of electrical power & energy systems": "ijepes",
    "international journal of electrical power and energy systems": "ijepes",
    "journal of modern power systems and clean energy": "mpce",
    "mpce": "mpce",
    "proceedings of the csee": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "proceedings of the chinese society of electrical engineering": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "zhongguo dianji gongcheng xuebao": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "zhongguo dianji gongcheng xuebao proceedings of the chinese society of electrical engineering": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "automation of electric power systems": "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316",
    "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316": "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316",
    "power system technology": "\u7535\u7f51\u6280\u672f",
    "\u7535\u7f51\u6280\u672f": "\u7535\u7f51\u6280\u672f",
}
PREFERRED_SOURCE_TITLES = {
    "ieee transactions on smart grid": "IEEE Transactions on Smart Grid",
    "ieee transactions on power systems": "IEEE Transactions on Power Systems",
    "ieee transactions on power delivery": "IEEE Transactions on Power Delivery",
    "ieee transactions on sustainable energy": "IEEE Transactions on Sustainable Energy",
    "ieee access": "IEEE Access",
    "applied energy": "Applied Energy",
    "energy": "Energy",
    "electric power systems research": "Electric Power Systems Research",
    "international journal of electrical power & energy systems": (
        "International Journal of Electrical Power & Energy Systems"
    ),
    "international journal of electrical power and energy systems": (
        "International Journal of Electrical Power & Energy Systems"
    ),
    "journal of modern power systems and clean energy": (
        "Journal of Modern Power Systems and Clean Energy"
    ),
    "mpce": "Journal of Modern Power Systems and Clean Energy",
    "proceedings of the csee": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "proceedings of the chinese society of electrical engineering": (
        "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5"
    ),
    "zhongguo dianji gongcheng xuebao": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "zhongguo dianji gongcheng xuebao proceedings of the chinese society of electrical engineering": (
        "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5"
    ),
    "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "automation of electric power systems": "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316",
    "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316": "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316",
    "power system technology": "\u7535\u7f51\u6280\u672f",
    "\u7535\u7f51\u6280\u672f": "\u7535\u7f51\u6280\u672f",
}
SOURCE_TITLE_KEY_ALIASES = {
    "ieeetrpowersyst": "IEEE Transactions on Power Systems",
    "ieeetranpowersyst": "IEEE Transactions on Power Systems",
    "powersystemsieeetransactionson": "IEEE Transactions on Power Systems",
    "ieeetransactionsonpowersystemsieeepowerenergysociety": (
        "IEEE Transactions on Power Systems"
    ),
    "ieeetransactionsonpowersystemsieeepowerandenergysociety": (
        "IEEE Transactions on Power Systems"
    ),
    "ieeetrsmartgrid": "IEEE Transactions on Smart Grid",
    "ieeetransmartgrid": "IEEE Transactions on Smart Grid",
    "smartgridieeetransactionson": "IEEE Transactions on Smart Grid",
    "ieeetransactionsonsmartgridieeepowerenergysociety": (
        "IEEE Transactions on Smart Grid"
    ),
    "ieeetrpowerdelivery": "IEEE Transactions on Power Delivery",
    "ieeetranpowerdelivery": "IEEE Transactions on Power Delivery",
    "powerdeliveryieeetransactionson": "IEEE Transactions on Power Delivery",
    "ieeetrsustainenergy": "IEEE Transactions on Sustainable Energy",
    "ieeetransustainenergy": "IEEE Transactions on Sustainable Energy",
    "sustainableenergyieeetransactionson": "IEEE Transactions on Sustainable Energy",
    "internationaljournalofelectricalpowerandenergysystems": (
        "International Journal of Electrical Power & Energy Systems"
    ),
    "proceedingsofthecsee": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "proceedingsofthechinesesocietyofelectricalengineering": (
        "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5"
    ),
    "zhongguodianjigongchengxuebao": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "automationofelectricpowersystems": "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316",
    "powersystemtechnology": "\u7535\u7f51\u6280\u672f",
    "mpce": "Journal of Modern Power Systems and Clean Energy",
}
DOI_SOURCE_TITLE_PREFIXES = {
    "10.1109/tpwrs.": "IEEE Transactions on Power Systems",
    "10.1109/tsg.": "IEEE Transactions on Smart Grid",
    "10.1109/tpwrd.": "IEEE Transactions on Power Delivery",
    "10.1109/tste.": "IEEE Transactions on Sustainable Energy",
    "10.1016/j.apenergy.": "Applied Energy",
    "10.1016/j.energy.": "Energy",
    "10.1016/j.ijepes.": "International Journal of Electrical Power & Energy Systems",
    "10.35833/mpce.": "Journal of Modern Power Systems and Clean Energy",
    "10.13334/j.0258-8013.pcsee.": "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5",
    "10.13335/j.1000-3673.pst.": "\u7535\u7f51\u6280\u672f",
    "10.52783/pst.": "\u7535\u7f51\u6280\u672f",
    "10.7500/aeps": "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316",
}
ANNUAL_JOURNAL_VOLUME_START_YEAR = {
    "ieee_tpwrd": 1986,
    "ieee_tpwrs": 1986,
    "ieee_tsg": 2010,
    "ieee_tste": 2010,
    "\u4e2d\u56fd\u7535\u673a\u5de5\u7a0b\u5b66\u62a5": 1980,
    "\u7535\u529b\u7cfb\u7edf\u81ea\u52a8\u5316": 1977,
    "\u7535\u7f51\u6280\u672f": 1977,
}


@dataclass(frozen=True, slots=True)
class LibraryLocation:
    journal_short_name: str
    volume_folder: str
    issue_folder: str | None
    directory: Path


def build_library_location(
    base_dir: Path,
    *,
    source_title: str | None,
    volume: str | None,
    issue: str | None,
    year: int | None = None,
) -> LibraryLocation:
    journal_short_name = resolve_journal_short_name(source_title)
    effective_volume = volume or infer_annual_volume(source_title, year)
    volume_folder = format_volume_folder(effective_volume)
    issue_folder = format_issue_folder(issue)
    directory = base_dir / journal_short_name / volume_folder
    if issue_folder:
        directory /= issue_folder
    return LibraryLocation(
        journal_short_name=journal_short_name,
        volume_folder=volume_folder,
        issue_folder=issue_folder,
        directory=directory,
    )


def build_reference_pdf_path(
    base_dir: Path,
    *,
    title: str,
    doi: str | None,
    source_title: str | None,
    year: int | None,
    volume: str | None,
    issue: str | None,
    original_filename: str | None = None,
) -> Path:
    location = build_library_location(
        base_dir,
        source_title=source_title,
        volume=volume,
        issue=issue,
        year=year,
    )
    filename_source = title or (Path(original_filename).stem if original_filename else "paper")
    stem = sanitize_filename(filename_source)
    suffix = doi_to_suffix(doi) if doi else "unknown-doi"
    if suffix not in stem.lower():
        stem = f"{stem}__{suffix}"
    return location.directory / f"{stem}.pdf"


def build_parsed_output_base(base_dir: Path, record: PaperRecord) -> Path:
    location = build_library_location(
        base_dir,
        source_title=record.source_title,
        volume=record.volume,
        issue=record.issue,
        year=record.year,
    )
    return location.directory / build_record_stem(record)


def build_analysis_output_base(base_dir: Path, record: PaperRecord) -> Path:
    parsed_base = build_parsed_output_base(base_dir, record)
    return parsed_base.parent / f"{parsed_base.name}-analysis"


def build_card_output_base(base_dir: Path, record: PaperRecord) -> Path:
    parsed_base = build_parsed_output_base(base_dir, record)
    return parsed_base.parent / f"{parsed_base.name}-card"


def build_record_stem(record: PaperRecord) -> str:
    if record.doi:
        return doi_to_suffix(record.doi)
    year = record.year or "unknown"
    return sanitize_filename(f"{record.title}-{year}").replace(" ", "-").lower()


def resolve_journal_short_name(source_title: str | None) -> str:
    canonical_title = normalize_known_source_title(source_title)
    normalized = normalize_source_title(canonical_title)
    if normalized in JOURNAL_SHORT_NAMES:
        return JOURNAL_SHORT_NAMES[normalized]
    if not canonical_title:
        return "unknown_journal"
    if contains_cjk(canonical_title):
        return sanitize_folder_name(canonical_title)
    fallback = sub(r"[^a-z0-9]+", "_", canonical_title.lower()).strip("_")
    return fallback[:64] or "unknown_journal"


def normalize_source_title(value: str | None) -> str:
    canonical_title = canonicalize_source_title(value)
    if not canonical_title:
        return ""
    return " ".join(canonical_title.lower().split())


def normalize_source_title_key(value: str | None) -> str:
    canonical_title = canonicalize_source_title(value)
    if not canonical_title:
        return ""
    ascii_friendly = canonical_title.casefold().replace("&", "and")
    return "".join(ch for ch in ascii_friendly if ch.isalnum())


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    normalized = normalized.removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    normalized = normalized.removeprefix("doi:")
    normalized = normalized.strip()
    return normalized.lower() or None


def infer_source_title_from_doi(doi: str | None) -> str | None:
    normalized = normalize_doi(doi)
    if not normalized:
        return None
    for prefix, title in DOI_SOURCE_TITLE_PREFIXES.items():
        if normalized.startswith(prefix):
            return title
    return None


def normalize_known_source_title(
    value: str | None,
    *,
    doi: str | None = None,
) -> str | None:
    inferred_from_doi = infer_source_title_from_doi(doi)
    if inferred_from_doi:
        return inferred_from_doi

    canonical_title = canonicalize_source_title(value)
    normalized = normalize_source_title(canonical_title)
    if not normalized:
        return canonical_title
    if normalized in PREFERRED_SOURCE_TITLES:
        return PREFERRED_SOURCE_TITLES[normalized]
    normalized_key = normalize_source_title_key(canonical_title)
    if normalized_key in SOURCE_TITLE_KEY_ALIASES:
        return SOURCE_TITLE_KEY_ALIASES[normalized_key]
    return canonical_title


def is_known_journal_doi(doi: str | None) -> bool:
    return infer_source_title_from_doi(doi) is not None


def infer_annual_volume(source_title: str | None, year: int | None) -> str | None:
    if year is None:
        return None
    journal_key = resolve_journal_short_name(source_title)
    start_year = ANNUAL_JOURNAL_VOLUME_START_YEAR.get(journal_key)
    if start_year is None or year < start_year:
        return None
    return str(year - start_year + 1)


def canonicalize_source_title(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(unescape(value).split())
    text = strip_trailing_volume_noise(text)
    return text or None


def strip_trailing_volume_noise(value: str) -> str:
    if not value:
        return value
    stripped = value
    patterns = (
        r"\s*(?:V\s*o\s*l(?:\s*u\s*m\s*e)?\.?\s*\d+)(?:\s*(?:N\s*o|I\s*s\s*s\s*u\s*e)\.?\s*\d+)?\s*$",
        r"\s*(?:Volume\s+\d+)(?:\s+Issue\s+\d+)?\s*$",
        r"\s*第\s*\d+\s*卷(?:\s*第\s*\d+\s*期)?\s*$",
    )
    for pattern in patterns:
        stripped = sub(pattern, "", stripped, flags=IGNORECASE)
    return " ".join(stripped.split())


def format_volume_folder(volume: str | None) -> str:
    token = normalize_numeric_token(volume) or "00"
    return f"v{token}"


def format_issue_folder(issue: str | None) -> str | None:
    token = normalize_numeric_token(issue)
    if not token:
        return None
    return f"i{token}"


def normalize_numeric_token(value: str | None) -> str | None:
    if not value:
        return None
    match = search(r"\d+", value)
    if match:
        digits = match.group(0)
        return digits.zfill(2) if len(digits) < 2 else digits
    cleaned = sub(r"[^a-zA-Z0-9]+", "", value.lower())
    return cleaned or None


def sanitize_filename(value: str) -> str:
    normalized = sub(r"[<>:\"/\\\\|?*]+", " ", value)
    normalized = sub(r"\s+", " ", normalized).strip()
    normalized = normalized[:120].rstrip(" .")
    return normalized or "paper"


def sanitize_folder_name(value: str) -> str:
    normalized = sub(r"[<>:\"/\\\\|?*]+", " ", value)
    normalized = sub(r"\s+", " ", normalized).strip()
    normalized = normalized[:80].rstrip(" .")
    return normalized or "\u672a\u77e5\u671f\u520a"


def contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def doi_to_suffix(doi: str | None) -> str:
    if not doi:
        return "unknown-doi"
    return sub(r"[^a-zA-Z0-9]+", "-", doi.strip().lower()).strip("-")
