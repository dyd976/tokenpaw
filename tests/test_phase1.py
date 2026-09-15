from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_token_monitor.adapters.claude import ClaudeAdapter
from agent_token_monitor.adapters.codex import CodexAdapter
from agent_token_monitor.alerts import AlertEngine
from agent_token_monitor.collector import Collector
from agent_token_monitor.models import ContextItemData, NormalizedEvent, ToolCallData, Usage
from agent_token_monitor.pricing import PricingRule, PricingTable
from agent_token_monitor.storage import SQLiteStore
from agent_token_monitor.utils import parse_timestamp


ROOT = Path(__file__).parent


def read_events(name: str) -> list[dict]:
    return [json.loads(line) for line in (ROOT / "fixtures" / name).read_text(encoding="utf-8").splitlines()]


class Phase1Tests(unittest.TestCase):
    def test_claude_usage_and_tool_shape(self) -> None:
        adapter = ClaudeAdapter()
        state: dict = {}
        normalized = []
        for index, event in enumerate(read_events("claude_sample.jsonl"), 1):
            normalized.extend(adapter.parse_event(event, "claude_sample.jsonl", index, state))
        usage = [item for item in normalized if item.event_kind == "turn_usage"]
        tools = [item for item in normalized if item.event_kind == "tool_call"]
        results = [item for item in normalized if item.event_kind == "tool_result"]
        prompts = [item for item in normalized if item.event_kind == "user_prompt"]
        self.assertEqual(len(usage), 2)
        self.assertEqual(prompts[0].turn_id, usage[0].turn_id)
        self.assertEqual(usage[0].usage.cached_input_tokens, 900)
        self.assertEqual(usage[0].usage.fresh_input_tokens, 1300)
        self.assertEqual(usage[0].usage.output_tokens, 200)
        self.assertEqual(tools[0].tool_call.tool_name, "Read")
        self.assertEqual(tools[0].tool_call.file_path, "C:/workspace/demo/src/scheduler.py")
        self.assertEqual(results[0].tool_call.tool_name, "Read")
        self.assertEqual(results[0].context_items[0].item_type, "file")

    def test_codex_usage_and_turn_context(self) -> None:
        adapter = CodexAdapter()
        state: dict = {}
        normalized = []
        for index, event in enumerate(read_events("codex_sample.jsonl"), 1):
            normalized.extend(adapter.parse_event(event, "codex_sample.jsonl", index, state))
        usage = [item for item in normalized if item.event_kind == "turn_usage"]
        tools = [item for item in normalized if item.event_kind == "tool_call"]
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0].usage.input_tokens, 2000)
        self.assertEqual(usage[0].usage.cached_input_tokens, 600)
        self.assertEqual(usage[0].usage.fresh_input_tokens, 1400)
        self.assertEqual(usage[0].usage.context_window, 128000)
        self.assertEqual(tools[0].tool_call.tool_name, "shell_command")

    def test_prompt_is_attached_to_measured_turn(self) -> None:
        adapter = CodexAdapter()
        state: dict = {}
        normalized = []
        for index, event in enumerate(read_events("codex_sample.jsonl"), 1):
            normalized.extend(adapter.parse_event(event, "codex_sample.jsonl", index, state))
        prompt = next(item for item in normalized if item.event_kind == "user_prompt")
        usage = next(item for item in normalized if item.event_kind == "turn_usage")
        self.assertEqual(prompt.turn_id, usage.turn_id)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                log = Path(directory) / "rollout.jsonl"
                log.write_text((ROOT / "fixtures" / "codex_sample.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
                Collector(store).scan([log], store_prompt_content=True)
                turn = store.turns(store.sessions()[0]["id"])[0]
                self.assertEqual(turn["user_prompt"], "Inspect the scheduler")
                self.assertEqual(turn["input_tokens"], 2000)
            finally:
                store.close()

    def test_legacy_prompt_only_turns_are_backfilled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                prompt = NormalizedEvent(agent_name="Codex", provider="openai", source_file="legacy.jsonl", line_number=1,
                                         event_kind="user_prompt", event_id="legacy-prompt", session_id="legacy-session",
                                         timestamp=parse_timestamp("2026-09-11T01:00:00Z"), prompt_text="legacy prompt",
                                         context_items=[])
                usage = NormalizedEvent(agent_name="Codex", provider="openai", source_file="legacy.jsonl", line_number=2,
                                        event_kind="turn_usage", event_id="legacy-usage", turn_id="usage-turn",
                                        session_id="legacy-session", timestamp=parse_timestamp("2026-09-11T01:00:01Z"),
                                        usage=Usage(input_tokens=100, cached_input_tokens=50, fresh_input_tokens=50))
                store.ingest(prompt, store_prompt_content=True)
                store.ingest(usage)
                self.assertEqual(store.backfill_prompt_turns(), 1)
                turns = store.turns(store.sessions()[0]["id"])
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["user_prompt"], "legacy prompt")
            finally:
                store.close()

    def test_session_aggregation_and_cache_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "rollout.jsonl"
            log.write_text((ROOT / "fixtures" / "codex_sample.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            store = SQLiteStore(root / "monitor.db")
            try:
                Collector(store).scan([root])
                summary = store.summary()
                self.assertEqual(summary["input_tokens"], 2000)
                self.assertEqual(summary["cached_tokens"], 600)
                self.assertEqual(summary["fresh_tokens"], 1400)
                self.assertAlmostEqual(summary["cache_hit_rate"], 0.3)
                sessions = store.sessions()
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["total_output_tokens"], 300)
                measured_turn = next(turn for turn in store.turns(sessions[0]["id"]) if turn["input_tokens"] is not None)
                self.assertAlmostEqual(measured_turn["context_utilization"], 2000 / 128000)
                self.assertIsNone(measured_turn["input_before_tokens"])
                self.assertEqual(measured_turn["input_after_tokens"], 2000)
                self.assertEqual(measured_turn["fresh_context_added_tokens"], 1400)
            finally:
                store.close()

    def test_aggregate_lists_sort_by_effective_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "claude.jsonl").write_text((ROOT / "fixtures" / "claude_sample.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            (root / "codex.jsonl").write_text((ROOT / "fixtures" / "codex_sample.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            store = SQLiteStore(root / "monitor.db")
            try:
                Collector(store).scan([root])
                agents = store.agents()
                projects = store.projects()
                self.assertEqual(agents[0]["name"], "Claude Code")
                self.assertEqual(projects[0]["agent_name"], "Claude Code")
                self.assertGreater(agents[0]["effective_input_tokens"] + agents[0]["output_tokens"],
                                   agents[1]["effective_input_tokens"] + agents[1]["output_tokens"])
            finally:
                store.close()

    def test_turn_impact_deduplicates_repeated_hashes_per_later_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                common = dict(agent_name="Codex", provider="openai", source_file="fixture.jsonl",
                              timestamp=parse_timestamp("2026-09-11T01:00:00Z"), session_id="impact-session")
                for index, turn_id in enumerate(("t1", "t2", "t3"), 1):
                    context = []
                    if turn_id == "t1":
                        context = [ContextItemData(item_type="file", source="read", token_count=100,
                                                   content_hash="same-content", file_path="src/a.py")]
                    elif turn_id == "t2":
                        # Two adapter records for the same content must count once.
                        context = [ContextItemData(item_type="file", source="read", token_count=100,
                                                   content_hash="same-content", file_path="src/a.py"),
                                    ContextItemData(item_type="tool_output", source="read.result", token_count=100,
                                                   content_hash="same-content", file_path="src/a.py")]
                    else:
                        context = [ContextItemData(item_type="file", source="read", token_count=100,
                                                   content_hash="same-content", file_path="src/a.py")]
                    event = NormalizedEvent(**{**common, "line_number": index, "event_id": f"usage-{turn_id}",
                                               "timestamp": parse_timestamp(f"2026-09-11T01:00:0{index}Z"),
                                               "event_kind": "turn_usage", "turn_id": turn_id,
                                               "usage": Usage(input_tokens=100, cached_input_tokens=50,
                                                              fresh_input_tokens=50), "context_items": context})
                    self.assertTrue(store.ingest(event))
                session_id = store.sessions()[0]["id"]
                first_turn_id = store.turns(session_id)[0]["id"]
                impact = store.turn_impact(first_turn_id)
                self.assertIsNotNone(impact)
                self.assertEqual(impact["persistent_context_turns"], 2)
                self.assertEqual(impact["potentially_avoidable_tokens"], 200)
                self.assertEqual(len(impact["items"]), 1)
                self.assertEqual(impact["items"][0]["injected_count"], 3)
                self.assertEqual(impact["items"][0]["cumulative_tokens"], 200)
                self.assertEqual(impact["confidence"], "Low")
                self.assertEqual(impact["attribution_method"], "content_hash_recurrence_estimate")
            finally:
                store.close()

    def test_aggregate_usage_quality_distinguishes_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                event = NormalizedEvent(agent_name="Codex", provider="openai", source_file="fixture.jsonl", line_number=1,
                                         event_kind="turn_usage", event_id="estimated-usage", turn_id="estimated-turn",
                                         session_id="estimated-session", timestamp=parse_timestamp("2026-09-11T01:00:00Z"),
                                         usage=Usage(input_tokens=100, output_tokens=20, is_estimated=True))
                self.assertTrue(store.ingest(event))
                self.assertEqual(store.agents()[0]["usage_quality"], "Estimated")
                self.assertEqual(store.projects()[0]["usage_quality"], "Estimated")
                self.assertEqual(store.sessions()[0]["usage_quality"], "Estimated")
            finally:
                store.close()

    def test_local_pricing_is_optional_and_computed_per_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pricing = root / "pricing.json"
            pricing.write_text(json.dumps({"pricing": [{"provider": "openai", "model": "gpt-5.6-terra",
                                                        "input_price": 1, "cached_input_price": 0.5,
                                                        "output_price": 2, "reasoning_price": 3}]}), encoding="utf-8")
            store = SQLiteStore(root / "monitor.db", pricing_path=pricing)
            try:
                event = NormalizedEvent(agent_name="Codex", provider="openai", source_file="fixture.jsonl", line_number=1,
                                        event_kind="turn_usage", event_id="price-1", turn_id="turn-1",
                                        session_id="price-session", timestamp=parse_timestamp("2026-09-11T01:00:00Z"),
                                        model="gpt-5.6-terra", usage=Usage(input_tokens=1000, cached_input_tokens=400,
                                                                            fresh_input_tokens=600, output_tokens=100,
                                                                            reasoning_tokens=10))
                self.assertTrue(store.ingest(event))
                turn = store.turns(store.sessions()[0]["id"])[0]
                self.assertAlmostEqual(turn["estimated_cost"], 0.00103)
                self.assertAlmostEqual(store.summary()["estimated_cost"], 0.00103)
            finally:
                store.close()

    def test_pricing_effective_dates_normalize_timezones(self) -> None:
        table = PricingTable([PricingRule(provider="openai", model="gpt-test", input_price=1,
                                           effective_from="2026-09-11T09:00:00+09:00",
                                           effective_to="2026-09-11T10:00:00+09:00")])
        self.assertAlmostEqual(table.estimate(provider="openai", model="gpt-test",
                                              timestamp="2026-09-11T00:30:00Z", cached_tokens=0,
                                              fresh_tokens=1000, input_tokens=1000, output_tokens=0,
                                              reasoning_tokens=0), 0.001)
        self.assertIsNone(table.estimate(provider="openai", model="gpt-test",
                                         timestamp="2026-09-11T01:00:00Z", cached_tokens=0,
                                         fresh_tokens=1000, input_tokens=1000, output_tokens=0,
                                         reasoning_tokens=0))

    def test_optional_prompt_storage_masks_sensitive_strings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                event = NormalizedEvent(agent_name="Codex", provider="openai", source_file="fixture.jsonl", line_number=1,
                                        event_kind="user_prompt", event_id="prompt-secret", session_id="secret-session",
                                        timestamp=parse_timestamp("2026-09-11T01:00:00Z"), prompt_text="api_key=sk-abcdefghijklmnop1234")
                self.assertTrue(store.ingest(event, store_prompt_content=True))
                prompt = store.turns(store.sessions()[0]["id"])[0]["user_prompt"]
                self.assertIn("[REDACTED]", prompt)
                self.assertNotIn("sk-abcdefghijklmnop1234", prompt)
            finally:
                store.close()

    def test_duplicate_prompt_replay_backfills_content_without_duplicate_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                event = NormalizedEvent(agent_name="Codex", provider="openai", source_file="fixture.jsonl", line_number=1,
                                        event_kind="user_prompt", event_id="prompt-replay", session_id="replay-session",
                                        turn_id="turn-replay", timestamp=parse_timestamp("2026-09-11T01:00:00Z"),
                                        prompt_text="Explain the scheduler")
                self.assertTrue(store.ingest(event, store_prompt_content=False))
                self.assertFalse(store.ingest(event, store_prompt_content=True))
                turns = store.turns(store.sessions()[0]["id"])
                self.assertEqual(len(turns), 1)
                self.assertEqual(turns[0]["user_prompt"], "Explain the scheduler")
            finally:
                store.close()

    def test_duplicate_and_incremental_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "rollout.jsonl"
            source = ROOT / "fixtures" / "codex_sample.jsonl"
            log.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            store = SQLiteStore(root / "monitor.db")
            try:
                collector = Collector(store)
                first = collector.scan([root])
                second = collector.scan([root])
                self.assertGreater(first["events"], 0)
                self.assertEqual(second["events"], 0)
                self.assertEqual(second["duplicates"], 0)
                before = store.summary()["output_tokens"]
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("{\"timestamp\":\"2026-09-11T01:00:03Z\",\"ordinal\":7,\"type\":\"event_msg\",\"payload\":{\"type\":\"token_count\",\"info\":{\"last_token_usage\":{\"input_tokens\":100,\"cached_input_tokens\":0,\"output_tokens\":10,\"reasoning_output_tokens\":0}}}}\n")
                third = collector.scan([root])
                self.assertGreater(third["events"], 0)
                self.assertEqual(store.summary()["output_tokens"], before + 10)
            finally:
                store.close()

    def test_partial_jsonl_line_waits_until_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "rollout.jsonl"
            first_line = (ROOT / "fixtures" / "codex_sample.jsonl").read_text(encoding="utf-8").splitlines()[0]
            log.write_text(first_line, encoding="utf-8")
            store = SQLiteStore(root / "monitor.db")
            try:
                collector = Collector(store)
                first = collector.scan([root])
                self.assertEqual(first["events"], 0)
                with log.open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                second = collector.scan([root])
                self.assertGreater(second["events"], 0)
                self.assertEqual(collector.scan([root])["events"], 0)
            finally:
                store.close()

    def test_corrupt_and_unknown_event_do_not_crash_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "rollout.jsonl"
            valid = read_events("codex_sample.jsonl")[0]
            log.write_text(json.dumps(valid) + "\nnot-json\n{\"type\":\"future_schema\",\"payload\":{\"text\":\"do not retain\",\"version\":7}}\n", encoding="utf-8")
            store = SQLiteStore(root / "monitor.db")
            try:
                stats = Collector(store).scan([root])
                self.assertGreaterEqual(stats["errors"], 1)
                self.assertGreaterEqual(stats["unknown"], 1)
                count = store.connection.execute("SELECT COUNT(*) FROM ingested_events WHERE parsed=0").fetchone()[0]
                self.assertGreaterEqual(count, 2)
                unknown = store.connection.execute("SELECT raw_json FROM ingested_events WHERE raw_type='future_schema'").fetchone()
                self.assertIsNotNone(unknown)
                self.assertIn("future_schema", unknown[0])
                self.assertNotIn("do not retain", unknown[0])
            finally:
                store.close()

    def test_alert_rules_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                common = dict(agent_name="Codex", provider="openai", source_file="fixture.jsonl", line_number=1,
                              timestamp=None, session_id="alert-session", model="gpt-test", cwd="C:/workspace/demo")
                first = NormalizedEvent(**{**common, "timestamp": parse_timestamp("2026-09-11T01:00:01+00:00")}, event_kind="turn_usage", event_id="u1", turn_id="t1",
                                        usage=Usage(input_tokens=100, cached_input_tokens=80, fresh_input_tokens=20,
                                                    output_tokens=10, context_window=400))
                second = NormalizedEvent(**{**common, "timestamp": parse_timestamp("2026-09-11T01:00:02+00:00")}, event_kind="turn_usage", event_id="u2", turn_id="t2",
                                         usage=Usage(input_tokens=500, cached_input_tokens=0, fresh_input_tokens=500,
                                                     output_tokens=10, context_window=400))
                tool = NormalizedEvent(**{**common, "timestamp": parse_timestamp("2026-09-11T01:00:02+00:00")}, event_kind="tool_call", event_id="tool1", turn_id="t2",
                                       tool_call=ToolCallData(external_call_id="c1", tool_name="shell_command",
                                                              estimated_tokens=50001, target="C:/.codex/sessions/log.jsonl"))
                store.ingest(first)
                store.ingest(second)
                store.ingest(tool)
                self.assertGreater(AlertEngine(store).evaluate(), 0)
                first_count = store.connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
                self.assertEqual(AlertEngine(store).evaluate(), 0)
                self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], first_count)
                types = {row[0] for row in store.connection.execute("SELECT type FROM alerts")}
                self.assertIn("CONTEXT_SPIKE", types)
                self.assertIn("CACHE_DROP", types)
                self.assertIn("LARGE_TOOL_OUTPUT", types)
                self.assertNotIn("SESSION_LOG_INGESTION", types)
            finally:
                store.close()

    def test_alert_spike_falls_back_to_raw_input_without_cache_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "monitor.db")
            try:
                common = dict(agent_name="Codex", provider="openai", source_file="fixture.jsonl",
                              line_number=1, session_id="raw-input-alerts", model="gpt-test", cwd="C:/workspace/demo")
                for index, value in enumerate((100, 200_000), 1):
                    event = NormalizedEvent(**{**common, "line_number": index,
                                               "timestamp": parse_timestamp(f"2026-09-11T02:00:0{index}Z")},
                                             event_kind="turn_usage", event_id=f"raw-{index}", turn_id=f"raw-turn-{index}",
                                             usage=Usage(input_tokens=value, output_tokens=1))
                    store.ingest(event)
                self.assertGreater(AlertEngine(store).evaluate(), 0)
                types = {row[0] for row in store.connection.execute("SELECT type FROM alerts")}
                self.assertIn("CONTEXT_SPIKE", types)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
