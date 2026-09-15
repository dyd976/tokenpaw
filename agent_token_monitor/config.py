from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load an optional local YAML/JSON configuration without making startup fragile."""
    raw_path = path or os.environ.get("TOKEN_MONITOR_CONFIG")
    if not raw_path:
        return {}
    config_path = Path(raw_path).expanduser()
    if not config_path.exists():
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
        if config_path.suffix.lower() == ".json":
            value = json.loads(text)
        else:
            try:
                import yaml
                value = yaml.safe_load(text)
            except ImportError:
                value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    return value if isinstance(value, dict) else {}


def collector_paths(config: dict[str, Any], name: str, default: list[str]) -> list[str]:
    value = section(section(config, "collectors"), name)
    if value.get("enabled") is False:
        return []
    paths = value.get("paths", default)
    if not isinstance(paths, list):
        return default
    return [str(item) for item in paths if item]


def privacy_options(config: dict[str, Any]) -> dict[str, bool]:
    value = section(config, "privacy")
    prompt_override = os.environ.get("TOKEN_MONITOR_STORE_PROMPT_CONTENT")
    store_prompt_content = (prompt_override.lower() in {"1", "true", "yes", "on"}
                            if prompt_override is not None
                            else bool(value.get("store_prompt_content", False)))
    return {
        "store_prompt_content": store_prompt_content,
        "store_tool_output": bool(value.get("store_tool_output", False)),
        "store_file_content": bool(value.get("store_file_content", False)),
        "mask_sensitive": bool(value.get("mask_sensitive_strings", True)),
    }


def alert_thresholds(config: dict[str, Any]):
    from agent_token_monitor.alerts import AlertThresholds

    value = section(config, "alerts")
    fields = set(AlertThresholds.__dataclass_fields__)
    return AlertThresholds(**{key: value[key] for key in fields if key in value})
