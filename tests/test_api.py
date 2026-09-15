from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_token_monitor.api import create_app
from agent_token_monitor.collector import Collector
from agent_token_monitor.storage import SQLiteStore

from test_phase1 import ROOT


class ApiTests(unittest.TestCase):
    def test_codex_thread_name_is_exposed_as_session_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "rollout.jsonl"
            log.write_text((ROOT / "fixtures" / "codex_sample.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            index = root / "session_index.jsonl"
            index.write_text(json.dumps({"id": "codex-session-1", "thread_name": "토큰 컨트롤 센터 구축"}, ensure_ascii=False) + "\n", encoding="utf-8")
            store = SQLiteStore(root / "monitor.db")
            Collector(store).scan([root])
            store.close()
            with patch.dict(os.environ, {"CODEX_SESSION_INDEX_PATH": str(index)}):
                app = create_app(root / "monitor.db")
                with TestClient(app) as client:
                    sessions = client.get("/api/sessions").json()
                    self.assertEqual(sessions[0]["session_name"], "토큰 컨트롤 센터 구축")
                    self.assertEqual(client.get(f"/api/sessions/{sessions[0]['id']}").json()["session_name"], "토큰 컨트롤 센터 구축")

    def test_claude_custom_title_is_exposed_as_session_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "claude.jsonl"
            log.write_text(
                (ROOT / "fixtures" / "claude_sample.jsonl").read_text(encoding="utf-8")
                + json.dumps({"type": "custom-title", "sessionId": "claude-session-1", "customTitle": "스케줄러 분석"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            store = SQLiteStore(root / "monitor.db")
            Collector(store).scan([log])
            store.close()
            with patch.dict(os.environ, {"CLAUDE_LOG_PATH": str(log)}):
                app = create_app(root / "monitor.db")
                with TestClient(app) as client:
                    sessions = client.get("/api/sessions").json()
                    self.assertEqual(sessions[0]["session_name"], "스케줄러 분석")

    def test_read_endpoints_return_phase1_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "rollout.jsonl"
            log.write_text((ROOT / "fixtures" / "codex_sample.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            store = SQLiteStore(root / "monitor.db")
            Collector(store).scan([root])
            store.close()
            app = create_app(root / "monitor.db")
            with TestClient(app) as client:
                cors = client.options("/api/overview", headers={"Origin": "http://tauri.localhost", "Access-Control-Request-Method": "GET"})
                self.assertEqual(cors.headers.get("access-control-allow-origin"), "http://tauri.localhost")
                overview = client.get("/api/overview?start=2026-09-11T00:00:00Z&end=2026-09-12T00:00:00Z")
                self.assertEqual(overview.status_code, 200)
                self.assertEqual(overview.json()["today"]["output_tokens"], 300)
                self.assertEqual(overview.json()["today"]["effective_input_tokens"], 2000)
                self.assertEqual(overview.json()["today"]["total_tokens"], 2300)
                self.assertEqual(overview.json()["today"]["by_agent"][0]["name"], "Codex")
                self.assertEqual(overview.json()["today"]["by_agent"][0]["effective_input_tokens"], 2000)
                self.assertEqual(overview.json()["today"]["by_agent"][0]["total_tokens"], 2300)
                self.assertEqual(overview.json()["today"]["by_agent"][0]["reasoning_tokens"], 80)
                self.assertEqual(overview.json()["today"]["by_agent"][0]["usage_quality"], "Actual")
                self.assertEqual(overview.json()["today"]["usage_quality"], "Actual")
                self.assertEqual(overview.json()["today"]["actual_turns"], 1)
                self.assertEqual(overview.json()["today"]["estimated_turns"], 0)
                self.assertEqual(overview.json()["top_sessions"][0]["total_tokens"], 2300)
                self.assertEqual(overview.json()["top_projects"][0]["total_tokens"], 2300)
                self.assertEqual(overview.json()["top_projects"][0]["session_count"], 1)
                scoped_overview = client.get("/api/overview?start=2026-09-11T01:00:01Z&end=2026-09-11T01:00:03Z&agent=Codex")
                self.assertEqual(scoped_overview.status_code, 200)
                self.assertEqual(scoped_overview.json()["today"]["effective_input_tokens"], 2000)
                self.assertEqual(client.get("/api/agents").json()[0]["name"], "Codex")
                sessions = client.get("/api/sessions").json()
                self.assertEqual(len(sessions), 1)
                session_id = sessions[0]["id"]
                self.assertAlmostEqual(sessions[0]["context_utilization"], 2000 / 128000)
                self.assertEqual(sessions[0]["prompt_coverage"]["status"], "COMPLETE")
                self.assertEqual(sessions[0]["prompt_coverage"]["linked_actual_tokens"], 2300)
                self.assertEqual(sessions[0]["health_status"], sessions[0]["status"])
                self.assertEqual(len(client.get(f"/api/sessions?session_id={session_id}").json()), 1)
                self.assertEqual(client.get("/api/sessions?session_id=missing").json(), [])
                turns = client.get(f"/api/sessions/{session_id}/turns").json()
                self.assertEqual(len(turns), 1)
                self.assertIn(300, [turn["output_tokens"] for turn in turns])
                self.assertEqual(turns[0]["total_tokens"], 2300)
                self.assertTrue(turns[0]["prompt_linked"])
                filtered_turns = client.get(f"/api/sessions/{session_id}/turns?start=2026-09-11T02:00:00Z&end=2026-09-11T23:59:59Z")
                self.assertEqual(filtered_turns.status_code, 200)
                self.assertEqual(filtered_turns.json(), [])
                insights = client.get(f"/api/sessions/{session_id}/insights")
                self.assertEqual(insights.status_code, 200)
                self.assertEqual(insights.json()["prompt_coverage"]["status"], "COMPLETE")
                timeline_response = client.get(f"/api/sessions/{session_id}/timeline")
                self.assertEqual(timeline_response.status_code, 200)
                self.assertTrue(any(row["event_type"] == "turn" for row in timeline_response.json()))
                self.assertTrue(any(row["event_type"] == "tool_call" for row in timeline_response.json()))
                timeline_turn = next(row for row in timeline_response.json() if row["event_type"] == "turn")
                self.assertEqual(timeline_turn["total_tokens"], 2300)
                future_timeline = client.get(f"/api/sessions/{session_id}/timeline?start=2099-01-01T00:00:00Z&end=2099-01-02T00:00:00Z")
                self.assertEqual(future_timeline.status_code, 200)
                self.assertEqual(future_timeline.json(), [])
                future_insights = client.get(f"/api/sessions/{session_id}/insights?start=2099-01-01T00:00:00Z&end=2099-01-02T00:00:00Z")
                self.assertEqual(future_insights.status_code, 200)
                self.assertEqual(future_insights.json()["effective_input_tokens"], 0)
                self.assertEqual(future_insights.json()["tool_calls"], 0)
                self.assertIsNone(future_insights.json()["estimated_cost"])
                tokenized_turn = next(turn for turn in turns if turn["input_tokens"] is not None)
                impact = client.get(f"/api/turns/{tokenized_turn['id']}/impact")
                self.assertEqual(impact.status_code, 200)
                self.assertEqual(impact.json()["turn_id"], tokenized_turn["id"])
                detail = client.get(f"/api/turns/{tokenized_turn['id']}")
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json()["tool_summary"]["tools_used"], 1)
                self.assertEqual(detail.json()["total_tokens"], 2300)
                self.assertIn("files_read", detail.json()["tool_summary"])
                self.assertIsNone(detail.json()["likely_cause"])
                self.assertFalse(detail.json()["tool_summary"]["token_attribution_available"])
                self.assertIsNone(detail.json()["tool_calls"][0]["estimated_tokens"])
                self.assertAlmostEqual(client.get(f"/api/sessions/{session_id}").json()["context_utilization"], 2000 / 128000)
                self.assertEqual(client.get(f"/api/sessions/{session_id}").json()["health_status"], "FINISHED")
                timeline = client.get("/api/usage/timeline?bucket_minutes=5")
                self.assertEqual(timeline.status_code, 200)
                self.assertIn("effective_input_tokens", timeline.json()[0])
                self.assertIn("models", timeline.json()[0])
                self.assertEqual(timeline.json()[0]["models"]["openai::gpt-5.6-terra"]["provider"], "openai")
                filtered_timeline = client.get("/api/usage/timeline?bucket_minutes=5&agent=Codex&model=gpt-5.6-terra&min_tokens=1500&max_tokens=2500")
                self.assertEqual(filtered_timeline.status_code, 200)
                self.assertEqual(filtered_timeline.json()[0]["effective_input_tokens"], 2000)
                session_timeline = client.get(f"/api/usage/timeline?session_id={session_id}&bucket_minutes=5")
                self.assertEqual(session_timeline.status_code, 200)
                self.assertEqual(session_timeline.json()[0]["effective_input_tokens"], 2000)
                session_overview = client.get(f"/api/overview?session_id={session_id}&start=2026-09-11T01:00:00Z")
                self.assertEqual(session_overview.status_code, 200)
                self.assertEqual(session_overview.json()["today"]["effective_input_tokens"], 2000)
                self.assertEqual(len(session_overview.json()["top_sessions"]), 1)
                empty_overview = client.get("/api/overview?start=2026-09-11T01:00:00Z&max_tokens=1000")
                self.assertEqual(empty_overview.status_code, 200)
                self.assertEqual(empty_overview.json()["today"]["effective_input_tokens"], 0)
                after_range = client.get("/api/overview?start=2026-09-11T02:00:00Z&end=2026-09-11T23:59:59Z")
                self.assertEqual(after_range.status_code, 200)
                self.assertEqual(after_range.json()["today"]["effective_input_tokens"], 0)
                before_range = client.get("/api/overview?start=2026-09-10T00:00:00Z&end=2026-09-11T00:59:59Z")
                self.assertEqual(before_range.status_code, 200)
                self.assertEqual(before_range.json()["today"]["effective_input_tokens"], 0)
                self.assertIn("effective_input_tokens", client.get("/api/usage/models").json()[0])
                self.assertEqual(client.get("/api/usage/models").json()[0]["total_tokens"], 2300)
                self.assertAlmostEqual(client.get("/api/usage/models").json()[0]["cache_hit_rate"], 0.3)
                self.assertEqual(client.get("/api/usage/models").json()[0]["provider"], "openai")
                self.assertEqual(client.get("/api/usage/models").json()[0]["actual_turns"], 1)
                self.assertIn("agent_name", client.get("/api/usage/projects").json()[0])
                self.assertEqual(client.get("/api/usage/projects").json()[0]["total_tokens"], 2300)
                self.assertEqual(client.get("/api/agents").json()[0]["total_tokens"], 2300)
                self.assertEqual(client.get("/api/projects").json()[0]["total_tokens"], 2300)
                self.assertEqual(client.get(f"/api/sessions/{session_id}").json()["total_tokens"], 2300)
                self.assertEqual(client.get("/api/usage/timeline?bucket_minutes=5").json()[0]["total_tokens"], 2300)
                self.assertEqual(client.get("/api/alerts").json(), [])
                self.assertGreaterEqual(len(client.get("/api/search?q=Codex").json()), 1)
                self.assertGreaterEqual(len(client.get("/api/search?q=workspace").json()), 1)
                scoped_sessions = client.get("/api/sessions?start=2026-09-11T01:00:01Z&end=2026-09-11T01:00:03Z").json()
                self.assertEqual(len(scoped_sessions), 1)
                self.assertEqual(scoped_sessions[0]["effective_input_tokens"], 2000)
                self.assertAlmostEqual(scoped_sessions[0]["context_utilization"], 2000 / 128000)
                self.assertEqual(client.get("/api/agents?provider=openai&start=2026-09-11T01:00:01Z").json()[0]["effective_input_tokens"], 2000)
                self.assertEqual(client.get("/api/agents?provider=openai&start=2026-09-11T01:00:01Z").json()[0]["usage_quality"], "Actual")
                self.assertEqual(client.get("/api/projects?agent=Codex&start=2026-09-11T01:00:01Z").json()[0]["effective_input_tokens"], 2000)
                self.assertEqual(client.get("/api/projects?agent=Codex&start=2026-09-11T01:00:01Z").json()[0]["reasoning_tokens"], 80)
                self.assertEqual(client.get("/api/usage/models?agent=Codex&start=2026-09-11T01:00:01Z").json()[0]["effective_input_tokens"], 2000)
                self.assertEqual(client.get("/api/projects?model=gpt-5.6-terra&start=2026-09-11T01:00:01Z").json()[0]["effective_input_tokens"], 2000)
                self.assertEqual(client.get("/api/usage/projects?provider=openai&start=2026-09-11T01:00:01Z").json()[0]["effective_input_tokens"], 2000)


if __name__ == "__main__":
    unittest.main()
