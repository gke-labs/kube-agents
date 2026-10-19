"""Seam: cron relay adapter → session KV relay → chat_delivery_watch, with no chat route.

A `next` install whose gateway holds Slack with no home channel (#3385). The
relay must post nowhere, and the refusal must survive the trip it takes to the
reader: the route's 502 `detail`, the adapter's `last_delivery_error`, and the
ledger issue's platform column, which `chat_delivery_watch` parses out of it.
The cronjob tool repeats the first 200 characters of the same string in chat,
so the setting it names has to land inside them.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from _seams import API_KEY, REPO_ROOT, SCRIPTS_DIR, KVServer, fake_executable

sys.path.insert(0, str(SCRIPTS_DIR))
import chat_delivery_watch  # noqa: E402

ADAPTER_PATH = REPO_ROOT / "deploy" / "docker" / "plugins" / "chat" / "adapter.py"
# What the Hermes cronjob tool shows the model of `last_delivery_error`.
CRONJOB_TOOL_ERROR_CHARS = 200
BOTH_PLATFORMS_OFF = (
    "platforms:\n  google_chat:\n    enabled: false\n  slack:\n    enabled: false\n"
)
REPORT = {"job_id": "fleet-wide-cost-analysis", "profile": "platform", "report": "finding"}


def _load_adapter():
    spec = importlib.util.spec_from_file_location("chat_relay_adapter", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCronRelayWithNoHomeChannel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = _load_adapter()

    def _relay(self, gateway_platform: str):
        """Relay one report through the real adapter and server; return the
        adapter's error and the argv of every `hermes` the server ran."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            managed = tmp_path / "managed"
            managed.mkdir()
            (managed / "config.yaml").write_text(BOTH_PLATFORMS_OFF)
            record = tmp_path / "hermes-calls.jsonl"
            fake_executable(
                tmp_path / "bin",
                "hermes",
                f"""
                import json, sys
                with open({str(record)!r}, "a") as handle:
                    handle.write(json.dumps(sys.argv[1:]) + "\\n")
                sys.exit(1)
                """,
            )
            server = KVServer(
                tmp_path,
                env={
                    "HERMES_MANAGED_DIR": str(managed),
                    "A2A_NOTIFY_CONVERSATIONS": gateway_platform,
                    "A2A_NOTIFY_PLATFORM": "",
                },
                path_prepend=str(tmp_path / "bin"),
            )
            try:
                error, _ = self.adapter._post(f"{server.url}/v1/cron-reports", REPORT, API_KEY)
            finally:
                server.stop()
            calls = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
        return error, calls

    def test_slack_refusal_reaches_the_ledger_and_the_cronjob_tool(self):
        error, calls = self._relay("slack")
        self.assertEqual(calls, [], "nothing may be posted")
        self.assertEqual(chat_delivery_watch.platforms_from(error), ["slack"])
        self.assertEqual(chat_delivery_watch.grade_error(error), chat_delivery_watch.GRADE_HARD)
        self.assertIn(
            "set spec.integration.slack.homeChannel / SLACK_HOME_CHANNEL",
            error[:CRONJOB_TOOL_ERROR_CHARS],
        )

    def test_google_chat_refusal_fits_the_cronjob_tool(self):
        error, calls = self._relay("google_chat")
        self.assertEqual(calls, [])
        self.assertEqual(chat_delivery_watch.platforms_from(error), ["google_chat"])
        self.assertIn(
            "set spec.integration.googleChat.homeChannel / GOOGLE_CHAT_HOME_CHANNEL",
            error[:CRONJOB_TOOL_ERROR_CHARS],
        )


if __name__ == "__main__":
    unittest.main()
