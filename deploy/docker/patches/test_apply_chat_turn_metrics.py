"""Unit tests for apply_chat_turn_metrics.py against miniature gateway files.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The shipped tree is checked at image build by the grep lines beside the
applier in deploy/docker/Dockerfile; these tests cover the applier's contract
(one count at each of the three sites, one import per file, and a refusal when
an anchor is missing, repeated or already patched) and the helper module's
behaviour with and without the plugin in reach.
"""

import importlib.util
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import apply_chat_turn_metrics as applier

# The shape of gateway/run_turn.py at v2026.9.14, cut to what the patch touches.
RUN_TURN_STUB = '''from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import asyncio


class GatewayRunner:
    async def _hmwa_handle(self, event, source, session_entry, session_key, prepared):
        try:
            agent_result = {"final_response": "ok"}
            return await self._hmwa_deliver_turn_response(
                event, source, session_entry, session_key, 1, agent_result, [], "ok", "", False,
            )
        except Exception as e:
            return await self._hmwa_agent_error_reply(e, event, source, session_entry, session_key, prepared)

    async def _hmwa_agent_error_reply(self, e, event, source, session_entry, session_key, prepared):
        """``except Exception`` body of the agent turn: stop typing, log, persist the inbound user
        turn once and close it, and build the sanitized user-facing error reply."""
        # Retain Slack thread/workspace routing so a failed turn cannot leave its status visible.
        await self._hmwa_stop_typing_for_turn(event, source)
        return "Sorry"
'''

# gateway/run_turn_runner.py after apply_slack_boilerplate.py, cut the same way.
RUNNER_STUB = '''from __future__ import annotations

import asyncio
import json
import logging


class _RunAgentWorker:
    def run_agent(self, runner, ctx):
        try:
            model = runner._resolve_session_agent_runtime(source=ctx.source)
        except Exception as exc:
            # kube-agents patch: KAGE_SLACK_UX logs the exception and keeps it out of
            # a Slack reply; see gateway/slack_boilerplate.py.
            return {"final_response": _kage_slack_boilerplate.error_reply(
                ctx.source.platform, f"Provider authentication failed: {exc}",
            ), "messages": [], "api_calls": 0, "tools": []}
        return model
'''


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.run_turn = _write(self.root, applier.RUN_TURN, RUN_TURN_STUB)
        self.runner = _write(self.root, applier.RUN_TURN_RUNNER, RUNNER_STUB)

    def test_the_three_sites_count_and_each_file_imports_the_helper_once(self):
        with mock.patch("sys.stdout"):
            applier.apply(self.root)
        run_turn = self.run_turn.read_text(encoding="utf-8")
        runner = self.runner.read_text(encoding="utf-8")
        self.assertEqual(1, run_turn.count("from gateway import chat_turn_metrics as _kage_chat_turn_metrics"))
        self.assertEqual(1, runner.count("from gateway import chat_turn_metrics as _kage_chat_turn_metrics"))
        self.assertEqual(2, run_turn.count("_kage_chat_turn_metrics.record_turn_error(source)"))
        self.assertEqual(1, runner.count("_kage_chat_turn_metrics.record_turn_error(ctx.source)"))
        # The error-reply count is the first statement of the body, before the typing stop.
        body = run_turn.split("async def _hmwa_agent_error_reply", 1)[1]
        self.assertLess(body.index("record_turn_error(source)"), body.index("_hmwa_stop_typing_for_turn"))
        # The failed-result count guards on the result and sits before the delivery.
        self.assertIn('if isinstance(agent_result, dict) and agent_result.get("failed"):', run_turn)
        self.assertLess(run_turn.index('agent_result.get("failed")'), run_turn.index("_hmwa_deliver_turn_response("))
        # Both files still parse.
        compile(run_turn, str(self.run_turn), "exec")
        compile(runner, str(self.runner), "exec")

    def test_a_patched_tree_is_refused(self):
        with mock.patch("sys.stdout"):
            applier.apply(self.root)
        with self.assertRaises(SystemExit):
            applier.apply(self.root)

    def test_a_moved_anchor_fails_the_build(self):
        _write(self.root, applier.RUN_TURN, RUN_TURN_STUB.replace("# Retain Slack thread", "# Retain the thread"))
        with self.assertRaises(SystemExit):
            applier.apply(self.root)

    def test_a_repeated_anchor_fails_the_build(self):
        _write(self.root, applier.RUN_TURN_RUNNER, RUNNER_STUB + RUNNER_STUB.replace("class _RunAgentWorker", "class _Other"))
        with self.assertRaises(SystemExit):
            applier.apply(self.root)


class HelperTest(unittest.TestCase):
    """gateway/chat_turn_metrics.py: counts on the plugin's registry when it is
    importable, counts nothing and never raises when it is not."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "chat_turn_metrics_under_test", Path(__file__).with_name("chat_turn_metrics.py")
        )
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)

    def test_a_turn_error_reaches_the_plugins_registry_under_its_platform(self):
        registry = mock.Mock()
        metrics = SimpleNamespace(REGISTRY=registry)
        with mock.patch.dict(sys.modules, {"chat_metrics": SimpleNamespace(metrics=metrics), "chat_metrics.metrics": metrics}):
            self.helper.record_turn_error(SimpleNamespace(platform=SimpleNamespace(value="slack")))
            self.helper.record_turn_error(SimpleNamespace(platform="google_chat"))
        self.assertEqual([mock.call("slack"), mock.call("google_chat")], registry.record_turn_error.call_args_list)

    def test_a_gateway_without_the_plugin_counts_nothing_and_logs_once(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with (
            mock.patch.object(self.helper, "DEFAULT_PLUGINS_DIR", temp.name),
            mock.patch.dict(sys.modules, {"chat_metrics": None}),
            self.assertLogs(self.helper.LOGGER, level=logging.INFO) as logs,
        ):
            self.helper.record_turn_error(SimpleNamespace(platform="slack"))
            self.helper.record_turn_error(SimpleNamespace(platform="slack"))
        self.assertEqual(1, len(logs.records))

    def test_a_registry_that_raises_does_not_fail_the_turn(self):
        registry = mock.Mock()
        registry.record_turn_error.side_effect = RuntimeError("boom")
        metrics = SimpleNamespace(REGISTRY=registry)
        with mock.patch.dict(sys.modules, {"chat_metrics": SimpleNamespace(metrics=metrics), "chat_metrics.metrics": metrics}):
            self.helper.record_turn_error(SimpleNamespace(platform="slack"))


if __name__ == "__main__":
    unittest.main()
