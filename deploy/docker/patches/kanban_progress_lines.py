"""Mid-run progress lines for the kanban notifier.

Installed into the image at ``/opt/hermes/gateway/kanban_progress_lines.py``
and wired into ``gateway/kanban_watchers.py`` by
``deploy/docker/patches/apply_kanban_progress_lines.py``.

**The problem.** A delegated card is silent from the moment it is claimed until
the moment it completes. Measured on the ``kage-management`` board over 298
runs, that silence is a p50 of 264 seconds and a p90 of 1102 seconds. The
plumbing around it costs about a minute; the silence is what makes delegation
feel slower than doing the work in the chat thread. The workaround the personas
used to prescribe — split the work into child cards so each completion posts a
line — buys visibility by paying a fresh dispatch tick, a fresh 17–19 second
worker cold start, and a fresh worker context per stage. It makes the real
number worse to improve the perceived one.

**The unlock.** ``kanban_heartbeat(note=...)`` already writes a ``heartbeat``
event carrying that note (``hermes_cli/kanban_db.py``); the notifier simply
does not deliver that kind. Two properties make delivering it nearly free:

1. The per-tool-call auto-heartbeats fired by ``tools/kanban_tools.py`` write
   ``payload=None``. All 2,107 heartbeat rows on the live board are noteless,
   so "has a note" separates a deliberate progress update from a liveness ping
   exactly — no new event kind and no schema change.
2. ``heartbeat`` is absent from ``_WAKE_KINDS`` in the notifier, so a progress
   line posts straight into the chat thread without waking the creator's agent.
   It costs zero LLM turns, which is why a worker can afford to send several.

``progress_note`` is the whole filter: it returns the note a human should see,
or ``""`` for everything else. The empty return is what keeps the auto-
heartbeats silent, so it is the single most important behaviour here.

Length is capped through ``clip_handoff`` rather than a hard slice, for the
same reason the completion handoff is: a note that ends in a link must not have
that link severed into a dead one. See ``kanban_handoff_clip.py``.

One message per card, not one per note
--------------------------------------
Delivering each note as its own chat message solved the silence and created a
second problem: a five-milestone card is five messages, and Google Chat pings
every member of the space for each of them. Progress is worth *showing* and not
worth *interrupting* for — the completion is the interruption people want.

So :func:`deliver` keeps **one rolling message per card**. The first note posts
normally; every note after it re-renders the accumulated trail into that same
message via ``adapter.edit_message`` (Google Chat ``messages.patch``), which
updates the thread without re-notifying. When the card reaches a terminal state
the rolling message is settled — the ``⏳`` becomes ``✓`` or ``⏹`` — and the
result posts as a message of its own, which is the one that should ping.

Three properties of the surrounding code make this nearly free:

1. Every notifier line leaves through a single ``adapter.send`` call site, so
   the whole behaviour hangs off one anchor.
2. ``BasePlatformAdapter.edit_message`` returns ``SendResult(success=False)``
   on platforms that cannot edit, so the fallback to a fresh message needs no
   capability check and no platform name test. Nothing is gated on
   ``google_chat``.
3. ``send()`` already returns a ``SendResult`` carrying the ``message_id`` the
   next edit needs.

The tracking map is **in-process**, hung off the watcher instance exactly like
``kanban_notify_delivery.high_water``. Two consequences, both accepted:

* A gateway restart mid-card loses the map, so the next note starts a fresh
  rolling message. The trail is split; nothing is lost.
* It assumes a single notifier, which is the same assumption
  ``kanban_notify_delivery.py`` documents at length — the operator renders
  ``replicas: 1`` with ``strategy: Recreate``.

The one thing that is *not* optional is the replay guard. That same module made
delivery at-least-once: a batch of three heartbeats that fails on the third is
re-read and replayed whole on the next tick. Without ``last_event_id`` on the
entry the first two bullets would be appended a second time, so the fix for a
lost message would become a stuttering one.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

try:  # In the image the patches live under the ``gateway`` package.
    from gateway.kanban_handoff_clip import clip_handoff
except ImportError:  # Unit tests import the patch modules flat.
    from kanban_handoff_clip import clip_handoff

logger = logging.getLogger(__name__)

# A progress line is a status ping, not the report. The completion handoff
# still carries the full result at DEFAULT_LIMIT (1200), and a worker that
# needs more room than this should be completing, not heartbeating.
DEFAULT_NOTE_LIMIT = 300


def progress_note(payload: object, limit: int = DEFAULT_NOTE_LIMIT) -> str:
    """Return the deliverable progress note on a heartbeat event's payload.

    Returns ``""`` — meaning "deliver nothing" — for a ``None`` payload, a
    payload that is not a mapping, a missing ``note`` key, or a blank note.
    Only a note a worker deliberately wrote produces a chat line.
    """
    if not isinstance(payload, dict):
        return ""
    return clip_handoff(payload.get("note"), limit)


# --- what belongs in the rolling message -----------------------------------

#: Event kinds that update the card's rolling message instead of posting one of
#: their own. Everything else the notifier reaches the send site with is
#: terminal: it settles the rolling message and then posts separately.
#:
#: ``status`` is in here for correctness rather than for effect — no code path
#: in hermes-agent v2026.8.3 writes an event of that kind, so the notifier's
#: ``elif kind == "status":`` branch never runs today. Listing it means a card
#: whose status transitions start being recorded folds them into the trail
#: rather than resuming one-message-per-event.
ROLLING_KINDS = ("heartbeat", "status")

#: Leading marker on the rolling message. ``IN_PROGRESS`` while the card runs;
#: on a terminal event the message is re-rendered with one of the other two so
#: a finished card never leaves an hourglass sitting in the thread.
#:
#: Two settled markers rather than one, because the rolling message must not
#: imply success for a card that crashed. Which outcome it *was* is carried by
#: the terminal message posted directly beneath it.
IN_PROGRESS = "⏳"
FINISHED = "✓"
STOPPED = "⏹"

BULLET = "• "

#: Rendered in place of the bullets dropped to stay inside the budget.
ELIDED = "• […]"

#: Trail length caps. ``MAX_RENDER`` is deliberately below the Google Chat
#: adapter's ``_MAX_TEXT_LENGTH`` (4000): at that ceiling ``send()`` chunks the
#: text into a *second* message and ``edit_message()`` truncates it silently,
#: and either one defeats the point of keeping the card to one message.
MAX_LINES = 20
MAX_RENDER = 3500

#: Backstop on the per-watcher message map, same role and same size as
#: ``kanban_notify_delivery.MAX_TRACKED``: entries are dropped when their card
#: reaches a terminal state, so a healthy gateway holds one per in-flight card
#: and never approaches this. It bounds a board whose cards never terminate.
MAX_TRACKED = 2048

#: Attribute the map hangs off on the watcher instance. Same lazily-initialised
#: pattern as upstream's ``_kanban_sub_fail_counts``.
_ATTR = "_kanban_progress_messages"


def rolling_line(kind: str, payload: object) -> str:
    """Return the trail entry for an event, or ``""`` if it contributes none.

    Only ever consulted for a kind in :data:`ROLLING_KINDS`; the empty return
    is for a rolling event that turned out to carry nothing worth showing (a
    ``status`` event with no status on it). Routing is by *kind*, not by this
    being non-empty — an event that rolls must not fall through and settle the
    message just because its payload was thin.
    """
    if kind == "heartbeat":
        return progress_note(payload)
    if kind == "status":
        status = payload.get("status") if isinstance(payload, dict) else None
        status = str(status).strip() if status else ""
        return f"→ {status}" if status else ""
    return ""


def render(
    header: str, lines: Sequence[str], marker: str = IN_PROGRESS,
) -> str:
    """Render the rolling message: a marker, the card's header, and the trail.

    A single-entry trail renders as one line, which makes the first note of
    every card **byte-identical** to what the notifier posted before this
    existed — the common case is unchanged, and only a card that actually
    reports twice grows a bulleted body.
    """
    head = f"{marker} {header}".rstrip()
    kept = [line for line in lines if line]
    if not kept:
        return head
    elided = len(kept) > MAX_LINES
    kept = kept[-MAX_LINES:]
    while True:
        text = _compose(head, kept, elided)
        if len(text) <= MAX_RENDER or len(kept) <= 1:
            # One entry still over budget means a single enormous line, which
            # the 300-character note clip makes unreachable from a heartbeat.
            # Clipped rather than trusted, on a whitespace boundary so a
            # trailing URL survives whole — the same guarantee progress_note
            # gives, for the same reason.
            return clip_handoff(text, MAX_RENDER)
        kept.pop(0)
        elided = True


def _compose(head: str, kept: Sequence[str], elided: bool) -> str:
    if len(kept) == 1 and not elided:
        return f"{head} {kept[0]}".strip()
    body = ([ELIDED] if elided else []) + [f"{BULLET}{line}" for line in kept]
    return "\n".join([head] + body)


def settled_marker(kind: str) -> str:
    """The marker a terminal event leaves the rolling message showing."""
    return FINISHED if kind == "completed" else STOPPED


# --- the per-card message map ----------------------------------------------


def sub_key(sub: dict) -> tuple:
    """The subscription's identity — the four columns the cursor is keyed on.

    A local copy rather than an import of the identical
    ``kanban_notify_delivery.sub_key``: that module is copied into the image
    hundreds of lines further down ``deploy/docker/Dockerfile`` than this one,
    so importing it at module scope would break this patch's own build-time
    check. ``test_kanban_progress_lines.py`` asserts the two agree.
    """
    return (
        sub["task_id"],
        sub["platform"],
        sub["chat_id"],
        sub.get("thread_id") or "",
    )


def tracked_messages(watcher: Any) -> dict:
    """The watcher's in-process card→rolling-message map, made on first use."""
    tracked = getattr(watcher, _ATTR, None)
    if tracked is None:
        tracked = {}
        setattr(watcher, _ATTR, tracked)
    return tracked


def _remember(
    tracked: dict, key: tuple, message_id: str, lines: list, event_id: int,
) -> None:
    tracked[key] = {
        "message_id": message_id,
        "lines": lines,
        "last_event_id": int(event_id),
    }
    while len(tracked) > MAX_TRACKED:
        # dicts preserve insertion order; re-assigning an existing key does not
        # move it, so this evicts the least recently *added* card. The cost is
        # that its next note starts a new message — the restart behaviour, on
        # one card.
        tracked.pop(next(iter(tracked)), None)


async def deliver(
    watcher: Any,
    adapter: Any,
    sub: dict,
    kind: str,
    ev: Any,
    message: str,
    metadata: Optional[dict],
    header: str,
) -> Any:
    """Deliver one notifier event, rolling progress into a single message.

    Drop-in for the ``await adapter.send(...)`` the notifier used to call, and
    returns what that call site expects: the adapter's ``SendResult``, or
    ``None`` on the suppressed-replay path (the notifier reads
    ``getattr(res, "success", True)``, so ``None`` means "delivered").

    Terminal events are unchanged from the caller's point of view — a new
    message, with the artifact upload and failure accounting that follow it
    untouched. The only thing added on that path is settling the rolling
    message first, and that is best-effort: a failed cosmetic edit must not
    reach the notifier's ``except``, where it would rewind the cursor and count
    against the subscription's send-failure budget.
    """
    chat_id = sub["chat_id"]
    tracked = tracked_messages(watcher)
    key = sub_key(sub)
    entry = tracked.get(key)
    event_id = int(getattr(ev, "id", 0) or 0)

    if kind not in ROLLING_KINDS:
        if entry and entry["message_id"] and entry["lines"]:
            try:
                await adapter.edit_message(
                    chat_id,
                    entry["message_id"],
                    render(header, entry["lines"], settled_marker(kind)),
                )
            except Exception as exc:
                logger.debug(
                    "kanban progress: could not settle the rolling message "
                    "for %s: %s", sub.get("task_id"), exc,
                )
        tracked.pop(key, None)
        return await adapter.send(chat_id, message, metadata=metadata)

    line = rolling_line(kind, getattr(ev, "payload", None)) or message
    if entry and event_id and event_id <= entry["last_event_id"]:
        # An at-least-once replay of something this process already appended.
        # Reported as delivered so the cursor still advances past it.
        logger.debug(
            "kanban progress: event %s for %s already in the rolling message",
            event_id, sub.get("task_id"),
        )
        return None

    if entry and entry["message_id"]:
        lines = entry["lines"] + [line]
        result = await adapter.edit_message(
            chat_id, entry["message_id"], render(header, lines),
        )
        if getattr(result, "success", False):
            entry["lines"] = lines
            entry["last_event_id"] = max(entry["last_event_id"], event_id)
            return result
        # The message was deleted, or this platform cannot edit at all. Both
        # end the same way: forget it and post the note as its own message,
        # which is exactly the pre-rolling behaviour.
        logger.debug(
            "kanban progress: editing the rolling message for %s failed (%s); "
            "posting a new one", sub.get("task_id"),
            getattr(result, "error", None) or "no reason given",
        )
        tracked.pop(key, None)

    result = await adapter.send(
        chat_id, render(header, [line]), metadata=metadata,
    )
    if getattr(result, "success", True) is not False:
        message_id = getattr(result, "message_id", None)
        if message_id:
            _remember(tracked, key, message_id, [line], event_id)
    return result
