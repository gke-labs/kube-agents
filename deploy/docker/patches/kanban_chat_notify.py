"""Deliver kanban card events to a chat platform the A2A gateway holds, through chat.notify.

Installed into the image at ``/opt/hermes/gateway/kanban_chat_notify.py`` and
wired into ``gateway/kanban_watchers_notifier.py`` by
``deploy/docker/patches/apply_kanban_chat_notify.py``.

The gap
-------
Under ``spec.mode: next`` the operator does not render the Hermes Google Chat
platform: the A2A gateway consumes the Chat backend. A card an alert's triage
creates still subscribes to the alert's Google Chat thread
(``kanban_event_routing`` resolves the api_server session to it), and the
notifier only collects subscriptions whose platform has a connected adapter
(``_Collector.active_platforms``). So every such subscription is skipped,
silently, on every tick: no claim, no failure count, no unsubscribe, and the
card's report never reaches the thread.

The fix
-------
The operator names the platform the gateway holds in ``A2A_NOTIFY_PLATFORM``
(the same switch ``agents/platform/scripts/chat_notify.py`` reads). For that
platform, and only inside the notifier, two things change:

1. :func:`active_platforms` counts it as served, so its subscriptions are
   collected.
2. :func:`resolve` hands delivery a :class:`ChatNotifyAdapter` when no live
   adapter answers for it. The stand-in posts with ``a2a notify``, which asks
   the gateway to post to the home channel over its chat.notify route.

The stand-in is never registered in ``runner.adapters``, so nothing else in the
gateway believes the platform is connected. It is a full
``BasePlatformAdapter`` on purpose: the notifier's failure wakes re-enter the
creator's thread through ``handle_message`` as on a real adapter, and the
turn's replies come back through this adapter's ``send``. ``edit_message`` is
left at the base default (unsupported), which tells the rolling progress line
to post anew instead of editing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

# The operator's switch (platformagent_manifests.go, a2aNotifyPlatformEnvVar).
NOTIFY_PLATFORM_ENV = "A2A_NOTIFY_PLATFORM"
# The bus CLI in the agent image, on PATH.
A2A_CLI = "a2a"
# The exit status `a2a notify` uses when the gateway took the request and did
# not answer in time: the post may still land, so it is not re-sent.
NOTIFY_OUTCOME_UNKNOWN = 3
# Bounds one send end to end. `a2a notify` waits up to 60s for the gateway.
SEND_TIMEOUT_SECONDS = 90.0
# The attribute the stand-in is cached under on the runner, which outlives the
# per-tick collector and the per-delivery notification.
RUNNER_ATTR = "_kage_chat_notify_adapter"


def routed_platform() -> str:
    """The platform name the gateway holds, or "" when none."""
    return os.environ.get(NOTIFY_PLATFORM_ENV, "").strip()


def active_platforms(names: set) -> set:
    """``names`` plus the routed platform, when there is one."""
    routed = routed_platform()
    return names | {routed} if routed else names


def resolve(runner: Any, platform: Any, adapter: Any) -> Any:
    """The adapter to deliver with: ``adapter`` when there is one, else the stand-in for the routed platform."""
    if adapter is not None:
        return adapter
    name = getattr(platform, "value", str(platform)).lower()
    if not name or name != routed_platform():
        return None
    stand_in = getattr(runner, RUNNER_ATTR, None)
    if stand_in is None or stand_in.platform != platform:
        stand_in = ChatNotifyAdapter(platform, runner)
        setattr(runner, RUNNER_ATTR, stand_in)
    return stand_in


class ChatNotifyAdapter(BasePlatformAdapter):
    """Send-only adapter for a platform the A2A gateway holds; posts via ``a2a notify``."""

    def __init__(self, platform: Platform, runner: Any) -> None:
        super().__init__(PlatformConfig(enabled=True), platform)
        handler_factory = getattr(runner, "_primary_message_handler", None)
        if callable(handler_factory):
            self.set_message_handler(handler_factory())

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group"}

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        thread = str((metadata or {}).get("thread_id") or "").strip()
        argv = [A2A_CLI, "notify", "--platform", self.platform.value]
        if thread:
            argv += ["--thread", thread]
        argv += ["--", content]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=SEND_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - every failure is a failed send, reported to the caller
            return SendResult(success=False, error=f"a2a notify: {exc}")
        if proc.returncode == NOTIFY_OUTCOME_UNKNOWN:
            # Possibly posted: reporting failure would make the notifier send
            # it again, which is the worse of the two outcomes.
            logger.warning("chat.notify: no answer in time for %s; treating as sent", chat_id)
            return SendResult(success=True)
        if proc.returncode != 0:
            return SendResult(success=False, error=(err.decode(errors="replace").strip() or f"exit {proc.returncode}"))
        try:
            answer = json.loads(out.decode(errors="replace") or "{}")
        except ValueError:
            answer = {}
        return SendResult(success=True, message_id=answer.get("message_id") or None, raw_response=answer)
