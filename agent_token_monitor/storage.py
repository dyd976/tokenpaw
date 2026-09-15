from __future__ import annotations

import json
import os
import platform
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_token_monitor.models import NormalizedEvent
from agent_token_monitor.pricing import PricingTable
from agent_token_monitor.utils import canonical_json, mask_sensitive_strings, parse_timestamp, stable_id

ACTUAL_USAGE_ALERT_TYPES = ("CONTEXT_SPIKE", "CACHE_DROP", "ABNORMAL_BURN", "CONTEXT_PRESSURE")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
    version TEXT, host TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), project_name TEXT NOT NULL,
    project_path TEXT, git_repository TEXT, git_branch TEXT,
    UNIQUE(agent_id, project_path)
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL REFERENCES agents(id), project_id TEXT REFERENCES projects(id),
    external_session_id TEXT NOT NULL, source_file TEXT, started_at TEXT, ended_at TEXT, model TEXT,
    total_input_tokens INTEGER NOT NULL DEFAULT 0, total_cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_fresh_input_tokens INTEGER NOT NULL DEFAULT 0, total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_reasoning_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost REAL, status TEXT NOT NULL DEFAULT 'ACTIVE',
    UNIQUE(agent_id, external_session_id)
);
CREATE TABLE IF NOT EXISTS turns (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), external_turn_id TEXT NOT NULL,
    timestamp TEXT, user_prompt TEXT, model TEXT, input_tokens INTEGER, cached_input_tokens INTEGER,
    fresh_input_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER, context_window INTEGER,
    context_utilization REAL, estimated_cost REAL, is_estimated INTEGER NOT NULL DEFAULT 0, raw_usage_json TEXT,
    UNIQUE(session_id, external_turn_id)
);
CREATE TABLE IF NOT EXISTS context_items (
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), type TEXT NOT NULL, source TEXT,
    token_count INTEGER, content_hash TEXT, file_path TEXT, tool_name TEXT, mcp_server TEXT,
    subagent_name TEXT, is_estimated INTEGER NOT NULL DEFAULT 0, content TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), external_call_id TEXT,
    tool_name TEXT NOT NULL, started_at TEXT, ended_at TEXT, input_size INTEGER, output_size INTEGER,
    estimated_tokens INTEGER, target TEXT, file_path TEXT, is_error INTEGER NOT NULL DEFAULT 0,
    UNIQUE(turn_id, external_call_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY, agent_id TEXT, session_id TEXT, turn_id TEXT, timestamp TEXT,
    severity TEXT, type TEXT, title TEXT, description TEXT, metric TEXT, baseline REAL,
    current_value REAL, resolved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS source_files (
    path TEXT PRIMARY KEY, agent_name TEXT NOT NULL, byte_offset INTEGER NOT NULL DEFAULT 0,
    file_size INTEGER NOT NULL DEFAULT 0, mtime_ns INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}', last_error TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingested_events (
    idempotency_key TEXT PRIMARY KEY, agent_name TEXT NOT NULL, session_external_id TEXT,
    event_id TEXT, timestamp TEXT, event_hash TEXT NOT NULL, source_file TEXT NOT NULL,
    line_number INTEGER NOT NULL, raw_type TEXT, parsed INTEGER NOT NULL DEFAULT 1,
    unknown_reason TEXT, raw_json TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_turns_session_time ON turns(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_context_hash ON context_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_context_turn_id ON context_items(turn_id);
CREATE INDEX IF NOT EXISTS idx_tool_turn_id ON tool_calls(turn_id);
CREATE INDEX IF NOT EXISTS idx_ingested_source ON ingested_events(source_file, line_number);
"""


class SQLiteStore:
    def __init__(self, path: str | Path, pricing_path: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # FastAPI request handlers and the polling collector can run concurrently.
        # A sqlite connection must not be shared across those threads, even when
        # check_same_thread=False is enabled, so keep one connection per thread.
        self._connections: dict[int, sqlite3.Connection] = {}
        self._connections_lock = threading.RLock()
        connection = self.connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        connection.commit()
        self.pricing = PricingTable.from_file(pricing_path or os.environ.get("TOKEN_MONITOR_PRICING_PATH"))
        self._backfill_context_utilization()
        if self.pricing.rules:
            self._refresh_costs_from_raw_usage()

    @property
    def connection(self) -> sqlite3.Connection:
        thread_id = threading.get_ident()
        with self._connections_lock:
            connection = self._connections.get(thread_id)
            if connection is None:
                connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=10000")
                connection.execute("PRAGMA foreign_keys=ON")
                self._connections[thread_id] = connection
            return connection

    def close(self) -> None:
        with self._connections_lock:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()


    def _refresh_costs_from_raw_usage(self) -> None:
        rows = self.connection.execute("""SELECT t.id, t.session_id, t.timestamp, t.model, t.input_tokens,
            t.cached_input_tokens, t.fresh_input_tokens, t.output_tokens, t.reasoning_tokens, t.raw_usage_json,
            a.provider FROM turns t JOIN sessions s ON s.id=t.session_id JOIN agents a ON a.id=s.agent_id
            WHERE t.raw_usage_json IS NOT NULL""").fetchall()
        for row in rows:
            try:
                raw = json.loads(row["raw_usage_json"] or "{}")
            except json.JSONDecodeError:
                raw = {}
            cost = self.pricing.estimate(provider=row["provider"], model=row["model"], timestamp=row["timestamp"],
                                         cached_tokens=row["cached_input_tokens"], fresh_tokens=row["fresh_input_tokens"],
                                         input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
                                         reasoning_tokens=row["reasoning_tokens"])
            if cost is not None:
                self.connection.execute("UPDATE turns SET estimated_cost=? WHERE id=?", (cost, row["id"]))
        self.connection.execute("""UPDATE sessions SET estimated_cost=(SELECT SUM(t.estimated_cost) FROM turns t
            WHERE t.session_id=sessions.id AND t.estimated_cost IS NOT NULL)""")
        self.connection.commit()

    def _backfill_context_utilization(self) -> None:
        """Fill utilization for turns collected before this metric was persisted."""
        self.connection.execute(
            """UPDATE turns SET context_utilization=CASE
                 WHEN context_window IS NULL OR context_window <= 0 THEN NULL
                 WHEN cached_input_tokens IS NOT NULL OR fresh_input_tokens IS NOT NULL
                   THEN (COALESCE(cached_input_tokens,0) + COALESCE(fresh_input_tokens,0)) * 1.0 / context_window
                 ELSE COALESCE(input_tokens,0) * 1.0 / context_window
               END
               WHERE context_window IS NOT NULL"""
        )
        self.connection.commit()

    def source_state(self, path: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM source_files WHERE path = ?", (path,)).fetchone()
        if row is None:
            return {"path": path, "byte_offset": 0, "file_size": 0, "mtime_ns": 0, "state": {}}
        return {"path": path, "byte_offset": row["byte_offset"], "file_size": row["file_size"],
                "mtime_ns": row["mtime_ns"], "state": json.loads(row["state_json"] or "{}")}

    def save_source_state(self, path: str, agent_name: str, byte_offset: int, file_size: int,
                          mtime_ns: int, state: dict[str, Any], last_error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """INSERT INTO source_files(path, agent_name, byte_offset, file_size, mtime_ns, state_json, last_error, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET agent_name=excluded.agent_name, byte_offset=excluded.byte_offset,
               file_size=excluded.file_size, mtime_ns=excluded.mtime_ns, state_json=excluded.state_json,
               last_error=excluded.last_error, updated_at=excluded.updated_at""",
            (path, agent_name, byte_offset, file_size, mtime_ns, json.dumps(state, ensure_ascii=False), last_error, now),
        )
        self.connection.commit()

    def has_prompt_content(self) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM turns WHERE user_prompt IS NOT NULL AND trim(user_prompt) <> '' LIMIT 1"
        ).fetchone()
        return row is not None

    def reset_source_states(self) -> None:
        """Request one full local replay so newly enabled metadata can be backfilled."""
        self.connection.execute(
            "UPDATE source_files SET byte_offset=0, file_size=0, mtime_ns=0, state_json='{}', last_error=NULL"
        )
        self.connection.commit()

    def record_unknown(self, *, agent_name: str, source_file: str, line_number: int, event_id: str,
                       timestamp: str | None, raw_type: str | None, event_hash: str,
                       reason: str, raw_event: dict[str, Any] | None = None, commit: bool = True) -> bool:
        key = stable_id(agent_name, event_id, timestamp, event_hash)
        return self._insert_ingested(key, agent_name, None, event_id, timestamp, event_hash, source_file,
                                     line_number, raw_type, parsed=False, unknown_reason=reason, raw_event=raw_event,
                                     commit=commit)

    def ingest(self, event: NormalizedEvent, *, store_prompt_content: bool = False,
               store_tool_output: bool = False, store_file_content: bool = False,
               mask_sensitive: bool = True, commit: bool = True) -> bool:
        event_hash = stable_id(event.raw_type, event.event_id, asdict(event.usage) if event.usage else None,
                               asdict(event.tool_call) if event.tool_call else None,
                               [(item.item_type, item.content_hash, item.token_count) for item in event.context_items])
        timestamp = event.timestamp.isoformat() if event.timestamp else None
        key = stable_id(event.agent_name, event.session_id, event.event_id, timestamp, event_hash)
        inserted = self._insert_ingested(key, event.agent_name, event.session_id, event.event_id, timestamp, event_hash,
                                         event.source_file, event.line_number, event.raw_type, parsed=True,
                                         raw_event=event.raw_event, commit=commit)
        if not inserted:
            if event.event_kind == "user_prompt" and store_prompt_content and event.prompt_text:
                self._refresh_duplicate_prompt(event, mask_sensitive=mask_sensitive, commit=commit)
            return False
        agent_id = stable_id("agent", event.agent_name, event.provider)
        project_path = event.cwd or ""
        project_id = stable_id("project", agent_id, project_path)
        session_id = stable_id("session", agent_id, event.session_id)
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute(
            """INSERT INTO agents(id,name,provider,version,host,created_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET version=COALESCE(excluded.version, agents.version)""",
            (agent_id, event.agent_name, event.provider, event.version, platform.node(), now),
        )
        self.connection.execute(
            """INSERT INTO projects(id,agent_id,project_name,project_path,git_repository,git_branch) VALUES(?,?,?,?,?,?)
               ON CONFLICT(agent_id,project_path) DO UPDATE SET project_name=excluded.project_name,
               git_repository=COALESCE(excluded.git_repository,projects.git_repository),
               git_branch=COALESCE(excluded.git_branch,projects.git_branch)""",
            (project_id, agent_id, Path(project_path).name if project_path else "Unknown", project_path,
             event.git_repository, event.git_branch),
        )
        self.connection.execute(
            """INSERT INTO sessions(id,agent_id,project_id,external_session_id,source_file,started_at,ended_at,model,status)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(agent_id,external_session_id) DO UPDATE SET project_id=COALESCE(excluded.project_id,sessions.project_id),
               ended_at=COALESCE(excluded.ended_at,sessions.ended_at), model=COALESCE(excluded.model,sessions.model),
               status=CASE WHEN excluded.status='ACTIVE' THEN 'ACTIVE' ELSE excluded.status END""",
            (session_id, agent_id, project_id, event.session_id, event.source_file, timestamp, timestamp,
             event.model, "ACTIVE"),
        )

        turn_db_id: str | None = None
        if event.turn_id:
            turn_db_id = self._ensure_turn(session_id, event.turn_id, timestamp, event.model)
        if event.event_kind == "user_prompt":
            turn_db_id = self._ensure_turn(session_id, event.turn_id or f"prompt:{event.event_id}", timestamp, event.model)
            if store_prompt_content and event.prompt_text is not None:
                prompt = mask_sensitive_strings(event.prompt_text) if mask_sensitive else event.prompt_text
                self.connection.execute("UPDATE turns SET user_prompt=? WHERE id=?", (prompt, turn_db_id))
        if event.usage and turn_db_id:
            u = event.usage
            estimated_cost = self.pricing.estimate(
                provider=event.provider, model=event.model, timestamp=timestamp,
                cached_tokens=u.cached_input_tokens, fresh_tokens=u.fresh_input_tokens,
                input_tokens=u.input_tokens, output_tokens=u.output_tokens, reasoning_tokens=u.reasoning_tokens,
            )
            effective_input = ((u.cached_input_tokens or 0) + (u.fresh_input_tokens or 0)
                               if u.cached_input_tokens is not None or u.fresh_input_tokens is not None
                               else (u.input_tokens or 0))
            self.connection.execute(
                """UPDATE turns SET model=?, input_tokens=?, cached_input_tokens=?, fresh_input_tokens=?, output_tokens=?,
                   reasoning_tokens=?, context_window=?, context_utilization=CASE WHEN ? IS NOT NULL AND ? > 0 THEN ? * 1.0 / ? ELSE NULL END,
                   estimated_cost=?, is_estimated=?, raw_usage_json=? WHERE id=?""",
                (event.model, u.input_tokens, u.cached_input_tokens, u.fresh_input_tokens, u.output_tokens,
                 u.reasoning_tokens, u.context_window, u.context_window, u.context_window, effective_input, u.context_window,
                 estimated_cost, int(u.is_estimated), json.dumps(u.raw_usage, ensure_ascii=False), turn_db_id),
            )
            self.connection.execute(
                """UPDATE sessions SET total_input_tokens=total_input_tokens+?, total_cached_input_tokens=total_cached_input_tokens+?,
                   total_fresh_input_tokens=total_fresh_input_tokens+?, total_output_tokens=total_output_tokens+?,
                   total_reasoning_tokens=total_reasoning_tokens+?, estimated_cost=CASE WHEN ? IS NULL THEN estimated_cost ELSE COALESCE(estimated_cost,0)+? END,
                   model=COALESCE(?,model) WHERE id=?""",
                (u.input_tokens or 0, u.cached_input_tokens or 0, u.fresh_input_tokens or 0,
                 u.output_tokens or 0, u.reasoning_tokens or 0, estimated_cost, estimated_cost, event.model, session_id),
            )
        if turn_db_id:
            for item in event.context_items:
                content = event.prompt_text if store_prompt_content and item.item_type == "user_prompt" else None
                if item.item_type == "tool_output" and not store_tool_output:
                    content = None
                if item.item_type == "file" and not store_file_content:
                    content = None
                if content is not None and mask_sensitive:
                    content = mask_sensitive_strings(content)
                item_id = stable_id("context", turn_db_id, item.item_type, item.content_hash, item.source)
                self.connection.execute(
                    """INSERT OR IGNORE INTO context_items(id,turn_id,type,source,token_count,content_hash,file_path,
                       tool_name,mcp_server,subagent_name,is_estimated,content) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item_id, turn_db_id, item.item_type, item.source, item.token_count, item.content_hash,
                     item.file_path, item.tool_name, item.mcp_server, item.subagent_name, int(item.is_estimated), content),
                )
        if event.tool_call and turn_db_id:
            tool = event.tool_call
            tool_id = stable_id("tool", turn_db_id, tool.external_call_id, tool.tool_name)
            if event.event_kind == "tool_result" and tool.external_call_id:
                updated = self.connection.execute(
                    """UPDATE tool_calls SET ended_at=COALESCE(?,ended_at), output_size=COALESCE(?,output_size),
                       estimated_tokens=COALESCE(?,estimated_tokens), target=COALESCE(?,target), file_path=COALESCE(?,file_path),
                       is_error=? WHERE turn_id=? AND external_call_id=?""",
                    (tool.ended_at.isoformat() if tool.ended_at else None, tool.output_size, tool.estimated_tokens,
                     tool.target, tool.file_path, int(tool.is_error), turn_db_id, tool.external_call_id),
                )
                if updated.rowcount == 0:
                    self.connection.execute(
                        """INSERT OR IGNORE INTO tool_calls(id,turn_id,external_call_id,tool_name,started_at,ended_at,input_size,
                           output_size,estimated_tokens,target,file_path,is_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (tool_id, turn_db_id, tool.external_call_id, tool.tool_name,
                         tool.started_at.isoformat() if tool.started_at else None, tool.ended_at.isoformat() if tool.ended_at else None,
                         tool.input_size, tool.output_size, tool.estimated_tokens, tool.target, tool.file_path, int(tool.is_error)),
                    )
            else:
                self.connection.execute(
                    """INSERT OR IGNORE INTO tool_calls(id,turn_id,external_call_id,tool_name,started_at,ended_at,input_size,
                       output_size,estimated_tokens,target,file_path,is_error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tool_id, turn_db_id, tool.external_call_id, tool.tool_name,
                     tool.started_at.isoformat() if tool.started_at else None, tool.ended_at.isoformat() if tool.ended_at else None,
                     tool.input_size, tool.output_size, tool.estimated_tokens, tool.target, tool.file_path, int(tool.is_error)),
                )
        if commit:
            self.connection.commit()
        return True

    def _refresh_duplicate_prompt(self, event: NormalizedEvent, *, mask_sensitive: bool, commit: bool) -> None:
        """Backfill prompt text during an idempotent replay without duplicating usage."""
        agent_id = stable_id("agent", event.agent_name, event.provider)
        session_id = stable_id("session", agent_id, event.session_id)
        external_turn_id = event.turn_id or f"prompt:{event.event_id}"
        turn_id = stable_id("turn", session_id, external_turn_id)
        row = self.connection.execute("SELECT id FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None and event.timestamp is not None:
            row = self.connection.execute(
                "SELECT id FROM turns WHERE session_id=? AND timestamp=? ORDER BY id LIMIT 1",
                (session_id, event.timestamp.isoformat()),
            ).fetchone()
        if row is None:
            return
        prompt = mask_sensitive_strings(event.prompt_text) if mask_sensitive else event.prompt_text
        self.connection.execute(
            "UPDATE turns SET user_prompt=? WHERE id=? AND (user_prompt IS NULL OR trim(user_prompt)='')",
            (prompt, row["id"]),
        )
        for item in event.context_items:
            if item.item_type != "user_prompt":
                continue
            self.connection.execute(
                "UPDATE context_items SET content=? WHERE turn_id=? AND type='user_prompt' AND content_hash=?",
                (prompt, row["id"], item.content_hash),
            )
        if commit:
            self.connection.commit()

    def _ensure_turn(self, session_id: str, external_turn_id: str, timestamp: str | None, model: str | None) -> str:
        turn_id = stable_id("turn", session_id, external_turn_id)
        self.connection.execute(
            """INSERT INTO turns(id,session_id,external_turn_id,timestamp,model) VALUES(?,?,?,?,?)
               ON CONFLICT(session_id,external_turn_id) DO UPDATE SET timestamp=COALESCE(excluded.timestamp,turns.timestamp),
               model=COALESCE(excluded.model,turns.model)""",
            (turn_id, session_id, external_turn_id, timestamp, model),
        )
        return turn_id

    def backfill_prompt_turns(self) -> int:
        """Merge legacy prompt-only turns into the first measured turn after them.

        Older collector versions created ``prompt:*`` turns before the provider's
        usage event. This migration is local and idempotent; it never reads or
        changes provider log files.
        """
        prompt_rows = self.connection.execute(
            "SELECT id, session_id, timestamp, user_prompt FROM turns WHERE external_turn_id LIKE 'prompt:%'"
        ).fetchall()
        merged = 0
        for prompt in prompt_rows:
            target = self.connection.execute(
                """SELECT id, timestamp, user_prompt FROM turns
                   WHERE session_id=? AND external_turn_id NOT LIKE 'prompt:%'
                     AND input_tokens IS NOT NULL AND timestamp >= ?
                   ORDER BY timestamp, id LIMIT 1""",
                (prompt["session_id"], prompt["timestamp"]),
            ).fetchone()
            if target is None:
                target = self.connection.execute(
                    """SELECT id, timestamp, user_prompt FROM turns
                       WHERE session_id=? AND external_turn_id NOT LIKE 'prompt:%'
                         AND timestamp >= ?
                       ORDER BY timestamp, id LIMIT 1""",
                    (prompt["session_id"], prompt["timestamp"]),
                ).fetchone()
            if target is None:
                continue
            context_rows = self.connection.execute(
                "SELECT id FROM context_items WHERE turn_id=?", (prompt["id"],)
            ).fetchall()
            for context in context_rows:
                self.connection.execute("UPDATE context_items SET turn_id=? WHERE id=?", (target["id"], context["id"]))
            if prompt["user_prompt"] and not target["user_prompt"]:
                self.connection.execute("UPDATE turns SET user_prompt=? WHERE id=?", (prompt["user_prompt"], target["id"]))
            self.connection.execute("DELETE FROM turns WHERE id=?", (prompt["id"],))
            merged += 1
        if merged:
            self.connection.commit()
        return merged

    def _insert_ingested(self, key: str, agent_name: str, session_external_id: str | None, event_id: str,
                         timestamp: str | None, event_hash: str, source_file: str, line_number: int,
                         raw_type: str | None, *, parsed: bool, unknown_reason: str | None = None,
                         raw_event: dict[str, Any] | None = None, commit: bool = True) -> bool:
        raw_json = canonical_json(raw_event) if raw_event is not None else None
        cursor = self.connection.execute(
            """INSERT OR IGNORE INTO ingested_events(idempotency_key,agent_name,session_external_id,event_id,timestamp,
               event_hash,source_file,line_number,raw_type,parsed,unknown_reason,raw_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, agent_name, session_external_id, event_id, timestamp, event_hash, source_file, line_number,
             raw_type, int(parsed), unknown_reason, raw_json, datetime.now(timezone.utc).isoformat()),
        )
        if commit:
            self.connection.commit()
        return cursor.rowcount == 1

    def summary(self) -> dict[str, Any]:
        row = self.connection.execute("""SELECT COUNT(*) AS sessions,
            COALESCE(SUM(total_input_tokens),0) AS input_tokens,
            COALESCE(SUM(total_cached_input_tokens),0) AS cached_tokens,
            COALESCE(SUM(total_fresh_input_tokens),0) AS fresh_tokens,
            COALESCE(SUM(total_output_tokens),0) AS output_tokens,
            COALESCE(SUM(total_reasoning_tokens),0) AS reasoning_tokens,
            SUM(estimated_cost) AS estimated_cost
            FROM sessions""").fetchone()
        result = dict(row)
        result["effective_input_tokens"] = result["cached_tokens"] + result["fresh_tokens"] or result["input_tokens"]
        result["total_tokens"] = result["effective_input_tokens"] + result["output_tokens"]
        result["estimated_cost"] = None
        result["cost_available"] = False
        denominator = result["cached_tokens"] + result["fresh_tokens"]
        result["cache_hit_rate"] = (result["cached_tokens"] / denominator) if denominator else None
        return result

    @staticmethod
    def _add_usage_quality(item: dict[str, Any]) -> dict[str, Any]:
        """Expose whether an aggregate is backed by provider usage, estimates, or both."""
        actual = int(item.get("actual_turns") or 0)
        estimated = int(item.get("estimated_turns") or 0)
        input_tokens = item.get("effective_input_tokens")
        if input_tokens is not None:
            output_tokens = item.get("total_output_tokens") if "total_output_tokens" in item else item.get("output_tokens")
            item["total_tokens"] = input_tokens + (output_tokens or 0)
        item["usage_quality"] = "Mixed" if actual and estimated else "Estimated" if estimated else "Actual"
        item["has_estimates"] = bool(estimated)
        item["estimated_cost"] = None
        item["cost_available"] = False
        return item

    def _add_health_status(self, item: dict[str, Any]) -> dict[str, Any]:
        """Expose the highest unresolved alert severity alongside activity status."""
        rows = self.connection.execute(
            "SELECT severity FROM alerts WHERE session_id=? AND resolved=0",
            (item["id"],),
        ).fetchall()
        severities = {row["severity"] for row in rows}
        item["health_status"] = "CRITICAL" if "CRITICAL" in severities else "WARNING" if "WARNING" in severities else item.get("status")
        return item

    def _prompt_linked(self, turn_id: str) -> bool:
        """Return whether the provider usage turn has a recorded user prompt event.

        This deliberately checks the event marker rather than ``user_prompt`` text:
        prompt content may be disabled by the privacy setting while the linkage is
        still available and trustworthy.
        """
        row = self.connection.execute(
            "SELECT 1 FROM context_items WHERE turn_id=? AND type='user_prompt' LIMIT 1",
            (turn_id,),
        ).fetchone()
        return row is not None

    def _prompt_coverage(self, session_id: str, *, start: str | None = None,
                         end: str | None = None) -> dict[str, Any]:
        """Summarize which provider-measured usage can be linked to a prompt.

        Token values here are provider-recorded turn totals only. No attribution is
        made from a prompt to a file, tool, or later context.
        """
        turns = self.turns(session_id, start=start, end=end)
        measured = [turn for turn in turns if not bool(turn.get("is_estimated")) and any(
            turn.get(key) is not None for key in (
                "input_tokens", "cached_input_tokens", "fresh_input_tokens",
                "output_tokens", "reasoning_tokens"))]
        linked = [turn for turn in measured if bool(turn.get("prompt_linked"))]
        unlinked = [turn for turn in measured if not bool(turn.get("prompt_linked"))]

        def total(turn: dict[str, Any]) -> int:
            return self._effective_input(turn) + (turn.get("output_tokens") or 0)

        actual_tokens = sum(total(turn) for turn in measured)
        linked_tokens = sum(total(turn) for turn in linked)
        unlinked_tokens = sum(total(turn) for turn in unlinked)
        coverage = linked_tokens / actual_tokens if actual_tokens else None
        if not measured:
            status = "NO_DATA"
        elif not linked:
            status = "UNAVAILABLE"
        elif unlinked:
            status = "PARTIAL"
        else:
            status = "COMPLETE"
        return {
            "status": status,
            "actual_usage_turns": len(measured),
            "linked_prompt_turns": len(linked),
            "unlinked_usage_turns": len(unlinked),
            "prompt_content_stored_turns": sum(bool(turn.get("user_prompt")) for turn in linked),
            "actual_tokens": actual_tokens,
            "linked_actual_tokens": linked_tokens,
            "unlinked_actual_tokens": unlinked_tokens,
            "linked_token_coverage": coverage,
            "token_attribution_available": False,
            "note": "프롬프트가 연결된 턴의 provider 기록만 묶었습니다. 미연결 usage와 활동별 토큰 원인은 별도로 유지합니다.",
        }

    def _add_prompt_coverage(self, item: dict[str, Any], *, start: str | None = None,
                             end: str | None = None) -> dict[str, Any]:
        item["prompt_coverage"] = self._prompt_coverage(item["id"], start=start, end=end)
        return item

    def sessions(self, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if start or end:
            clauses: list[str] = []
            params: list[Any] = []
            if start:
                clauses.append("datetime(t.timestamp) >= datetime(?)")
                params.append(start)
            if end:
                clauses.append("datetime(t.timestamp) <= datetime(?)")
                params.append(end)
            where = " AND ".join(clauses)
            rows = self.connection.execute(f"""SELECT s.id, s.agent_id, s.project_id, s.external_session_id, s.source_file,
                s.started_at, s.ended_at, COALESCE(MAX(t.model), s.model) AS model,
                COALESCE(SUM(t.input_tokens),0) AS total_input_tokens,
                COALESCE(SUM(t.cached_input_tokens),0) AS total_cached_input_tokens,
                COALESCE(SUM(t.fresh_input_tokens),0) AS total_fresh_input_tokens,
                COALESCE(SUM(t.output_tokens),0) AS total_output_tokens,
                COALESCE(SUM(t.reasoning_tokens),0) AS total_reasoning_tokens,
                SUM(CASE WHEN t.is_estimated=0 AND (t.input_tokens IS NOT NULL OR t.cached_input_tokens IS NOT NULL OR
                    t.fresh_input_tokens IS NOT NULL OR t.output_tokens IS NOT NULL OR t.reasoning_tokens IS NOT NULL) THEN 1 ELSE 0 END) AS actual_turns,
                SUM(CASE WHEN t.is_estimated=1 THEN 1 ELSE 0 END) AS estimated_turns,
                SUM(t.estimated_cost) AS estimated_cost, MAX(t.context_utilization) AS context_utilization, s.status,
                a.name AS agent_name, a.provider, p.project_name, p.project_path,
                p.git_repository, p.git_branch, MAX(t.timestamp) AS last_activity
                FROM sessions s JOIN agents a ON a.id=s.agent_id
                LEFT JOIN projects p ON p.id=s.project_id JOIN turns t ON t.session_id=s.id
                WHERE {where} GROUP BY s.id ORDER BY last_activity DESC""", params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["effective_input_tokens"] = item["total_cached_input_tokens"] + item["total_fresh_input_tokens"] or item["total_input_tokens"]
                denominator = item["total_cached_input_tokens"] + item["total_fresh_input_tokens"]
                item["cache_hit_rate"] = item["total_cached_input_tokens"] / denominator if denominator else None
                result.append(self._add_prompt_coverage(self._add_health_status(self._add_usage_quality(item)), start=start, end=end))
            return result
        rows = self.connection.execute("""SELECT s.*, a.name AS agent_name, a.provider, p.project_name, p.project_path,
            p.git_repository, p.git_branch,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id=s.id AND tx.is_estimated=0 AND
                (tx.input_tokens IS NOT NULL OR tx.cached_input_tokens IS NOT NULL OR tx.fresh_input_tokens IS NOT NULL OR
                 tx.output_tokens IS NOT NULL OR tx.reasoning_tokens IS NOT NULL)) AS actual_turns,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id=s.id AND tx.is_estimated=1) AS estimated_turns
            FROM sessions s JOIN agents a ON a.id=s.agent_id LEFT JOIN projects p ON p.id=s.project_id
            ORDER BY COALESCE(s.ended_at,s.started_at) DESC""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["effective_input_tokens"] = item["total_cached_input_tokens"] + item["total_fresh_input_tokens"] or item["total_input_tokens"]
            denominator = item["total_cached_input_tokens"] + item["total_fresh_input_tokens"]
            item["cache_hit_rate"] = item["total_cached_input_tokens"] / denominator if denominator else None
            context_row = self.connection.execute(
                "SELECT MAX(context_utilization) AS context_utilization FROM turns WHERE session_id=?",
                (item["id"],),
            ).fetchone()
            item["context_utilization"] = context_row["context_utilization"] if context_row else None
            result.append(self._add_prompt_coverage(self._add_health_status(self._add_usage_quality(item))))
        return result

    def refresh_session_status(self, *, active_minutes: int = 5, idle_minutes: int = 30) -> None:
        now = datetime.now(timezone.utc)
        rows = self.connection.execute("SELECT id, ended_at FROM sessions").fetchall()
        for row in rows:
            timestamp = parse_timestamp(row["ended_at"])
            if timestamp is None:
                status = "FINISHED"
            else:
                age_minutes = max(0.0, (now - timestamp).total_seconds() / 60)
                status = "ACTIVE" if age_minutes <= active_minutes else "IDLE" if age_minutes <= idle_minutes else "FINISHED"
            self.connection.execute("UPDATE sessions SET status=? WHERE id=?", (status, row["id"]))
        self.connection.commit()

    def agents(self, *, start: str | None = None, end: str | None = None, provider: str | None = None) -> list[dict[str, Any]]:
        if start or end or provider:
            clauses: list[str] = []
            params: list[Any] = []
            if start:
                clauses.append("datetime(t.timestamp) >= datetime(?)")
                params.append(start)
            if end:
                clauses.append("datetime(t.timestamp) <= datetime(?)")
                params.append(end)
            if provider:
                clauses.append("a.provider = ?")
                params.append(provider)
            where = "WHERE " + " AND ".join(clauses)
            rows = self.connection.execute(f"""SELECT a.id, a.name, a.provider, a.version, a.host,
                COUNT(DISTINCT s.id) AS session_count, COALESCE(SUM(t.input_tokens),0) AS input_tokens,
                COALESCE(SUM(t.cached_input_tokens),0) AS cached_tokens,
                COALESCE(SUM(t.fresh_input_tokens),0) AS fresh_tokens,
                COALESCE(SUM(t.output_tokens),0) AS output_tokens,
                COALESCE(SUM(t.reasoning_tokens),0) AS reasoning_tokens,
                SUM(CASE WHEN t.is_estimated=0 AND (t.input_tokens IS NOT NULL OR t.cached_input_tokens IS NOT NULL OR
                    t.fresh_input_tokens IS NOT NULL OR t.output_tokens IS NOT NULL OR t.reasoning_tokens IS NOT NULL) THEN 1 ELSE 0 END) AS actual_turns,
                SUM(CASE WHEN t.is_estimated=1 THEN 1 ELSE 0 END) AS estimated_turns,
                SUM(t.estimated_cost) AS estimated_cost
                FROM turns t JOIN sessions s ON s.id=t.session_id JOIN agents a ON a.id=s.agent_id
                {where} GROUP BY a.id ORDER BY CASE WHEN (COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)) > 0
                    THEN COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)
                    ELSE COALESCE(SUM(t.input_tokens),0) END + COALESCE(SUM(t.output_tokens),0) DESC""", params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["effective_input_tokens"] = item["cached_tokens"] + item["fresh_tokens"] or item["input_tokens"]
                denominator = item["cached_tokens"] + item["fresh_tokens"]
                item["cache_hit_rate"] = item["cached_tokens"] / denominator if denominator else None
                result.append(self._add_usage_quality(item))
            return result
        rows = self.connection.execute("""SELECT a.id, a.name, a.provider, a.version, a.host,
            COUNT(s.id) AS session_count, COALESCE(SUM(s.total_input_tokens),0) AS input_tokens,
            COALESCE(SUM(s.total_cached_input_tokens),0) AS cached_tokens,
            COALESCE(SUM(s.total_fresh_input_tokens),0) AS fresh_tokens,
            COALESCE(SUM(s.total_output_tokens),0) AS output_tokens,
            COALESCE(SUM(s.total_reasoning_tokens),0) AS reasoning_tokens,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id IN (SELECT id FROM sessions WHERE agent_id=a.id)
                AND tx.is_estimated=0 AND (tx.input_tokens IS NOT NULL OR tx.cached_input_tokens IS NOT NULL OR
                tx.fresh_input_tokens IS NOT NULL OR tx.output_tokens IS NOT NULL OR tx.reasoning_tokens IS NOT NULL)) AS actual_turns,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id IN (SELECT id FROM sessions WHERE agent_id=a.id)
                AND tx.is_estimated=1) AS estimated_turns,
            SUM(s.estimated_cost) AS estimated_cost
            FROM agents a LEFT JOIN sessions s ON s.agent_id=a.id GROUP BY a.id
            ORDER BY CASE WHEN (cached_tokens + fresh_tokens) > 0 THEN cached_tokens + fresh_tokens ELSE input_tokens END + output_tokens DESC""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["estimated_cost"] = None
            item["cost_available"] = False
            item["effective_input_tokens"] = item["cached_tokens"] + item["fresh_tokens"] or item["input_tokens"]
            denominator = item["cached_tokens"] + item["fresh_tokens"]
            item["cache_hit_rate"] = item["cached_tokens"] / denominator if denominator else None
            result.append(self._add_usage_quality(item))
        return result

    def projects(self, *, start: str | None = None, end: str | None = None,
                 agent: str | None = None, provider: str | None = None,
                 model: str | None = None) -> list[dict[str, Any]]:
        if start or end or agent or provider or model:
            clauses: list[str] = []
            params: list[Any] = []
            if start:
                clauses.append("datetime(t.timestamp) >= datetime(?)")
                params.append(start)
            if end:
                clauses.append("datetime(t.timestamp) <= datetime(?)")
                params.append(end)
            if agent:
                clauses.append("a.name = ?")
                params.append(agent)
            if provider:
                clauses.append("a.provider = ?")
                params.append(provider)
            if model:
                clauses.append("t.model = ?")
                params.append(model)
            where = "WHERE " + " AND ".join(clauses)
            rows = self.connection.execute(f"""SELECT p.id, COALESCE(p.project_name,'Unknown') AS project_name,
                p.project_path, p.git_repository, p.git_branch, a.name AS agent_name,
                COUNT(DISTINCT s.id) AS session_count, COALESCE(SUM(t.input_tokens),0) AS input_tokens,
                COALESCE(SUM(t.cached_input_tokens),0) AS cached_tokens,
                COALESCE(SUM(t.fresh_input_tokens),0) AS fresh_tokens,
                COALESCE(SUM(t.output_tokens),0) AS output_tokens,
                COALESCE(SUM(t.reasoning_tokens),0) AS reasoning_tokens,
                SUM(CASE WHEN t.is_estimated=0 AND (t.input_tokens IS NOT NULL OR t.cached_input_tokens IS NOT NULL OR
                    t.fresh_input_tokens IS NOT NULL OR t.output_tokens IS NOT NULL OR t.reasoning_tokens IS NOT NULL) THEN 1 ELSE 0 END) AS actual_turns,
                SUM(CASE WHEN t.is_estimated=1 THEN 1 ELSE 0 END) AS estimated_turns,
                SUM(t.estimated_cost) AS estimated_cost
                FROM turns t JOIN sessions s ON s.id=t.session_id
                LEFT JOIN projects p ON p.id=s.project_id JOIN agents a ON a.id=s.agent_id
                {where} GROUP BY p.id ORDER BY CASE WHEN (COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)) > 0
                    THEN COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)
                    ELSE COALESCE(SUM(t.input_tokens),0) END + COALESCE(SUM(t.output_tokens),0) DESC""", params).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["effective_input_tokens"] = item["cached_tokens"] + item["fresh_tokens"] or item["input_tokens"]
                result.append(self._add_usage_quality(item))
            return result
        rows = self.connection.execute("""SELECT p.id, p.project_name, p.project_path, p.git_repository, p.git_branch,
            a.name AS agent_name, COUNT(s.id) AS session_count,
            COALESCE(SUM(s.total_input_tokens),0) AS input_tokens,
            COALESCE(SUM(s.total_cached_input_tokens),0) AS cached_tokens,
            COALESCE(SUM(s.total_fresh_input_tokens),0) AS fresh_tokens,
            COALESCE(SUM(s.total_output_tokens),0) AS output_tokens,
            COALESCE(SUM(s.total_reasoning_tokens),0) AS reasoning_tokens,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id IN (SELECT id FROM sessions WHERE project_id=p.id)
                AND tx.is_estimated=0 AND (tx.input_tokens IS NOT NULL OR tx.cached_input_tokens IS NOT NULL OR
                tx.fresh_input_tokens IS NOT NULL OR tx.output_tokens IS NOT NULL OR tx.reasoning_tokens IS NOT NULL)) AS actual_turns,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id IN (SELECT id FROM sessions WHERE project_id=p.id)
                AND tx.is_estimated=1) AS estimated_turns,
            SUM(s.estimated_cost) AS estimated_cost
            FROM projects p JOIN agents a ON a.id=p.agent_id LEFT JOIN sessions s ON s.project_id=p.id
            GROUP BY p.id
            ORDER BY CASE WHEN (cached_tokens + fresh_tokens) > 0 THEN cached_tokens + fresh_tokens ELSE input_tokens END + output_tokens DESC""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["effective_input_tokens"] = item["cached_tokens"] + item["fresh_tokens"] or item["input_tokens"]
            result.append(self._add_usage_quality(item))
        return result

    def session(self, session_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("""SELECT s.*, a.name AS agent_name, a.provider, p.project_name, p.project_path,
            p.git_repository, p.git_branch,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id=s.id AND tx.is_estimated=0 AND
                (tx.input_tokens IS NOT NULL OR tx.cached_input_tokens IS NOT NULL OR tx.fresh_input_tokens IS NOT NULL OR
                 tx.output_tokens IS NOT NULL OR tx.reasoning_tokens IS NOT NULL)) AS actual_turns,
            (SELECT COUNT(*) FROM turns tx WHERE tx.session_id=s.id AND tx.is_estimated=1) AS estimated_turns
            FROM sessions s JOIN agents a ON a.id=s.agent_id
            LEFT JOIN projects p ON p.id=s.project_id WHERE s.id=?""", (session_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["effective_input_tokens"] = item["total_cached_input_tokens"] + item["total_fresh_input_tokens"] or item["total_input_tokens"]
        item["total_tokens"] = item["effective_input_tokens"] + item["total_output_tokens"]
        denominator = item["total_cached_input_tokens"] + item["total_fresh_input_tokens"]
        item["cache_hit_rate"] = item["total_cached_input_tokens"] / denominator if denominator else None
        context_row = self.connection.execute(
            "SELECT MAX(context_utilization) AS context_utilization FROM turns WHERE session_id=?",
            (session_id,),
        ).fetchone()
        item["context_utilization"] = context_row["context_utilization"] if context_row else None
        return self._add_prompt_coverage(self._add_health_status(self._add_usage_quality(item)))

    def turns(self, session_id: str, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        clauses = ["session_id=?"]
        params: list[Any] = [session_id]
        if start:
            clauses.append("datetime(timestamp) >= datetime(?)")
            params.append(start)
        if end:
            clauses.append("datetime(timestamp) <= datetime(?)")
            params.append(end)
        rows = self.connection.execute(
            f"SELECT * FROM turns WHERE {' AND '.join(clauses)} ORDER BY timestamp, id", params
        ).fetchall()
        result = []
        previous_input: int | None = None
        previous_fresh: int | None = None
        for row in rows:
            item = dict(row)
            item["estimated_cost"] = None
            item["cost_available"] = False
            current_input = self._effective_input(item)
            current_fresh = item.get("fresh_input_tokens")
            item["effective_input_tokens"] = current_input
            item["total_tokens"] = current_input + (item.get("output_tokens") or 0)
            item["prompt_linked"] = self._prompt_linked(item["id"])
            item["prompt_content_stored"] = bool(item.get("user_prompt"))
            item["prompt_linkage"] = "linked" if item["prompt_linked"] else "unlinked"
            item["input_before_tokens"] = previous_input
            item["input_after_tokens"] = current_input
            item["input_delta_tokens"] = current_input - previous_input if previous_input is not None else 0
            item["fresh_context_added_tokens"] = max(current_fresh - previous_fresh, 0) if current_fresh is not None and previous_fresh is not None else current_fresh
            result.append(item)
            previous_input = current_input
            previous_fresh = current_fresh if current_fresh is not None else previous_fresh
        return result

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["estimated_cost"] = None
        item["cost_available"] = False
        item["effective_input_tokens"] = self._effective_input(item)
        item["total_tokens"] = item["effective_input_tokens"] + (item.get("output_tokens") or 0)
        sequence = self.turns(item["session_id"])
        decorated = next((value for value in sequence if value["id"] == turn_id), None)
        if decorated is not None:
            for key in ("input_before_tokens", "input_after_tokens", "input_delta_tokens", "fresh_context_added_tokens",
                        "prompt_linked", "prompt_content_stored", "prompt_linkage"):
                item[key] = decorated[key]
        item["context_items"] = [self._public_context_item(dict(value)) for value in self.connection.execute(
            "SELECT * FROM context_items WHERE turn_id=? ORDER BY id", (turn_id,)).fetchall()]
        item["tool_calls"] = [self._public_tool_call(dict(value)) for value in self.connection.execute(
            "SELECT * FROM tool_calls WHERE turn_id=? ORDER BY started_at, id", (turn_id,)).fetchall()]
        tool_names = [str(tool.get("tool_name") or "").lower() for tool in item["tool_calls"]]
        item["tool_summary"] = {
            "tools_used": len(item["tool_calls"]),
            "files_read": sum(bool(tool.get("file_path")) for tool in item["tool_calls"]),
            "mcp_calls": sum("mcp" in name for name in tool_names),
            "subagents": sum(any(marker in name for marker in ("agent", "subagent", "task")) for name in tool_names),
            "token_attribution_available": False,
        }
        item["likely_cause"] = None
        return item

    def turn_impact(self, turn_id: str) -> dict[str, Any] | None:
        """Report observed context recurrence without inventing token impact."""
        turn = self.turn(turn_id)
        if turn is None:
            return None
        ordered_turns = self.turns(turn["session_id"])
        turn_index = next((index for index, item in enumerate(ordered_turns) if item["id"] == turn_id), -1)
        if turn_index < 0:
            return None
        later_turns = ordered_turns[turn_index + 1:]
        introduced_by_hash: dict[str, list[dict[str, Any]]] = {}
        for item in turn["context_items"]:
            content_hash = item.get("content_hash")
            if content_hash:
                introduced_by_hash.setdefault(str(content_hash), []).append(item)
        items: list[dict[str, Any]] = []
        affected_later_turn_ids: set[str] = set()
        for content_hash, same_hash_items in introduced_by_hash.items():
            repeated_turn_ids: list[str] = []
            for later in later_turns:
                match = self.connection.execute(
                    "SELECT 1 FROM context_items WHERE turn_id=? AND content_hash=? LIMIT 1",
                    (later["id"], content_hash),
                ).fetchone()
                if match:
                    repeated_turn_ids.append(later["id"])
                    affected_later_turn_ids.add(later["id"])
            context = same_hash_items[0]
            items.append({
                "type": context.get("type"),
                "source": context.get("source"),
                "file_path": context.get("file_path"),
                "tool_name": context.get("tool_name"),
                "content_hash": content_hash,
                "unique_tokens": None,
                "injected_count": 1 + len(repeated_turn_ids),
                "subsequent_turns": len(repeated_turn_ids),
                "cumulative_tokens": None,
                "token_attribution_available": False,
            })
        persistent = len(affected_later_turn_ids)
        return {
            "turn_id": turn_id,
            "session_id": turn["session_id"],
            "initial_effective_input_tokens": self._effective_input(turn),
            "persistent_context_turns": persistent,
            "persistent_context_impact_tokens": None,
            "potentially_avoidable_tokens": None,
            "token_attribution_available": False,
            "attribution_note": "반복된 content hash와 이후 턴 수만 실제 기록으로 확인했습니다. provider가 원인별 토큰 귀속을 제공하지 않아 영향 토큰은 계산하지 않습니다.",
            "items": items,
        }

    def session_timeline(self, session_id: str, *, start: str | None = None, end: str | None = None) -> list[dict[str, Any]] | None:
        """Build a timeline of measured turns and observed side events."""
        if self.session(session_id) is None:
            return None
        events: list[dict[str, Any]] = []
        sequence = 0
        for turn in self.turns(session_id, start=start, end=end):
            sequence += 1
            effective_input = self._effective_input(turn)
            measured = any(turn.get(key) is not None for key in (
                "input_tokens", "cached_input_tokens", "fresh_input_tokens", "output_tokens", "reasoning_tokens"))
            events.append({
                "event_id": f"turn:{turn['id']}",
                "event_type": "turn",
                "timestamp": turn.get("timestamp"),
                "turn_id": turn["id"],
                "title": turn.get("user_prompt") or "Assistant turn",
                "input_tokens": turn.get("input_tokens"),
                "effective_input_tokens": effective_input,
                "cached_input_tokens": turn.get("cached_input_tokens"),
                "fresh_input_tokens": turn.get("fresh_input_tokens"),
                "output_tokens": turn.get("output_tokens"),
                "reasoning_tokens": turn.get("reasoning_tokens"),
                "total_tokens": effective_input + (turn.get("output_tokens") or 0),
                "estimated_cost": None,
                "cost_available": False,
                "is_estimated": False,
                "token_count_available": measured,
                "sequence": sequence,
            })
            context_items = [dict(row) for row in self.connection.execute(
                "SELECT * FROM context_items WHERE turn_id=? ORDER BY id", (turn["id"],)).fetchall()]
            tool_calls = [dict(row) for row in self.connection.execute(
                "SELECT * FROM tool_calls WHERE turn_id=? ORDER BY started_at, id", (turn["id"],)).fetchall()]
            for item in context_items:
                sequence += 1
                events.append({
                    "event_id": f"context:{item['id']}",
                    "event_type": "context_item",
                    "context_type": item.get("type"),
                    "timestamp": turn.get("timestamp"),
                    "turn_id": turn["id"],
                    "title": item.get("file_path") or item.get("tool_name") or item.get("type") or "Context item",
                    "token_count": None,
                    "file_path": item.get("file_path"),
                    "tool_name": item.get("tool_name"),
                    "mcp_server": item.get("mcp_server"),
                    "subagent_name": item.get("subagent_name"),
                    "content_hash": item.get("content_hash"),
                    "is_estimated": False,
                    "token_count_available": False,
                    "sequence": sequence,
                })
            for tool in tool_calls:
                sequence += 1
                events.append({
                    "event_id": f"tool:{tool['id']}",
                    "event_type": "tool_call",
                    "timestamp": tool.get("started_at") or turn.get("timestamp"),
                    "turn_id": turn["id"],
                    "title": tool.get("tool_name") or "Tool call",
                    "tool_name": tool.get("tool_name"),
                    "target": tool.get("target"),
                    "file_path": tool.get("file_path"),
                    "input_size": tool.get("input_size"),
                    "output_size": tool.get("output_size"),
                    "token_count": None,
                    "token_count_available": False,
                    "sequence": sequence,
                })
        events.sort(key=lambda item: (parse_timestamp(item.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc), item["sequence"]))
        return events

    def session_insights(self, session_id: str, *, start: str | None = None, end: str | None = None) -> dict[str, Any] | None:
        """Return provider-measured usage and observed activity only.

        Provider logs do not expose a complete context-to-token attribution graph,
        so this method intentionally never reports tool/context token estimates.
        """
        session = self.session(session_id)
        if session is None:
            return None
        turns = self.turns(session_id, start=start, end=end)
        measured = [turn for turn in turns if not bool(turn.get("is_estimated")) and any(
            turn.get(key) is not None for key in ("input_tokens", "cached_input_tokens", "fresh_input_tokens", "output_tokens", "reasoning_tokens"))]
        effective_input = sum(self._effective_input(turn) for turn in measured)
        output = sum(turn.get("output_tokens") or 0 for turn in measured)
        cached = sum(turn.get("cached_input_tokens") or 0 for turn in measured)
        fresh = sum(turn.get("fresh_input_tokens") or 0 for turn in measured)
        cache_rate = cached / (cached + fresh) if cached + fresh else None
        spikes: list[dict[str, Any]] = []
        for index, current in enumerate(measured):
            if index == 0:
                continue
            previous = self._effective_input(measured[index - 1])
            delta = self._effective_input(current) - previous
            if delta >= 50_000 or (previous > 0 and delta / previous >= 1):
                spikes.append({"turn_id": current["id"], "timestamp": current.get("timestamp"),
                               "previous_tokens": previous, "current_tokens": self._effective_input(current),
                               "delta_tokens": delta})
        turn_ids = [turn["id"] for turn in turns]
        placeholders = ",".join("?" for _ in turn_ids)
        tools = [dict(row) for row in self.connection.execute(
            f"SELECT * FROM tool_calls WHERE turn_id IN ({placeholders})", turn_ids
        ).fetchall()] if turn_ids else []
        context_rows = [dict(row) for row in self.connection.execute(
            f"SELECT * FROM context_items WHERE turn_id IN ({placeholders})", turn_ids
        ).fetchall()] if turn_ids else []
        by_hash: dict[str, list[dict[str, Any]]] = {}
        for item in context_rows:
            if item.get("content_hash"):
                by_hash.setdefault(item["content_hash"], []).append(item)
        repeated_contexts = [items for items in by_hash.values() if len({item["turn_id"] for item in items}) > 1]
        attribution: dict[str, dict[str, Any]] = {}
        for item in context_rows:
            item_type = str(item.get("type") or "other")
            bucket = attribution.setdefault(item_type, {"type": item_type, "items": 0})
            bucket["items"] += 1
        repeated_items: list[dict[str, Any]] = []
        for items in repeated_contexts:
            occurrences = len({item["turn_id"] for item in items})
            sample = items[0]
            repeated_items.append({"type": sample.get("type"), "file_path": sample.get("file_path"),
                                   "tool_name": sample.get("tool_name"), "injected_count": occurrences,
                                   "token_attribution_available": False})
        file_counts: dict[str, int] = {}
        for tool in tools:
            if tool.get("file_path"):
                file_counts[tool["file_path"]] = file_counts.get(tool["file_path"], 0) + 1
        duplicate_files = len([path for path in file_counts.values() if path >= 2])
        if start or end:
            alert_clauses = ["session_id=?", "type IN ('CONTEXT_SPIKE','CACHE_DROP','ABNORMAL_BURN','CONTEXT_PRESSURE')"]
            alert_params: list[Any] = [session_id]
            if start:
                alert_clauses.append("datetime(timestamp) >= datetime(?)")
                alert_params.append(start)
            if end:
                alert_clauses.append("datetime(timestamp) <= datetime(?)")
                alert_params.append(end)
            alert_count = self.connection.execute(
                f"SELECT COUNT(*) FROM alerts WHERE {' AND '.join(alert_clauses)}", alert_params
            ).fetchone()[0]
        else:
            alert_count = self.connection.execute("SELECT COUNT(*) FROM alerts WHERE session_id=? AND type IN ('CONTEXT_SPIKE','CACHE_DROP','ABNORMAL_BURN','CONTEXT_PRESSURE')", (session_id,)).fetchone()[0]
        tool_activity: dict[str, dict[str, Any]] = {}
        for tool in tools:
            name = str(tool.get("tool_name") or "unknown")
            activity = tool_activity.setdefault(name, {"tool_name": name, "calls": 0, "targets": []})
            activity["calls"] += 1
            if tool.get("target") and tool["target"] not in activity["targets"]:
                activity["targets"].append(tool["target"])
        return {
            "session_id": session_id,
            "effective_input_tokens": effective_input,
            "cached_input_tokens": cached,
            "fresh_input_tokens": fresh,
            "output_tokens": output,
            "estimated_cost": None,
            "cache_hit_rate": cache_rate,
            "actual_turns": len(measured),
            "estimated_turns": 0,
            "tool_calls": len(tools),
            "context_items": len(context_rows),
            "context_attribution": sorted(attribution.values(), key=lambda item: item["items"], reverse=True),
            "repeated_context_items": len(repeated_contexts),
            "duplicate_file_paths": duplicate_files,
            "spikes": spikes[:20],
            "alerts": alert_count,
            "token_attribution_available": False,
            "cost_available": False,
            "prompt_coverage": self._prompt_coverage(session_id, start=start, end=end),
            "tool_activity": sorted(tool_activity.values(), key=lambda item: item["calls"], reverse=True)[:10],
            "repeated_contexts": sorted(repeated_items, key=lambda item: item["injected_count"], reverse=True)[:10],
        }

    def _file_read_counts(self, session_id: str) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT file_path, COUNT(*) AS count FROM tool_calls WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?) AND file_path IS NOT NULL GROUP BY file_path",
            (session_id,),
        ).fetchall()
        return {row["file_path"]: row["count"] for row in rows}

    @staticmethod
    def _effective_input(turn: dict[str, Any]) -> int:
        cached = turn.get("cached_input_tokens")
        fresh = turn.get("fresh_input_tokens")
        if cached is not None or fresh is not None:
            return (cached or 0) + (fresh or 0)
        return turn.get("input_tokens") or 0

    def alerts(self, *, resolved: bool | None = None, alert_type: str | None = None,
               severity: str | None = None, agent: str | None = None,
               start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        # Only alerts based on provider-measured usage are user-facing.
        clauses: list[str] = ["al.type IN ('CONTEXT_SPIKE','CACHE_DROP','ABNORMAL_BURN','CONTEXT_PRESSURE')"]
        params: list[Any] = []
        if resolved is not None:
            clauses.append("al.resolved = ?")
            params.append(int(resolved))
        if alert_type:
            clauses.append("al.type = ?")
            params.append(alert_type)
        if severity:
            clauses.append("al.severity = ?")
            params.append(severity)
        if agent:
            clauses.append("a.name = ?")
            params.append(agent)
        if start:
            clauses.append("datetime(al.timestamp) >= datetime(?)")
            params.append(start)
        if end:
            clauses.append("datetime(al.timestamp) <= datetime(?)")
            params.append(end)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        result: list[dict[str, Any]] = []
        for row in self.connection.execute(f"""SELECT al.*, a.name AS agent_name, a.provider,
                   s.external_session_id, p.project_name, p.project_path
            FROM alerts al LEFT JOIN agents a ON a.id=al.agent_id
            LEFT JOIN sessions s ON s.id=al.session_id LEFT JOIN projects p ON p.id=s.project_id
            {where} ORDER BY al.timestamp DESC""", params).fetchall():
            item = dict(row)
            item["likely_cause"] = self._alert_cause(item.get("turn_id"))
            result.append(item)
        return result

    def _alert_cause(self, turn_id: str | None) -> dict[str, Any] | None:
        return None

    @staticmethod
    def _public_context_item(item: dict[str, Any]) -> dict[str, Any]:
        item["token_count"] = None
        item["token_count_available"] = False
        return item

    @staticmethod
    def _public_tool_call(item: dict[str, Any]) -> dict[str, Any]:
        item["estimated_tokens"] = None
        item["token_count_available"] = False
        return item

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search locally stored metadata and optional content without reading source logs."""
        needle = f"%{query.strip()}%"
        limit = max(1, min(limit, 200))
        results: list[dict[str, Any]] = []
        searches = [
            ("turn", """SELECT t.id AS result_id, t.session_id, t.id AS turn_id, t.timestamp,
                              COALESCE(t.user_prompt, '') AS title, 'Prompt / turn' AS match_kind,
                              a.name AS agent_name, COALESCE(p.project_name, '') AS project_name,
                              t.is_estimated
                       FROM turns t JOIN sessions s ON s.id=t.session_id
                       JOIN agents a ON a.id=s.agent_id LEFT JOIN projects p ON p.id=s.project_id
                       WHERE t.user_prompt LIKE ? OR t.model LIKE ? OR t.external_turn_id LIKE ?
                          OR s.external_session_id LIKE ? OR a.name LIKE ? OR p.project_name LIKE ?
                          OR p.project_path LIKE ? OR p.git_repository LIKE ? OR p.git_branch LIKE ?""",
             (needle,) * 9),
            ("context", """SELECT c.id AS result_id, t.session_id, c.turn_id, t.timestamp,
                              COALESCE(NULLIF(c.file_path, ''), NULLIF(c.tool_name, ''),
                                       NULLIF(c.source, ''), c.type) AS title,
                              'Context item' AS match_kind, a.name AS agent_name,
                              COALESCE(p.project_name, '') AS project_name, c.is_estimated
                       FROM context_items c JOIN turns t ON t.id=c.turn_id
                       JOIN sessions s ON s.id=t.session_id JOIN agents a ON a.id=s.agent_id
                       LEFT JOIN projects p ON p.id=s.project_id
                       WHERE c.type LIKE ? OR c.source LIKE ? OR c.file_path LIKE ?
                          OR c.tool_name LIKE ? OR c.mcp_server LIKE ? OR c.subagent_name LIKE ?
                          OR c.content LIKE ? OR s.external_session_id LIKE ? OR a.name LIKE ?
                          OR p.project_name LIKE ? OR p.project_path LIKE ? OR p.git_repository LIKE ? OR p.git_branch LIKE ?""",
             (needle,) * 13),
            ("tool", """SELECT tc.id AS result_id, t.session_id, tc.turn_id, t.timestamp,
                              tc.tool_name AS title, 'Tool call' AS match_kind,
                              a.name AS agent_name, COALESCE(p.project_name, '') AS project_name,
                              1 AS is_estimated
                       FROM tool_calls tc JOIN turns t ON t.id=tc.turn_id
                       JOIN sessions s ON s.id=t.session_id JOIN agents a ON a.id=s.agent_id
                       LEFT JOIN projects p ON p.id=s.project_id
                       WHERE tc.tool_name LIKE ? OR tc.target LIKE ? OR tc.file_path LIKE ?
                          OR s.external_session_id LIKE ? OR a.name LIKE ? OR p.project_name LIKE ?
                          OR p.project_path LIKE ? OR p.git_repository LIKE ? OR p.git_branch LIKE ?""",
             (needle,) * 9),
            ("alert", """SELECT al.id AS result_id, al.session_id, al.turn_id, al.timestamp,
                              al.title AS title, al.type AS match_kind,
                              COALESCE(a.name, '') AS agent_name, COALESCE(p.project_name, '') AS project_name, 1 AS is_estimated
                       FROM alerts al LEFT JOIN agents a ON a.id=al.agent_id
                       LEFT JOIN sessions s ON s.id=al.session_id LEFT JOIN projects p ON p.id=s.project_id
                       WHERE al.type LIKE ? OR al.title LIKE ? OR al.description LIKE ?
                          OR s.external_session_id LIKE ? OR a.name LIKE ? OR p.project_name LIKE ?
                          OR p.project_path LIKE ? OR p.git_repository LIKE ? OR p.git_branch LIKE ?""",
             (needle,) * 9),
        ]
        for _, statement, params in searches:
            results.extend(dict(row) for row in self.connection.execute(statement, params).fetchall())
        results.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
        return results[:limit]

    def usage_by_model(self, *, start: str | None = None, end: str | None = None,
                       agent: str | None = None, provider: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("datetime(t.timestamp) >= datetime(?)")
            params.append(start)
        if end:
            clauses.append("datetime(t.timestamp) <= datetime(?)")
            params.append(end)
        if agent:
            clauses.append("a.name = ?")
            params.append(agent)
        if provider:
            clauses.append("a.provider = ?")
            params.append(provider)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(f"""SELECT a.provider AS provider, a.name AS agent_name,
            COALESCE(t.model,'Unknown') AS model,
            COUNT(*) AS turns,
            SUM(CASE WHEN t.is_estimated=0 AND (t.input_tokens IS NOT NULL OR t.cached_input_tokens IS NOT NULL OR
                t.fresh_input_tokens IS NOT NULL OR t.output_tokens IS NOT NULL OR t.reasoning_tokens IS NOT NULL) THEN 1 ELSE 0 END) AS actual_turns,
            SUM(CASE WHEN t.is_estimated=1 THEN 1 ELSE 0 END) AS estimated_turns,
            COALESCE(SUM(t.input_tokens),0) AS input_tokens,
            COALESCE(SUM(t.cached_input_tokens),0) AS cached_tokens,
            COALESCE(SUM(t.fresh_input_tokens),0) AS fresh_tokens,
            COALESCE(SUM(t.output_tokens),0) AS output_tokens,
            COALESCE(SUM(t.reasoning_tokens),0) AS reasoning_tokens,
            SUM(t.estimated_cost) AS estimated_cost
            FROM turns t JOIN sessions s ON s.id=t.session_id JOIN agents a ON a.id=s.agent_id
            {where} GROUP BY a.provider, a.name, t.model ORDER BY CASE WHEN (COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)) > 0
                THEN COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)
                ELSE COALESCE(SUM(t.input_tokens),0) END + COALESCE(SUM(t.output_tokens),0) DESC""", params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["effective_input_tokens"] = item["cached_tokens"] + item["fresh_tokens"] or item["input_tokens"]
            item["total_tokens"] = item["effective_input_tokens"] + item["output_tokens"]
            denominator = item["cached_tokens"] + item["fresh_tokens"]
            item["cache_hit_rate"] = item["cached_tokens"] / denominator if denominator else None
            result.append(self._add_usage_quality(item))
        return result

    def usage_by_project(self, *, start: str | None = None, end: str | None = None,
                         agent: str | None = None, provider: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("datetime(t.timestamp) >= datetime(?)")
            params.append(start)
        if end:
            clauses.append("datetime(t.timestamp) <= datetime(?)")
            params.append(end)
        if agent:
            clauses.append("a.name = ?")
            params.append(agent)
        if provider:
            clauses.append("a.provider = ?")
            params.append(provider)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        if where:
            rows = self.connection.execute(f"""SELECT p.id AS project_id, COALESCE(p.project_name,'Unknown') AS project_name,
                p.project_path, a.name AS agent_name, COUNT(DISTINCT s.id) AS sessions, COALESCE(SUM(t.input_tokens),0) AS input_tokens,
                COALESCE(SUM(t.cached_input_tokens),0) AS cached_tokens,
                COALESCE(SUM(t.fresh_input_tokens),0) AS fresh_tokens,
                COALESCE(SUM(t.output_tokens),0) AS output_tokens,
                COALESCE(SUM(t.reasoning_tokens),0) AS reasoning_tokens,
                SUM(t.estimated_cost) AS estimated_cost
                FROM turns t JOIN sessions s ON s.id=t.session_id LEFT JOIN projects p ON p.id=s.project_id
                JOIN agents a ON a.id=s.agent_id {where}
                GROUP BY p.id ORDER BY CASE WHEN (COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)) > 0
                    THEN COALESCE(SUM(t.cached_input_tokens),0) + COALESCE(SUM(t.fresh_input_tokens),0)
                    ELSE COALESCE(SUM(t.input_tokens),0) END + COALESCE(SUM(t.output_tokens),0) DESC""", params).fetchall()
        else:
            rows = self.connection.execute("""SELECT p.id AS project_id, COALESCE(p.project_name,'Unknown') AS project_name,
                p.project_path, a.name AS agent_name, COUNT(s.id) AS sessions, COALESCE(SUM(s.total_input_tokens),0) AS input_tokens,
                COALESCE(SUM(s.total_cached_input_tokens),0) AS cached_tokens,
                COALESCE(SUM(s.total_fresh_input_tokens),0) AS fresh_tokens,
                COALESCE(SUM(s.total_output_tokens),0) AS output_tokens,
                COALESCE(SUM(s.total_reasoning_tokens),0) AS reasoning_tokens,
                SUM(s.estimated_cost) AS estimated_cost
                FROM projects p JOIN agents a ON a.id=p.agent_id LEFT JOIN sessions s ON s.project_id=p.id
                GROUP BY p.id ORDER BY CASE WHEN (cached_tokens + fresh_tokens) > 0 THEN cached_tokens + fresh_tokens ELSE input_tokens END + output_tokens DESC""").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["estimated_cost"] = None
            item["cost_available"] = False
            item["effective_input_tokens"] = item["cached_tokens"] + item["fresh_tokens"] or item["input_tokens"]
            item["total_tokens"] = item["effective_input_tokens"] + item["output_tokens"]
            denominator = item["cached_tokens"] + item["fresh_tokens"]
            item["cache_hit_rate"] = item["cached_tokens"] / denominator if denominator else None
            result.append(item)
        return result

    def usage_timeline(self, bucket_minutes: int = 60, *, start: str | None = None,
                       end: str | None = None, agent_name: str | None = None,
                       provider: str | None = None, project: str | None = None,
                       model: str | None = None, git_repository: str | None = None,
                       git_branch: str | None = None, min_tokens: int | None = None,
                       max_tokens: int | None = None, min_cost: float | None = None,
                       max_cost: float | None = None, alert_type: str | None = None,
                       session_id: str | None = None) -> list[dict[str, Any]]:
        bucket_minutes = bucket_minutes if bucket_minutes in {5, 15, 60, 1440} else 60
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("datetime(t.timestamp) >= datetime(?)")
            params.append(start)
        if end:
            clauses.append("datetime(t.timestamp) <= datetime(?)")
            params.append(end)
        if agent_name:
            clauses.append("a.name = ?")
            params.append(agent_name)
        if provider:
            clauses.append("a.provider = ?")
            params.append(provider)
        if project:
            clauses.append("(p.project_name = ? OR p.project_path = ?)")
            params.extend([project, project])
        if model:
            clauses.append("t.model = ?")
            params.append(model)
        if git_repository:
            clauses.append("p.git_repository = ?")
            params.append(git_repository)
        if git_branch:
            clauses.append("p.git_branch = ?")
            params.append(git_branch)
        if alert_type:
            clauses.append("s.id IN (SELECT session_id FROM alerts WHERE type = ?)")
            params.append(alert_type)
        if session_id:
            clauses.append("s.id = ?")
            params.append(session_id)
        effective_expression = "CASE WHEN t.cached_input_tokens IS NOT NULL OR t.fresh_input_tokens IS NOT NULL THEN COALESCE(t.cached_input_tokens,0) + COALESCE(t.fresh_input_tokens,0) ELSE COALESCE(t.input_tokens,0) END"
        if min_tokens is not None:
            clauses.append(f"{effective_expression} >= ?")
            params.append(min_tokens)
        if max_tokens is not None:
            clauses.append(f"{effective_expression} <= ?")
            params.append(max_tokens)
        if min_cost is not None:
            clauses.append("t.estimated_cost IS NOT NULL AND t.estimated_cost >= ?")
            params.append(min_cost)
        if max_cost is not None:
            clauses.append("t.estimated_cost IS NOT NULL AND t.estimated_cost <= ?")
            params.append(max_cost)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(f"""SELECT t.timestamp, t.input_tokens, t.cached_input_tokens,
            t.fresh_input_tokens, t.output_tokens, t.reasoning_tokens, t.estimated_cost, t.is_estimated,
            a.name AS agent_name, a.provider AS provider, t.model AS model
            FROM turns t JOIN sessions s ON s.id=t.session_id JOIN agents a ON a.id=s.agent_id
            LEFT JOIN projects p ON p.id=s.project_id {where}
            ORDER BY t.timestamp""", params).fetchall()
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            timestamp = parse_timestamp(row["timestamp"])
            if timestamp is None:
                continue
            epoch = int(timestamp.timestamp())
            bucket_epoch = epoch - (epoch % (bucket_minutes * 60))
            bucket = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).isoformat()
            item = buckets.setdefault(bucket, {"bucket": bucket, "input_tokens": 0, "effective_input_tokens": 0, "cached_tokens": 0,
                                               "fresh_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                                               "estimated_cost": None,
                                               "actual_turns": 0, "estimated_turns": 0, "agents": {}, "models": {}})
            for field, column in (("input_tokens", "input_tokens"), ("cached_tokens", "cached_input_tokens"),
                                  ("fresh_tokens", "fresh_input_tokens"), ("output_tokens", "output_tokens"),
                                  ("reasoning_tokens", "reasoning_tokens")):
                item[field] += row[column] or 0
            item["effective_input_tokens"] += ((row["cached_input_tokens"] or 0) + (row["fresh_input_tokens"] or 0)
                                                 if row["cached_input_tokens"] is not None or row["fresh_input_tokens"] is not None
                                                 else (row["input_tokens"] or 0))
            if row["estimated_cost"] is not None:
                item["estimated_cost"] = (item["estimated_cost"] or 0) + row["estimated_cost"]
            item["estimated_turns" if row["is_estimated"] else "actual_turns"] += 1
            agent = item["agents"].setdefault(row["agent_name"], {"input_tokens": 0, "effective_input_tokens": 0, "cached_tokens": 0,
                                                                    "fresh_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                                                                    "estimated_cost": None, "actual_turns": 0, "estimated_turns": 0})
            agent["input_tokens"] += row["input_tokens"] or 0
            agent["effective_input_tokens"] += ((row["cached_input_tokens"] or 0) + (row["fresh_input_tokens"] or 0)
                                                  if row["cached_input_tokens"] is not None or row["fresh_input_tokens"] is not None
                                                  else (row["input_tokens"] or 0))
            agent["cached_tokens"] += row["cached_input_tokens"] or 0
            agent["fresh_tokens"] += row["fresh_input_tokens"] or 0
            agent["output_tokens"] += row["output_tokens"] or 0
            agent["reasoning_tokens"] += row["reasoning_tokens"] or 0
            agent["estimated_turns" if row["is_estimated"] else "actual_turns"] += 1
            if row["estimated_cost"] is not None:
                agent["estimated_cost"] = (agent["estimated_cost"] or 0) + row["estimated_cost"]
            model_name = row["model"] or "Unknown"
            provider = row["provider"] or "unknown"
            model_key = f"{provider}::{model_name}"
            model = item["models"].setdefault(model_key, {"provider": provider, "agent_name": row["agent_name"],
                                                            "model": model_name, "input_tokens": 0,
                                                            "effective_input_tokens": 0, "cached_tokens": 0,
                                                            "fresh_tokens": 0, "output_tokens": 0,
                                                            "reasoning_tokens": 0, "estimated_cost": None,
                                                            "actual_turns": 0, "estimated_turns": 0})
            model["input_tokens"] += row["input_tokens"] or 0
            model["effective_input_tokens"] += ((row["cached_input_tokens"] or 0) + (row["fresh_input_tokens"] or 0)
                                                   if row["cached_input_tokens"] is not None or row["fresh_input_tokens"] is not None
                                                   else (row["input_tokens"] or 0))
            model["cached_tokens"] += row["cached_input_tokens"] or 0
            model["fresh_tokens"] += row["fresh_input_tokens"] or 0
            model["output_tokens"] += row["output_tokens"] or 0
            model["reasoning_tokens"] += row["reasoning_tokens"] or 0
            model["estimated_turns" if row["is_estimated"] else "actual_turns"] += 1
            if row["estimated_cost"] is not None:
                model["estimated_cost"] = (model["estimated_cost"] or 0) + row["estimated_cost"]
        for item in buckets.values():
            item["estimated_cost"] = None
            item["cost_available"] = False
            item["total_tokens"] = item["effective_input_tokens"] + item["output_tokens"]
            for agent in item["agents"].values():
                agent["estimated_cost"] = None
                agent["cost_available"] = False
                agent["total_tokens"] = agent["effective_input_tokens"] + agent["output_tokens"]
            for model in item["models"].values():
                model["estimated_cost"] = None
                model["cost_available"] = False
                model["total_tokens"] = model["effective_input_tokens"] + model["output_tokens"]
        return list(buckets.values())
