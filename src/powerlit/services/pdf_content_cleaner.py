from __future__ import annotations

from html import unescape
from re import IGNORECASE, compile, sub

from powerlit.services.library_layout import resolve_journal_short_name

REPEATED_NOISE_PATTERNS = (
    compile(r"^downloaded from .+ on .+$", IGNORECASE),
    compile(r"^digital object identifier\b.*$", IGNORECASE),
    compile(r"^doi:\s*10\.\d{4,9}/.+$", IGNORECASE),
    compile(r"^received\b.*$", IGNORECASE),
    compile(r"^(manuscript|article) received\b.*$", IGNORECASE),
    compile(r"^date of publication\b.*$", IGNORECASE),
    compile(r"^date of current version\b.*$", IGNORECASE),
    compile(r"^recommended by associate editor\b.*$", IGNORECASE),
    compile(r"^this work was supported\b.*$", IGNORECASE),
    compile(r"^personal use is permitted\b.*$", IGNORECASE),
    compile(r"^color versions of one or more\b.*$", IGNORECASE),
    compile(r"^for information on obtaining reprints\b.*$", IGNORECASE),
    compile(r"^\d+\s+of\s+\d+$", IGNORECASE),
    compile(r"^page\s+\d+$", IGNORECASE),
    compile(r"^\d+$"),
)

IEEE_NOISE_PATTERNS = (
    compile(r"^\d{3,5}\s*ieee\b.*$", IGNORECASE),
    compile(r"^\d{4}\s+ieee\b.*$", IGNORECASE),
    compile(r"^10\.\d{4,9}/tpwrs\..+$", IGNORECASE),
    compile(r"^10\.\d{4,9}/tsg\..+$", IGNORECASE),
    compile(r"^10\.\d{4,9}/tste\..+$", IGNORECASE),
    compile(r"^10\.\d{4,9}/tpwrd\..+$", IGNORECASE),
    compile(r"^this article has been accepted for publication.*$", IGNORECASE),
    compile(r"^current version published .*$", IGNORECASE),
    compile(r"^authorized licensed use limited to\b.*$", IGNORECASE),
)

MARKDOWN_TABLE_SEPARATOR_RE = compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
MARKDOWN_BULLET_RE = compile(r"^\s*[-*+]\s+")
MARKDOWN_HEADING_RE = compile(r"^\s{0,3}#{1,6}\s+")
MARKDOWN_LINK_RE = compile(r"\[([^\]]+)\]\([^)]+\)")
MARKDOWN_IMAGE_RE = compile(r"!\[[^\]]*]\([^)]+\)")
MARKDOWN_INLINE_CODE_RE = compile(r"`([^`]+)`")
PAGE_MARKER_RE = compile(r"^\[Page \d+\]$", IGNORECASE)
CODE_FENCE_RE = compile(r"^\s*```")
HEADING_RE = compile(r"^\s{0,3}#{1,6}\s+")
LIST_RE = compile(r"^\s{0,3}(?:[-*+]|\d+\.)\s+")
BLOCKQUOTE_RE = compile(r"^\s{0,3}>")
TABLE_RE = compile(r"^\s*\|.*\|\s*$")
MATH_BLOCK_RE = compile(r"^\s*(?:\$\$|\\\[|\\\]|\\begin\{|\\end\{)")
HORIZONTAL_RULE_RE = compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
MARKDOWN_IMAGE_LINE_RE = compile(r"^\s*!\[[^\]]*]\([^)]+\)\s*$")
HTML_IMAGE_LINE_RE = compile(r"^\s*<img\b[^>]*>\s*$", IGNORECASE)
AUTHOR_BIO_HEADING_PATTERNS = (
    compile(r"^\s*#{1,6}\s*(author biographies|biographies|about the authors)\s*$", IGNORECASE),
    compile(r"^\s*(author biographies|biographies|about the authors)\s*$", IGNORECASE),
)
EMPTY_TOKEN_RE = compile(r"\[empty\]", IGNORECASE)
NOISE_BLOCK_START_PATTERNS = (
    compile(r"^received\b.*$", IGNORECASE),
    compile(r"^(manuscript|article) received\b.*$", IGNORECASE),
    compile(r"^date of publication\b.*$", IGNORECASE),
    compile(r"^this work was supported\b.*$", IGNORECASE),
    compile(r"^color versions of one or more\b.*$", IGNORECASE),
    compile(r"^digital object identi", IGNORECASE),
    compile(r"^authorized licensed use limited to\b.*$", IGNORECASE),
    compile(r"^personal use is permitted\b.*$", IGNORECASE),
)
AFFILIATION_BLOCK_START_PATTERNS = (
    compile(r"^.+\bare with the\b.*$", IGNORECASE),
    compile(r"^.+\bis with the\b.*$", IGNORECASE),
    compile(r"^corresponding author\b.*$", IGNORECASE),
)
NOISE_BLOCK_CONTINUATION_PATTERNS = (
    compile(r"^paper no\.\b.*$", IGNORECASE),
    compile(r"^\(corresponding author:.*$", IGNORECASE),
    compile(r"^https?://doi\.org/10\.\d{4,9}/.+$", IGNORECASE),
    compile(r"^restrictions apply\.?$", IGNORECASE),
)


def clean_mineru_markdown(markdown_text: str, *, source_title: str | None) -> str:
    journal_key = resolve_journal_short_name(source_title)
    cleaned_lines: list[str] = []
    in_frontmatter = False

    for index, raw_line in enumerate(markdown_text.splitlines()):
        line = raw_line.rstrip()
        normalized = normalize_noise_text(line)

        if index == 0 and normalized == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if normalized == "---":
                in_frontmatter = False
            continue
        if should_drop_noise_line(normalized, journal_key=journal_key):
            continue
        cleaned_lines.append(line)

    return normalize_mineru_layout(cleaned_lines)


def clean_extracted_text(extracted_text: str, *, source_title: str | None) -> str:
    journal_key = resolve_journal_short_name(source_title)
    cleaned_lines: list[str] = []
    dropping_noise_block = False

    for raw_line in extracted_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        normalized = normalize_noise_text(line)

        if PAGE_MARKER_RE.match(stripped):
            dropping_noise_block = False
            cleaned_lines.append(stripped)
            continue

        if dropping_noise_block:
            if not normalized:
                dropping_noise_block = False
                continue
            if should_continue_noise_block(normalized, journal_key=journal_key):
                continue
            dropping_noise_block = False

        if not normalized:
            cleaned_lines.append("")
            continue
        if should_start_noise_block(normalized, journal_key=journal_key):
            dropping_noise_block = True
            continue
        if should_drop_noise_line(normalized, journal_key=journal_key):
            continue
        cleaned_lines.append(line)

    return collapse_blank_lines(cleaned_lines)


def clean_direct_pdf_markdown(markdown_text: str) -> str:
    cleaned_lines: list[str] = []
    dropping_author_bios = False

    for raw_line in markdown_text.splitlines():
        line = EMPTY_TOKEN_RE.sub("", raw_line).rstrip()
        normalized = normalize_noise_text(line)

        if dropping_author_bios:
            continue
        if any(pattern.match(normalized) for pattern in AUTHOR_BIO_HEADING_PATTERNS):
            dropping_author_bios = True
            continue
        if not normalized:
            cleaned_lines.append("")
            continue
        cleaned_lines.append(line)

    return collapse_blank_lines(cleaned_lines)


def should_drop_noise_line(line: str, *, journal_key: str) -> bool:
    if not line:
        return False
    if any(pattern.match(line) for pattern in REPEATED_NOISE_PATTERNS):
        return True
    if journal_key.startswith("ieee_") and any(
        pattern.match(line) for pattern in IEEE_NOISE_PATTERNS
    ):
        return True
    return False


def should_start_noise_block(line: str, *, journal_key: str) -> bool:
    if any(pattern.match(line) for pattern in NOISE_BLOCK_START_PATTERNS):
        return True
    if journal_key.startswith("ieee_") and any(
        pattern.match(line) for pattern in AFFILIATION_BLOCK_START_PATTERNS
    ):
        return True
    return False


def should_continue_noise_block(line: str, *, journal_key: str) -> bool:
    if should_drop_noise_line(line, journal_key=journal_key):
        return True
    if any(pattern.match(line) for pattern in NOISE_BLOCK_CONTINUATION_PATTERNS):
        return True
    if journal_key.startswith("ieee_") and (
        "(e-mail:" in line.lower() or "doi.org/10." in line.lower()
    ):
        return True
    return False


def markdown_to_clean_text(markdown_text: str) -> str:
    lines: list[str] = []
    in_code_fence = False

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            lines.append(line)
            continue
        if MARKDOWN_TABLE_SEPARATOR_RE.match(line):
            continue

        line = MARKDOWN_IMAGE_RE.sub("", line)
        line = MARKDOWN_LINK_RE.sub(r"\1", line)
        line = MARKDOWN_INLINE_CODE_RE.sub(r"\1", line)
        line = MARKDOWN_HEADING_RE.sub("", line)
        line = MARKDOWN_BULLET_RE.sub("", line)
        line = line.replace("|", " ")
        line = " ".join(line.split())
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def normalize_noise_text(value: str) -> str:
    text = unescape(value.replace("\xa0", " "))
    text = sub(r"\s+", " ", text).strip()
    return text


def normalize_mineru_layout(lines: list[str]) -> str:
    output: list[str] = []
    paragraph: list[str] = []
    in_code_fence = False
    in_math_block = False

    def flush_paragraph() -> None:
        if not paragraph:
            return
        output.append(join_wrapped_paragraph(paragraph))
        paragraph.clear()

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if CODE_FENCE_RE.match(stripped):
            flush_paragraph()
            output.append(stripped)
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            output.append(line)
            continue

        if stripped == "$$":
            flush_paragraph()
            output.append(stripped)
            in_math_block = not in_math_block
            continue
        if in_math_block:
            output.append(line)
            continue

        if not stripped:
            flush_paragraph()
            output.append("")
            continue

        normalized_line = normalize_inline_markdown_spacing(stripped)
        if should_drop_markdown_image_line(normalized_line):
            flush_paragraph()
            continue
        if is_structural_markdown_line(normalized_line):
            flush_paragraph()
            output.append(normalized_line)
            continue
        paragraph.append(normalized_line)

    flush_paragraph()
    return collapse_blank_lines(output)


def is_structural_markdown_line(line: str) -> bool:
    return bool(
        HEADING_RE.match(line)
        or LIST_RE.match(line)
        or BLOCKQUOTE_RE.match(line)
        or TABLE_RE.match(line)
        or MATH_BLOCK_RE.match(line)
        or HORIZONTAL_RULE_RE.match(line)
        or line.startswith("![")
    )


def should_drop_markdown_image_line(line: str) -> bool:
    return bool(
        MARKDOWN_IMAGE_LINE_RE.match(line)
        or HTML_IMAGE_LINE_RE.match(line)
        or ".mineru.assets/" in line
        or "](images/" in line
        or 'src="images/' in line
        or "src='images/" in line
    )


def join_wrapped_paragraph(lines: list[str]) -> str:
    merged = ""
    for line in lines:
        chunk = line.strip()
        if not chunk:
            continue
        if not merged:
            merged = chunk
            continue
        if merged.endswith("-") and chunk[:1].isalnum():
            merged = merged[:-1] + chunk
            continue
        merged = f"{merged} {chunk}"
    return normalize_inline_markdown_spacing(merged)


def normalize_inline_markdown_spacing(value: str) -> str:
    text = unescape(value.replace("\xa0", " ")).strip()
    text = sub(r"[ \t]+", " ", text)
    text = sub(r"(?<=[A-Za-z0-9])\s+(?=[,.;:!?%])", "", text)
    text = sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，。；：！？、）】》％])", "", text)
    text = sub(r"(?<=[（【《])\s+(?=[\u4e00-\u9fff])", "", text)
    text = sub(r"(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z0-9])", "", text)
    text = sub(r"(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def collapse_blank_lines(lines: list[str]) -> str:
    output: list[str] = []
    blank_streak = 0
    for line in lines:
        stripped = line.rstrip()
        if not stripped:
            blank_streak += 1
            if blank_streak <= 1:
                output.append("")
            continue
        blank_streak = 0
        output.append(stripped)
    return "\n".join(output).strip() + "\n"
