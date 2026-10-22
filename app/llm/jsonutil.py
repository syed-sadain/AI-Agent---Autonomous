"""Tolerant JSON extraction. Small open models love code fences and trailing commas."""
from __future__ import annotations

import json
import re
from typing import Any

from .base import LLMOutputError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def extract_json(text: str) -> Any:
    if not text or not text.strip():
        raise LLMOutputError("empty response")

    candidates: list[str] = [text.strip()]
    fence = _FENCE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    last_err: Exception | None = None
    for cand in candidates:
        for variant in (cand, _TRAILING_COMMA.sub(r"\1", cand)):
            try:
                return json.loads(variant)
            except json.JSONDecodeError as exc:
                last_err = exc
    raise LLMOutputError(f"invalid JSON: {last_err}")
