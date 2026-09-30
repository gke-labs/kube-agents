"""Host tests for the KAGE_SLACK_UX boilerplate patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_boilerplate.py

The fixtures carry upstream's call sites verbatim (v2026.9.14) inside trimmed
stand-ins for the four files: the cron wrapper and the per-target send lanes,
the heartbeat's mode read, the notice send and the lifecycle notices. The
tests apply the patch, exec the patched and unpatched fixtures, and compare
what each sends: with the flag off, or for any platform but Slack, the two
must be identical.
"""

import asyncio
import importlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

import apply_slack_boilerplate as applier
import slack_boilerplate as runtime
import verify_slack_boilerplate as verifier

DELIVERY = '''\
"""Fixture standing in for cron/scheduler_delivery.py."""
SENT = []


def _prepare_target_delivery(target):
    return target


def _deliver_via_live_adapter(t, content, media_files, **kwargs):
    SENT.append(("live", t.platform_name, content))
    return t.live_adapter_ready


def _deliver_standalone(t, content, media_files, target_errors, delivery_errors):
    SENT.append(("standalone", t.platform_name, content))


def _deliver_result(job, content, targets, wrap_response=True):
    delivery_errors = []
    unverified_targets: list = []
    if wrap_response:
        task_name = job.get("name", job["id"])
        delivery_content = (
            f"Cronjob Response: {task_name}\\n"
            f"(job_id: {job.get('id', '')})\\n"
            f"-------------\\n\\n"
            f"{content}\\n\\n"
            "To stop or manage this job, send me a new message "
            f"(e.g. \\"stop reminder {task_name}\\")."
        )
    else:
        delivery_content = content

    from gateway.platforms.base import BasePlatformAdapter
    media_files, cleaned_delivery_content = BasePlatformAdapter.extract_media(delivery_content)
    for target in targets:
        t = _prepare_target_delivery(target)
        if t is None:
            continue
        target_errors: list = []
        delivered = t.live_adapter_ready and _deliver_via_live_adapter(
            t, cleaned_delivery_content, media_files,
            target_errors=target_errors, delivery_errors=delivery_errors,
            unverified_targets=unverified_targets,
        )
        if not delivered:
            _deliver_standalone(
                t, cleaned_delivery_content, media_files, target_errors, delivery_errors)
'''

PLATFORMS_BASE = '''\
"""Fixture standing in for gateway/platforms/base.py."""


class BasePlatformAdapter:
    @staticmethod
    def extract_media(content):
        return [], content.replace("MEDIA:/tmp/a.png", "").strip()
'''

RUN_TURN = '''\
"""Fixture standing in for gateway/run_turn.py."""


class TurnRunner:
    async def _run_agent_notify_long_running(
        self, disp, turn_ctx, _executor_task_holder: list,
    ) -> None:
        """Periodic "still working" heartbeat."""
        _NOTIFY_INTERVAL = 180
        _long_running_mode = disp._display_surface_mode("long_running_notifications", default=True, allow_generic=True)
        if _NOTIFY_INTERVAL <= 0 or _long_running_mode == "off":
            return
        return _long_running_mode
'''

RUN_SHUTDOWN = '''\
"""Fixture standing in for gateway/run_shutdown.py."""
from typing import Optional


def _send_failed(result):
    return not getattr(result, "success", False)


def _send_error(result):
    return getattr(result, "error", None)


class GatewayShutdownMixin:
    @staticmethod
    async def _send_notice_logged(
        adapter, chat_id: str, msg: str, platform_str: str, fail_fmt: str, raise_fmt: Optional[str] = None, **kw
    ) -> bool:
        """``adapter.send`` whose failure is debug-logged."""
        try:
            result = await adapter.send(chat_id, msg, **kw)
        except Exception:
            return False
        if _send_failed(result):
            return False
        return True

    async def _notify_active_sessions_of_shutdown(self):
        msg = "⚠️ Gateway shutting down — Your current task will be interrupted."
        if self._restart_requested:
            msg = (
                "⚠️ Gateway restarting — Your current task will be interrupted. "
                "Send any message after restart and I'll try to resume where you left off."
            )
        return msg

    async def _notify_interrupted_cron_jobs(self, job, job_id):
        action = "restarting" if self._restart_requested else "shutting down"
        msg = (
            f"⚠️ Cron job '{job.get('name') or job_id}' was interrupted — "
            f"the gateway is {action} and killed the run before it "
            "finished. No result was produced for this run."
        )
        return msg
'''

RUN_NOTIFICATIONS = '''\
"""Fixture standing in for gateway/run_notifications.py."""


class GatewayNotificationsMixin:
    async def _send_restart_notification(self, transport, platform, chat_id):
        result = await transport.send(
                platform, str(chat_id), "♻ Gateway restarted successfully. Your session continues.",
                metadata=None,
            )
        return result

    async def _send_home_channel_message(self, platform, home, transport, message: str, failure_fmt: str) -> bool:
        """Best-effort send to one home channel; True on success, failures logged with ``failure_fmt``."""
        from gateway.run import _non_conversational_metadata
        result = await transport.send(platform, str(home.chat_id), message, metadata=None)
        return result

    async def _send_home_channel_startup_notifications(self, *, skip_targets=None):
        message = "♻️ Gateway online — Hermes is back and ready."
        free_tier_line = self._free_tier_startup_line()
        if free_tier_line:
            message = f"{message}\\n{free_tier_line}"
        return message
'''

FIXTURES = {
    applier.DELIVERY: DELIVERY,
    applier.RUN_TURN: RUN_TURN,
    applier.RUN_SHUTDOWN: RUN_SHUTDOWN,
    applier.RUN_NOTIFICATIONS: RUN_NOTIFICATIONS,
    "gateway/platforms/base.py": PLATFORMS_BASE,
    "gateway/run.py": "def _non_conversational_metadata(metadata, platform=None):\n    return metadata\n",
}

REPORT = "Your fleet: 3 clusters, all healthy."
FLAG_ON = {"KAGE_SLACK_UX": "1"}


def _run(coro):
    return asyncio.run(coro)


class _Root:
    """A throwaway Hermes root holding the fixtures and the runtime module."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp())
        for relative, text in FIXTURES.items():
            path = self.dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        for package in ("gateway", "gateway/platforms"):
            (self.dir / package / "__init__.py").write_text("")
        shutil.copy(HERE / "slack_boilerplate.py", self.dir / "gateway" / "slack_boilerplate.py")
        # On the path for the root's lifetime: the delivery fixture imports
        # gateway.platforms.base when it is called, not when it is loaded.
        sys.path.insert(0, str(self.dir))
        self._forget_gateway()

    @staticmethod
    def _forget_gateway():
        for module in [m for m in sys.modules if m == "gateway" or m.startswith("gateway.")]:
            sys.modules.pop(module)

    def load(self, relative, name):
        """Exec one (possibly patched) fixture with ``gateway`` importable."""
        namespace = {"__name__": name}
        exec(compile((self.dir / relative).read_text(), name, "exec"), namespace)  # noqa: S102
        return namespace

    def cleanup(self):
        sys.path.remove(str(self.dir))
        self._forget_gateway()
        shutil.rmtree(self.dir, ignore_errors=True)


def _target(platform, live=True):
    return SimpleNamespace(platform_name=platform, chat_id="C1", job={"id": "j1"}, live_adapter_ready=live)


class _Transport:
    def __init__(self):
        self.sent = []

    async def send(self, platform, chat_id, message, metadata=None):
        self.sent.append(message)
        return SimpleNamespace(success=True)


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, msg, **kw):
        self.sent.append(msg)
        return SimpleNamespace(success=True)


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)

    def test_applies_once(self):
        applier.apply(self.root.dir)
        for relative in (applier.DELIVERY, applier.RUN_TURN, applier.RUN_SHUTDOWN, applier.RUN_NOTIFICATIONS):
            self.assertIn(applier.BUILD_MARKER, (self.root.dir / relative).read_text())
        with self.assertRaises(SystemExit):
            applier.apply(self.root.dir)

    def test_drift_fails_loudly_and_writes_nothing(self):
        path = self.root.dir / applier.RUN_NOTIFICATIONS
        drifted = RUN_NOTIFICATIONS.replace("Your session continues.", "Welcome back.")
        path.write_text(drifted)
        with self.assertRaises(SystemExit) as caught:
            applier.apply(self.root.dir)
        self.assertIn("post-restart notice", str(caught.exception))
        self.assertEqual(path.read_text(), drifted)
        self.assertEqual((self.root.dir / applier.DELIVERY).read_text(), DELIVERY)

    def test_verifier_passes_on_patched_tree(self):
        applier.apply(self.root.dir)
        verifier.main(self.root.dir)

    def test_verifier_refuses_unpatched_tree(self):
        with self.assertRaises(SystemExit):
            verifier.main(self.root.dir)

    def test_verifier_refuses_a_reworded_upstream_notice(self):
        applier.apply(self.root.dir)
        path = self.root.dir / applier.RUN_SHUTDOWN
        path.write_text(path.read_text().replace("Gateway shutting down", "Gateway stopping"))
        with self.assertRaises(SystemExit) as caught:
            verifier.main(self.root.dir)
        self.assertIn("Gateway stopping", str(caught.exception))

    def test_verifier_refuses_a_dropped_wrapper(self):
        applier.apply(self.root.dir)
        path = self.root.dir / applier.DELIVERY
        path.write_text(path.read_text().replace("Cronjob Response: ", "Report: "))
        with self.assertRaises(SystemExit):
            verifier.main(self.root.dir)


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        self.upstream = self.root.load(applier.DELIVERY, "upstream")
        applier.apply(self.root.dir)

    def _sends(self, namespace, targets, env, content=REPORT):
        namespace["SENT"].clear()
        with mock.patch.dict(os.environ, env):
            namespace["_deliver_result"]({"id": "j1", "name": "inventory"}, content, targets)
        return list(namespace["SENT"])

    def test_flag_off_is_upstream(self):
        patched = self.root.load(applier.DELIVERY, "patched")
        targets = [_target("slack"), _target("slack", live=False), _target("telegram")]
        with mock.patch.dict(os.environ, {"KAGE_SLACK_UX": ""}):
            self.assertEqual(self._sends(patched, targets, {}), self._sends(self.upstream, targets, {}))

    def test_flag_on_unwraps_slack_on_both_lanes_only(self):
        patched = self.root.load(applier.DELIVERY, "patched")
        targets = [_target("slack"), _target("slack", live=False), _target("chat"), _target("google_chat")]
        sent = self._sends(patched, targets, FLAG_ON)
        upstream = self._sends(self.upstream, targets, FLAG_ON)
        self.assertEqual(
            sent[:2],
            [("live", "slack", REPORT), ("standalone", "slack", REPORT)],
        )
        self.assertEqual(sent[2:], upstream[2:])
        self.assertTrue(all(text.startswith("Cronjob Response: inventory") for _, _, text in sent[2:]))

    def test_flag_on_keeps_media_extraction(self):
        patched = self.root.load(applier.DELIVERY, "patched")
        sent = self._sends(patched, [_target("slack")], FLAG_ON, content=f"{REPORT}\nMEDIA:/tmp/a.png")
        self.assertEqual(sent, [("live", "slack", REPORT)])


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        self.upstream = self.root.load(applier.RUN_TURN, "upstream")["TurnRunner"]()
        applier.apply(self.root.dir)
        self.patched = self.root.load(applier.RUN_TURN, "patched")["TurnRunner"]()

    def _mode(self, runner, platform, mode, env):
        disp = SimpleNamespace(_display_surface_mode=lambda *a, **k: mode)
        turn_ctx = SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value=platform)))
        with mock.patch.dict(os.environ, env):
            return _run(runner._run_agent_notify_long_running(disp, turn_ctx, []))

    def test_matrix(self):
        for platform in ("slack", "google_chat"):
            for mode in ("raw", "generic", "off"):
                for env in ({"KAGE_SLACK_UX": ""}, FLAG_ON):
                    with self.subTest(platform=platform, mode=mode, env=env):
                        expected = self._mode(self.upstream, platform, mode, env)
                        if platform == "slack" and mode == "raw" and env is FLAG_ON:
                            expected = "generic"
                        self.assertEqual(self._mode(self.patched, platform, mode, env), expected)


class NoticeTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        self.upstream_shutdown = self.root.load(applier.RUN_SHUTDOWN, "upstream")["GatewayShutdownMixin"]
        self.upstream_notes = self.root.load(applier.RUN_NOTIFICATIONS, "upstream")["GatewayNotificationsMixin"]
        applier.apply(self.root.dir)
        self.shutdown = self.root.load(applier.RUN_SHUTDOWN, "patched")["GatewayShutdownMixin"]
        self.notes = self.root.load(applier.RUN_NOTIFICATIONS, "patched")["GatewayNotificationsMixin"]

    def _notices(self):
        """Every lifecycle notice, as upstream's fixture renders it."""
        out = []
        for restart in (False, True):
            owner = SimpleNamespace(_restart_requested=restart)
            out.append(_run(self.upstream_shutdown._notify_active_sessions_of_shutdown(owner)))
            out.append(_run(self.upstream_shutdown._notify_interrupted_cron_jobs(owner, {"name": "inventory"}, "j1")))
        for line in (None, "Inference: free tier"):
            owner = SimpleNamespace(_free_tier_startup_line=lambda line=line: line)
            out.append(_run(self.upstream_notes._send_home_channel_startup_notifications(owner)))
        return out

    def _send_notice(self, cls, platform, msg, env):
        adapter = _Adapter()
        with mock.patch.dict(os.environ, env):
            _run(cls._send_notice_logged(adapter, "C1", msg, platform, "fail %s %s %s"))
        return adapter.sent[0]

    def _home(self, cls, platform, msg, env):
        transport = _Transport()
        with mock.patch.dict(os.environ, env):
            _run(cls._send_home_channel_message(None, platform, SimpleNamespace(chat_id="C1"), transport, msg, "f"))
        return transport.sent[0]

    def _restarted(self, cls, platform, env):
        transport = _Transport()
        with mock.patch.dict(os.environ, env):
            _run(cls._send_restart_notification(None, transport, platform, "C1"))
        return transport.sent[0]

    def test_flag_off_and_other_platforms_are_upstream(self):
        for platform, env in (("slack", {"KAGE_SLACK_UX": ""}), ("google_chat", FLAG_ON), ("telegram", FLAG_ON)):
            for notice in self._notices():
                with self.subTest(platform=platform, notice=notice):
                    self.assertEqual(
                        self._send_notice(self.shutdown, platform, notice, env),
                        self._send_notice(self.upstream_shutdown, platform, notice, env),
                    )
                    self.assertEqual(self._home(self.notes, platform, notice, env), notice)
            self.assertEqual(
                self._restarted(self.notes, platform, env), self._restarted(self.upstream_notes, platform, env)
            )

    def test_flag_on_slack_rewords_every_notice(self):
        texts = [self._send_notice(self.shutdown, "slack", n, FLAG_ON) for n in self._notices()]
        texts.append(self._restarted(self.notes, "slack", FLAG_ON))
        for text in texts:
            with self.subTest(text=text):
                self.assertNotIn("Gateway", text)
                self.assertNotIn("Hermes", text)
                self.assertFalse(text.startswith(("⚠", "♻")))
        self.assertEqual(
            texts[:4],
            [
                "I'm going offline for a moment, so I've had to stop what I was working on.",
                (
                    "I had to stop the scheduled job 'inventory' before it finished because I was going offline, "
                    "so there's no result from this run."
                ),
                (
                    "I'm restarting, so I've had to stop what I was working on. "
                    "Send me a message once I'm back and I'll pick up where I left off."
                ),
                (
                    "I had to stop the scheduled job 'inventory' before it finished because I was restarting, "
                    "so there's no result from this run."
                ),
            ],
        )
        self.assertEqual(texts[4:], ["I'm back online.", "I'm back online.\nInference: free tier",
                                     "I'm back. We can carry on where we left off."])

    def test_home_channel_rewords_the_startup_notice(self):
        notice = "♻️ Gateway online — Hermes is back and ready."
        self.assertEqual(self._home(self.notes, "slack", notice, FLAG_ON), "I'm back online.")

    def test_unknown_text_passes_through(self):
        with mock.patch.dict(os.environ, FLAG_ON):
            self.assertEqual(runtime.notice_text("slack", "⚠️ Something new"), "⚠️ Something new")
            self.assertEqual(runtime.notice_text("slack", None), None)


class RuntimeTest(unittest.TestCase):
    def test_platform_enum_and_string_alike(self):
        with mock.patch.dict(os.environ, FLAG_ON):
            slack = SimpleNamespace(platform=SimpleNamespace(value="slack"))
            self.assertEqual(runtime.long_running_mode(slack, "raw"), "generic")
            self.assertEqual(runtime.long_running_mode(SimpleNamespace(platform="SLACK"), "raw"), "generic")
            self.assertEqual(runtime.long_running_mode(SimpleNamespace(), "raw"), "raw")

    def test_cron_logs_only_when_it_unwraps(self):
        with mock.patch.dict(os.environ, FLAG_ON), mock.patch.object(runtime.logger, "info") as info:
            runtime.cron_delivery_text(_target("slack"), REPORT, REPORT, lambda s: ([], s))
            self.assertFalse(info.called)
            runtime.cron_delivery_text(_target("slack"), REPORT, "wrapped", lambda s: ([], s))
            self.assertTrue(info.called)


class MissingPresenterTest(unittest.TestCase):
    def setUp(self):
        importlib.reload(runtime)

    def test_treated_as_off(self):
        with mock.patch.object(runtime, "_presenter", None), mock.patch.dict(os.environ, FLAG_ON):
            self.assertFalse(runtime.enabled())
            self.assertEqual(runtime.cron_delivery_text(_target("slack"), REPORT, "w", lambda s: ([], s)), "w")

    def test_warns_only_when_the_flag_is_on(self):
        for value, warns in (("0", False), ("false", False), ("on", True)):
            importlib.reload(runtime)
            with self.subTest(flag=value), mock.patch.object(runtime, "_presenter", None), mock.patch.dict(
                os.environ, {"KAGE_SLACK_UX": value}
            ), mock.patch.object(runtime.logger, "warning") as warning:
                runtime.enabled()
                self.assertEqual(warning.called, warns)


if __name__ == "__main__":
    unittest.main()
