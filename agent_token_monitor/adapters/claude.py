from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from agent_token_monitor.adapters.base import AgentAdapter
from agent_token_monitor.models import ContextItemData, NormalizedEvent, ToolCallData, Usage
from agent_token_monitor.utils import content_hash, estimate_tokens, first_value, parse_timestamp, text_from_content


class ClaudeAdapter(AgentAdapter):
    name = "Claude Code"
    provider = "anthropic"

    def can_parse(self, path: Path, sample: Iterable[dict[str, Any]]) -> bool:
        events = list(sample)
        return any(event.get("type") in {"assistant", "user"} and ("sessionId" in event or "message" in event) for event in events)

    def detect_version(self, event: dict[str, Any]) -> str | None:
        return event.get("version") or event.get("message", {}).get("model")

    @staticmethod
    def _metadata(event: dict[str, Any]) -> tuple[str, str | None, str | None, str | None, str | None, str | None]:
        session_id = str(event.get("sessionId") or event.get("session_id") or "unknown-claude-session")
        return (
            session_id,
            event.get("cwd"),
            event.get("version"),
            event.get("gitBranch"),
            event.get("gitRepository") or event.get("repository"),
            event.get("timestamp"),
        )

    def parse_event(self, event: dict[str, Any], source_file: str, line_number: int, state: dict[str, Any]) -> list[NormalizedEvent]:
        event_type = event.get("type")
        session_id, cwd, version, branch, repository, timestamp_value = self._metadata(event)
        timestamp = parse_timestamp(timestamp_value)
        event_id = str(event.get("uuid") or event.get("requestId") or event.get("promptId") or f"line-{line_number}")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        result: list[NormalizedEvent] = []

        if event_type == "assistant" and isinstance(message.get("usage"), dict):
            usage_raw = message["usage"]
            raw_input = self._int(usage_raw.get("input_tokens"))
            cache_read = self._int(usage_raw.get("cache_read_input_tokens"))
            cache_creation = self._int(usage_raw.get("cache_creation_input_tokens"))
            usage = Usage(
                input_tokens=raw_input,
                cached_input_tokens=cache_read,
                fresh_input_tokens=(raw_input or 0) + (cache_creation or 0) if raw_input is not None or cache_creation is not None else None,
                output_tokens=self._int(usage_raw.get("output_tokens")),
                reasoning_tokens=self._int(usage_raw.get("reasoning_output_tokens")),
                raw_usage=usage_raw,
            )
            turn_id = str(state.pop("pending_prompt_turn_id", None) or event.get("requestId") or event_id)
            state["last_turn_id"] = turn_id
            context_items: list[ContextItemData] = []
            tool_blocks = [block for block in message.get("content", []) if isinstance(block, dict)] if isinstance(message.get("content"), list) else []
            for block in tool_blocks:
                if block.get("type") == "tool_use":
                    tool = self._tool_call(block, timestamp)
                    state.setdefault("tool_calls", {})[str(block.get("id") or block.get("name"))] = {
                        "name": tool.tool_name, "target": tool.target, "file_path": tool.file_path,
                    }
                    result.append(NormalizedEvent(
                        agent_name=self.name, provider=self.provider, event_kind="tool_call", source_file=source_file,
                        line_number=line_number, event_id=f"{event_id}:tool:{block.get('id') or block.get('name')}", timestamp=timestamp,
                        session_id=session_id, turn_id=turn_id, model=message.get("model"), version=version, cwd=cwd,
                        git_branch=branch, git_repository=repository, tool_call=tool, raw_type=event_type,
                    ))
            result.append(NormalizedEvent(
                agent_name=self.name, provider=self.provider, event_kind="turn_usage", source_file=source_file,
                line_number=line_number, event_id=event_id, timestamp=timestamp, session_id=session_id, turn_id=turn_id,
                model=message.get("model"), version=version, cwd=cwd, git_branch=branch, git_repository=repository,
                usage=usage, context_items=context_items, raw_type=event_type,
            ))
            return result

        if event_type == "user":
            prompt_id = event_id
            state["last_prompt_id"] = prompt_id
            prompt_text = text_from_content(message.get("content")) if message else ""
            tool_result_blocks = [block for block in message.get("content", []) if isinstance(block, dict)] if isinstance(message.get("content"), list) else []
            if any(block.get("type") == "tool_result" for block in tool_result_blocks):
                for block in tool_result_blocks:
                    if block.get("type") != "tool_result":
                        continue
                    tool_id = str(block.get("tool_use_id") or "unknown")
                    metadata = state.get("tool_calls", {}).get(tool_id, {})
                    tool_name = str(metadata.get("name") or "unknown")
                    output = block.get("content")
                    context_type = self._context_type(tool_name, metadata.get("target"))
                    result.append(NormalizedEvent(
                        agent_name=self.name, provider=self.provider, event_kind="tool_result", source_file=source_file,
                        line_number=line_number, event_id=f"{event_id}:result:{tool_id}", timestamp=timestamp,
                        session_id=session_id, turn_id=state.get("last_turn_id"), version=version, cwd=cwd,
                        git_branch=branch, git_repository=repository,
                        tool_call=ToolCallData(external_call_id=tool_id, tool_name=tool_name, ended_at=timestamp,
                                               output_size=len(str(output)) if output is not None else 0,
                                               estimated_tokens=estimate_tokens(output), target=metadata.get("target"),
                                               file_path=metadata.get("file_path"), is_error=bool(block.get("is_error"))),
                        context_items=[ContextItemData(item_type=context_type, source=f"claude.{context_type}",
                                                       token_count=estimate_tokens(output), content_hash=content_hash(output),
                                                       file_path=metadata.get("file_path"), tool_name=tool_name,
                                                       mcp_server=metadata.get("target") if context_type == "mcp" else None,
                                                       subagent_name=tool_name if context_type == "subagent" else None,
                                                       is_estimated=True)],
                        raw_type=event_type,
                    ))
            elif prompt_text or message:
                state["pending_prompt_turn_id"] = prompt_id
                result.append(NormalizedEvent(
                    agent_name=self.name, provider=self.provider, event_kind="user_prompt", source_file=source_file,
                    line_number=line_number, event_id=event_id, timestamp=timestamp, session_id=session_id,
                    turn_id=prompt_id, version=version, cwd=cwd, git_branch=branch, git_repository=repository,
                    prompt_text=prompt_text, context_items=[ContextItemData(item_type="user_prompt", source="claude.user",
                                                                            token_count=estimate_tokens(prompt_text),
                                                                            content_hash=content_hash(prompt_text), is_estimated=True)],
                    raw_type=event_type,
                ))
            return result

        if event_type in {"system", "mode"}:
            return [NormalizedEvent(agent_name=self.name, provider=self.provider, event_kind=event_type,
                                    source_file=source_file, line_number=line_number, event_id=event_id,
                                    timestamp=timestamp, session_id=session_id, version=version, cwd=cwd,
                                    git_branch=branch, git_repository=repository, raw_type=event_type)]
        return []

    @staticmethod
    def _context_type(tool_name: str, target: Any) -> str:
        name = tool_name.lower()
        target_text = str(target or "").lower()
        if name in {"read", "readfile", "read_file", "cat"} or "read" in name:
            return "file"
        if any(value in name for value in ("bash", "shell", "terminal", "command")):
            return "shell_output"
        if "mcp" in name or "mcp" in target_text:
            return "mcp"
        if any(value in name for value in ("agent", "subagent", "task")):
            return "subagent"
        return "tool_output"

    @staticmethod
    def _tool_call(block: dict[str, Any], timestamp: Any) -> ToolCallData:
        name = str(block.get("name") or "unknown")
        payload = block.get("input")
        target = None
        file_path = None
        if isinstance(payload, dict):
            target = first_value(payload, "command", "cmd", "url", "path", "file_path", "pattern")
            file_path = first_value(payload, "file_path", "path") if name.lower() in {"read", "readfile", "read_file"} else None
        return ToolCallData(external_call_id=str(block.get("id") or ""), tool_name=name, started_at=parse_timestamp(timestamp),
                            input_size=len(str(payload)) if payload is not None else 0, estimated_tokens=estimate_tokens(payload),
                            target=str(target) if target is not None else None, file_path=str(file_path) if file_path else None)

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
