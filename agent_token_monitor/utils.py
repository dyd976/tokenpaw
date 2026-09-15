from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def stable_id(*parts: object) -> str:
    value = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        value = canonical_json(value)
    return max(1, (len(value) + 3) // 4)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, tz=timezone.utc)
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def expand_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def first_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
                    pieces.append(item["text"])
        return "\n".join(pieces)
    if isinstance(content, dict):
        return text_from_content([content])
    return ""


_SENSITIVE_PATTERNS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|secret|password|passwd|token)\b\s*[=:]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "[REDACTED]"),
)


def mask_sensitive_strings(value: str) -> str:
    """Mask common credentials before optional local content storage."""
    masked = value
    for pattern, replacement in _SENSITIVE_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


_RAW_EVENT_CONTENT_KEYS = {
    "content", "text", "prompt", "user_prompt", "input", "output", "arguments",
    "message", "messages", "delta", "transcript", "source_code",
}


def privacy_safe_raw_event(value: Any) -> Any:
    """Keep unknown-event structure while excluding likely transcript/code payloads.

    Unknown events are useful for diagnosing provider schema changes, but storing an
    entire future event would bypass the default prompt/tool/file privacy settings.
    Preserve keys and scalar metadata; omit common content-bearing fields.
    """
    if isinstance(value, dict):
        return {
            str(key): privacy_safe_raw_event(item)
            for key, item in value.items()
            if str(key).lower() not in _RAW_EVENT_CONTENT_KEYS
        }
    if isinstance(value, list):
        return [privacy_safe_raw_event(item) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 500:
            return value[:500] + "…"
        return value
    return str(value)
