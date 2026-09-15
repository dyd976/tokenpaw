from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from agent_token_monitor.adapters.base import AgentAdapter
from agent_token_monitor.models import ContextItemData, NormalizedEvent, ToolCallData, Usage
from agent_token_monitor.utils import content_hash, estimate_tokens, first_value, parse_timestamp, text_from_content


class CodexAdapter(AgentAdapter):
    name = "Codex"
    provider = "openai"

    def can_parse(self, path: Path, sample: Iterable[dict[str, Any]]) -> bool:
        return any(event.get("type") in {"session_meta", "turn_context"} and isinstance(event.get("payload"), dict) for event in sample)

    def detect_version(self, event: dict[str, Any]) -> str | None:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        return payload.get("cli_version") or payload.get("model")

    def parse_event(self, event: dict[str, Any], source_file: str, line_number: int, state: dict[str, Any]) -> list[NormalizedEvent]:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        envelope_type = str(event.get("type") or "")
        payload_type = str(payload.get("type") or "")
        timestamp = parse_timestamp(event.get("timestamp") or payload.get("timestamp"))
        ordinal = event.get("ordinal", line_number)
        session_id = str(state.get("session_id") or payload.get("session_id") or f"codex-file:{Path(source_file).stem}")
        model = state.get("model")
        cwd = state.get("cwd")
        branch = state.get("git_branch")
        repository = state.get("git_repository")

        if envelope_type == "session_meta":
            session_id = str(payload.get("session_id") or session_id)
            state.update({"session_id": session_id, "cwd": payload.get("cwd"), "model": payload.get("model"),
                          "model_provider": payload.get("model_provider"), "cli_version": payload.get("cli_version")})
            git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
            state.update({"git_branch": git.get("branch"), "git_repository": git.get("repository_url")})
            return [self._event(event, source_file, line_number, "session_start", session_id, None, payload.get("cli_version"),
                                state, timestamp, raw_type=envelope_type)]

        if envelope_type == "turn_context":
            turn_id = str(payload.get("turn_id") or f"turn:{ordinal}")
            state.update({"session_id": session_id, "current_turn_id": turn_id, "cwd": payload.get("cwd") or cwd,
                          "model": payload.get("model") or model, "usage_seen": False,
                          "pending_prompt_turn_id": None})
            state["context_window"] = payload.get("model_context_window") or payload.get("context_window")
            return []

        if envelope_type == "event_msg" and payload_type == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            latest = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
            total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
            usage_raw = latest or total
            if not usage_raw:
                return []
            input_tokens = self._int(usage_raw.get("input_tokens"))
            cached = self._int(usage_raw.get("cached_input_tokens"))
            fresh = max(input_tokens - cached, 0) if input_tokens is not None and cached is not None else input_tokens
            usage = Usage(input_tokens=input_tokens, cached_input_tokens=cached, fresh_input_tokens=fresh,
                          output_tokens=self._int(usage_raw.get("output_tokens")),
                          reasoning_tokens=self._int(usage_raw.get("reasoning_output_tokens")),
                          context_window=self._int(info.get("model_context_window") or state.get("context_window")),
                          raw_usage=usage_raw)
            base_turn = str(state.get("current_turn_id") or f"turn:{ordinal}")
            turn_id = base_turn if not state.get("usage_seen") else f"{base_turn}:{ordinal}"
            state["usage_seen"] = True
            state["last_turn_id"] = turn_id
            return [self._event(event, source_file, line_number, "turn_usage", session_id, turn_id,
                                state.get("cli_version"), state, timestamp, model=state.get("model"), usage=usage,
                                raw_type=payload_type)]

        if envelope_type == "response_item" and payload_type == "function_call":
            call_id = str(payload.get("call_id") or payload.get("id") or f"call:{ordinal}")
            arguments = payload.get("arguments")
            try:
                arg_value = json.loads(arguments) if isinstance(arguments, str) else arguments
            except json.JSONDecodeError:
                arg_value = arguments
            name = str(payload.get("name") or "unknown")
            target = first_value(arg_value, "command", "cmd", "path", "file_path", "url") if isinstance(arg_value, dict) else None
            tool = ToolCallData(external_call_id=call_id, tool_name=name, started_at=timestamp,
                                input_size=len(str(arguments)) if arguments is not None else 0,
                                estimated_tokens=estimate_tokens(arguments), target=str(target) if target else None,
                                file_path=str(first_value(arg_value, "path", "file_path")) if isinstance(arg_value, dict) and first_value(arg_value, "path", "file_path") else None)
            state.setdefault("pending_calls", {})[call_id] = {"name": name, "turn_id": state.get("current_turn_id"),
                                                               "target": str(target) if target else None,
                                                               "file_path": str(first_value(arg_value, "path", "file_path")) if isinstance(arg_value, dict) and first_value(arg_value, "path", "file_path") else None}
            return [self._event(event, source_file, line_number, "tool_call", session_id, state.get("current_turn_id"),
                                state.get("cli_version"), state, timestamp, event_id=f"{ordinal}:{call_id}",
                                tool_call=tool, raw_type=payload_type)]

        if envelope_type == "response_item" and payload_type == "function_call_output":
            call_id = str(payload.get("call_id") or "unknown")
            pending = state.get("pending_calls", {}).get(call_id, {})
            output = payload.get("output")
            tool_name = str(pending.get("name") or "unknown")
            context_type = self._context_type(tool_name, pending.get("target"))
            tool = ToolCallData(external_call_id=call_id, tool_name=tool_name, ended_at=timestamp,
                                output_size=len(str(output)) if output is not None else 0, estimated_tokens=estimate_tokens(output),
                                target=pending.get("target"), file_path=pending.get("file_path"))
            return [self._event(event, source_file, line_number, "tool_result", session_id,
                                pending.get("turn_id") or state.get("current_turn_id"), state.get("cli_version"), state,
                                timestamp, event_id=f"{ordinal}:{call_id}:output", tool_call=tool,
                                context_items=[ContextItemData(item_type=context_type, source=f"codex.{context_type}",
                                                               token_count=estimate_tokens(output), content_hash=content_hash(output),
                                                               file_path=pending.get("file_path"), tool_name=tool_name,
                                                               mcp_server=pending.get("target") if context_type == "mcp" else None,
                                                               subagent_name=tool_name if context_type == "subagent" else None,
                                                               is_estimated=True)],
                                raw_type=payload_type)]

        if envelope_type == "response_item" and payload_type == "message" and payload.get("role") == "user":
            text = text_from_content(payload.get("content"))
            turn_id = str(state.get("current_turn_id") or f"prompt:{ordinal}")
            state["pending_prompt_turn_id"] = turn_id
            return [self._event(event, source_file, line_number, "user_prompt", session_id, turn_id,
                                state.get("cli_version"), state, timestamp, prompt_text=text,
                                context_items=[ContextItemData(item_type="user_prompt", source="codex.message",
                                       token_count=estimate_tokens(text), content_hash=content_hash(text), is_estimated=True)],
                                raw_type=payload_type)]
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

    def _event(self, event: dict[str, Any], source_file: str, line_number: int, kind: str, session_id: str,
               turn_id: str | None, version: str | None, state: dict[str, Any], timestamp: Any,
               event_id: str | None = None, model: str | None = None, usage: Usage | None = None,
               tool_call: ToolCallData | None = None, context_items: list[ContextItemData] | None = None,
               prompt_text: str | None = None, raw_type: str | None = None) -> NormalizedEvent:
        return NormalizedEvent(agent_name=self.name, provider=self.provider, event_kind=kind, source_file=source_file,
                               line_number=line_number, event_id=event_id or f"{event.get('ordinal', line_number)}:{kind}",
                               timestamp=timestamp, session_id=session_id, turn_id=turn_id, model=model or state.get("model"),
                               version=version, cwd=state.get("cwd"), git_branch=state.get("git_branch"),
                               git_repository=state.get("git_repository"), prompt_text=prompt_text, usage=usage,
                               tool_call=tool_call, context_items=context_items or [], raw_type=raw_type)

    @staticmethod
    def _int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
