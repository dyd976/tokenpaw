from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_token_monitor.config import alert_thresholds, collector_paths, load_config, privacy_options


class ConfigTests(unittest.TestCase):
    def test_local_config_controls_paths_privacy_and_alert_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "collectors": {
                    "claude": {"enabled": False, "paths": ["C:/ignored"]},
                    "codex": {"paths": ["C:/codex"]},
                },
                "privacy": {"store_prompt_content": True, "mask_sensitive_strings": False},
                "alerts": {"large_tool_output": 1234, "context_warning": 55},
            }), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(collector_paths(config, "claude", ["default"]), [])
            self.assertEqual(collector_paths(config, "codex", ["default"]), ["C:/codex"])
            self.assertEqual(privacy_options(config), {"store_prompt_content": True, "store_tool_output": False,
                                                       "store_file_content": False, "mask_sensitive": False})
            thresholds = alert_thresholds(config)
            self.assertEqual(thresholds.large_tool_output, 1234)
            self.assertEqual(thresholds.context_warning, 55)


if __name__ == "__main__":
    unittest.main()
