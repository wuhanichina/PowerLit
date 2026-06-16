from __future__ import annotations

from pathlib import Path

import yaml


def load_topics(path: Path = Path("config/topics.power-system.yml")) -> dict[str, dict[str, str]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload.get("topics", {})
