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
platform, and only inside the notifier:

1. :func:`active_platforms` counts it as served, so its subscriptions are
   collected.
2. :func:`resolve` hands delivery a :class:`ChatNotifyAdapter` when no live
   adapter answers for a subscription that names a thread. The stand-in posts
   with ``a2a notify``, which asks the gateway to post into that thread over its
   chat.notify route.
3. :func:`fresh_events` drops events older than :data:`STALE_EVENT_SECONDS`
   from a claim, so the first rollout does not replay the backlog every
   skipped subscription accumulated; the cursor still advances past them.

Only the subscription's thread is forwarded, and the gateway posts only into
threads of the home channel, so a thread of another space is refused (and the
notifier drops that subscription after its consecutive-failure limit, as for
any destination that cannot be reached). A subscription with no thread (a DM,
or a space as a whole) is not delivered at all: posting it as a new thread in
the home channel would move text meant for one space into another.

When the route is not there (the gateway restarting, its route unarmed, the
bus unreachable: ``a2a notify`` exit 4) the stand-in marks it down for
:data:`ROUTE_DOWN_BACKOFF_SECONDS` and :func:`resolve` answers no adapter
meanwhile, which is upstream's disconnected-adapter path: skipped without a
claim and without spending the failure budget. Without the backoff a gateway
roll would unsubscribe every card with a pending event.

The stand-in is never registered in ``runner.adapters``, so nothing else in the
gateway believes the platform is connected, and it is not offered under
``multiplex_profiles``, where upstream's ``None`` can be a deliberate refusal
rather than an absent adapter. It is a full ``BasePlatformAdapter`` so the
notifier's failure wakes re-enter the creator's thread through
``handle_message``; the runner cannot resolve this adapter mid-turn, so typing,
streaming and tool progress are skipped and only the turn's final reply comes
back, through ``send``. ``edit_message`` is left at the base default
(unsupported), which tells the rolling progress line to post each note anew;
attachments are not posted (there is no file route), and say so in the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

# The operator's switch (written by platformagent_manifests.go, defined as
# a2aNotifyPlatformEnvVar in platformagent_a2a_manifests.go).
NOTIFY_PLATFORM_ENV = "A2A_NOTIFY_PLATFORM"
# The bus CLI in the agent image, on PATH.
A2A_CLI = "a2a"
# `a2a notify` exit statuses (a2a/cmd/a2a/notify.go): the gateway took the
# request and did not answer (the post may still land, so it is not re-sent),
# and the route is not there right now (nothing posted).
NOTIFY_OUTCOME_UNKNOWN = 3
NOTIFY_ROUTE_UNAVAILABLE = 4
# How long `a2a notify` waits for the gateway's answer, passed explicitly so
# the send timeout below can be derived from it.
NOTIFY_WAIT_SECONDS = 60
# The CLI's own bound on connecting (cliTimeout in a2a/cmd/a2a/main.go), and a
# margin for process start and exit.
NOTIFY_CONNECT_SECONDS = 30
NOTIFY_MARGIN_SECONDS = 15
SEND_TIMEOUT_SECONDS = NOTIFY_WAIT_SECONDS + NOTIFY_CONNECT_SECONDS + NOTIFY_MARGIN_SECONDS
# How long a route that answered exit 4 is treated as down. A gateway roll
# (Recreate) is tens of seconds; this keeps the notifier from spending a
# subscription's failure budget on it.
ROUTE_DOWN_BACKOFF_SECONDS = 60
# Events older than this are advanced past without posting: on the first
# rollout they are the backlog of a subscription nobody could deliver, and a
# completion from days ago posted now is noise, a stale failure wake worse.
STALE_EVENT_SECONDS = 6 * 3600
# The attribute the stand-in is cached under on the runner, which outlives the
# per-tick collector and the per-delivery notification.
RUNNER_ATTR = "_kage_chat_notify_adapter"


def routed_platform() -> str:
    """The platform name the gateway holds, or "" when none."""
    return os.environ.get(NOTIFY_PLATFORM_ENV, "").strip()


def routes(platform: Any) -> bool:
    """Whether ``platform`` (an enum or a string) is the one the gateway holds."""
    name = str(getattr(platform, "value", platform) or "").lower()
    return bool(name) and name == routed_platform()


def active_platforms(names: set) -> set:
    """``names`` plus the routed platform, when there is one."""
    routed = routed_platform()
    return names | {routed} if routed else names


def resolve(runner: Any, platform: Any, adapter: Any, sub: Optional[dict] = None) -> Any:
    """The adapter to deliver ``sub`` with: ``adapter`` when there is one, else the stand-in, or None."""
    if adapter is not None or not routes(platform):
        return adapter
    if getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        return None
    if sub is not None and not str(sub.get("thread_id") or "").strip():
        return None
    stand_in = getattr(runner, RUNNER_ATTR, None)
    if stand_in is None or stand_in.platform != platform:
        stand_in = ChatNotifyAdapter(platform, runner)
        setattr(runner, RUNNER_ATTR, stand_in)
    if stand_in.route_down():
        return None
    return stand_in


def fresh_events(claim: Optional[dict], now: Optional[float] = None) -> Optional[dict]:
    """``claim`` with events older than STALE_EVENT_SECONDS removed, for the routed platform only."""
    if not claim or not routes((claim.get("sub") or {}).get("platform")):
        return claim
    cutoff = (time.time() if now is None else now) - STALE_EVENT_SECONDS
    events = claim.get("events") or []
    kept = [ev for ev in events if (getattr(ev, "created_at", 0) or 0) >= cutoff]
    if len(kept) != len(events):
        logger.info("kanban notifier: skipping %d event(s) older than %ds for %s on %s (cursor still advances)",
                    len(events) - len(kept), STALE_EVENT_SECONDS,
                    claim["sub"].get("task_id"), claim["sub"].get("platform"))
        claim = dict(claim, events=kept)
    return claim


class ChatNotifyAdapter(BasePlatformAdapter):
    """Send-only adapter for a platform the A2A gateway holds; posts via ``a2a notify``."""

    def __init__(self, platform: Platform, runner: Any) -> None:
        super().__init__(PlatformConfig(enabled=True), platform)
        self._route_down_until = 0.0
        handler_factory = getattr(runner, "_primary_message_handler", None)
        if callable(handler_factory):
            self.set_message_handler(handler_factory())

    def route_down(self) -> bool:
        return time.monotonic() < self._route_down_until

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group"}

    async def _send_media_fallback_notice(self, method: str, kind: str, path: str, chat_id: str,
                                          caption: Optional[str], reply_to: Optional[str],
                                          metadata: Optional[Dict[str, Any]], *,
                                          file_name: Optional[str] = None) -> SendResult:
        # There is no file route over chat.notify. The base posts a "couldn't
        # deliver the attachment" line per file; here that would be one post
        # per artifact on every completed card, so it is logged instead.
        logger.info("chat.notify: %s not posted (no file route): %s", kind, file_name or "attachment")
        return SendResult(success=False, error="attachments are not posted over chat.notify")

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        thread = str((metadata or {}).get("thread_id") or "").strip()
        argv = [A2A_CLI, "notify", "--platform", self.platform.value, "--timeout", f"{NOTIFY_WAIT_SECONDS}s"]
        if thread:
            argv += ["--thread", thread]
        argv += ["--", content]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=SEND_TIMEOUT_SECONDS)
        except BaseException as exc:
            # A timed-out or cancelled child must not live on and post after
            # the notifier has counted the send as failed and retried it.
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if not isinstance(exc, Exception):
                raise
            return SendResult(success=False, error=f"a2a notify: {exc}")
        if proc.returncode == NOTIFY_OUTCOME_UNKNOWN:
            logger.warning("chat.notify: no answer in time for %s; treating as sent", chat_id)
            return SendResult(success=True)
        if proc.returncode == NOTIFY_ROUTE_UNAVAILABLE:
            self._route_down_until = time.monotonic() + ROUTE_DOWN_BACKOFF_SECONDS
            logger.warning("chat.notify: route unavailable; holding deliveries for %ds", ROUTE_DOWN_BACKOFF_SECONDS)
        if proc.returncode != 0:
            return SendResult(success=False, error=(err.decode(errors="replace").strip() or f"exit {proc.returncode}"))
        try:
            answer = json.loads(out.decode(errors="replace") or "{}")
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        return SendResult(success=True, message_id=answer.get("message_id") or None, raw_response=answer)
