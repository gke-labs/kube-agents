"""Every Hermes profile config gives the gateway's provider the same model-call deadlines.

The agent reaches its model through the custom provider in each profile's
config.yaml. Hermes reads `providers.<id>.stale_timeout_seconds` and
`request_timeout_seconds` for it (hermes_cli/timeouts.py); a profile without
them, or with the block under another key, falls back to Hermes' own defaults
(180s stale, 1800s per read). The three configs carry the block separately, so
this holds them together.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CONFIGS = (
    _REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml",
    _REPO_ROOT / "agents" / "chat" / "config.yaml",
    _REPO_ROOT / "agents" / "cluster" / "config.yaml",
)


class TestGatewayDeadlines(unittest.TestCase):
    def test_each_profile_sets_both_deadlines_on_its_model_provider(self) -> None:
        for path in _CONFIGS:
            with self.subTest(config=str(path.relative_to(_REPO_ROOT))):
                config = yaml.safe_load(path.read_text())
                provider = config["model"]["provider"]
                self.assertEqual(provider, "custom")
                deadlines = config["providers"][provider]
                self.assertEqual(deadlines["stale_timeout_seconds"], 300)
                self.assertEqual(deadlines["request_timeout_seconds"], 900)


if __name__ == "__main__":
    unittest.main()
