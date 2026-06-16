from __future__ import annotations

from datetime import datetime
from pathlib import Path

from powerlit.models import JournalSpec, PaperRecord
from powerlit.services.export import write_markdown


def write_weekly_report(
    output_dir: Path,
    *,
    generated_at: datetime,
    journals: list[JournalSpec],
    new_records: dict[str, list[PaperRecord]],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{generated_at.strftime('%Y-%m-%d')}-weekly-journal-sync.md"
    path = output_dir / filename
    lines = [
        "# 本周新增论文报告",
        "",
        f"- 生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 监控期刊数：{len(journals)}",
        f"- 新增论文总数：{sum(len(items) for items in new_records.values())}",
        "",
    ]
    for journal in journals:
        records = new_records.get(journal.short_name, [])
        lines.extend(
            [
                f"## {journal.short_name} | {journal.title}",
                "",
                f"- 新增论文：{len(records)}",
                "",
            ]
        )
        if not records:
            lines.append("本次未发现新增论文。")
            lines.append("")
            continue
        for index, record in enumerate(sorted(records, key=sort_key), start=1):
            lines.extend(
                [
                    f"### {index}. {record.title}",
                    "",
                    f"- DOI: {record.doi or 'missing'}",
                    f"- 年份: {record.year or 'unknown'}",
                    f"- 卷期: {format_volume_issue(record.volume, record.issue)}",
                    f"- 出版商页面: {record.publisher_url or 'missing'}",
                    "",
                ]
            )
    write_markdown(path, "\n".join(lines).rstrip() + "\n")
    return path


def sort_key(record: PaperRecord) -> tuple[int, str]:
    return (-(record.year or 0), record.title.lower())


def format_volume_issue(volume: str | None, issue: str | None) -> str:
    if volume and issue:
        return f"v{volume}/i{issue}"
    if volume:
        return f"v{volume}"
    return "unknown"
