from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from agent_token_monitor.cli import _print_status


class _FakeStore:
    def agents(self, **_: object) -> list[dict]:
        return [{"name": "No cache agent", "effective_input_tokens": 100, "output_tokens": 10,
                 "input_tokens": 100, "cached_tokens": 0, "fresh_tokens": 100,
                 "cache_hit_rate": None, "estimated_cost": None}]

    def sessions(self, **_: object) -> list[dict]:
        return [{"project_name": "demo", "total_cached_input_tokens": 0,
                 "total_fresh_input_tokens": 100, "total_output_tokens": 10,
                 "external_session_id": "session-1", "health_status": "FINISHED"}]

    def projects(self, **_: object) -> list[dict]:
        return [{"project_name": "demo", "effective_input_tokens": 100,
                 "output_tokens": 10}]

    def alerts(self, **_: object) -> list[dict]:
        return []

    def summary(self) -> dict:
        return {"sessions": 1}


class CliTests(unittest.TestCase):
    def test_status_handles_missing_cache_rate(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            _print_status(_FakeStore(), False)
        self.assertIn("cache Unknown", output.getvalue())
        self.assertIn("Top session: demo", output.getvalue())
        self.assertIn("Top project: demo", output.getvalue())


if __name__ == "__main__":
    unittest.main()
