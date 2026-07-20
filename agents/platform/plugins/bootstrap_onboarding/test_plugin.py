"""Unit tests for the bootstrap_onboarding plugin's pre_llm_call state machine.

Run: python3 -m unittest agents/platform/plugins/bootstrap_onboarding/test_plugin.py

The Hermes framework (gateway.session_context, cron.jobs) is not importable
here, so the plugin's optional imports resolve to None at load time. Tests
inject MagicMocks for update_job / trigger_job / get_session_env to assert the
plugin's side effects (origin binding, delivery trigger, presence marker) and
the greeting context it returns.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import plugin  # noqa: E402


def _fake_session_env(**values):
    def _get(name, default=""):
        return values.get(name, default)
    return _get


class PreLlmCallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        (self.data_dir / "onboarding").mkdir()
        (self.data_dir / "onboarding" / "scan_in_progress.md").write_text(
            "IN-PROGRESS-INSTRUCTIONS", encoding="utf-8"
        )
        (self.data_dir / "onboarding" / "scan_completed.md").write_text(
            "COMPLETED-INSTRUCTIONS", encoding="utf-8"
        )
        # Point the plugin at the temp workspace.
        self._env = mock.patch.dict("os.environ", {"HERMES_HOME": str(self.data_dir)})
        self._env.start()
        # Inject framework doubles.
        self.update_job = mock.MagicMock()
        self.trigger_job = mock.MagicMock()
        self._patches = [
            mock.patch.object(plugin, "update_job", self.update_job),
            mock.patch.object(plugin, "trigger_job", self.trigger_job),
            mock.patch.object(
                plugin,
                "get_session_env",
                _fake_session_env(
                    HERMES_SESSION_PLATFORM="google_chat",
                    HERMES_SESSION_CHAT_ID="spaces/AAA",
                    HERMES_SESSION_THREAD_ID="threads/T1",
                ),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._env.stop()
        self._tmp.cleanup()

    def _call(self, **overrides):
        kwargs = {"is_first_turn": True, "platform": "google_chat", "session_id": "20260720_120000_abcd1234"}
        kwargs.update(overrides)
        return plugin.handle_pre_llm_call(**kwargs)

    # --- turns that must be ignored -------------------------------------

    def test_non_first_turn_is_ignored(self):
        self.assertIsNone(self._call(is_first_turn=False))
        self.update_job.assert_not_called()
        self.assertFalse((self.data_dir / ".user_aligned").exists())

    def test_cron_platform_is_ignored(self):
        self.assertIsNone(self._call(platform="cron"))
        self.assertFalse((self.data_dir / ".user_aligned").exists())

    def test_cron_session_prefix_is_ignored(self):
        self.assertIsNone(self._call(session_id="cron_bootstrap-inventory-scan_20260720_120000"))
        self.assertFalse((self.data_dir / ".user_aligned").exists())

    def test_already_completed_is_ignored(self):
        (self.data_dir / ".bootstrap_completed").touch()
        self.assertIsNone(self._call())
        self.update_job.assert_not_called()
        self.trigger_job.assert_not_called()

    def test_no_onboarding_assets_is_ignored(self):
        # Remove onboarding dir; /opt/defaults/onboarding is absent in tests.
        for f in (self.data_dir / "onboarding").iterdir():
            f.unlink()
        (self.data_dir / "onboarding").rmdir()
        self.assertIsNone(self._call())

    # --- Case A: user connects mid-scan (INVENTORY.md absent) -----------

    def test_case_a_binds_triggers_and_injects_in_progress(self):
        result = self._call()
        self.assertIn("SCAN IN PROGRESS", result["context"])
        self.assertIn("IN-PROGRESS-INSTRUCTIONS", result["context"])
        # Never leak inventory content into the turn.
        self.assertNotIn("COMPLETED-INSTRUCTIONS", result["context"])
        # Presence marker set, delivery bound to origin and triggered.
        self.assertTrue((self.data_dir / ".user_aligned").exists())
        self.update_job.assert_called_once_with(
            "bootstrap-inventory-delivery",
            {
                "deliver": "origin",
                "origin": {
                    "platform": "google_chat",
                    "chat_id": "spaces/AAA",
                    "thread_id": "threads/T1",
                },
            },
        )
        self.trigger_job.assert_called_once_with("bootstrap-inventory-delivery")

    # --- Case B: user connects after scan finished (INVENTORY.md present) -

    def test_case_b_injects_completed_without_inventory_content(self):
        (self.data_dir / "INVENTORY.md").write_text("SECRET-FLEET-DATA", encoding="utf-8")
        result = self._call()
        self.assertIn("SCAN COMPLETED", result["context"])
        self.assertIn("COMPLETED-INSTRUCTIONS", result["context"])
        # The plugin must NOT inject the inventory itself (delivery is verbatim).
        self.assertNotIn("SECRET-FLEET-DATA", result["context"])
        self.assertTrue((self.data_dir / ".user_aligned").exists())
        self.trigger_job.assert_called_once_with("bootstrap-inventory-delivery")

    def test_origin_binding_happens_before_user_aligned(self):
        # update_job (origin binding) must precede touching .user_aligned so the
        # delivery job never fires against a stale target.
        calls = []
        self.update_job.side_effect = lambda *a, **k: calls.append(
            ("bind", (self.data_dir / ".user_aligned").exists())
        )
        self._call()
        self.assertEqual(calls, [("bind", False)])

    def test_missing_thread_id_omitted_from_origin(self):
        with mock.patch.object(
            plugin,
            "get_session_env",
            _fake_session_env(
                HERMES_SESSION_PLATFORM="google_chat",
                HERMES_SESSION_CHAT_ID="spaces/AAA",
            ),
        ):
            self._call()
        _, updates = self.update_job.call_args[0]
        self.assertEqual(updates["origin"], {"platform": "google_chat", "chat_id": "spaces/AAA"})


if __name__ == "__main__":
    unittest.main()
