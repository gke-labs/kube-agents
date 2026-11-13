"""Seam: cron relay adapter → session KV relay → chat_delivery_watch, with no home channel.

A `next` install whose gateway holds one platform with no home channel set
anywhere: `A2A_NOTIFY_PLATFORM` empty (no CR home), `A2A_NOTIFY_CONVERSATIONS`
naming the platform, no projected Slack channel, both Hermes platforms off.
Every armed route then takes the home posts, so the relay sends the report to
the gateway, which refuses it for want of a home channel and keeps it for its
digest. Nothing goes through `hermes send`, and the refusal still reaches the
reader: the route's 502 `detail`, the adapter's `last_delivery_error`, and the
ledger issue's platform column, which `chat_delivery_watch` parses out of it.
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
# `a2a notify`'s exit status for a post the gateway refused for want of a home
# channel and kept for its digest (chat_notify.NOTIFY_KEPT).
NOTIFY_KEPT = 5
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
        adapter's error, the argv of every `hermes` the server ran, and the
        argv of every `a2a` it ran."""
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
            notify_record = tmp_path / "a2a-calls.jsonl"
            fake_executable(
                tmp_path / "bin",
                "a2a",
                f"""
                import json, sys
                with open({str(notify_record)!r}, "a") as handle:
                    handle.write(json.dumps(sys.argv[1:]) + "\\n")
                sys.exit({NOTIFY_KEPT})
                """,
            )
            server = KVServer(
                tmp_path,
                env={
                    "HERMES_MANAGED_DIR": str(managed),
                    "A2A_NOTIFY_CONVERSATIONS": gateway_platform,
                    "A2A_NOTIFY_PLATFORM": "",
                    "NATS_URL": "nats://nats.invalid:4222",
                    "A2A_BUS_USER": "agent",
                },
                path_prepend=str(tmp_path / "bin"),
            )
            try:
                error, _ = self.adapter._post(f"{server.url}/v1/cron-reports", REPORT, API_KEY)
            finally:
                server.stop()
            calls = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
            notifies = [json.loads(line) for line in notify_record.read_text().splitlines()] if notify_record.exists() else []
        return error, calls, notifies

    def test_slack_report_goes_to_the_gateway_and_the_refusal_reaches_the_ledger(self):
        error, calls, notifies = self._relay("slack")
        self.assertEqual(calls, [], "nothing may go through hermes send")
        self.assertEqual([argv[:3] for argv in notifies], [["notify", "--platform", "slack"]])
        self.assertEqual(chat_delivery_watch.platforms_from(error), ["slack"])

    def test_google_chat_report_goes_to_the_gateway(self):
        error, calls, notifies = self._relay("google_chat")
        self.assertEqual(calls, [])
        self.assertEqual([argv[:3] for argv in notifies], [["notify", "--platform", "google_chat"]])
        self.assertEqual(chat_delivery_watch.platforms_from(error), ["google_chat"])


if __name__ == "__main__":
    unittest.main()
