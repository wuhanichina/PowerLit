from __future__ import annotations

from pathlib import Path

from powerlit.models import PaperRecord


def render_obsidian_note(
    record: PaperRecord,
    *,
    body: str,
) -> str:
    return ensure_markdown_title(body, record.title)


def ensure_markdown_title(value: str, title: str) -> str:
    body = value.strip()
    if body.startswith("---"):
        parts = body.split("\n---", 1)
        if len(parts) == 2:
            body = parts[1].strip()
    if not body:
        body = f"# {title}"
    if not body.startswith("# "):
        body = f"# {title}\n\n{body}"
    return body.rstrip() + "\n"


def obsidian_path(path: Path | None) -> str:
    if path is None:
        return "unknown"
    root = Path.cwd().resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.name


def escape_frontmatter(value: str) -> str:
    return value.replace('"', "'")
