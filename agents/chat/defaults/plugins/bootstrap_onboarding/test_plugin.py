"""Unit tests for the bootstrap_onboarding plugin's pre_llm_call state machine.

Run: python3 -m unittest agents/chat/defaults/plugins/bootstrap_onboarding/test_plugin.py

The Hermes framework (gateway.session_context, cron.jobs) is not importable
here, so the plugin's optional imports resolve to None at load time. Tests
inject MagicMocks for update_job / trigger_job / get_session_env to assert the
plugin's side effects (origin binding, delivery trigger, presence marker) and
the greeting context it returns.
"""

import enum
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import plugin  # noqa: E402

# The one bench stack that writes the eval seam's marker.
EVAL_STACK_DIR = "bench/tf/prebuilt/first-install-hello/"


class _Platform(enum.Enum):
    GOOGLE_CHAT = "google_chat"
    SLACK = "slack"


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

    def test_platform_without_durable_delivery_is_ignored(self):
        self.assertIsNone(self._call(platform="test-local-surface-01"))
        self.update_job.assert_not_called()
        self.trigger_job.assert_not_called()
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

    # --- one-time means one time ----------------------------------------

    def test_greets_only_once_across_sessions(self):
        """Every new session opens with is_first_turn=True.

        A second user, a new thread, or a pruned history therefore re-enters
        this hook — and before the greeted marker existed, each one re-greeted,
        re-marked presence, and re-pointed the delivery job at itself. Only
        .bootstrap_completed stopped it, and that does not exist until the
        report has been delivered, which can be many minutes away or never.
        """
        first = self._call()
        self.assertIsNotNone(first)

        second = self._call(session_id="20260720_130000_efgh5678")
        self.assertIsNone(second)
        self.update_job.assert_called_once()  # delivery still bound to the first chat
        self.trigger_job.assert_called_once()

    def test_greeted_marker_is_written(self):
        self._call()
        self.assertTrue((self.data_dir / plugin.GREETED_MARKER).exists())

    def test_completed_still_suppresses_the_greeting(self):
        # The marker is additive: an already-delivered deployment (which
        # predates the marker, so does not have one) must stay quiet too.
        (self.data_dir / ".bootstrap_completed").touch()
        self.assertIsNone(self._call())
        self.assertFalse((self.data_dir / plugin.GREETED_MARKER).exists())

    def test_unbindable_turn_primes_nothing_and_leaves_the_flow_armed(self):
        """A session with no chat origin cannot receive the report.

        Marking presence there would let the delivery job fire while still set
        to `deliver: local` — the single-use report emitted into the void, with
        .bootstrap_completed set so nothing ever produces it again. Better to
        stay silent and let the next real chat turn prime onboarding.
        """
        with mock.patch.object(
            plugin, "get_session_env", _fake_session_env(HERMES_SESSION_PLATFORM="cli")
        ):
            self.assertIsNone(self._call(platform="cli"))

        self.update_job.assert_not_called()
        self.trigger_job.assert_not_called()
        self.assertFalse((self.data_dir / ".user_aligned").exists())
        self.assertFalse((self.data_dir / plugin.GREETED_MARKER).exists())

        # ...and a real chat turn afterwards still gets its greeting.
        self.assertIsNotNone(self._call())
        self.assertTrue((self.data_dir / ".user_aligned").exists())

    # --- the home channel ------------------------------------------------

    def _arm_home_channel(self, configured=None, persist_error=None):
        """Install doubles for Hermes' home-channel API; returns (persist, save_env_value).

        HomeChannel is a recorder that returns its keyword arguments, so the
        persisted value is the exact set of fields the plugin passed.
        """
        gateway_config = mock.MagicMock()
        gateway_config.get_home_channel.return_value = configured
        self.gateway_config = gateway_config
        persist = mock.MagicMock(side_effect=persist_error)
        save_env_value = mock.MagicMock()
        for name, value in (
            ("HomeChannel", lambda **fields: fields),
            ("Platform", _Platform),
            ("load_gateway_config", mock.MagicMock(return_value=gateway_config)),
            ("persist_home_channel", persist),
            ("save_env_value", save_env_value),
        ):
            patcher = mock.patch.object(plugin, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        return persist, save_env_value

    def test_first_durable_turn_sets_the_home_channel(self):
        persist, save_env_value = self._arm_home_channel()
        self._call()
        persist.assert_called_once()
        (home,), _ = persist.call_args
        self.assertEqual(home["platform"], _Platform.GOOGLE_CHAT)
        self.assertEqual(home["chat_id"], "spaces/AAA")
        # Scheduled reports start their own threads, not the opening message's.
        self.assertNotIn("thread_id", home)
        save_env_value.assert_any_call("GOOGLE_CHAT_HOME_CHANNEL", "spaces/AAA")
        save_env_value.assert_any_call("GOOGLE_CHAT_HOME_CHANNEL_THREAD_ID", "")

    def test_slack_turn_sets_the_slack_home_channel(self):
        persist, save_env_value = self._arm_home_channel()
        with mock.patch.object(
            plugin,
            "get_session_env",
            _fake_session_env(HERMES_SESSION_PLATFORM="slack", HERMES_SESSION_CHAT_ID="C123"),
        ):
            self._call(platform="slack")
        self.gateway_config.get_home_channel.assert_called_once_with(_Platform.SLACK)
        (home,), _ = persist.call_args
        self.assertEqual((home["platform"], home["chat_id"]), (_Platform.SLACK, "C123"))
        save_env_value.assert_any_call("SLACK_HOME_CHANNEL", "C123")

    def test_configured_home_channel_is_left_alone(self):
        persist, save_env_value = self._arm_home_channel(configured=mock.sentinel.home)
        self.assertIsNotNone(self._call())
        self.gateway_config.get_home_channel.assert_called_once_with(_Platform.GOOGLE_CHAT)
        persist.assert_not_called()
        save_env_value.assert_not_called()

    def test_non_durable_turn_sets_no_home_channel(self):
        persist, save_env_value = self._arm_home_channel()
        with mock.patch.object(
            plugin, "get_session_env", _fake_session_env(HERMES_SESSION_PLATFORM="cli", HERMES_SESSION_CHAT_ID="c1")
        ):
            self._call(platform="cli")
        self._call(platform="test-local-surface-01")
        persist.assert_not_called()
        save_env_value.assert_not_called()

    def test_home_channel_has_every_field_load_gateway_config_reads(self):
        # HomeChannel.from_dict indexes platform and chat_id; a block missing
        # one raises out of load_gateway_config for every platform.
        persist, _ = self._arm_home_channel()
        with mock.patch.object(
            plugin,
            "get_session_env",
            _fake_session_env(HERMES_SESSION_PLATFORM="google_chat", HERMES_SESSION_CHAT_ID="spaces/AAA"),
        ):
            self._call()
        persist.assert_called_once()
        (home,), _ = persist.call_args
        for field in ("platform", "chat_id", "name"):
            self.assertTrue(home.get(field), f"{field} is empty")
        self.assertEqual(home["name"], "spaces/AAA")

    def test_failed_home_channel_write_still_primes_onboarding(self):
        _, save_env_value = self._arm_home_channel(persist_error=OSError("read-only"))
        self.assertIn("SCAN IN PROGRESS", self._call()["context"])
        self.trigger_job.assert_called_once()
        self.assertTrue((self.data_dir / ".user_aligned").exists())
        save_env_value.assert_not_called()

    # --- the eval seam ----------------------------------------------------

    def _plant(self, variant="in_progress", phrase="just installed you", suffix="-running", age=0):
        marker = self.data_dir / f".bootstrap_greet_eval{suffix}"
        request = {"variant": variant, "phrase": phrase, "written_at": time.time() - age}
        marker.write_text(json.dumps(request), encoding="utf-8")
        return marker

    def _eval_call(self, **overrides):
        kwargs = {"platform": "api_server", "user_message": "hi! priya here, just installed you"}
        kwargs.update(overrides)
        return self._call(**kwargs)

    def test_eval_marker_greets_on_a_platform_the_allowlist_excludes(self):
        self._plant()
        result = self._eval_call()
        self.assertIsNotNone(result)
        self.assertIn("SCAN IN PROGRESS", result["context"])
        self.assertIn("IN-PROGRESS-INSTRUCTIONS", result["context"])

    def test_eval_marker_variant_picks_the_completed_instructions(self):
        self._plant(variant="completed")
        result = self._eval_call()
        self.assertIn("SCAN COMPLETED", result["context"])
        self.assertIn("COMPLETED-INSTRUCTIONS", result["context"])

    def test_eval_marker_touches_no_onboarding_state(self):
        self._plant()
        self.assertIsNotNone(self._eval_call())
        self.update_job.assert_not_called()
        self.trigger_job.assert_not_called()
        self.assertFalse((self.data_dir / ".user_aligned").exists())
        self.assertFalse((self.data_dir / plugin.GREETED_MARKER).exists())

    def test_eval_marker_greets_even_after_onboarding_happened(self):
        (self.data_dir / plugin.GREETED_MARKER).touch()
        (self.data_dir / ".bootstrap_completed").touch()
        self._plant()
        self.assertIsNotNone(self._eval_call())

    def test_eval_marker_survives_a_retried_opening_turn(self):
        # The harness re-sends the opening turn in the same conversation after
        # a dropped connection; by then the first attempt is in the history.
        marker = self._plant()
        self.assertIsNotNone(self._eval_call())
        result = self._eval_call(is_first_turn=False)
        self.assertIn("SCAN IN PROGRESS", result["context"])
        self.assertTrue(marker.exists())

    def test_eval_marker_waits_for_its_phrase(self):
        marker = self._plant()
        self.assertIsNone(self._eval_call(user_message="list my clusters"))
        self.assertTrue(marker.exists())

    def test_eval_marker_is_not_taken_by_another_turn_or_cron(self):
        marker = self._plant()
        self.assertIsNone(self._eval_call(is_first_turn=False, user_message="what is its status?"))
        self.assertIsNone(self._eval_call(platform="cron"))
        self.assertIsNone(self._eval_call(session_id="cron_abc"))
        self.assertTrue(marker.exists())

    def test_eval_marker_is_not_consulted_on_a_chat_platform(self):
        (self.data_dir / plugin.GREETED_MARKER).touch()
        self._plant()
        self.assertIsNone(self._eval_call(platform="google_chat"))
        self.assertIsNone(self._eval_call(platform="slack"))

    def test_eval_marker_with_a_short_phrase_is_ignored(self):
        for suffix, phrase in (("-blank", " "), ("-hi", "hi"), ("-padded", "   priya   ")):
            self._plant(phrase=phrase, suffix=suffix)
        self.assertIsNone(self._eval_call())

    def test_eval_marker_with_a_padded_phrase_matches_it_trimmed(self):
        self._plant(phrase="  just installed you ")
        result = self._eval_call(user_message="hi! priya here, just installed you")
        self.assertIn("SCAN IN PROGRESS", result["context"])

    def test_eval_marker_past_its_age_is_ignored_with_a_warning(self):
        # A request a failed destroy left behind, or one stamped by a skewed clock.
        for age in (plugin.EVAL_REQUEST_MAX_AGE_SECONDS + 60, -plugin.EVAL_REQUEST_MAX_AGE_SECONDS - 60):
            with self.subTest(age=age):
                self._plant(age=age)
                with self.assertLogs(plugin.logger, level="WARNING") as logs:
                    self.assertIsNone(self._eval_call())
                self.assertIn("written_at", logs.output[0])

    def test_eval_marker_without_a_readable_written_at_is_ignored(self):
        for suffix, written_at in (
            ("-none", None),
            ("-text", "now"),
            ("-bool", True),
            ("-nan", float("nan")),
            ("-huge", 10**400),
        ):
            request = {"variant": "in_progress", "phrase": "just installed you"}
            if written_at is not None:
                request["written_at"] = written_at
            marker = self.data_dir / f".bootstrap_greet_eval{suffix}"
            marker.write_text(json.dumps(request), encoding="utf-8")
        self.assertIsNone(self._eval_call())

    def test_unreadable_eval_marker_is_ignored(self):
        (self.data_dir / ".bootstrap_greet_eval-a").write_text("not json", encoding="utf-8")
        (self.data_dir / ".bootstrap_greet_eval-b").write_text("[]", encoding="utf-8")
        (self.data_dir / ".bootstrap_greet_eval-c").write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
        self.assertIsNone(self._eval_call())

    def test_concurrent_eval_markers_are_each_taken_by_their_own_phrase(self):
        running = self._plant(phrase="just installed you", suffix="-running")
        done = self._plant(variant="completed", phrase="just set you up", suffix="-done")
        result = self._eval_call(user_message="hey, priya here, just set you up")
        self.assertIn("SCAN COMPLETED", result["context"])
        result = self._eval_call(user_message="hi! priya here, just installed you")
        self.assertIn("SCAN IN PROGRESS", result["context"])
        self.assertTrue(running.exists())
        self.assertTrue(done.exists())

    def test_only_the_hello_stack_and_this_plugin_name_the_eval_marker(self):
        repo = Path(__file__).resolve().parents[5]
        try:
            listed = subprocess.run(
                ["git", "grep", "-l", "--fixed-strings", plugin.EVAL_GREET_MARKER],
                cwd=repo, capture_output=True, text=True, check=False,
            )
        except FileNotFoundError:
            self.skipTest("git is not installed")
        if listed.returncode > 1:
            self.skipTest(f"git grep failed: {listed.stderr.strip()}")
        plugin_dir = Path(__file__).resolve().parent.relative_to(repo).as_posix() + "/"
        strays = [
            path for path in listed.stdout.splitlines()
            if not (path.startswith(EVAL_STACK_DIR) or path.startswith(plugin_dir))
        ]
        self.assertEqual(strays, [], "only the bench stack may write the eval seam's marker")

    def test_absent_eval_marker_leaves_the_real_flow_unchanged(self):
        self.assertIsNone(self._eval_call())
        result = self._call(user_message="hi! priya here, just installed you")
        self.assertIn("SCAN IN PROGRESS", result["context"])
        self.update_job.assert_called_once()
        self.assertTrue((self.data_dir / plugin.GREETED_MARKER).exists())


if __name__ == "__main__":
    unittest.main()
