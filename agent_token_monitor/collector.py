from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterable

from agent_token_monitor.adapters import AgentAdapter, ClaudeAdapter, CodexAdapter
from agent_token_monitor.alerts import AlertEngine, AlertThresholds
from agent_token_monitor.storage import SQLiteStore
from agent_token_monitor.utils import content_hash, expand_path, privacy_safe_raw_event, stable_id

logger = logging.getLogger(__name__)


class Collector:
    def __init__(self, store: SQLiteStore, adapters: Iterable[AgentAdapter] | None = None,
                 alert_thresholds: AlertThresholds | None = None):
        self.store = store
        self.adapters = list(adapters or [ClaudeAdapter(), CodexAdapter()])
        self.alert_thresholds = alert_thresholds
        self._prompt_replay_requested = False

    def discover(self, paths: Iterable[str | Path]) -> list[Path]:
        files: list[Path] = []
        for raw_path in paths:
            path = expand_path(raw_path)
            if path.is_file() and path.suffix.lower() in {".jsonl", ".ndjson"}:
                files.append(path)
            elif path.is_dir():
                files.extend(item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in {".jsonl", ".ndjson"})
        return sorted(set(files))

    def scan(self, paths: Iterable[str | Path], *, store_prompt_content: bool = False,
             store_tool_output: bool = False, store_file_content: bool = False,
             mask_sensitive: bool = True) -> dict[str, int]:
        stats = {"files": 0, "lines": 0, "events": 0, "unknown": 0, "duplicates": 0, "errors": 0}
        if (not self._prompt_replay_requested and store_prompt_content
                and os.environ.get("TOKEN_MONITOR_REPROCESS_PROMPTS", "0").lower() in {"1", "true", "yes"}
                and not self.store.has_prompt_content()):
            self.store.reset_source_states()
            self._prompt_replay_requested = True
        for path in self.discover(paths):
            stats["files"] += 1
            file_stats = self._scan_file(path, store_prompt_content=store_prompt_content,
                                         store_tool_output=store_tool_output, store_file_content=store_file_content,
                                         mask_sensitive=mask_sensitive)
            for key, value in file_stats.items():
                stats[key] += value
        self.store.backfill_prompt_turns()
        self.store.refresh_session_status()
        AlertEngine(self.store, self.alert_thresholds).evaluate()
        return stats

    def _scan_file(self, path: Path, *, store_prompt_content: bool, store_tool_output: bool,
                   store_file_content: bool, mask_sensitive: bool) -> dict[str, int]:
        stats = {"lines": 0, "events": 0, "unknown": 0, "duplicates": 0, "errors": 0}
        state_record = self.store.source_state(str(path))
        try:
            stat = path.stat()
            offset = state_record["byte_offset"] if stat.st_size >= state_record["byte_offset"] else 0
            if offset == 0 and state_record["byte_offset"] != 0:
                state_record["state"] = {}
            adapter, sample = self._detect_adapter(path, offset)
            if adapter is None:
                self.store.save_source_state(str(path), "unknown", offset, stat.st_size, stat.st_mtime_ns, state_record["state"], "No adapter matched")
                return stats
            state = dict(state_record.get("state") or {})
            line_number = int(state.get("line_number", 0))
            with path.open("rb") as handle:
                handle.seek(offset)
                chunk = handle.read()
            complete_end = chunk.rfind(b"\n") + 1
            payload = chunk[:complete_end]
            new_offset = offset + complete_end
            for raw_line in payload.splitlines():
                stats["lines"] += 1
                line_number += 1
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    stats["errors"] += 1
                    self.store.record_unknown(agent_name=adapter.name, source_file=str(path), line_number=line_number,
                                               event_id=f"line-{line_number}", timestamp=None, raw_type=None,
                                               event_hash=content_hash(raw_line.decode("utf-8", "replace")),
                                               reason=f"invalid JSON: {exc}",
                                               raw_event={"raw_line_preview": "<omitted by privacy policy>"}, commit=False)
                    continue
                try:
                    events = adapter.normalize(event, str(path), line_number, state)
                except Exception as exc:  # One bad provider event must not stop collection.
                    logger.exception("adapter error in %s:%s", path, line_number)
                    stats["errors"] += 1
                    self.store.record_unknown(agent_name=adapter.name, source_file=str(path), line_number=line_number,
                                               event_id=str(event.get("uuid") or event.get("id") or f"line-{line_number}"),
                                               timestamp=str(event.get("timestamp")) if event.get("timestamp") else None,
                                               raw_type=str(event.get("type")), event_hash=content_hash(event),
                                               reason=f"adapter error: {exc}", raw_event=privacy_safe_raw_event(event), commit=False)
                    continue
                if not events:
                    if event.get("type") not in {"queue-operation", "custom-title", "last-prompt", "attachment",
                                                  "file-history-snapshot", "file-history-delta", "ai-title", "atis-latch",
                                                  "mode", "system", "world_state", "item_completed", "task_started",
                                                  "task_complete", "thread_settings_applied", "compacted"}:
                        stats["unknown"] += 1
                        self.store.record_unknown(agent_name=adapter.name, source_file=str(path), line_number=line_number,
                                                   event_id=str(event.get("uuid") or event.get("id") or f"line-{line_number}"),
                                                   timestamp=str(event.get("timestamp")) if event.get("timestamp") else None,
                                                   raw_type=str(event.get("type")), event_hash=content_hash(event),
                                                   reason="unsupported event type", raw_event=privacy_safe_raw_event(event), commit=False)
                    continue
                for normalized in events:
                    # Store hashes and metadata only; the raw event is deliberately not attached by default.
                    normalized.raw_event = None
                    if self.store.ingest(normalized, store_prompt_content=store_prompt_content,
                                         store_tool_output=store_tool_output, store_file_content=store_file_content,
                                         mask_sensitive=mask_sensitive,
                                         commit=False):
                        stats["events"] += 1
                    else:
                        stats["duplicates"] += 1
            state["line_number"] = line_number
            self.store.save_source_state(str(path), adapter.name, new_offset, stat.st_size, stat.st_mtime_ns, state, None)
        except OSError as exc:
            stats["errors"] += 1
            self.store.save_source_state(str(path), "unknown", state_record["byte_offset"], state_record["file_size"],
                                         state_record["mtime_ns"], state_record.get("state") or {}, str(exc))
        return stats

    def _detect_adapter(self, path: Path, offset: int) -> tuple[AgentAdapter | None, list[dict]]:
        sample: list[dict] = []
        try:
            with path.open("rb") as handle:
                handle.seek(0)
                for raw in handle:
                    if len(sample) >= 30:
                        break
                    try:
                        value = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(value, dict):
                        sample.append(value)
        except OSError:
            return None, sample
        for adapter in self.adapters:
            if adapter.can_parse(path, sample):
                return adapter, sample
        return None, sample
