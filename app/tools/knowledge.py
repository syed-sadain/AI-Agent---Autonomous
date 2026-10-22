"""Tiny keyword-scored retrieval over a mock knowledge base (allowed by the brief)."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_WORD = re.compile(r"[a-z0-9]+")


class KnowledgeBase:
    def __init__(self, path: Path) -> None:
        self.entries: list[dict[str, Any]] = json.loads(Path(path).read_text(encoding="utf-8"))

    def search(self, query: str, k: int = 2) -> list[dict[str, Any]]:
        words = set(_WORD.findall(query.lower()))
        scored = []
        for entry in self.entries:
            score = len(words & set(entry["keywords"]))
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [e for _, e in scored[:k]]
