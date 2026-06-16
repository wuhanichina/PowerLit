from __future__ import annotations

from powerlit.models import Author, DocumentType, PaperRecord


def format_gbt_7714(record: PaperRecord) -> str:
    authors = format_authors(record.authors)
    title = record.title

    if record.document_type == DocumentType.CONFERENCE:
        source = record.source_title or "[会议名称缺失]"
        location = "[出版地不详]"
        publisher = record.publisher or "[出版者不详]"
        year = str(record.year or "[年份缺失]")
        pages = record.pages or record.article_number
        body = f"{authors}. {title}[C]//{source}. {location}: {publisher}, {year}"
        if pages:
            body += f": {pages}"
    else:
        source = record.source_title or "[刊名缺失]"
        year = str(record.year or "[年份缺失]")
        volume_issue = build_volume_issue(record.volume, record.issue)
        pages = record.pages or record.article_number
        body = f"{authors}. {title}[J]. {source}, {year}"
        if volume_issue:
            body += f", {volume_issue}"
        if pages:
            body += f": {pages}"

    if record.doi:
        body += f". DOI: {record.doi}"
    else:
        body += "."
    return body


def format_authors(authors: list[Author]) -> str:
    if not authors:
        return "[作者缺失]"

    formatted = [format_author(author) for author in authors[:3]]
    if len(authors) > 3:
        formatted.append("et al")
    return ", ".join(formatted)


def format_author(author: Author) -> str:
    if author.literal:
        literal = " ".join(author.literal.split())
        if contains_cjk(literal):
            return literal
        family, given = split_literal_name(literal)
        if family:
            initials = build_initials(given or "")
            if initials:
                return f"{family.upper()} {initials}"
            return family.upper()
        return literal

    family = (author.family or "").strip()
    given = (author.given or "").strip()
    initials = build_initials(given)
    if family and initials:
        return f"{family.upper()} {initials}"
    if family:
        return family.upper()
    if initials:
        return initials
    return "[作者缺失]"


def build_volume_issue(volume: str | None, issue: str | None) -> str:
    if volume and issue:
        return f"{volume}({issue})"
    if volume:
        return volume
    if issue:
        return f"({issue})"
    return ""


def build_initials(given: str) -> str:
    return " ".join(f"{part[0].upper()}" for part in given.replace("-", " ").split() if part)


def split_literal_name(literal: str) -> tuple[str | None, str | None]:
    if "," in literal:
        family, given = [part.strip() for part in literal.split(",", maxsplit=1)]
        return family or None, given or None

    parts = literal.split()
    if len(parts) >= 2:
        return parts[-1], " ".join(parts[:-1])
    return None, None


def contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)
