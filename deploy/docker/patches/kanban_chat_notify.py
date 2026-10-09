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
3. :func:`fresh_events` drops, from a claim, events that were already older
   than :data:`STALE_EVENT_SECONDS` when routed delivery first went live, so
   the first rollout does not replay the backlog every skipped subscription
   accumulated; the cursor still advances past them. The moment is kept under
   ``$HERMES_HOME`` (the agent's volume), so this is a one-time skip: an
   outage after the rollout delays events, and drops none.

Two kinds of subscription reach it. An alert's triage card names a thread of
the home channel, which is forwarded as ``--thread``; the gateway posts only
into threads of the home channel, so a thread of another space is refused (and
the notifier drops that subscription after its consecutive-failure limit, as
for any destination that cannot be reached). A card filed in a gateway
conversation (the hermes-bridge's ``a2a-*`` session) names that conversation's
key and context id, recorded by the bridge and swapped in by
``kanban_event_routing``; it is forwarded as ``--conversation`` and
``--context``, and the gateway posts it only into the live conversation whose
session record carries that context. A subscription with no thread (a DM,
or a space as a whole) is not delivered at all: posting it as a new thread in
the home channel would move text meant for one space into another.

When the route is not there (the gateway restarting, its route unarmed, the
bus unreachable: ``a2a notify`` exit 4), :func:`resolve` answers no adapter,
which is upstream's disconnected-adapter path: skipped without a claim and
without spending the failure budget. It learns this from a probe (an empty
notify, which an armed gateway refuses at once) that the collector's pre-claim
authorization runs on its worker thread, and from any send that meets exit
4. An up answer is trusted for :data:`ROUTE_PROBE_TTL_SECONDS`; a down one
holds the route down for :data:`ROUTE_DOWN_BACKOFF_SECONDS` and is probed
again after it. So a send that meets an outage inside that window spends one
unit, and marks the route down for the rest. Without this a gateway roll
would unsubscribe every card with a pending event.

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
import math
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

# The operator's switch (written by platformagent_manifests.go, defined as
# a2aNotifyPlatformEnvVar in platformagent_a2a_manifests.go).
NOTIFY_PLATFORM_ENV = "A2A_NOTIFY_PLATFORM"
# The platform whose gateway conversations a card can report back to
# (a2aNotifyConversationsEnvVar). Rendered whenever the gateway arms the route,
# home channel or not; A2A_NOTIFY_PLATFORM above is home posts, and needs one.
NOTIFY_CONVERSATIONS_ENV = "A2A_NOTIFY_CONVERSATIONS"
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
# Events already older than this when routed delivery first went live are
# advanced past without posting: they are the backlog of a subscription nobody
# could deliver, and a completion from days ago posted now is noise, a stale
# failure wake worse.
STALE_EVENT_SECONDS = 6 * 3600
# Where the moment routed delivery first went live is kept: a file under
# $HERMES_HOME, which is on the agent's volume, so a pod restart does not move
# it and the stale skip happens once per install.
HERMES_HOME_ENV = "HERMES_HOME"
ROUTED_SINCE_FILE = "kanban_chat_notify.routed_since"
# The sibling the record is written to first, then renamed over it.
ROUTED_SINCE_TMP_SUFFIX = ".tmp"
# How long an up answer from the route probe is trusted, and how long a probe
# may take. The probe is an empty notify: an armed gateway refuses it at once
# ("text is empty"), and no responders (exit 4) means the route is not there.
# The collector authorizes every routed subscription on every tick, work or
# not, so this bounds the probes an idle install makes.
ROUTE_PROBE_TTL_SECONDS = 300
ROUTE_PROBE_TIMEOUT_SECONDS = 5
# The gateway's conversation-key prefixes (gchatConversationID,
# slackConversationID). A subscription whose thread is one of these was routed
# to a gateway conversation by kanban_event_routing (the hermes-bridge records
# the route), and its chat_id is the session that holds the route, whose
# context id conversation_context reads back: the report is sent with
# --conversation and --context rather than --thread.
CONVERSATION_KEY_PREFIXES = ("gchat:", "slack:")
# The attribute the stand-in is cached under on the runner, which outlives the
# per-tick collector and the per-delivery notification.
RUNNER_ATTR = "_kage_chat_notify_adapter"
RUNNER_CONVERSATION_ATTR = "_kage_chat_notify_conversation_adapter"
# Where a gateway conversation's route is read back: session-kv's routing
# table, the same database and key kanban_event_routing reads
# (session_kv_server.CONVERSATION_ROUTE_KEY).
SESSION_KV_DB_ENV = "SESSION_KV_DB_PATH"
SESSION_KV_DEFAULT_DB = "/var/lib/kube-agents/session/session_kv.db"
CONVERSATION_ROUTE_KEY = "conversation_route"
SESSION_KV_TIMEOUT_SECONDS = 2.0
# The in-pod API server a conversation card's wake self-posts into, as the
# hermes-bridge's api executor posts its turns (a2a/hermes-bridge/api.go,
# DefaultAPIURL and DefaultAPIModel), with the pod's key.
API_SERVER_HOST = "127.0.0.1"
API_SERVER_PORT = 8642
API_SERVER_MODEL = "model-default"
API_SERVER_KEY_ENV = "API_SERVER_KEY"

# routed_since's answer, read or written once per process.
_routed_since: Optional[float] = None


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def routed_platform() -> str:
    """The platform name the gateway holds, or "" when none."""
    return os.environ.get(NOTIFY_PLATFORM_ENV, "").strip()


def routes(platform: Any) -> bool:
    """Whether ``platform`` (an enum or a string) is the one the gateway holds."""
    name = str(getattr(platform, "value", platform) or "").lower()
    return bool(name) and name == routed_platform()


def conversation_platform() -> str:
    """The platform whose gateway conversations cards report back to, or "" when none."""
    return os.environ.get(NOTIFY_CONVERSATIONS_ENV, "").strip()


def is_conversation(sub: Optional[dict]) -> bool:
    """Whether ``sub`` is addressed to a gateway conversation (its thread is a conversation key)."""
    return str((sub or {}).get("thread_id") or "").strip().startswith(CONVERSATION_KEY_PREFIXES)


def active_platforms(names: set) -> set:
    """``names`` plus the routed platforms, when there are any."""
    return names | {p for p in (routed_platform(), conversation_platform()) if p}


def resolve(runner: Any, platform: Any, adapter: Any, sub: Optional[dict] = None) -> Any:
    """The adapter to deliver ``sub`` with: ``adapter`` when there is one, else a stand-in, or None.

    A subscription addressed to a gateway conversation gets the conversation
    stand-in, armed by A2A_NOTIFY_CONVERSATIONS; any other gets the home stand-in,
    armed by A2A_NOTIFY_PLATFORM.
    """
    if adapter is not None:
        return adapter
    conversation = is_conversation(sub)
    name = str(getattr(platform, "value", platform) or "").lower()
    if conversation:
        if not name or name != conversation_platform():
            return None
    elif not routes(platform):
        return adapter
    if getattr(getattr(runner, "config", None), "multiplex_profiles", False):
        return None
    if sub is not None and not str(sub.get("thread_id") or "").strip():
        return None
    attr, cls = ((RUNNER_CONVERSATION_ATTR, ConversationNotifyAdapter) if conversation
                 else (RUNNER_ATTR, ChatNotifyAdapter))
    stand_in = getattr(runner, attr, None)
    if stand_in is None or stand_in.platform != platform:
        stand_in = cls(platform, runner)
        setattr(runner, attr, stand_in)
    if not stand_in.route_up():
        return None
    return stand_in


def _write_routed_since(path: str, value: float) -> None:
    """Write the go-live record whole or not at all: a sibling file, then a rename over it."""
    tmp = path + ROUTED_SINCE_TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(f"{value}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def routed_since(now: float) -> float:
    """When routed delivery first went live on this install: read, or recorded as ``now``.

    Kept under $HERMES_HOME so it survives a restart. When it cannot be kept,
    this process's first call stands in, and the log says a restart moves it.
    """
    global _routed_since
    if _routed_since is not None:
        return _routed_since
    home = os.environ.get(HERMES_HOME_ENV, "").strip()
    path = os.path.join(home, ROUTED_SINCE_FILE) if home else ""
    value = None
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                value = float(handle.read().strip())
            if not math.isfinite(value) or value > now:
                raise ValueError(f"{value} is not a past time")
        except FileNotFoundError:
            value = None
        except (OSError, ValueError) as exc:
            # The record exists but cannot be read back: a write cut short, or
            # a value no clock produces. It was written once, when routing went
            # live, so its mtime is that moment; recording now instead would
            # move the cutoff forward and drop events that are not stale.
            try:
                value = min(os.path.getmtime(path), now)
            except OSError:
                value = None
            logger.warning("kanban notifier: %s unreadable (%s); using %s", path, exc,
                           "its mtime" if value is not None else "now")
            if value is not None:
                try:
                    _write_routed_since(path, value)
                except OSError as write_exc:
                    # A full or read-only volume, the likeliest cause of the
                    # short write: the mtime still stands for this process.
                    logger.warning("kanban notifier: cannot rewrite %s (%s); "
                                   "a restart will read its mtime again", path, write_exc)
    if value is None:
        value = now
        try:
            if not path:
                raise OSError(f"{HERMES_HOME_ENV} is not set")
            _write_routed_since(path, value)
        except OSError as exc:
            logger.warning("kanban notifier: cannot record when routed delivery went live (%s); "
                           "a restart will skip stale events again", exc)
    _routed_since = value
    return value


def fresh_events(claim: Optional[dict], now: Optional[float] = None) -> Optional[dict]:
    """``claim`` minus events already STALE_EVENT_SECONDS old when routing went live, for the routed platform only."""
    if not claim or not routes((claim.get("sub") or {}).get("platform")):
        return claim
    cutoff = routed_since(time.time() if now is None else now) - STALE_EVENT_SECONDS
    events = claim.get("events") or []
    kept = [ev for ev in events if (getattr(ev, "created_at", 0) or 0) >= cutoff]
    if len(kept) != len(events):
        logger.info("kanban notifier: skipping %d event(s) from before routed delivery went live, "
                    "older than %ds then, for %s on %s (cursor still advances)",
                    len(events) - len(kept), STALE_EVENT_SECONDS,
                    claim["sub"].get("task_id"), claim["sub"].get("platform"))
        claim = dict(claim, events=kept)
    return claim


#: How long a fanned-out child's answer waits for its parent to settle before
#: it is posted anyway: the parent's answer is the one the thread wants, but a
#: parent that never settles must not swallow what its child found.
FOLD_HOLD_SECONDS = 30 * 60
#: Parent statuses that mean it is still working toward its answer: the
#: child's answer waits. A blocked parent (failed, gave up, or waiting on the
#: user) releases it at once.
FOLD_WAITING_STATUSES = frozenset({"triage", "todo", "scheduled", "ready", "running", "review"})
#: kanban_children_settled's record of the card each worker's card was
#: created by.
WORKER_CHILDREN_TABLE = "kanban_worker_children"


def _fold_parent(conn: Any, child_id: str, sub: dict) -> Optional[tuple]:
    """``(status, completed_at)`` of the card that created ``child_id``, when
    that card is subscribed to the same thread; else None.

    A child its parent gates (a ``task_links`` edge from the parent, the
    continuation kanban_children_settled exempts) is never folded: it runs
    after its parent completes, so its answer is the answer.
    """
    try:
        row = conn.execute(
            f"SELECT c.creator_id FROM {WORKER_CHILDREN_TABLE} c WHERE c.child_id = ?"
            " AND NOT EXISTS (SELECT 1 FROM task_links l WHERE l.parent_id = c.creator_id AND l.child_id = c.child_id)",
            (child_id,),
        ).fetchone()
        if not row:
            return None
        parent = row[0]
        same_thread = conn.execute(
            "SELECT 1 FROM kanban_notify_subs WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (parent, sub.get("platform") or "", sub.get("chat_id") or "", sub.get("thread_id") or ""),
        ).fetchone()
        if not same_thread:
            return None
        task = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (parent,)).fetchone()
        return (task[0], task[1]) if task else None
    except sqlite3.Error as exc:
        # No children table on an install without kanban_children_settled,
        # or a schema change: deliver as upstream would.
        logger.debug("kanban notifier: fan-out fold skipped for %s: %s", child_id, exc)
        return None


def _rewind(conn: Any, sub: dict, claimed: int, to: int) -> bool:
    """Move the subscription's cursor back from ``claimed`` to ``to`` (CAS)."""
    from hermes_cli import kanban_db_notify
    return kanban_db_notify.rewind_notify_cursor(
        conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
        thread_id=sub.get("thread_id") or "", claimed_cursor=claimed, old_cursor=to,
    )


def fold_fanout(conn: Any, claim: Optional[dict], now: Optional[float] = None) -> Optional[dict]:
    """``claim`` with a fanned-out child's answer folded into its parent's.

    A worker that fans a question out to child cards completes its own card
    with the synthesis, and kanban_children_settled holds that completion until
    the children settle. Each child inherited the thread's subscription
    (kanban_auto_subscribe), so without this the thread gets every child's
    answer and then the parent's: the same answer, two or more times. Routed
    delivery only (the A2A gateway's platforms); the Hermes path and its own
    Slack fold are untouched.

    For a child's ``completed`` event whose parent is subscribed to the same
    thread:

    - parent done (or archived) after the child completed: the event is
      dropped, cursor advanced, so neither its post nor its wake runs;
    - parent still working, for under FOLD_HOLD_SECONDS: the event is held.
      Events before it deliver, and the cursor is rewound to just before it,
      so the next tick claims it again;
    - otherwise (the parent blocked, failed or gave up, finished before the
      child, or the hold ran out): it delivers as upstream would, so nothing
      a child found is lost when its parent's answer does not come.

    Every other event kind (blocked, gave_up, crashed, progress) delivers.
    """
    if not claim:
        return claim
    sub = claim.get("sub") or {}
    if (sub.get("platform") or "").lower() not in {p for p in (routed_platform(), conversation_platform()) if p}:
        return claim
    events = list(claim.get("events") or [])
    now = time.time() if now is None else now
    kept: list = []
    for index, ev in enumerate(events):
        if getattr(ev, "kind", "") != "completed":
            kept.append(ev)
            continue
        parent = _fold_parent(conn, sub.get("task_id") or "", sub)
        if parent is None:
            kept.append(ev)
            continue
        status, completed_at = parent
        created = getattr(ev, "created_at", 0) or 0
        if status in ("done", "archived") and (completed_at or 0) >= created:
            logger.info("kanban notifier: %s's answer folded into its parent's (parent done); not posted",
                        sub.get("task_id"))
            continue
        if status in FOLD_WAITING_STATUSES and now - created < FOLD_HOLD_SECONDS:
            hold_from = int(getattr(ev, "id"))
            try:
                rewound = _rewind(conn, sub, int(claim["cursor"]), hold_from - 1)
            except Exception as exc:  # a failed hold must not lose the event
                logger.warning("kanban notifier: could not hold %s's answer for its parent: %s", sub.get("task_id"), exc)
                kept.extend(events[index:])
                break
            if not rewound:
                kept.extend(events[index:])
                break
            logger.info("kanban notifier: holding %s's answer until its parent settles (parent %s)",
                        sub.get("task_id"), status)
            if not kept:
                return None
            return dict(claim, events=kept, cursor=hold_from - 1)
        kept.append(ev)
    if len(kept) == len(events):
        return claim
    return dict(claim, events=kept) if kept else None


class ChatNotifyAdapter(BasePlatformAdapter):
    """Send-only adapter for a platform the A2A gateway holds; posts via ``a2a notify``."""

    def __init__(self, platform: Platform, runner: Any) -> None:
        super().__init__(PlatformConfig(enabled=True), platform)
        self._route_down_until = 0.0
        self._probed_at = float("-inf")
        self._route_ok = True
        handler_factory = getattr(runner, "_primary_message_handler", None)
        if callable(handler_factory):
            self.set_message_handler(handler_factory())

    def notify_target(self, chat_id: str, thread: str) -> list:
        """The argv that addresses the post: a home-channel thread, or none for a new one."""
        return ["--thread", thread] if thread else []

    def route_down(self) -> bool:
        return time.monotonic() < self._route_down_until

    def route_up(self) -> bool:
        """Whether a delivery should be attempted now.

        False while a down answer (a probe's, or a send's exit 4) holds the
        route down. Otherwise the answer of a probe, run only off the event
        loop: the collector authorizes each subscription on a worker thread
        before it claims, so a route seen down there is skipped, unclaimed and
        uncounted. An up answer is trusted for ROUTE_PROBE_TTL_SECONDS; a down
        one is probed again once its backoff ends. Delivery runs on the loop
        and reads the last answer.
        """
        if self.route_down():
            return False
        now = time.monotonic()
        if now - self._probed_at >= ROUTE_PROBE_TTL_SECONDS and not _on_event_loop():
            self._probed_at = now
            self._route_ok = self._probe()
            if not self._route_ok:
                self.mark_route_down()
        return self._route_ok

    def mark_route_down(self) -> None:
        """Hold deliveries for the backoff, then probe again rather than trust an old up answer."""
        self._route_down_until = time.monotonic() + ROUTE_DOWN_BACKOFF_SECONDS
        self._route_ok = False
        self._probed_at = float("-inf")

    def _probe(self) -> bool:
        try:
            done = subprocess.run(
                [A2A_CLI, "notify", "--platform", self.platform.value,
                 "--timeout", f"{ROUTE_PROBE_TIMEOUT_SECONDS}s", "--", ""],
                stdin=subprocess.DEVNULL, capture_output=True,
                timeout=ROUTE_PROBE_TIMEOUT_SECONDS + NOTIFY_CONNECT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - a probe that cannot run says nothing about the route
            logger.warning("chat.notify: route probe could not run: %s", exc)
            return True
        if done.returncode == NOTIFY_ROUTE_UNAVAILABLE:
            logger.warning("chat.notify: route unavailable; holding deliveries for %ds", ROUTE_DOWN_BACKOFF_SECONDS)
            return False
        return True

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
        try:
            argv += self.notify_target(str(chat_id or "").strip(), thread)
        except LookupError as exc:
            logger.warning("chat.notify: %s; the card's report is not posted", exc)
            return SendResult(success=False, error=str(exc))
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
            self.mark_route_down()
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


def conversation_context(session_id: str) -> str:
    """The context id of the gateway conversation ``session_id`` holds the route for, or ""."""
    path = os.environ.get(SESSION_KV_DB_ENV) or SESSION_KV_DEFAULT_DB
    if not session_id or not os.path.exists(path):
        return ""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=SESSION_KV_TIMEOUT_SECONDS)) as conn:
            row = conn.execute("SELECT metadata FROM session_metadata WHERE session_id = ?", (session_id,)).fetchone()
        route = (json.loads(row[0]) if row and row[0] else {}).get(CONVERSATION_ROUTE_KEY)
    except Exception as exc:  # noqa: BLE001 - an unreadable route is a failed send, said in the log
        logger.warning("chat.notify: route for %s unreadable: %s", session_id, exc)
        return ""
    return str(route.get("context_id") or "") if isinstance(route, dict) else ""


class ConversationNotifyAdapter(ChatNotifyAdapter):
    """The stand-in for a card filed in a gateway conversation.

    The subscription is (platform, the bridge session holding the route, the
    conversation key). Its report goes out with ``--conversation`` and the
    route's context id. It stays push-capable, because the notifier skips a
    non-push adapter's text pings in notify+wake and the report would never be
    posted. A wake for the card is not run as a turn in a new session that
    posts into the conversation: ``handle_message`` self-posts it into the
    bridge's own session (the source's chat_id) through the in-pod API server,
    where the conversation's history is, and its reply goes nowhere, as a wake
    for the bridge's session did before.
    """

    def __init__(self, platform: Platform, runner: Any) -> None:
        super().__init__(platform, runner)
        self._host, self._port, self._model_name = API_SERVER_HOST, API_SERVER_PORT, API_SERVER_MODEL
        self._api_key = os.environ.get(API_SERVER_KEY_ENV, "")

    def notify_target(self, chat_id: str, thread: str) -> list:
        context = conversation_context(chat_id)
        if not context:
            raise LookupError(f"no conversation route recorded for {chat_id}")
        return ["--conversation", thread, "--context", context]

    async def handle_message(self, event: Any) -> None:
        if not getattr(event, "internal", False):
            # Nothing inbound reaches a send-only stand-in; say so if it does.
            logger.warning("chat.notify: conversation stand-in got a non-internal event; ignored")
            return
        from gateway.wake import _self_post_chat_completion

        session = str(getattr(getattr(event, "source", None), "chat_id", "") or "")
        await _self_post_chat_completion(self, text=event.text, session_id=session)
        event._gateway_accepted = True
