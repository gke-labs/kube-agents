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
import os
import sqlite3
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
# The repository copy of the module the image ships at plugin.SANDBOX_EXEC_PATH.
# A report name the eval seam accepts with from_report.
BENCH_REPORT = "INVENTORY.bench-done.md"
REPO_SANDBOX_EXEC = Path(__file__).resolve().parents[4] / "platform" / "scripts" / "sandbox_exec.py"
# The repository copy of the module the image ships at plugin.CHAT_NOTIFY_PATH.
REPO_CHAT_NOTIFY = Path(__file__).resolve().parents[4] / "platform" / "scripts" / "chat_notify.py"
# The repository copy of the module the image ships at plugin.BOOTSTRAP_HANDOFF_PATH.
REPO_BOOTSTRAP_HANDOFF = Path(__file__).resolve().parents[3] / "scripts" / "bootstrap_handoff.py"
# A session the hermes-bridge opened for a Slack thread, and the route it recorded for it.
BRIDGE_SESSION = "a2a-ctx-21f41a7dc20859301d2307b8"
SLACK_ROUTE = {
    "platform": "slack",
    "conversation": "slack:C0HOME/1792185148.859139",
    "context_id": "ctx-21f41a7dc20859301d2307b8",
    "ask": "question",
}
# The opening turn's id, which both hooks receive.
TURN = "turn-7f3a"


# Hermes' Platform also has non-durable members, so a turn past a regressed
# allowlist resolves instead of raising.
class _Platform(enum.Enum):
    GOOGLE_CHAT = "google_chat"
    SLACK = "slack"
    CLI = "cli"
    API_SERVER = "api_server"


def _fake_session_env(**values):
    def _get(name, default=""):
        return values.get(name, default)
    return _get


def _touch_failing(prefix):
    """A Path.touch that fails for names starting with ``prefix`` and works for the rest."""
    real_touch = Path.touch

    def touch(path, *args, **kwargs):
        if path.name.startswith(prefix):
            raise OSError("disk full")
        return real_touch(path, *args, **kwargs)

    return touch


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
        # Point the plugin at the temp workspace, and the real sandbox_exec at
        # a managed config that is not there: the shell sandbox is off.
        self._env = mock.patch.dict(
            "os.environ",
            {
                "HERMES_HOME": str(self.data_dir),
                "HERMES_MANAGED_CONFIG_PATH": str(self.data_dir / "no-managed-config.yaml"),
            },
        )
        self._env.start()
        # Inject framework doubles.
        # A store with oobe only, unless a test says otherwise: update_job answers None for a
        # job it does not have, as Hermes' does.
        self.store_ids = {"oobe"}
        self.update_job = mock.MagicMock(side_effect=lambda job_id, updates: {} if job_id in self.store_ids else None)
        # trigger_job answers None for a job the store does not have, as Hermes' does.
        self.trigger_job = mock.MagicMock(side_effect=lambda job_id, *a, **k: {} if job_id in self.store_ids else None)
        self._patches = [
            mock.patch.object(plugin, "SANDBOX_EXEC_PATH", REPO_SANDBOX_EXEC),
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
        plugin._greeting_turns.clear()

    def _call(self, **overrides):
        kwargs = {"is_first_turn": True, "platform": "google_chat", "session_id": "20260720_120000_abcd1234",
                  "turn_id": TURN}
        kwargs.update(overrides)
        return plugin.handle_pre_llm_call(**kwargs)

    def _end_turn(self, **overrides):
        """The same turn's post_llm_call, as Hermes fires it once the reply is final."""
        kwargs = {"platform": "google_chat", "session_id": "20260720_120000_abcd1234", "turn_id": TURN,
                  "assistant_response": "Hi there, I'm kube-agents"}
        kwargs.update(overrides)
        return plugin.handle_post_llm_call(**kwargs)

    def _holds(self):
        return sorted(p.name for p in self.data_dir.glob(f"{plugin.GREETING_PENDING_PREFIX}*"))

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
        # Presence marker set, delivery bound to origin and held behind the greeting.
        self.assertTrue((self.data_dir / ".user_aligned").exists())
        self.update_job.assert_any_call(
            "oobe",
            {
                "deliver": "origin",
                "origin": {
                    "platform": "google_chat",
                    "chat_id": "spaces/AAA",
                    "thread_id": "threads/T1",
                },
            },
        )
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])
        self.trigger_job.assert_not_called()
        # The greeting is on its way: the hold goes and the job is marked due.
        self._end_turn()
        self.assertEqual(self._holds(), [])
        self.trigger_job.assert_called_once_with("oobe")

    def test_both_delivery_jobs_present_are_bound_and_only_oobe_triggered(self):
        # A post-fold install still carries the disabled old entry; binding it keeps the link if
        # the install is rolled back to an image from before the fold.
        self.store_ids = {"oobe", "bootstrap-inventory-delivery"}
        self._call()
        self.assertEqual([c.args[0] for c in self.update_job.call_args_list], ["oobe", "bootstrap-inventory-delivery"])
        self._end_turn()
        # Triggering also enables a job; the old entry stays disabled.
        self.trigger_job.assert_called_once_with("oobe")

    def test_an_install_without_oobe_binds_the_old_delivery_job(self):
        # oobe removed itself under an earlier image; the old job still delivers.
        self.store_ids = {"bootstrap-inventory-delivery"}
        self._call()
        self.assertTrue((self.data_dir / ".user_aligned").exists())
        self._end_turn()
        # The oobe call answered None (no such job, nothing enabled); the next present job is due.
        self.assertEqual([c.args[0] for c in self.trigger_job.call_args_list], ["oobe", "bootstrap-inventory-delivery"])

    def test_an_install_without_oobe_triggers_the_old_job_at_once_when_the_hold_fails(self):
        self.store_ids = {"bootstrap-inventory-delivery"}
        with mock.patch.object(Path, "touch", _touch_failing(plugin.GREETING_PENDING_PREFIX)):
            self._call()
        self.trigger_job.assert_called_once_with("bootstrap-inventory-delivery")

    # --- the greeting hold ------------------------------------------------

    def test_the_hold_is_written_before_the_aligned_marker(self):
        # Once .user_aligned exists a tick may deliver; the hold must already be there.
        seen = []
        real_touch = Path.touch

        def touch(path, *args, **kwargs):
            if path.name == ".user_aligned":
                seen.append(self._holds())
            return real_touch(path, *args, **kwargs)

        with mock.patch.object(Path, "touch", touch):
            self._call()
        self.assertEqual(seen, [[f"{plugin.GREETING_PENDING_PREFIX}{TURN}"]])

    def test_a_failed_hold_triggers_at_once(self):
        with mock.patch.object(Path, "touch", _touch_failing(plugin.GREETING_PENDING_PREFIX)):
            self.assertIsNotNone(self._call())
        self.assertEqual(self._holds(), [])
        self.trigger_job.assert_called_once_with("oobe")
        self.assertTrue((self.data_dir / ".user_aligned").exists())

    def test_a_turn_without_an_id_triggers_at_once(self):
        self.assertIsNotNone(self._call(turn_id=""))
        self.assertEqual(self._holds(), [])
        self.trigger_job.assert_called_once_with("oobe")

    def test_another_turn_ending_touches_neither_the_hold_nor_the_job(self):
        self._call()
        with mock.patch.object(plugin, "_drop_hold") as drop:
            self._end_turn(turn_id="turn-other")
        drop.assert_not_called()
        self.trigger_job.assert_not_called()
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])

    def test_a_session_id_change_between_the_hooks_still_releases_the_hold(self):
        # Hermes rotates the session id on compression; the turn id stays.
        self._call(session_id="20260720_120000_abcd1234")
        self._end_turn(session_id="20260720_120500_ffff0000")
        self.assertEqual(self._holds(), [])
        self.trigger_job.assert_called_once_with("oobe")

    def test_a_hold_delivery_aged_out_is_released_without_error(self):
        self._call()
        (self.data_dir / f"{plugin.GREETING_PENDING_PREFIX}{TURN}").unlink()
        self._end_turn()
        self.trigger_job.assert_called_once_with("oobe")

    def test_a_turn_from_before_a_restart_is_not_held_in_this_process(self):
        # The hold file is on the volume; the set is not. The file ages out in delivery.
        self._call()
        plugin._greeting_turns.clear()
        self._end_turn()
        self.trigger_job.assert_not_called()
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])

    def test_register_installs_both_hooks(self):
        ctx = mock.MagicMock()
        plugin.register(ctx)
        self.assertEqual(
            [c.args for c in ctx.register_hook.call_args_list],
            [("pre_llm_call", plugin.handle_pre_llm_call), ("post_llm_call", plugin.handle_post_llm_call)],
        )

    def test_no_delivery_job_at_all_primes_nothing(self):
        self.store_ids = set()
        self.assertIsNone(self._call())
        self.assertFalse((self.data_dir / ".user_aligned").exists())

    # --- Case B: user connects after scan finished (INVENTORY.md present) -

    def test_case_b_injects_completed_without_inventory_content(self):
        (self.data_dir / "INVENTORY.md").write_text("SECRET-FLEET-DATA", encoding="utf-8")
        result = self._call()
        self.assertIn("SCAN COMPLETED", result["context"])
        self.assertIn("COMPLETED-INSTRUCTIONS", result["context"])
        # The plugin must NOT inject the inventory itself (delivery is verbatim).
        self.assertNotIn("SECRET-FLEET-DATA", result["context"])
        self.assertTrue((self.data_dir / ".user_aligned").exists())
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])
        self._end_turn()
        self.trigger_job.assert_called_once_with("oobe")

    # --- the report on the shell sandbox's volume ------------------------

    def _sandbox(self, read=None):
        """A sandbox_exec double with the shell sandbox on."""
        fake = mock.MagicMock()
        fake.sandbox_enabled.return_value = True
        fake.read_bytes.side_effect = read or (lambda *a, **k: None)
        return mock.patch.object(plugin, "_load_sandbox_exec", return_value=fake), fake

    def test_report_on_the_sandbox_greets_as_completed(self):
        patch, fake = self._sandbox(read=lambda *a, **k: b"#")
        with patch:
            result = self._call()
        self.assertIn("SCAN COMPLETED", result["context"])
        fake.read_bytes.assert_called_once_with(
            f"{plugin.SANDBOX_HOME}/{plugin.REPORT_NAME}",
            max_bytes=1,
            timeout=plugin.REPORT_CHECK_TIMEOUT_SECONDS,
        )

    def test_report_on_the_agent_pod_only_is_not_ready_with_the_sandbox_on(self):
        (self.data_dir / "INVENTORY.md").write_text("STALE", encoding="utf-8")
        patch, _ = self._sandbox()
        with patch:
            result = self._call()
        self.assertIn("SCAN IN PROGRESS", result["context"])

    def test_sandbox_read_that_fails_greets_as_in_progress_and_still_primes(self):
        for error in (
            subprocess.TimeoutExpired(["ssh"], plugin.REPORT_CHECK_TIMEOUT_SECONDS),
            RuntimeError("ssh failed"),
        ):
            with self.subTest(error=type(error).__name__):
                for marker in (".user_aligned", plugin.GREETED_MARKER):
                    (self.data_dir / marker).unlink(missing_ok=True)
                self.trigger_job.reset_mock()

                def read(*a, **k):
                    raise error

                patch, _ = self._sandbox(read=read)
                with patch:
                    result = self._call()
                self.assertIn("SCAN IN PROGRESS", result["context"])
                self.assertTrue((self.data_dir / ".user_aligned").exists())
                self.assertTrue((self.data_dir / plugin.GREETED_MARKER).exists())
                self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])

    def test_unknown_sandbox_state_falls_back_to_the_local_report(self):
        (self.data_dir / "INVENTORY.md").write_text("REPORT", encoding="utf-8")
        with mock.patch.object(plugin, "_load_sandbox_exec", side_effect=RuntimeError("bad config")):
            result = self._call()
        self.assertIn("SCAN COMPLETED", result["context"])

    def test_unloadable_sandbox_exec_falls_back_to_the_local_report(self):
        with mock.patch.object(plugin, "SANDBOX_EXEC_PATH", self.data_dir / "absent.py"):
            result = self._call()
        self.assertIn("SCAN IN PROGRESS", result["context"])

    def test_report_is_read_before_delivery_can_claim_it(self):
        # Delivery moves the report aside once it holds .user_aligned and a trigger.
        seen = []

        def read(*a, **k):
            seen.append(((self.data_dir / ".user_aligned").exists(), self.trigger_job.called))
            return b"#"

        patch, _ = self._sandbox(read=read)
        with patch:
            self._call()
        self.assertEqual(seen, [(False, False)])

    def test_the_repository_sandbox_exec_loads_and_reads_the_sandbox_off(self):
        module = plugin._load_sandbox_exec()
        self.assertFalse(module.sandbox_enabled())

    def test_origin_binding_happens_before_user_aligned(self):
        # update_job (origin binding) must precede touching .user_aligned so the
        # delivery job never fires against a stale target.
        calls = []
        self.update_job.side_effect = lambda *a, **k: calls.append(
            ("bind", (self.data_dir / ".user_aligned").exists())
        ) or {}
        self._call()
        self.assertEqual(calls, [("bind", False)] * len(plugin.DELIVERY_JOB_IDS))
        self.assertTrue((self.data_dir / ".user_aligned").exists())

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
        # Delivery still bound to the first chat: no bind and no trigger for the second session.
        self.assertEqual(self.update_job.call_count, len(plugin.DELIVERY_JOB_IDS))
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])
        self.trigger_job.assert_not_called()

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

    def test_failed_bind_sets_no_home_channel(self):
        persist, save_env_value = self._arm_home_channel()
        self.update_job.side_effect = RuntimeError("cron store unavailable")
        self.assertIsNone(self._call())
        persist.assert_not_called()
        save_env_value.assert_not_called()

    def test_later_session_sets_no_home_channel(self):
        persist, _ = self._arm_home_channel()
        self._call()
        self._call(session_id="20260720_130000_efgh5678")
        persist.assert_called_once()

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

    # --- the greeting's home-channel sentence ------------------------------

    def test_greeting_says_reports_come_here_when_the_turn_set_the_home_channel(self):
        self._arm_home_channel()
        self.assertIn(plugin.HOME_CHANNEL_INSTRUCTION, self._call()["context"])

    def test_completed_greeting_also_gets_the_sentence(self):
        self._arm_home_channel()
        (self.data_dir / "INVENTORY.md").write_text("REPORT", encoding="utf-8")
        context = self._call()["context"]
        self.assertIn("SCAN COMPLETED", context)
        self.assertIn(plugin.HOME_CHANNEL_INSTRUCTION, context)

    def test_home_channel_sentence_promises_no_later_post(self):
        # The completed greeting says the summary is already here, so the
        # sentence must not promise a later post.
        self.assertNotIn("I'll post", plugin.HOME_CHANNEL_INSTRUCTION)

    def test_greeting_says_nothing_about_reports_when_a_home_channel_was_configured(self):
        self._arm_home_channel(configured=mock.sentinel.home)
        self.assertNotIn(plugin.HOME_CHANNEL_INSTRUCTION, self._call()["context"])

    def test_greeting_says_nothing_about_reports_when_the_write_failed(self):
        self._arm_home_channel(persist_error=OSError("read-only"))
        self.assertNotIn(plugin.HOME_CHANNEL_INSTRUCTION, self._call()["context"])

    def test_greeting_says_nothing_about_reports_without_the_home_channel_api(self):
        # setUp leaves Hermes' home-channel names at None, as on an older Hermes.
        self.assertNotIn(plugin.HOME_CHANNEL_INSTRUCTION, self._call()["context"])

    def test_home_channel_sentence_names_no_command(self):
        # The command to move the home channel is different on each platform.
        self.assertNotIn("/", plugin.HOME_CHANNEL_INSTRUCTION)

    def test_failed_home_channel_write_still_primes_onboarding(self):
        _, save_env_value = self._arm_home_channel(persist_error=OSError("read-only"))
        self.assertIn("SCAN IN PROGRESS", self._call()["context"])
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])
        self.assertTrue((self.data_dir / ".user_aligned").exists())
        save_env_value.assert_not_called()

    # --- the eval seam ----------------------------------------------------

    def _plant(self, variant="in_progress", phrase="just installed you", suffix="-running", age=0, **extra):
        marker = self.data_dir / f".bootstrap_greet_eval{suffix}"
        request = {"variant": variant, "phrase": phrase, "written_at": time.time() - age, **extra}
        marker.write_text(json.dumps(request), encoding="utf-8")
        return marker

    def _eval_call(self, **overrides):
        kwargs = {"platform": "api_server", "user_message": "hi! priya here, just installed you"}
        kwargs.update(overrides)
        return self._call(**kwargs)

    def test_eval_marker_with_home_channel_set_adds_the_sentence(self):
        self._plant(home_channel_set=True)
        self.assertIn(plugin.HOME_CHANNEL_INSTRUCTION, self._eval_call()["context"])

    def test_eval_marker_without_home_channel_set_adds_no_sentence(self):
        self._plant()
        self.assertNotIn(plugin.HOME_CHANNEL_INSTRUCTION, self._eval_call()["context"])

    def test_eval_marker_home_channel_set_must_be_true_not_truthy(self):
        self._plant(home_channel_set="true")
        self.assertNotIn(plugin.HOME_CHANNEL_INSTRUCTION, self._eval_call()["context"])

    def test_eval_marker_with_home_channel_set_writes_no_home_channel(self):
        persist, save_env_value = self._arm_home_channel()
        self._plant(home_channel_set=True)
        self._eval_call()
        persist.assert_not_called()
        save_env_value.assert_not_called()

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

    def test_eval_marker_from_report_reads_its_own_file_in_the_sandbox(self):
        self._plant(variant=plugin.EVAL_VARIANT_FROM_REPORT, report=BENCH_REPORT)
        for report, tag in ((b"#", "SCAN COMPLETED"), (None, "SCAN IN PROGRESS")):
            with self.subTest(report=report):
                patch, fake = self._sandbox(read=lambda *a, **k: report)
                with patch:
                    result = self._eval_call()
                self.assertIn(tag, result["context"])
                fake.read_bytes.assert_called_once_with(
                    f"{plugin.SANDBOX_HOME}/{BENCH_REPORT}",
                    max_bytes=1,
                    timeout=plugin.REPORT_CHECK_TIMEOUT_SECONDS,
                )

    def test_eval_marker_from_report_never_reads_the_real_report(self):
        # The real report on the local path, the sandbox off: only the named file counts.
        (self.data_dir / plugin.REPORT_NAME).write_text("REPORT", encoding="utf-8")
        self._plant(variant=plugin.EVAL_VARIANT_FROM_REPORT, report=BENCH_REPORT)
        self.assertIn("SCAN IN PROGRESS", self._eval_call()["context"])
        (self.data_dir / BENCH_REPORT).write_text("BENCH", encoding="utf-8")
        self.assertIn("SCAN COMPLETED", self._eval_call()["context"])

    def test_eval_marker_from_report_needs_a_bench_report_name(self):
        for report in (None, "", plugin.REPORT_NAME, "../INVENTORY.bench-x.md", "/opt/data/INVENTORY.bench-x.md",
                       "INVENTORY.bench-X.md", ".ssh/id_rsa", "INVENTORY.bench-x.md\n"):
            with self.subTest(report=report):
                extra = {} if report is None else {"report": report}
                self._plant(variant=plugin.EVAL_VARIANT_FROM_REPORT, **extra)
                patch, fake = self._sandbox(read=lambda *a, **k: b"#")
                with patch:
                    self.assertIsNone(self._eval_call())
                fake.read_bytes.assert_not_called()

    def test_eval_marker_from_report_touches_no_onboarding_state(self):
        self._plant(variant=plugin.EVAL_VARIANT_FROM_REPORT, report=BENCH_REPORT)
        patch, _ = self._sandbox(read=lambda *a, **k: b"#")
        with patch:
            self.assertIsNotNone(self._eval_call())
        self.update_job.assert_not_called()
        self.trigger_job.assert_not_called()
        self.assertFalse((self.data_dir / ".user_aligned").exists())
        self.assertFalse((self.data_dir / plugin.GREETED_MARKER).exists())

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
        self.assertEqual(self.update_job.call_count, len(plugin.DELIVERY_JOB_IDS))
        self.assertTrue((self.data_dir / plugin.GREETED_MARKER).exists())


class ConversationPrimingTest(unittest.TestCase):
    """Under spec.mode next: an API-server first turn the hermes-bridge relayed from a chat."""

    # The same workspace and framework doubles, without running PreLlmCallTest's tests again.
    tearDown = PreLlmCallTest.tearDown
    _call = PreLlmCallTest._call
    _end_turn = PreLlmCallTest._end_turn
    _holds = PreLlmCallTest._holds
    _arm_home_channel = PreLlmCallTest._arm_home_channel

    def setUp(self):
        PreLlmCallTest.setUp(self)
        self.db = self.data_dir / "session_kv.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT)")
        self._route(BRIDGE_SESSION, SLACK_ROUTE)
        env = mock.patch.dict(
            "os.environ", {"SESSION_KV_DB_PATH": str(self.db), "A2A_NOTIFY_CONVERSATIONS": "slack"}
        )
        env.start()
        self.addCleanup(env.stop)
        for patcher in (
            mock.patch.object(plugin, "CHAT_NOTIFY_PATH", REPO_CHAT_NOTIFY),
            mock.patch.object(plugin, "BOOTSTRAP_HANDOFF_PATH", REPO_BOOTSTRAP_HANDOFF),
            mock.patch.object(plugin, "_chat_notify", None),
            mock.patch.object(plugin, "_unserved_warned", set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _route(self, session_id, route):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_metadata VALUES (?, ?)",
                (session_id, json.dumps({"conversation_route": route})),
            )

    def _bridge_call(self, **overrides):
        return self._call(**{"platform": "api_server", "session_id": BRIDGE_SESSION, **overrides})

    def _assert_unprimed(self):
        for name in (plugin.CONVERSATION_FILE, plugin.CONVERSATION_ALIGNED_MARKER, plugin.GREETED_MARKER,
                     ".user_aligned", plugin.PRIMING_MARKER):
            self.assertFalse((self.data_dir / name).exists(), name)
        self.trigger_job.assert_not_called()

    def test_a_bridged_slack_turn_records_the_conversation_and_greets(self):
        persist, save_env_value = self._arm_home_channel()
        result = self._bridge_call()
        self.assertIn("SCAN IN PROGRESS", result["context"])
        self.assertNotIn(plugin.HOME_CHANNEL_INSTRUCTION, result["context"])
        record = json.loads((self.data_dir / plugin.CONVERSATION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(
            {k: record[k] for k in ("platform", "conversation", "context_id")},
            {k: SLACK_ROUTE[k] for k in ("platform", "conversation", "context_id")},
        )
        self.assertIsInstance(record["bound_at"], float)
        self.assertTrue((self.data_dir / plugin.CONVERSATION_ALIGNED_MARKER).exists())
        self.assertTrue((self.data_dir / plugin.GREETED_MARKER).exists())
        self.assertFalse((self.data_dir / plugin.PRIMING_MARKER).exists())
        # Held behind the greeting; the turn's end marks oobe due.
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])
        self.trigger_job.assert_not_called()
        self._end_turn(platform="api_server", session_id=BRIDGE_SESSION)
        self.assertEqual(self._holds(), [])
        self.trigger_job.assert_called_once_with("oobe")
        # The scheduler cannot post to a gateway conversation: no job is pointed anywhere, the
        # origin path's marker stays absent, and no Hermes home channel is written.
        self.update_job.assert_not_called()
        self.assertFalse((self.data_dir / ".user_aligned").exists())
        persist.assert_not_called()
        save_env_value.assert_not_called()

    def test_a_ready_report_gives_the_completed_greeting(self):
        (self.data_dir / "INVENTORY.md").write_text("# report\n", encoding="utf-8")
        self.assertIn("SCAN COMPLETED", self._bridge_call()["context"])

    def test_a_google_chat_route_primes_when_the_gateway_serves_it(self):
        self._route(BRIDGE_SESSION, {**SLACK_ROUTE, "platform": "google_chat",
                                     "conversation": "gchat:spaces/S/threads/T"})
        with mock.patch.dict("os.environ", {"A2A_NOTIFY_CONVERSATIONS": "google_chat"}):
            self.assertIsNotNone(self._bridge_call())
        record = json.loads((self.data_dir / plugin.CONVERSATION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["conversation"], "gchat:spaces/S/threads/T")

    def test_an_api_server_turn_with_no_route_is_ignored(self):
        # The bench, curl, and the inject and A2A doors: no chat conversation behind the session.
        self.assertIsNone(self._bridge_call(session_id="a2a-ctx-inject-only"))
        self.assertIsNone(self._bridge_call(session_id="20260720_120000_abcd1234"))
        self._assert_unprimed()

    def test_a_route_with_no_context_is_ignored(self):
        self._route(BRIDGE_SESSION, {**SLACK_ROUTE, "context_id": ""})
        self.assertIsNone(self._bridge_call())
        self._assert_unprimed()

    def test_a_route_to_a_platform_without_durable_delivery_is_ignored(self):
        self._route(BRIDGE_SESSION, {**SLACK_ROUTE, "platform": "discord", "conversation": "discord:g/c"})
        with mock.patch.dict("os.environ", {"A2A_NOTIFY_CONVERSATIONS": "discord"}):
            self.assertIsNone(self._bridge_call())
        self._assert_unprimed()

    def test_a_gateway_that_does_not_serve_the_platform_leaves_onboarding_unprimed(self):
        for served in ("", "google_chat"):
            with self.subTest(served=served), mock.patch.dict("os.environ", {"A2A_NOTIFY_CONVERSATIONS": served}):
                self.assertIsNone(self._bridge_call())
                self._assert_unprimed()

    def test_chat_notify_is_loaded_once(self):
        with mock.patch.object(plugin, "_load_image_module", wraps=plugin._load_image_module) as load:
            self._bridge_call(session_id="a2a-ctx-none-1")
            self._bridge_call(session_id="a2a-ctx-none-2", is_first_turn=False)
        self.assertEqual([c.args[0] for c in load.call_args_list].count(REPO_CHAT_NOTIFY), 1)

    def test_an_unserved_platform_is_warned_about_once(self):
        with mock.patch.dict("os.environ", {"A2A_NOTIFY_CONVERSATIONS": "google_chat"}), \
                self.assertLogs(plugin.logger, level="WARNING") as logs:
            self._bridge_call()
            self._bridge_call(is_first_turn=False)
        self.assertEqual(sum("does not serve" in line for line in logs.output), 1)

    def test_an_unreadable_session_store_leaves_onboarding_unprimed(self):
        self.db.write_text("not a database", encoding="utf-8")
        self.assertIsNone(self._bridge_call())
        self._assert_unprimed()

    def test_a_later_turn_in_the_conversation_primes_once_the_route_is_there(self):
        # The first message found no route (the bridge's write or this read failed); the
        # conversation keeps its session, so the next message is not a first turn.
        self.assertIsNone(self._bridge_call(session_id="a2a-ctx-late"))
        self._route("a2a-ctx-late", {**SLACK_ROUTE, "conversation": "slack:C0HOME/2.2", "context_id": "ctx-late"})
        self.assertIsNotNone(self._bridge_call(session_id="a2a-ctx-late", is_first_turn=False))
        record = json.loads((self.data_dir / plugin.CONVERSATION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["conversation"], "slack:C0HOME/2.2")
        # Once greeted, no later turn primes again.
        self.assertIsNone(self._bridge_call(is_first_turn=False))
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])
        self.trigger_job.assert_not_called()

    def test_a_later_turn_on_a_chat_platform_is_still_ignored(self):
        self.assertIsNone(self._call(is_first_turn=False))
        self.assertFalse((self.data_dir / ".user_aligned").exists())

    def test_a_failed_marker_touch_leaves_no_binding_behind(self):
        real_touch = Path.touch

        def touch(path, *args, **kwargs):
            if path.name == plugin.CONVERSATION_ALIGNED_MARKER:
                raise OSError("disk full")
            return real_touch(path, *args, **kwargs)

        with mock.patch.object(Path, "touch", touch):
            self.assertIsNone(self._bridge_call())
        self.assertFalse((self.data_dir / plugin.CONVERSATION_FILE).exists())
        self.assertFalse((self.data_dir / plugin.PRIMING_MARKER).exists())
        # Nor a hold: delivery would wait on it for a binding that is not there.
        self.assertEqual(self._holds(), [])
        self._end_turn(platform="api_server", session_id=BRIDGE_SESSION)
        self.trigger_job.assert_not_called()

    def test_a_live_priming_holds_off_a_second_first_turn(self):
        (self.data_dir / plugin.PRIMING_MARKER).touch()
        self.assertIsNone(self._bridge_call())
        self.assertFalse((self.data_dir / plugin.CONVERSATION_FILE).exists())
        self.assertTrue((self.data_dir / plugin.PRIMING_MARKER).exists())
        # A losing priming writes no hold either.
        self.assertEqual(self._holds(), [])

    def test_the_hold_comes_after_the_binding_and_before_the_aligned_marker(self):
        seen = []
        real_touch = Path.touch

        def touch(path, *args, **kwargs):
            if path.name == plugin.CONVERSATION_ALIGNED_MARKER:
                seen.append(((self.data_dir / plugin.CONVERSATION_FILE).exists(), self._holds()))
            return real_touch(path, *args, **kwargs)

        with mock.patch.object(Path, "touch", touch):
            self._bridge_call()
        self.assertEqual(seen, [(True, [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])])

    def test_a_failed_hold_on_the_conversation_path_triggers_at_once(self):
        with mock.patch.object(Path, "touch", _touch_failing(plugin.GREETING_PENDING_PREFIX)):
            self.assertIsNotNone(self._bridge_call())
        self.assertTrue((self.data_dir / plugin.CONVERSATION_ALIGNED_MARKER).exists())
        self.trigger_job.assert_called_once_with("oobe")

    def test_a_stale_priming_is_taken_over(self):
        marker = self.data_dir / plugin.PRIMING_MARKER
        marker.touch()
        old = time.time() - plugin.PRIMING_STALE_SECONDS - 1
        os.utime(marker, (old, old))
        self.assertIsNotNone(self._bridge_call())
        self.assertTrue((self.data_dir / plugin.CONVERSATION_FILE).exists())
        self.assertFalse(marker.exists())

    def test_the_next_first_turn_after_priming_is_ignored(self):
        self._bridge_call()
        self._route("a2a-ctx-second", {**SLACK_ROUTE, "conversation": "slack:D0DM/1.1", "context_id": "ctx-second"})
        self.assertIsNone(self._bridge_call(session_id="a2a-ctx-second"))
        record = json.loads((self.data_dir / plugin.CONVERSATION_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["conversation"], SLACK_ROUTE["conversation"])
        self.assertEqual(self._holds(), [f"{plugin.GREETING_PENDING_PREFIX}{TURN}"])
        self.trigger_job.assert_not_called()

    def test_a_sweep_from_before_this_path_is_not_primed(self):
        marker = self.data_dir / plugin.SCAN_FILED_MARKER
        marker.write_text("task_id=t_1\nfiled_at=1\n", encoding="utf-8")
        old = time.time() - plugin.SCAN_MAX_AGE_SECONDS - 1
        os.utime(marker, (old, old))
        self.assertIsNone(self._bridge_call())
        self._assert_unprimed()

    def test_the_gate_reads_filed_at_before_the_mtime(self):
        # A copied data volume rewrites the mtime; the marker's own line keeps the filing time.
        marker = self.data_dir / plugin.SCAN_FILED_MARKER
        filed = time.time() - plugin.SCAN_MAX_AGE_SECONDS - 60
        marker.write_text(f"task_id=t_1\nfiled_at={filed}\n", encoding="utf-8")
        self.assertIsNone(self._bridge_call())
        self._assert_unprimed()
        marker.write_text(f"task_id=t_1\nfiled_at={time.time() - 60}\n", encoding="utf-8")
        old = time.time() - plugin.SCAN_MAX_AGE_SECONDS - 60
        os.utime(marker, (old, old))
        self.assertIsNotNone(self._bridge_call())

    def test_a_recent_sweep_is_primed(self):
        (self.data_dir / plugin.SCAN_FILED_MARKER).write_text("task_id=t_1\n", encoding="utf-8")
        self.assertIsNotNone(self._bridge_call())

    def test_a_failed_conversation_write_leaves_onboarding_unprimed(self):
        (self.data_dir / plugin.CONVERSATION_FILE).mkdir()
        self.assertIsNone(self._bridge_call())
        for name in (plugin.CONVERSATION_ALIGNED_MARKER, plugin.GREETED_MARKER, plugin.PRIMING_MARKER):
            self.assertFalse((self.data_dir / name).exists(), name)
        self.trigger_job.assert_not_called()

    def test_a_new_binding_starts_the_attempt_count_and_outcome_again(self):
        (self.data_dir / plugin.CONVERSATION_ATTEMPTS_FILE).write_text('{"attempts": 5}', encoding="utf-8")
        (self.data_dir / plugin.CONVERSATION_DONE_FILE).write_text("posted\n", encoding="utf-8")
        self.assertIsNotNone(self._bridge_call())
        self.assertFalse((self.data_dir / plugin.CONVERSATION_ATTEMPTS_FILE).exists())
        self.assertFalse((self.data_dir / plugin.CONVERSATION_DONE_FILE).exists())

    def test_a_turn_that_takes_the_priming_after_a_greeting_binds_nothing(self):
        # It passed the greeted check while another turn was priming, and takes the marker after.
        real_take = plugin._take_priming

        def take_after_greeting(data_dir):
            (data_dir / plugin.GREETED_MARKER).touch()
            return real_take(data_dir)

        with mock.patch.object(plugin, "_take_priming", side_effect=take_after_greeting):
            self.assertIsNone(self._bridge_call())
        self.assertFalse((self.data_dir / plugin.CONVERSATION_FILE).exists())
        self.assertFalse((self.data_dir / plugin.PRIMING_MARKER).exists())
        self.trigger_job.assert_not_called()

    def test_already_greeted_is_ignored(self):
        (self.data_dir / plugin.GREETED_MARKER).touch()
        self.assertIsNone(self._bridge_call())
        self.assertFalse((self.data_dir / plugin.CONVERSATION_FILE).exists())


if __name__ == "__main__":
    unittest.main()
