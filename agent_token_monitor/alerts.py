from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Any

from agent_token_monitor.storage import SQLiteStore
from agent_token_monitor.utils import stable_id


@dataclass(slots=True)
class AlertThresholds:
    context_spike_percent: float = 100
    context_spike_tokens: int = 50_000
    cache_drop_percent: float = 50
    large_tool_output: int = 50_000
    large_file_read: int = 50_000
    context_warning: float = 70
    context_critical: float = 85
    repeated_file_reads: int = 5
    repeated_context_count: int = 3
    fanout_count: int = 5


class AlertEngine:
    """Deterministic, idempotent heuristics over normalized Phase 1 data."""

    def __init__(self, store: SQLiteStore, thresholds: AlertThresholds | None = None):
        self.store = store
        self.thresholds = thresholds or AlertThresholds()

    def evaluate(self) -> int:
        created = 0
        sessions = self.store.sessions()
        for session in sessions:
            turns = self.store.turns(session["id"])
            created += self._session_rules(session, turns)
            created += self._tool_rules(session, turns)
            created += self._context_rules(session, turns)
        self.store.connection.commit()
        return created

    def _session_rules(self, session: dict[str, Any], turns: list[dict[str, Any]]) -> int:
        created = 0
        measured = [turn for turn in turns if turn["input_tokens"] is not None]
        for index, turn in enumerate(measured):
            current = self._effective_input(turn)
            previous = self._effective_input(measured[index - 1]) if index else 0
            if previous and ((current - previous) >= self.thresholds.context_spike_tokens or
                             current / previous - 1 >= self.thresholds.context_spike_percent / 100):
                created += self._insert(session, turn, "CONTEXT_SPIKE", "CRITICAL",
                                        "Input token spike detected",
                                        f"Input rose from {previous:,} to {current:,} tokens (+{current-previous:,}).",
                                        "effective_input_tokens", previous, current)
            if index >= 1:
                baseline_rates = [self._cache_rate(item) for item in measured[max(0, index - 5):index]]
                baseline_rates = [rate for rate in baseline_rates if rate is not None]
                current_rate = self._cache_rate(turn)
                if baseline_rates and current_rate is not None and current_rate < mean(baseline_rates) * (1 - self.thresholds.cache_drop_percent / 100):
                    created += self._insert(session, turn, "CACHE_DROP", "WARNING",
                                            "Cache hit rate dropped",
                                            f"Cache hit fell from {mean(baseline_rates):.1%} average to {current_rate:.1%}.",
                                            "cache_hit_rate", mean(baseline_rates), current_rate)
            if index >= 1:
                prior = [self._effective_input(item) for item in measured[max(0, index - 5):index]]
                baseline = mean(prior) if prior else 0
                if baseline and current > baseline * 3:
                    created += self._insert(session, turn, "ABNORMAL_BURN", "WARNING",
                                            "Abnormal token burn",
                                            f"Current effective input {current:,} is over 3x the previous five-turn mean ({baseline:,.0f}).",
                                            "effective_input_tokens", baseline, current)
            if turn["context_window"] and current:
                utilization = current / turn["context_window"] * 100
                if utilization >= self.thresholds.context_critical:
                    created += self._insert(session, turn, "CONTEXT_PRESSURE", "CRITICAL",
                                            "Context window pressure is critical",
                                            f"Effective input uses approximately {utilization:.1f}% of the recorded context window.",
                                            "context_utilization", self.thresholds.context_critical, utilization)
                elif utilization >= self.thresholds.context_warning:
                    created += self._insert(session, turn, "CONTEXT_PRESSURE", "WARNING",
                                            "Context window pressure is high",
                                            f"Effective input uses approximately {utilization:.1f}% of the recorded context window.",
                                            "context_utilization", self.thresholds.context_warning, utilization)
        return created

    def _tool_rules(self, session: dict[str, Any], turns: list[dict[str, Any]]) -> int:
        created = 0
        turn_order = {turn["id"]: turn for turn in turns}
        tool_rows = self.store.connection.execute("SELECT * FROM tool_calls WHERE turn_id IN (SELECT id FROM turns WHERE session_id=?)", (session["id"],)).fetchall()
        file_hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
        fanout: list[dict[str, Any]] = []
        for row in tool_rows:
            tool = dict(row)
            turn = turn_order.get(tool["turn_id"])
            if not turn:
                continue
            estimated = tool["estimated_tokens"] or 0
            if estimated >= self.thresholds.large_tool_output:
                created += self._insert(session, turn, "LARGE_TOOL_OUTPUT", "WARNING",
                                        "Large tool output detected",
                                        f"{tool['tool_name']} produced approximately {estimated:,} estimated tokens.",
                                        "estimated_tool_tokens", self.thresholds.large_tool_output, estimated)
            if tool["file_path"]:
                file_hits[tool["file_path"]].append({"tool": tool, "turn": turn})
                if estimated >= self.thresholds.large_file_read:
                    created += self._insert(session, turn, "LARGE_FILE_READ", "WARNING",
                                            "Large file read detected",
                                            f"{tool['file_path']} added approximately {estimated:,} estimated tokens.",
                                            "estimated_file_tokens", self.thresholds.large_file_read, estimated)
            if any(token in (tool["tool_name"] or "").lower() for token in ("agent", "subagent", "task")):
                fanout.append({"tool": tool, "turn": turn})
        for file_path, hits in file_hits.items():
            ordered = sorted(hits, key=lambda item: (item["turn"]["timestamp"] or "", item["turn"]["id"]))
            for index, item in enumerate(ordered):
                window = ordered[max(0, index - 9):index + 1]
                if len(window) >= self.thresholds.repeated_file_reads:
                    created += self._insert(session, item["turn"], "REPEATED_FILE_READ", "WARNING",
                                            "Repeated file read detected",
                                            f"{file_path} was read {len(window)} times within the last ten turns.",
                                            "file_read_count", self.thresholds.repeated_file_reads, len(window))
                    break
        if len(fanout) >= self.thresholds.fanout_count:
            item = fanout[-1]
            created += self._insert(session, item["turn"], "AGENT_FANOUT", "WARNING",
                                    "High sub-agent fan-out detected",
                                    f"Detected {len(fanout)} agent/task tool calls in this session.",
                                    "fanout_count", self.thresholds.fanout_count, len(fanout))
        return created

    def _context_rules(self, session: dict[str, Any], turns: list[dict[str, Any]]) -> int:
        created = 0
        turn_by_id = {turn["id"]: turn for turn in turns}
        rows = self.store.connection.execute("""SELECT c.*, t.session_id FROM context_items c JOIN turns t ON t.id=c.turn_id
            WHERE t.session_id=? AND c.content_hash IS NOT NULL ORDER BY t.timestamp, c.id""", (session["id"],)).fetchall()
        by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_hash[row["content_hash"]].append(dict(row))
        for hash_value, items in by_hash.items():
            unique_turns = {item["turn_id"] for item in items}
            if len(unique_turns) >= self.thresholds.repeated_context_count:
                latest_turn = turn_by_id.get(items[-1]["turn_id"])
                if latest_turn:
                    created += self._insert(session, latest_turn, "REPEATED_CONTEXT", "WARNING",
                                            "Repeated context detected",
                                            f"The same content hash was injected across {len(unique_turns)} turns.",
                                            "repeated_context_turns", self.thresholds.repeated_context_count, len(unique_turns))
        return created

    def _insert(self, session: dict[str, Any], turn: dict[str, Any], alert_type: str, severity: str,
                title: str, description: str, metric: str, baseline: float, current: float) -> int:
        alert_id = stable_id("alert", alert_type, session["id"], turn["id"])
        cursor = self.store.connection.execute(
            """INSERT OR IGNORE INTO alerts(id,agent_id,session_id,turn_id,timestamp,severity,type,title,description,metric,baseline,current_value)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (alert_id, session["agent_id"], session["id"], turn["id"], turn["timestamp"], severity, alert_type,
             title, description, metric, baseline, current),
        )
        return cursor.rowcount

    @staticmethod
    def _effective_input(turn: dict[str, Any]) -> int:
        cached = turn.get("cached_input_tokens")
        fresh = turn.get("fresh_input_tokens")
        if cached is not None or fresh is not None:
            return (cached or 0) + (fresh or 0)
        return turn.get("input_tokens") or 0

    @staticmethod
    def _cache_rate(turn: dict[str, Any]) -> float | None:
        cached = turn["cached_input_tokens"] or 0
        fresh = turn["fresh_input_tokens"] or 0
        return cached / (cached + fresh) if cached + fresh else None
