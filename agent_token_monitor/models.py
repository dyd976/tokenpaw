from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class Usage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    fresh_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    context_window: int | None = None
    is_estimated: bool = False
    raw_usage: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCallData:
    external_call_id: str | None
    tool_name: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    input_size: int | None = None
    output_size: int | None = None
    estimated_tokens: int | None = None
    target: str | None = None
    file_path: str | None = None
    is_error: bool = False


@dataclass(slots=True)
class ContextItemData:
    item_type: str
    source: str | None = None
    token_count: int | None = None
    content_hash: str | None = None
    file_path: str | None = None
    tool_name: str | None = None
    mcp_server: str | None = None
    subagent_name: str | None = None
    is_estimated: bool = False


@dataclass(slots=True)
class NormalizedEvent:
    agent_name: str
    provider: str
    event_kind: str
    source_file: str
    line_number: int
    event_id: str
    timestamp: datetime | None
    session_id: str
    turn_id: str | None = None
    model: str | None = None
    version: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    git_repository: str | None = None
    prompt_text: str | None = None
    usage: Usage | None = None
    tool_call: ToolCallData | None = None
    context_items: list[ContextItemData] = field(default_factory=list)
    raw_type: str | None = None
    raw_event: dict[str, Any] | None = None
    unknown_reason: str | None = None

