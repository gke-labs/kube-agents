"""Host tests for the KAGE_SLACK_UX boilerplate patch. No Hermes install required.

Run: python3 -m pytest deploy/docker/patches/test_slack_boilerplate.py

The fixtures carry upstream's call sites verbatim (v2026.9.14) inside trimmed
stand-ins for the patched files: the cron wrapper and the per-target send
lanes, the heartbeat's mode read, the notice send, the lifecycle notices, the
home-channel startup and session-database loops, the busy acks with their
onboarding hint, the mid-turn slash-command and ``/steer`` replies, the
``/restart`` and ``/stop`` catalog entries, the Slack adapter's send, and the
system replies the verifier reads out of source. The tests apply the patch,
exec the patched and unpatched fixtures, and compare what each sends: with the
flag off, or for any platform but Slack, the two must be identical.
"""

import ast
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
from dataclasses import dataclass

SENT = []


@dataclass
class _TargetDelivery:
    job: dict
    platform_name: str
    chat_id: str
    live_adapter_ready: bool = False


def _prepare_target_delivery(target) -> "Optional[_TargetDelivery]":
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
        _status_thread_metadata = turn_ctx._status_thread_metadata
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
import logging

logger = logging.getLogger(__name__)
UNLINKED = []


def _notice_target_key(platform, chat_id, thread_id):
    return (platform, str(chat_id), thread_id)


class GatewayNotificationsMixin:
    async def _send_restart_notification(self, transport, platform, chat_id):
        platform_str = platform.value
        try:
            result = await transport.send(
                platform, str(chat_id), "♻ Gateway restarted successfully. Your session continues.",
                metadata=None,
            )
            return result
        finally:
            UNLINKED.append(chat_id)

    async def _send_home_channel_message(self, platform, home, transport, message: str, failure_fmt: str) -> bool:
        """Best-effort send to one home channel; True on success, failures logged with ``failure_fmt``."""
        from gateway.run import _non_conversational_metadata
        result = await transport.send(platform, str(home.chat_id), message, metadata=None)
        return result

    async def _send_home_channel_startup_notifications(self, *, skip_targets=None):
        delivered = set()
        skipped = skip_targets or set()
        message = "♻️ Gateway online — Hermes is back and ready."
        free_tier_line = self._free_tier_startup_line()
        if free_tier_line:
            message = f"{message}\\n{free_tier_line}"
        for platform, home, transport in self._home_channel_transports():
            target = _notice_target_key(platform.value, home.chat_id, home.thread_id)
            if target in skipped or target in delivered:
                continue
            if await self._send_home_channel_message(
                platform, home, transport, message, "Home-channel startup notification failed for %s:%s: %s",
            ):
                delivered.add(target)
        return delivered

    async def _send_session_db_warning_notifications(self) -> None:
        error = self._session_db_init_error
        if not error:
            return
        message = (
            f"⚠️ Session database unavailable — messages may not be persisted. "
            f"{error}\\nRun `hermes doctor` for diagnostics."
        )
        logger.warning("Broadcasting state.db failure warning to home channels: %s", error)
        for platform, _platform_cfg, home, transport in self._home_channel_transports():
            await self._send_home_channel_message(
                platform, home, transport, message, "state.db warning notification failed for %s:%s: %s",
            )

    def _format_process_running_message(self, session) -> str:
        new_output = self._redacted_output_tail(session, 500)
        short_cmd = getattr(session, "command", "") or ""
        header = "⏳ Background task still running" + (f" — `{short_cmd}`" if short_cmd else "")
        return f"{header}\\n\\nRecent output:\\n```\\n{new_output.strip()}\\n```" if new_output.strip() else header
'''

RUN_BUSY = '''\
"""Fixture standing in for gateway/run_busy.py."""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_hermes_home = Path("/tmp/hermes-fixture")


def _load_gateway_config():
    return {}


class GatewayBusyMixin:
    _BUSY_DEMOTED_TAIL = (
        " — your message is queued for when it finishes (use /stop to cancel everything)."
    )

    async def _send_busy_drain_notice(self, event, session_key: str, effective_mode: str) -> None:
        """Busy path while the gateway is restarting/stopping: queue (if allowed) and tell the user."""
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return
        if self._queue_during_drain_enabled(effective_mode):
            self._queue_or_replace_pending_event(session_key, event)
            message = f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
        else:
            message = f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."
        await self._send_busy_reply(event, adapter, message)

    async def _dispatch_busy_slash_command(self, name):
        return (
            f"⏳ Agent is running — `/{name}` can't run "
            f"mid-turn. Wait for the current response or `/stop` first."
        )

    async def _busy_goal_command(self, event, quick_key, source):
        return "Agent is running — use /goal status / pause / clear / wait mid-run, or /stop before setting a new goal."

    async def _busy_loop_command(self, event, quick_key, source):
        return "Agent is running — use /loop status / pause / stop mid-run, or /stop before setting a new loop."

    async def _busy_steer_command(self, event, running_agent, steer_text):
        def _queue_fallback(reply: str) -> str:
            return reply

        if running_agent is None:
            return _queue_fallback("Agent still starting — /steer queued for the next turn.")
        if not running_agent:
            return _queue_fallback("No active agent — /steer queued for the next turn.")
        try:
            running_agent.steer(steer_text)
        except Exception as exc:
            logger.warning("Steer failed: %s", exc)
            return f"⚠️ Steer failed: {exc}"
        preview = steer_text[:60] + ("..." if len(steer_text) > 60 else "")
        return f"⏩ Steer queued — arrives after the next tool call: '{preview}'"

    def _compose_busy_ack_message(self, event, status_parts, *, is_queue_mode):
        is_steer_mode = is_redirect_mode = demoted_for_subagents = demoted_for_compression = False
        status_detail = f" ({', '.join(status_parts)})" if status_parts else ""
        if is_steer_mode:
            head, tail = "⏩ Steered into current run", ". Your message arrives after the next tool call."
        elif is_redirect_mode:
            head, tail = "↪ Redirected current run", ". I'll adjust using your correction."
        elif is_queue_mode and demoted_for_subagents:
            # Explain the demotion: the follow-up didn't kill the subagent; /stop is the escape hatch.
            head, tail = "⏳ Subagent working", self._BUSY_DEMOTED_TAIL
        elif is_queue_mode and demoted_for_compression:
            head, tail = "⏳ Compressing context", self._BUSY_DEMOTED_TAIL
        elif is_queue_mode:
            head, tail = "⏳ Queued for the next turn", ". I'll respond once the current task finishes."
        else:
            head, tail = "⚡ Interrupting current task", ". I'll respond to your message shortly."
        message = f"{head}{status_detail}{tail}"

        # One-time onboarding hint about the queue/interrupt knob (flag persisted to config.yaml).
        try:
            from agent.onboarding import (BUSY_INPUT_FLAG, busy_input_hint_gateway, is_seen, mark_seen)
            if not is_seen(_load_gateway_config(), BUSY_INPUT_FLAG):
                _hint_mode = (
                    "steer" if is_steer_mode
                    else "queue" if is_queue_mode
                    else "redirect" if is_redirect_mode
                    else "interrupt"
                )
                message = f"{message}\\n\\n{busy_input_hint_gateway(_hint_mode)}"
                mark_seen(_hermes_home / "config.yaml", BUSY_INPUT_FLAG)
        except Exception as _onb_err:
            logger.debug("Failed to apply busy-input onboarding hint: %s", _onb_err)
        return message
'''

SLASH_COMMANDS = '''\
"""Fixture standing in for the /restart and /stop replies in gateway/slash_commands.py."""
from agent.i18n import t


def _stop(pending):
    return t("gateway.stop.stopped_pending") if pending else t("gateway.stop.stopped")


def _restart(count, in_progress):
    if in_progress:
        return t("gateway.draining", count=count) if count else t("gateway.restart.in_progress")
    return t("gateway.restart.restarting")
'''

LOCALES_EN = """\
gateway:
  draining:         "⏳ Draining {count} active agent(s) before restart..."
  goal_cleared:     "✓ Goal cleared."

  restart:
    in_progress:           "⏳ Gateway restart already in progress..."
    restarting:            "♻ Restarting gateway. If you aren't notified within 60 seconds, restart from the console with `hermes gateway restart`."

  stop:
    stopped_pending:       "⚡ Stopped. The agent hadn't started yet — you can continue this session."
    stopped:               "⚡ Stopped. You can continue this session."
    no_active:             "No active task to stop."
"""

ONBOARDING = '''\
"""Fixture standing in for agent/onboarding.py."""
BUSY_INPUT_FLAG = "busy_input"
SEEN = []


def is_seen(config, flag):
    return flag in SEEN


def mark_seen(path, flag):
    SEEN.append(flag)


def busy_input_hint_gateway(mode):
    return f"Tip: set display.busy_input_mode (now {mode}) in config.yaml."
'''

RUN_INBOUND = '''\
"""Fixture standing in for the replies in gateway/run_inbound.py."""


class EphemeralReply(str):
    pass


class GatewayInboundMixin:
    def _force_stopped(self):
        return EphemeralReply("⚡ Force-stopped. The agent was still starting — session unlocked.")

    def _drain_busy(self, queue_during_drain):
        return (
            f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
            if queue_during_drain
            else f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."
        )

    def _drain_command(self, command):
        return True, f"⏳ Gateway is {self._status_action_gerund()} and is not accepting new work right now.", command

    def _external_drain(self):
        return (
            "⏳ This agent is draining for a maintenance action and isn't "
            "accepting new turns right now. It'll be back in a moment — "
            "please resend shortly."
        )

    def _transcript_busy(self):
        return (
            "⏳ Another turn is still running on this session. To "
            "protect the transcript, this message was not processed. "
            "Wait for the active turn to finish, then resend it."
        )
'''

RUN_TURN_RUNNER = '''\
"""Fixture standing in for gateway/run_turn_runner.py."""


class GatewayTurnRunnerMixin:
    def _run_agent(self, ctx, runner):
        try:
            runner._resolve_session_agent_runtime(source=ctx.source)
        except Exception as exc:
            return {"final_response": f"⚠️ Provider authentication failed: {exc}", "messages": [], "api_calls": 0, "tools": []}
        return {"final_response": "ok"}
'''

RUN = '''\
"""Fixture standing in for gateway/run.py."""


def _non_conversational_metadata(metadata, platform=None):
    return metadata


class GatewayRunner:
    _BUSY_REJECT_TEXT = {
        "model": "Agent is running — wait or /stop first, then switch models.",
        "codex-runtime": "Agent is running — wait or /stop first, then change runtime.",
        "moa": "Agent is running — wait or /stop first, then run /moa."}

    def _status_action_gerund(self) -> str:
        return "restarting" if self._restart_requested else "shutting down"


_PROVIDER_ERROR_REPLIES = (
    (None, "⚠️ Provider authentication failed. Check the configured credentials; "
           "raw provider details are in the gateway logs."),
    (None, "⚠️ The model provider rejected the request. I kept the raw provider "
           "error out of chat; check gateway logs for details or try rephrasing."),
    (None, "⏱️ The model provider is rate-limiting requests. Please wait a moment and try again."),
    (None, "⚠️ The model server is not responding — it looks like the configured "
           "model endpoint is not running or is unreachable."),
)


def _gateway_provider_error_reply(text):
    return (
        "⚠️ The model provider failed after retries. I kept raw provider details "
        "out of chat; check gateway logs for diagnostics.")
'''

SLASH_COMMANDS_GOALS = '''\
"""Fixture standing in for gateway/slash_commands_goals.py."""


class GatewaySlashCommandsGoalsMixin:
    def _idle_cached_agent_or_error(self, event, verb):
        if event.source in self._running_agents:
            return event.source, None, f"Agent is running — wait for the turn to finish, then /{verb}."
        return event.source, None, None
'''

SLACK_ADAPTER = '''\
"""Fixture standing in for plugins/platforms/slack/adapter.py."""
from typing import Any, Dict, Optional


class SlackAdapter:
    def __init__(self):
        self.sent = []

    def _outbound_blocked(self, chat_id, what):
        return None

    async def _dm_target(self, chat_id, metadata):
        return chat_id

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None):
        """Send a message to a Slack channel or DM."""
        blocked = self._outbound_blocked(chat_id, "outbound generic send to")
        if blocked:
            return blocked
        chat_id = await self._dm_target(chat_id, metadata)
        thread_ts = None
        self.sent.append(content)
        return thread_ts

    async def edit_message(
        self, chat_id: str, message_id: str, content: str, *, finalize: bool = False,
        metadata: Optional[Dict[str, Any]] = None):
        """Edit a previously sent Slack message."""
        blocked = self._outbound_blocked(chat_id, "message edit in")
        if blocked:
            return blocked
        try:
            formatted = content
            self.sent.append(formatted)
        except Exception:
            return None
'''

FIXTURES = {
    applier.DELIVERY: DELIVERY,
    applier.RUN_TURN: RUN_TURN,
    verifier.RUN_SHUTDOWN: RUN_SHUTDOWN,
    verifier.SLASH_COMMANDS: SLASH_COMMANDS,
    verifier.LOCALES: LOCALES_EN,
    applier.RUN_NOTIFICATIONS: RUN_NOTIFICATIONS,
    applier.RUN_BUSY: RUN_BUSY,
    applier.SLACK_ADAPTER: SLACK_ADAPTER,
    verifier.RUN_INBOUND: RUN_INBOUND,
    verifier.RUN_TURN_RUNNER: RUN_TURN_RUNNER,
    verifier.RUN: RUN,
    verifier.SLASH_COMMANDS_GOALS: SLASH_COMMANDS_GOALS,
    "gateway/platforms/base.py": PLATFORMS_BASE,
    "agent/onboarding.py": ONBOARDING,
}
PACKAGES = ("gateway", "gateway/platforms", "agent", "plugins", "plugins/platforms", "plugins/platforms/slack")

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
        for package in PACKAGES:
            (self.dir / package).mkdir(parents=True, exist_ok=True)
            (self.dir / package / "__init__.py").write_text("")
        shutil.copy(HERE / "slack_boilerplate.py", self.dir / "gateway" / "slack_boilerplate.py")
        # On the path for the root's lifetime: the delivery fixture imports
        # gateway.platforms.base when it is called, not when it is loaded.
        sys.path.insert(0, str(self.dir))
        self._forget_gateway()

    @staticmethod
    def _forget_gateway():
        for module in [m for m in sys.modules if m.partition(".")[0] in ("gateway", "agent")]:
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
        self.sent.append((_platform(platform), message))
        return SimpleNamespace(success=True)


def _platform(platform):
    return getattr(platform, "value", platform)


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, msg, **kw):
        self.sent.append(msg)
        return SimpleNamespace(success=True)


def target_reads(source):
    """The attributes ``cron_delivery_text`` reads off ``target``.

    Every appearance of ``target`` must be ``target.<attr>`` or
    ``getattr(target, "<attr>", ...)``; any other (a keyword or positional
    hand-off, a container, an alias) carries it where this cannot follow, and
    fails rather than leaving a read unpinned.
    """
    fn = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "cron_delivery_text"
    )
    parents = {child: node for node in ast.walk(fn) for child in ast.iter_child_nodes(node)}
    read = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Name) and node.id == "target"):
            continue
        parent = parents[node]
        if isinstance(parent, ast.Attribute):
            read.add(parent.attr)
        elif (
            isinstance(parent, ast.Call) and ast.unparse(parent.func) == "getattr"
            and len(parent.args) >= 2 and parent.args[0] is node
            and isinstance(parent.args[1], ast.Constant)
        ):
            read.add(parent.args[1].value)
        else:
            raise AssertionError(f"target escapes at line {node.lineno}: {ast.unparse(parent)}")
    return read


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)

    def test_applies_once(self):
        applier.apply(self.root.dir)
        for relative in (
            applier.DELIVERY, applier.RUN_TURN, applier.RUN_NOTIFICATIONS, applier.RUN_BUSY, applier.SLACK_ADAPTER,
            applier.RUN_TURN_RUNNER,
        ):
            self.assertIn(applier.BUILD_MARKER, (self.root.dir / relative).read_text())
        # The interrupting notices are reworded in SlackAdapter.send; the shutdown module is untouched.
        self.assertEqual((self.root.dir / verifier.RUN_SHUTDOWN).read_text(), RUN_SHUTDOWN)
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
        path = self.root.dir / verifier.RUN_SHUTDOWN
        path.write_text(path.read_text().replace("Gateway shutting down", "Gateway stopping"))
        with self.assertRaises(SystemExit) as caught:
            verifier.main(self.root.dir)
        self.assertIn("Gateway stopping", str(caught.exception))

    def test_verifier_refuses_a_back_online_notice_reaching_slack(self):
        applier.apply(self.root.dir)
        runtime_path = self.root.dir / "gateway" / "slack_boilerplate.py"
        good = runtime_path.read_text()
        runtime_path.write_text(good.replace(
            "return _platform_name(platform) == PLATFORM and enabled()", "return False"))
        with self.assertRaises(SystemExit) as caught:
            verifier.main(self.root.dir)
        self.assertIn("drop_notice", str(caught.exception))
        runtime_path.write_text(good)
        path = self.root.dir / applier.RUN_NOTIFICATIONS
        patched = path.read_text()
        for exit_line in ("return None\n", "continue\n            target ="):
            with self.subTest(exit_line=exit_line):
                path.write_text(patched.replace(exit_line, "pass\n" + exit_line.partition("\n")[2], 1))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn("drop_notice(platform) guards", str(caught.exception))
        path.write_text(patched)
        verifier.main(self.root.dir)

    def test_verifier_refuses_a_reworded_upstream_system_reply(self):
        applier.apply(self.root.dir)
        for relative, old, new in (
            (verifier.RUN_INBOUND, "please resend shortly", "please retry"),
            (verifier.RUN, "Please wait a moment", "Please hold on"),
            (applier.RUN_BUSY, "after the next tool call.", "at the next tool call."),
            (applier.RUN_BUSY, "can't run ", "cannot run "),
            (verifier.LOCALES, "within 60 seconds", "within a minute"),
            (verifier.LOCALES, "You can continue", "You may continue"),
        ):
            with self.subTest(relative=relative):
                path = self.root.dir / relative
                patched = path.read_text()
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn(new, str(caught.exception))
                path.write_text(patched)

    def test_verifier_refuses_a_system_reply_or_hint_reaching_slack_unfiltered(self):
        applier.apply(self.root.dir)
        for relative, old, new, expected in (
            (applier.SLACK_ADAPTER, applier.SLACK_SEND_PATCHED, applier.SLACK_SEND_ANCHOR, "send does not"),
            (applier.SLACK_ADAPTER, applier.SLACK_EDIT_PATCHED, applier.SLACK_EDIT_ANCHOR, "edit_message does not"),
            (applier.RUN_BUSY, applier.BUSY_HINT_PATCHED, applier.BUSY_HINT_ANCHOR, "busy-input hint"),
            (applier.RUN_NOTIFICATIONS, applier.SESSION_DB_PATCHED, applier.SESSION_DB_ANCHOR, "_send_session_db"),
        ):
            with self.subTest(relative=relative):
                path = self.root.dir / relative
                patched = path.read_text()
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn(expected, str(caught.exception))
                path.write_text(patched)
        verifier.main(self.root.dir)

    def test_verifier_reads_the_action_words_from_upstream(self):
        applier.apply(self.root.dir)
        self.assertEqual(verifier.action_gerunds(self.root.dir), ("restarting", "shutting down"))
        self.assertEqual(verifier.cron_actions(self.root.dir), ("restarting", "shutting down"))
        for relative, old in (
            (verifier.RUN, 'else "shutting down"'),
            (verifier.RUN_SHUTDOWN, 'else "shutting down"'),
        ):
            with self.subTest(relative=relative, kind="new word"):
                path = self.root.dir / relative
                patched = path.read_text()
                path.write_text(patched.replace(old, 'else "updating"'))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn("'updating'", str(caught.exception))
                path.write_text(patched)
            with self.subTest(relative=relative, kind="not a literal"):
                path.write_text(patched.replace(old, "else self._reason"))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn("no longer built from string literals", str(caught.exception))
                path.write_text(patched)
        verifier.main(self.root.dir)

    def test_verifier_refuses_a_catalog_key_no_longer_looked_up(self):
        applier.apply(self.root.dir)
        path = self.root.dir / verifier.SLASH_COMMANDS
        path.write_text(path.read_text().replace('"gateway.restart.restarting"', '"gateway.restart.begin"'))
        with self.assertRaises(SystemExit) as caught:
            verifier.main(self.root.dir)
        self.assertIn("no longer looks up gateway.restart.restarting", str(caught.exception))

    def test_verifier_refuses_a_hook_reading_a_renamed_name(self):
        applier.apply(self.root.dir)
        for relative, old, new, name in (
            (applier.RUN_TURN, "self, disp, turn_ctx,", "self, disp, turn,", "turn_ctx"),
            (applier.DELIVERY, "def _deliver_result(job, content,", "def _deliver_result(job, text,", "content"),
            (applier.SLACK_ADAPTER, "self, chat_id: str, content: str,", "self, chat_id: str, text: str,", "content"),
            (applier.RUN_NOTIFICATIONS, "platform_str = platform.value", "platform_name = platform.value",
             "platform_str"),
            (applier.RUN_TURN_RUNNER, "def _run_agent(self, ctx, runner)", "def _run_agent(self, turn, runner)", "ctx"),
            (applier.RUN_BUSY, "def _busy_steer_command(self, event,", "def _busy_steer_command(self, ev,", "event"),
        ):
            with self.subTest(relative=relative, name=name):
                path = self.root.dir / relative
                patched = path.read_text()
                self.assertEqual(patched.count(old), 1)
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit) as caught:
                    verifier.check_bound(self.root.dir)
                self.assertIn(f"reads {name}, which nothing binds", str(caught.exception))
                path.write_text(patched)
        verifier.check_bound(self.root.dir)

    def test_verifier_refuses_a_renamed_target_field(self):
        # drive() hands cron_delivery_text a SimpleNamespace, and the helper reads
        # through getattr with a default, so a rename upstream raises nothing.
        applier.apply(self.root.dir)
        path = self.root.dir / applier.DELIVERY
        patched = path.read_text()
        for name in verifier.TARGET_FIELDS:
            with self.subTest(name=name):
                old = f"    {name}: "
                self.assertEqual(patched.count(old), 1)
                path.write_text(patched.replace(old, f"    {name}_v2: "))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn(f"no longer declares {name},", str(caught.exception))
        path.write_text(patched)
        verifier.main(self.root.dir)

    def test_target_fields_are_what_cron_delivery_text_reads(self):
        # A new getattr in the helper would otherwise go unpinned.
        self.assertEqual(target_reads(Path(runtime.__file__).read_text()), set(verifier.TARGET_FIELDS))

    def test_target_reads_refuses_target_escaping_the_helper(self):
        for escape in (
            "_log(job, target=target)",
            "_log(*[target])",
            "_log([target])",
            "t = target",
        ):
            with self.subTest(escape=escape):
                source = f"def cron_delivery_text(target):\n    getattr(target, 'chat_id', '')\n    {escape}\n"
                with self.assertRaises(AssertionError):
                    target_reads(source)

    def test_verifier_refuses_a_target_from_elsewhere(self):
        applier.apply(self.root.dir)
        path = self.root.dir / applier.DELIVERY
        patched = path.read_text()
        bind = "t = _prepare_target_delivery(target)"
        indent = patched.split(bind)[0].rsplit("\n", 1)[1]
        for old, new, detail in (
            ('-> "Optional[_TargetDelivery]"', '-> "Optional[_SlackTarget]"', "no longer annotated"),
            ('-> "Optional[_TargetDelivery]"', '-> "Union[_TargetDelivery, _SlackTarget]"', "no longer annotated"),
            ('-> "Optional[_TargetDelivery]"', '-> "Optional[_TargetDeliveryV2]"', "no longer annotated"),
            (bind, "t = _prepare_slack_target(target)", "no longer binds t"),
            (bind, f"{bind}\n{indent}t = target", "no longer binds t"),
        ):
            with self.subTest(detail=detail):
                self.assertEqual(patched.count(old), 1)
                path.write_text(patched.replace(old, new))
                with self.assertRaises(SystemExit) as caught:
                    verifier.check_target_fields(self.root.dir)
                self.assertIn(detail, str(caught.exception))
        path.write_text(patched)
        verifier.check_target_fields(self.root.dir)

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

    def _mode(self, runner, platform, mode, env, metadata=None):
        disp = SimpleNamespace(_display_surface_mode=lambda *a, **k: mode)
        turn_ctx = SimpleNamespace(
            source=SimpleNamespace(platform=SimpleNamespace(value=platform)), _status_thread_metadata=metadata)
        with mock.patch.dict(os.environ, env):
            return _run(runner._run_agent_notify_long_running(disp, turn_ctx, []))

    def test_matrix(self):
        for platform in ("slack", "google_chat"):
            for mode in ("raw", "generic", "off"):
                for env in ({"KAGE_SLACK_UX": ""}, FLAG_ON):
                    for metadata in (None, {"thread_id": "1.2"}, {"reply_to_message_id": "1.2"}):
                        with self.subTest(platform=platform, mode=mode, env=env, metadata=metadata):
                            expected = self._mode(self.upstream, platform, mode, env, metadata)
                            if platform == "slack" and env is FLAG_ON and mode == "raw":
                                expected = "generic"
                            self.assertEqual(self._mode(self.patched, platform, mode, env, metadata), expected)

    def test_slack_keeps_a_generic_heartbeat_in_a_thread(self):
        # The thread's status line can fail silently (no assistant:write), so the heartbeat stays.
        self.assertEqual(self._mode(self.patched, "slack", "raw", FLAG_ON, {"thread_id": "1.2"}), "generic")


class NoticeTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        self.upstream_ns = self.root.load(applier.RUN_NOTIFICATIONS, "upstream")
        self.upstream_notes = self.upstream_ns["GatewayNotificationsMixin"]
        self.upstream_slack = self.root.load(applier.SLACK_ADAPTER, "upstream")["SlackAdapter"]
        applier.apply(self.root.dir)
        self.shutdown = self.root.load(verifier.RUN_SHUTDOWN, "patched")["GatewayShutdownMixin"]
        self.slack = self.root.load(applier.SLACK_ADAPTER, "patched")["SlackAdapter"]
        self.notes_ns = self.root.load(applier.RUN_NOTIFICATIONS, "patched")
        self.notes = self.notes_ns["GatewayNotificationsMixin"]

    def _notices(self):
        """Every interrupting notice, as upstream's fixture renders it."""
        out = []
        for restart in (False, True):
            owner = SimpleNamespace(_restart_requested=restart)
            out.append(_run(self.shutdown._notify_active_sessions_of_shutdown(owner)))
            out.append(_run(self.shutdown._notify_interrupted_cron_jobs(owner, {"name": "inventory"}, "j1")))
        return out

    def _send_notice(self, adapter, platform, msg, env):
        """What ``adapter`` is handed when the shutdown path sends ``msg``."""
        with mock.patch.dict(os.environ, env):
            _run(self.shutdown._send_notice_logged(adapter, "C1", msg, platform, "fail %s %s %s"))
        return adapter.sent[0]

    def _restarted(self, namespace, platform, env):
        """What the post-restart notice sends, and whether the marker was unlinked."""
        transport = _Transport()
        namespace["UNLINKED"].clear()
        cls = namespace["GatewayNotificationsMixin"]
        with mock.patch.dict(os.environ, env):
            _run(cls._send_restart_notification(None, transport, SimpleNamespace(value=platform), "C1"))
        return transport.sent, list(namespace["UNLINKED"])

    def _startup(self, cls, platforms, env, free_tier_line=None):
        """What the home-channel startup notice sends to one home channel per platform."""
        transport = _Transport()
        owner = SimpleNamespace(
            _free_tier_startup_line=lambda: free_tier_line,
            _home_channel_transports=lambda: [
                (SimpleNamespace(value=p), SimpleNamespace(chat_id=f"C-{p}", thread_id=None), transport)
                for p in platforms
            ],
            _send_home_channel_message=lambda *a: cls._send_home_channel_message(None, *a),
        )
        with mock.patch.dict(os.environ, env):
            _run(cls._send_home_channel_startup_notifications(owner))
        return transport.sent

    def test_flag_off_and_other_platforms_are_upstream(self):
        for platform, env in (("slack", {"KAGE_SLACK_UX": ""}), ("google_chat", FLAG_ON), ("telegram", FLAG_ON)):
            for notice in self._notices():
                with self.subTest(platform=platform, notice=notice):
                    self.assertEqual(self._send_notice(_Adapter(), platform, notice, env), notice)
                    if platform == "slack":
                        self.assertEqual(
                            self._send_notice(self.slack(), platform, notice, env),
                            self._send_notice(self.upstream_slack(), platform, notice, env),
                        )
            with self.subTest(platform=platform, notice="restarted"):
                self.assertEqual(
                    self._restarted(self.notes_ns, platform, env), self._restarted(self.upstream_ns, platform, env)
                )
            for line in (None, "Inference: free tier"):
                with self.subTest(platform=platform, notice="online", line=line):
                    self.assertEqual(
                        self._startup(self.notes, [platform], env, line),
                        self._startup(self.upstream_notes, [platform], env, line),
                    )

    def test_flag_on_slack_rewords_the_interrupting_notices(self):
        texts = [self._send_notice(self.slack(), "slack", n, FLAG_ON) for n in self._notices()]
        for text in texts:
            with self.subTest(text=text):
                self.assertNotIn("Gateway", text)
                self.assertNotIn("Hermes", text)
                self.assertNotIn("once I'm back", text)
                self.assertFalse(text.startswith(("⚠", "♻")))
        self.assertEqual(
            texts,
            [
                "I'm going offline for a moment, so I've had to stop what I was working on.",
                (
                    "I had to stop the scheduled job 'inventory' before it finished because I was going offline, "
                    "so there's no result from this run."
                ),
                (
                    "I'm restarting, so I've had to stop what I was working on. "
                    "Send me a message in a minute and I'll pick up where I left off."
                ),
                (
                    "I had to stop the scheduled job 'inventory' before it finished because I was restarting, "
                    "so there's no result from this run."
                ),
            ],
        )

    def test_flag_on_slack_skips_the_restart_notice_and_still_unlinks_its_marker(self):
        self.assertEqual(self._restarted(self.notes_ns, "slack", FLAG_ON), ([], ["C1"]))

    def test_flag_on_slack_skips_the_online_notice_and_its_free_tier_line(self):
        for line in (None, "Inference: free tier"):
            with self.subTest(line=line):
                sent = self._startup(self.notes, ["slack", "telegram"], FLAG_ON, line)
                self.assertEqual(sent, self._startup(self.upstream_notes, ["telegram"], FLAG_ON, line))
                self.assertEqual([p for p, _ in sent], ["telegram"])

    def test_home_channel_helper_is_untouched(self):
        transport = _Transport()
        warning = "⚠️ Session database unavailable — messages may not be persisted."
        with mock.patch.dict(os.environ, FLAG_ON):
            _run(self.notes._send_home_channel_message(
                None, SimpleNamespace(value="slack"), SimpleNamespace(chat_id="C1"), transport, warning, "f"))
        self.assertEqual(transport.sent, [("slack", warning)])

    def test_unknown_text_passes_through(self):
        with mock.patch.dict(os.environ, FLAG_ON):
            self.assertEqual(runtime.system_text("⚠️ Something new"), "⚠️ Something new")


class SystemReplyTest(unittest.TestCase):
    """The busy acks, drain refusals and other system replies, through SlackAdapter.send."""

    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        self.upstream_adapter = self.root.load(applier.SLACK_ADAPTER, "upstream")["SlackAdapter"]
        applier.apply(self.root.dir)
        self.adapter = self.root.load(applier.SLACK_ADAPTER, "patched")["SlackAdapter"]
        self.replies = verifier.check_system(self.root.dir) + verifier.check_locale(self.root.dir)

    def _sent(self, cls, content, env):
        adapter = cls()
        with mock.patch.dict(os.environ, env):
            _run(adapter.send("C1", content))
        return adapter.sent[0]

    def _edited(self, cls, content, env):
        adapter = cls()
        with mock.patch.dict(os.environ, env):
            _run(adapter.edit_message("C1", "1700000000.000100", content, finalize=True))
        return adapter.sent[0]

    def test_edit_path_matches_send(self):
        with self.assertLogs("gateway.slack_boilerplate", "WARNING"):
            for reply in self.replies:
                with self.subTest(reply=reply):
                    self.assertEqual(self._edited(self.adapter, reply, FLAG_ON), self._sent(self.adapter, reply, FLAG_ON))
                    self.assertEqual(
                        self._edited(self.adapter, reply, {"KAGE_SLACK_UX": ""}),
                        self._edited(self.upstream_adapter, reply, {"KAGE_SLACK_UX": ""}),
                    )
        self.assertEqual(self._edited(self.adapter, REPORT, FLAG_ON), REPORT)

    def test_flag_off_is_upstream(self):
        for reply in self.replies:
            with self.subTest(reply=reply):
                self.assertEqual(
                    self._sent(self.adapter, reply, {"KAGE_SLACK_UX": ""}),
                    self._sent(self.upstream_adapter, reply, {"KAGE_SLACK_UX": ""}),
                )

    def test_flag_on_rewords_every_reply(self):
        with self.assertLogs("gateway.slack_boilerplate", "WARNING"):
            sent = {self._sent(self.adapter, reply, FLAG_ON) for reply in self.replies}
        self.assertEqual(
            sent,
            {
                "Got it — I'll pick this up when I finish the current one.",
                "Stopping what I was doing to look at this.",
                "Got it — I'll fold this into what I'm working on now.",
                "Got it — changing course with your correction.",
                "Got it — I'll pick this up when the current work finishes.",
                "I'm restarting — I'll pick this up when I'm back.",
                "I'm going offline for a moment — I'll pick this up when I'm back.",
                "I'm restarting — send that again in a minute.",
                "I'm going offline for a moment — send that again in a minute.",
                "I'm finishing some maintenance — try again in a minute.",
                "Still working on your last message — send this again once I've answered.",
                "Stopped.",
                "I'll restart as soon as the work in progress finishes.",
                "I'm already restarting — give me a minute.",
                "Restarting now. Send me a message in a minute or two.",
                runtime.STOPPED,
                "I'm in the middle of something — try `/model` again once I've answered.",
                "I'm in the middle of something — try `/codex-runtime` again once I've answered.",
                "I'm in the middle of something — try `/moa` again once I've answered.",
                "I'm in the middle of something — try `/refine` again once I've answered.",
                ("I'm in the middle of something — `/goal status`, `pause`, `clear` and `wait` work now; "
                 "set a new goal once I've answered."),
                ("I'm in the middle of something — `/loop status`, `pause` and `stop` work now; "
                 "start a new loop once I've answered."),
                "Got it — I'll pick this up next.",
                runtime.STEER_FAILED,
                "Still running `make build`.",
                "Still running `make build`.\n\nRecent output:\n```\nstep 3/9\n```",
                "Still running.",
                "Still running.\n\nRecent output:\n```\nstep 3/9\n```",
                "I can't reach the model right now (authentication failed).",
                "The model provider turned that request down. Try rephrasing it, or ask whoever runs me to check the logs.",
                "I'm being rate-limited. Give me a minute and try again.",
                "I can't reach the model right now. Try again in a minute.",
                "Something went wrong on my side. Try again?",
            },
        )

    def test_auth_failure_is_logged_not_sent(self):
        with self.assertLogs("gateway.slack_boilerplate", "WARNING") as logs:
            sent = self._sent(self.adapter, "⚠️ Provider authentication failed: 401 key sk-abc", FLAG_ON)
        self.assertEqual(sent, runtime.AUTH_FAILED)
        self.assertIn("401 key sk-abc", logs.output[0])

    def test_steer_failure_sends_no_exception_text(self):
        # Upstream logs the exception itself before replying.
        sent = self._sent(self.adapter, "⚠️ Steer failed: RuntimeError('queue closed')", FLAG_ON)
        self.assertEqual(sent, runtime.STEER_FAILED)

    def test_agent_text_opening_with_an_error_prefix_passes_through(self):
        for prefix in ("⚠️ Steer failed: ", "⚠️ Provider authentication failed: "):
            for text in (
                f"{prefix}the rollout.\n\nHere is what I found in the logs instead.",
                prefix + "x" * (runtime.ERROR_TEXT_MAX + 1),
            ):
                with self.subTest(prefix=prefix, length=len(text)):
                    self.assertEqual(self._sent(self.adapter, text, FLAG_ON), text)
        steer = "⏩ Steer queued — arrives after the next tool call: '"
        long_steer = f"{steer}{'y' * (runtime.STEER_PREVIEW_MAX + 1)}'"
        self.assertEqual(self._sent(self.adapter, long_steer, FLAG_ON), long_steer)
        preview = f"{steer}check\nthe ingress too'"
        self.assertNotEqual(self._sent(self.adapter, preview, FLAG_ON), preview)

    def test_command_specific_busy_refusals_name_the_command(self):
        for text, command in (
            ("Agent is running — wait or /stop first, then switch models.", "/model"),
            ("Agent is running — wait or /stop first, then change runtime.", "/codex-runtime"),
            ("Agent is running — wait or /stop first, then run /moa.", "/moa"),
            ("Agent is running — wait for the turn to finish, then /review.", "/review"),
        ):
            with self.subTest(text=text):
                sent = self._sent(self.adapter, text, FLAG_ON)
                self.assertEqual(sent, runtime.BUSY_RETRY.format(command=command))
                for word in verifier.SYSTEM_LEFTOVERS:
                    self.assertNotIn(word, sent)

    def test_restart_reply_drops_the_console_command_and_the_notice_promise(self):
        with mock.patch.dict(os.environ, FLAG_ON):
            text = runtime.system_text(verifier.check_locale(self.root.dir)[2])
        self.assertNotIn("console", text)
        self.assertNotIn("notified", text)

    def test_model_text_and_unknown_replies_pass_through(self):
        for text in (REPORT, "⏩ Steered into current run. Your message arrives sooner.",
                     "Stopped. The agent was still starting — session unlocked."):
            with self.subTest(text=text):
                self.assertEqual(self._sent(self.adapter, text, FLAG_ON), text)
        with mock.patch.dict(os.environ, FLAG_ON):
            self.assertIsNone(runtime.system_text(None))


class ErrorSiteTest(unittest.TestCase):
    """The steer and auth failure replies, built by the patched call sites themselves."""

    ERRORS = ("RuntimeError: queue closed\nTraceback (most recent call last):\n  sk-abc", "E" * 600)

    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        applier.apply(self.root.dir)
        self.busy = self.root.load(applier.RUN_BUSY, "patched")["GatewayBusyMixin"]
        self.turns = self.root.load(applier.RUN_TURN_RUNNER, "patched")["GatewayTurnRunnerMixin"]

    def _steer(self, platform, error, env):
        agent = SimpleNamespace(steer=mock.Mock(side_effect=RuntimeError(error)))
        event = SimpleNamespace(source=SimpleNamespace(platform=platform))
        with mock.patch.dict(os.environ, env):
            return _run(self.busy()._busy_steer_command(event, agent, "check the ingress"))

    def _auth(self, platform, error, env):
        runner = SimpleNamespace(_resolve_session_agent_runtime=mock.Mock(side_effect=RuntimeError(error)))
        ctx = SimpleNamespace(source=SimpleNamespace(platform=platform))
        with mock.patch.dict(os.environ, env):
            return self.turns()._run_agent(ctx, runner)["final_response"]

    def test_slack_gets_the_plain_reply_whatever_the_exception(self):
        for error in self.ERRORS:
            with self.subTest(length=len(error)):
                self.assertEqual(self._steer("slack", error, FLAG_ON), runtime.STEER_FAILED)
                with self.assertLogs("gateway.slack_boilerplate", "WARNING") as logs:
                    self.assertEqual(self._auth(SimpleNamespace(value="slack"), error, FLAG_ON), runtime.AUTH_FAILED)
                self.assertIn(error, logs.output[0])

    def test_flag_off_or_another_platform_keeps_upstream_reply(self):
        for platform, env in (("slack", {"KAGE_SLACK_UX": ""}), ("telegram", FLAG_ON)):
            for error in self.ERRORS:
                with self.subTest(platform=platform, length=len(error)):
                    self.assertEqual(self._steer(platform, error, env), f"⚠️ Steer failed: {error}")
                    self.assertEqual(
                        self._auth(platform, error, env), f"⚠️ Provider authentication failed: {error}")

    def test_verifier_refuses_an_unwrapped_site(self):
        for relative, anchor, patched in (
            (applier.RUN_BUSY, applier.STEER_FAILED_ANCHOR, applier.STEER_FAILED_PATCHED),
            (applier.RUN_TURN_RUNNER, applier.AUTH_FAILED_ANCHOR, applier.AUTH_FAILED_PATCHED),
        ):
            path = self.root.dir / relative
            good = path.read_text()
            with self.subTest(relative=relative):
                path.write_text(good.replace(patched, anchor))
                with self.assertRaises(SystemExit) as caught:
                    verifier.main(self.root.dir)
                self.assertIn("outside", str(caught.exception))
            path.write_text(good)
        verifier.main(self.root.dir)


class BusyHintTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        self.upstream = self.root.load(applier.RUN_BUSY, "upstream")["GatewayBusyMixin"]
        applier.apply(self.root.dir)
        self.patched = self.root.load(applier.RUN_BUSY, "patched")["GatewayBusyMixin"]

    def _ack(self, cls, platform, env):
        """The busy ack's text, and whether the hint was marked seen."""
        from agent import onboarding
        onboarding.SEEN.clear()
        event = SimpleNamespace(source=SimpleNamespace(platform=SimpleNamespace(value=platform)))
        with mock.patch.dict(os.environ, env):
            message = cls._compose_busy_ack_message(cls, event, ["3 min elapsed"], is_queue_mode=True)
        return message, list(onboarding.SEEN)

    def test_flag_off_and_other_platforms_are_upstream(self):
        for platform, env in (("slack", {"KAGE_SLACK_UX": ""}), ("telegram", FLAG_ON)):
            with self.subTest(platform=platform):
                upstream = self._ack(self.upstream, platform, env)
                self.assertEqual(self._ack(self.patched, platform, env), upstream)
                self.assertIn("Tip:", upstream[0])

    def test_flag_on_slack_drops_the_hint_and_leaves_it_unseen(self):
        self.assertEqual(
            self._ack(self.patched, "slack", FLAG_ON),
            ("⏳ Queued for the next turn (3 min elapsed). I'll respond once the current task finishes.", []),
        )


class SessionDbWarningTest(unittest.TestCase):
    def setUp(self):
        self.root = _Root()
        self.addCleanup(self.root.cleanup)
        self.upstream = self.root.load(applier.RUN_NOTIFICATIONS, "upstream")["GatewayNotificationsMixin"]
        applier.apply(self.root.dir)
        self.patched = self.root.load(applier.RUN_NOTIFICATIONS, "patched")["GatewayNotificationsMixin"]

    def _warn(self, cls, platforms, env):
        transport = _Transport()
        owner = SimpleNamespace(
            _session_db_init_error="database is locked",
            _home_channel_transports=lambda: [
                (SimpleNamespace(value=p), None, SimpleNamespace(chat_id=f"C-{p}"), transport) for p in platforms
            ],
            _send_home_channel_message=lambda *a: cls._send_home_channel_message(None, *a),
        )
        with mock.patch.dict(os.environ, env):
            _run(cls._send_session_db_warning_notifications(owner))
        return transport.sent

    def test_flag_off_is_upstream(self):
        env = {"KAGE_SLACK_UX": ""}
        sent = self._warn(self.patched, ["slack", "telegram"], env)
        self.assertEqual(sent, self._warn(self.upstream, ["slack", "telegram"], env))
        self.assertEqual(len(sent), 2)

    def test_flag_on_skips_slack_and_still_warns_elsewhere(self):
        sent = self._warn(self.patched, ["slack", "telegram"], FLAG_ON)
        self.assertEqual(sent, self._warn(self.upstream, ["telegram"], FLAG_ON))
        self.assertEqual([p for p, _ in sent], ["telegram"])


class RuntimeTest(unittest.TestCase):
    def test_platform_enum_and_string_alike(self):
        with mock.patch.dict(os.environ, FLAG_ON):
            slack = SimpleNamespace(platform=SimpleNamespace(value="slack"))
            self.assertEqual(runtime.long_running_mode(slack, "raw"), "generic")
            self.assertEqual(runtime.long_running_mode(slack, "generic"), "generic")
            self.assertEqual(runtime.long_running_mode(slack, "off"), "off")
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
