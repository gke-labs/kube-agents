#!/usr/bin/env python3
"""Build gate for the rolling-progress-message patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` immediately after
``apply_kanban_progress_lines.py``. The applier proves three anchors matched in
``gateway/kanban_watchers.py``; a matched anchor is the weaker half of every
concern here, because **every** failure mode of this patch is silent:

* **The wiring.** A trailer import that did not execute, or a ``deliver`` that
  no longer resolves, does not fail at build time — it raises inside the
  notifier's ``try`` at runtime, where the ``except`` counts a send failure and
  rewinds the cursor. The card goes quiet again and the log says the adapter
  failed.
* **The edit contract.** The fallback to a fresh message is predicated on
  ``BasePlatformAdapter.edit_message`` returning ``SendResult(success=False)``
  rather than raising, and on ``SendResult`` still carrying ``message_id``. If
  either changed, the honest outcome is "no rolling message" and the observable
  outcome is one message per note — exactly what shipped before this patch, so
  nothing looks broken.
* **The Google Chat side.** If the adapter stopped overriding ``edit_message``,
  the base's ``success=False`` would send every note as its own message. Again
  indistinguishable from the old behaviour.
* **The budget.** ``MAX_RENDER`` only means something relative to the adapter's
  own ``_MAX_TEXT_LENGTH``. Cross that and ``send()`` chunks into a second
  message while ``edit_message()`` truncates with a ``…`` — the one-message
  promise breaks quietly, in the tail, on the longest cards.
* **The replay guard.** ``kanban_notify_delivery.py`` made delivery
  at-least-once. Drop the guard and a partially-failed batch re-appends the
  bullets it already delivered; the trail stutters and no error is raised.

So this drives the *patched* runtime rather than reading it: the real
``watchers._progress_deliver`` the trailer resolved, on an instance of the real
``GatewayKanbanWatchersMixin`` the map hangs off, and the real
``BasePlatformAdapter``/``SendResult`` the fallback rests on.

One thing is deliberately **not** checked here: that this module's ``sub_key``
agrees with ``kanban_notify_delivery.sub_key``. That module is copied into the
image hundreds of Dockerfile lines below this stage and does not exist yet.
``test_kanban_progress_lines.py`` asserts the agreement host-side.

Usage::

    cd /opt/hermes && python3 verify_kanban_progress_lines.py
"""

from __future__ import annotations

import asyncio
import sys

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


import gateway.kanban_watchers as watchers  # noqa: E402
from gateway.kanban_progress_lines import (  # noqa: E402
    DEFAULT_NOTE_LIMIT,
    FINISHED,
    IN_PROGRESS,
    MAX_LINES,
    MAX_RENDER,
    MAX_TRACKED,
    ROLLING_KINDS,
    STOPPED,
    deliver,
    progress_note,
    render,
    rolling_line,
    settled_marker,
)
from gateway.platforms.base import BasePlatformAdapter, SendResult  # noqa: E402

with open("gateway/kanban_watchers.py", encoding="utf-8") as _notifier_src:
    NOTIFIER_SOURCE = _notifier_src.read()

GOOGLE_CHAT_ADAPTER = "plugins/platforms/google_chat/adapter.py"

# The header the notifier passes: `f"{board_tag}{tag}"` with a board slug and a
# @-mention, which is the shape every live delivery has.
HEADER = "[default] @platform "

# --- 1. The wiring resolved ---------------------------------------------------
# Both names arrive in one appended trailer, but they are checked separately:
# the render branch calls `_progress_note` and the send site calls
# `_progress_deliver`, so a rename breaks one path while the other keeps working
# and the build stays green.
print("import wiring:")
check(
    "the notifier resolved the progress-note import",
    hasattr(watchers, "_progress_note"),
    "the trailer import did not execute",
)
check(
    "the notifier resolved the rolling-delivery import",
    hasattr(watchers, "_progress_deliver"),
    "the trailer import did not execute; every note would still be its own message",
)
check(
    "the name the send site calls is this module's deliver",
    getattr(watchers, "_progress_deliver", None) is deliver,
    "something else is bound to the name the notifier calls",
)
check(
    "the trailer is applied exactly once",
    NOTIFIER_SOURCE.count("from gateway.kanban_progress_lines import") == 2
    and NOTIFIER_SOURCE.count("import deliver as _progress_deliver") == 1,
    "a duplicated trailer means the applier ran twice over one tree",
)

# --- 2. The send site ---------------------------------------------------------
# One call site is the whole reason this patch is three anchors and not thirty.
# If upstream grows a second `adapter.send` inside the notifier loop, the events
# leaving through it bypass the rolling message entirely.
print("send site:")
check(
    "the notifier delivers through the rolling helper",
    NOTIFIER_SOURCE.count("_send_res = await _progress_deliver(") == 1,
)
check(
    "upstream's direct send is gone from the notifier loop",
    "_send_res = await adapter.send(" not in NOTIFIER_SOURCE,
    "a surviving direct send would post progress notes as separate messages",
)
check(
    "the helper is given the card's header",
    'header=f"{board_tag}{tag}",' in NOTIFIER_SOURCE,
    "without it the rolling message loses the board slug and the @-mention",
)
_deliver_at = NOTIFIER_SOURCE.find("_send_res = await _progress_deliver(")
_check_at = NOTIFIER_SOURCE.find('if getattr(_send_res, "success", True) is False:')
check(
    "the failure check still reads the helper's return value",
    0 <= _deliver_at < _check_at,
    "the send-failure accounting is what makes delivery at-least-once",
)
check(
    "the heartbeat branch still builds the first rendering",
    'msg = f"⏳ {board_tag}{tag}{note}"' in NOTIFIER_SOURCE,
    "the render branch and the helper have to agree on the header",
)
check(
    "a first note is byte-identical to what the notifier posted before",
    render(HEADER, ["Reading the scheduler directly."])
    == f"⏳ {HEADER}Reading the scheduler directly.",
    "the common case — a card that reports once — must not change at all",
)

# --- 3. The platform contract the fallback rests on ---------------------------
# Nothing in this patch tests the platform name. It relies on the base adapter
# reporting an unsupported edit rather than raising, which is what lets a
# non-editing platform degrade to today's one-message-per-note behaviour with no
# capability check anywhere.
print("platform contract:")
unsupported = asyncio.run(
    BasePlatformAdapter.edit_message(object(), "chat", "msg-1", "text")
)
check(
    "the base adapter still reports an unsupported edit rather than raising",
    isinstance(unsupported, SendResult) and unsupported.success is False,
    f"got {unsupported!r}; the no-capability-check fallback assumes this",
)
check(
    "SendResult still carries the message id the next edit needs",
    "message_id" in getattr(SendResult, "__dataclass_fields__", {}),
    "without it nothing can be tracked and every note posts fresh",
)

try:
    with open(GOOGLE_CHAT_ADAPTER, encoding="utf-8") as _gchat_src:
        GCHAT_SOURCE = _gchat_src.read()
except OSError as exc:  # pragma: no cover - the plugin ships in the base image
    GCHAT_SOURCE = ""
    check("the Google Chat adapter is where it was", False, str(exc))

check(
    "the Google Chat adapter still overrides edit_message",
    "async def edit_message(" in GCHAT_SOURCE,
    "falling through to the base returns success=False for every note",
)
check(
    "it still edits in place rather than posting again",
    "self._patch_message(message_id" in GCHAT_SOURCE,
    "messages.patch is the API that updates a message without re-notifying",
)
_cap = [
    int(line.split("=")[1].strip())
    for line in GCHAT_SOURCE.splitlines()
    if line.startswith("_MAX_TEXT_LENGTH = ")
]
check(
    "the adapter's text cap was located",
    len(_cap) == 1,
    "re-derive the render budget against the constant that replaced it",
)
check(
    "the render budget stays under the adapter's cap",
    _cap and MAX_RENDER < _cap[0],
    f"MAX_RENDER={MAX_RENDER} against a cap of {_cap}; at the cap send() chunks "
    "into a second message and edit_message() truncates silently",
)

# --- 4. The rolling behaviour, driven ------------------------------------------
# Driven through the name the notifier resolved, so a trailer that bound
# something else is caught by behaviour and not only by section 1's identity
# check. Falling back to the module's own ``deliver`` when the trailer did not
# execute at all keeps the remaining sections reporting: a broken build is more
# useful with every failure named than with one AttributeError traceback.
_deliver = getattr(watchers, "_progress_deliver", deliver)

print("rolling delivery:")


class _Watcher(watchers.GatewayKanbanWatchersMixin):
    """A real watcher, minus the gateway it normally hangs off.

    Instantiated rather than faked because the message map is set on the
    instance: a mixin that grew ``__slots__`` would make that ``setattr`` raise
    inside the notifier's ``try``, which reads as a send failure.
    """

    def __init__(self):
        pass


class _Event:
    def __init__(self, event_id, payload=None):
        self.id = event_id
        self.payload = payload


class _Adapter:
    """Records what a platform would have been asked to do."""

    def __init__(self, can_edit=True, message_ids=None):
        self.can_edit = can_edit
        self.sent = []
        self.edited = []
        self._ids = list(message_ids or [])
        self._next = 0

    async def send(self, chat_id, content, metadata=None):
        self._next += 1
        message_id = (
            self._ids[self._next - 1]
            if self._next <= len(self._ids)
            else f"spaces/AAA/messages/{self._next}"
        )
        self.sent.append((chat_id, content, metadata, message_id))
        return SendResult(success=True, message_id=message_id)

    async def edit_message(self, chat_id, message_id, content):
        if not self.can_edit:
            return SendResult(success=False, error="Not supported")
        self.edited.append((chat_id, message_id, content))
        return SendResult(success=True, message_id=message_id)


SUB = {
    "task_id": "t_a8f58a2a",
    "platform": "google_chat",
    "chat_id": "spaces/AAAQ",
    "thread_id": "spaces/AAAQ/threads/xyz",
}
NOTES = (
    "Reading the scheduler directly: 8 configured jobs found.",
    "Found two separate cron stores — the board's view is stale.",
    "Checking delivery failures and per-job run gaps.",
)


def run_card(adapter, watcher=None, terminal="completed"):
    """Three progress notes then a completion, the way the notifier delivers."""
    watcher = watcher or _Watcher()
    for i, note in enumerate(NOTES, start=1):
        asyncio.run(
            _deliver(
                watcher, adapter, SUB, "heartbeat", _Event(i, {"note": note}),
                f"⏳ {HEADER}{note}", {"thread_id": SUB["thread_id"]},
                header=HEADER,
            )
        )
    if terminal:
        asyncio.run(
            _deliver(
                watcher, adapter, SUB, terminal, _Event(4),
                f"✔ {HEADER}Kanban {SUB['task_id']} done",
                {"thread_id": SUB["thread_id"]}, header=HEADER,
            )
        )
    return watcher


chat = _Adapter()
card = run_card(chat)
check(
    "three notes and a completion produce exactly two messages",
    len(chat.sent) == 2,
    f"got {len(chat.sent)}: {[s[1] for s in chat.sent]}",
)
check(
    "the notes after the first edit the message rather than post one",
    len(chat.edited) == 3,
    f"got {len(chat.edited)} edits; two for the notes, one to settle",
)
check(
    "every edit targets the message the first note created",
    {e[1] for e in chat.edited} == {chat.sent[0][3]},
    "a changing resource name means a new message per note in the space",
)
check(
    "the trail accumulates rather than replacing",
    all(note in chat.edited[1][2] for note in NOTES),
    chat.edited[1][2],
)
check(
    "the rolling message is still marked in progress while it rolls",
    chat.edited[1][2].startswith(IN_PROGRESS),
)
check(
    "the completion settles the rolling message",
    chat.edited[-1][2].startswith(FINISHED)
    and all(note in chat.edited[-1][2] for note in NOTES),
    chat.edited[-1][2],
)
check(
    "the completion is its own message, unchanged",
    chat.sent[1][1] == f"✔ {HEADER}Kanban {SUB['task_id']} done",
    f"got {chat.sent[1][1]!r}; this is the notification people want",
)
check(
    "the thread metadata still reaches the adapter",
    chat.sent[0][2] == {"thread_id": SUB["thread_id"]},
    "a rolling message posted outside the thread is worse than no message",
)
check(
    "the card is forgotten once it terminates",
    not getattr(card, "_kanban_progress_messages", {}),
    "entries that outlive their card are what MAX_TRACKED exists to bound",
)

failed = _Adapter()
run_card(failed, terminal="crashed")
check(
    "a failed card does not settle to a success marker",
    failed.edited[-1][2].startswith(STOPPED),
    failed.edited[-1][2],
)
check(
    "settled_marker agrees for every terminal kind the notifier delivers",
    settled_marker("completed") == FINISHED
    and {settled_marker(k) for k in ("blocked", "gave_up", "crashed", "timed_out")}
    == {STOPPED},
)

# The second card on the same watcher must not inherit the first one's message.
second = dict(SUB, task_id="t_b1c2d3e4")
asyncio.run(
    _deliver(
        card, chat, second, "heartbeat", _Event(9, {"note": "starting"}),
        f"⏳ {HEADER}starting", None, header=HEADER,
    )
)
check(
    "the next card starts a message of its own",
    len(chat.sent) == 3 and chat.sent[2][1] == f"⏳ {HEADER}starting",
    f"got {chat.sent[-1][1]!r}",
)

# --- 5. Degrading to the old behaviour ----------------------------------------
# Every one of these has to end at "one message per note", which is what shipped
# before the patch: worse than the rolling message, better than a lost note.
print("fallbacks:")
plain = _Adapter(can_edit=False)
run_card(plain, terminal=None)
check(
    "a platform that cannot edit posts each note as its own message",
    len(plain.sent) == 3 and plain.edited == [],
    f"got {len(plain.sent)} messages",
)
check(
    "and each of those is the single-line rendering, not a bulleted trail",
    all("\n" not in s[1] for s in plain.sent),
    "an unsupported edit must not leave the trail rendered into a new message",
)


class _Deleted(_Adapter):
    async def edit_message(self, chat_id, message_id, content):
        return SendResult(success=False, error="message not found")


deleted = _Deleted()
run_card(deleted, terminal=None)
check(
    "a deleted message is replaced rather than dropped",
    len(deleted.sent) == 3,
    f"got {len(deleted.sent)}; a failed edit must fall back to a fresh message",
)


class _ExplodingOnSettle(_Adapter):
    async def edit_message(self, chat_id, message_id, content):
        if content.startswith(IN_PROGRESS):
            return await super().edit_message(chat_id, message_id, content)
        raise RuntimeError("chat API unreachable")


settle_boom = _ExplodingOnSettle()
try:
    run_card(settle_boom)
    settled_ok = True
except Exception as exc:  # noqa: BLE001 - that is the failure being checked
    settled_ok = False
    print(f"       settling edit raised: {exc}")
check(
    "a settling edit that fails never reaches the notifier's except",
    settled_ok,
    "a cosmetic edit would rewind the cursor and re-deliver the completion",
)
check(
    "and the completion is posted anyway",
    len(settle_boom.sent) == 2,
    f"got {len(settle_boom.sent)} messages",
)


class _RefusingSend(_Adapter):
    async def send(self, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata, None))
        return SendResult(success=False, error="rate limited")


refusing = _RefusingSend()
refuser = _Watcher()
first = asyncio.run(
    _deliver(
        refuser, refusing, SUB, "heartbeat", _Event(1, {"note": NOTES[0]}),
        f"⏳ {HEADER}{NOTES[0]}", None, header=HEADER,
    )
)
check(
    "a refused send is reported to the notifier as a failure",
    getattr(first, "success", True) is False,
    "swallowing it would advance the cursor past an undelivered note",
)
check(
    "and nothing is tracked for a message that was never posted",
    not getattr(refuser, "_kanban_progress_messages", {}),
    "a tracked phantom would send every later note to an edit that cannot work",
)

# --- 6. The at-least-once replay ----------------------------------------------
# kanban_notify_delivery.py reads events and advances the cursor only after the
# batch is delivered, so a batch that fails partway is replayed whole. Without
# the guard the bullets already in the message are appended a second time.
print("replay:")
replay = _Adapter()
watcher = _Watcher()
for _ in range(2):
    for i, note in enumerate(NOTES, start=1):
        asyncio.run(
            _deliver(
                watcher, replay, SUB, "heartbeat", _Event(i, {"note": note}),
                f"⏳ {HEADER}{note}", None, header=HEADER,
            )
        )
tracked = getattr(watcher, "_kanban_progress_messages", {})
entry = next(iter(tracked.values()), {})
check(
    "a replayed batch does not append its notes twice",
    entry.get("lines") == list(NOTES),
    f"got {entry.get('lines')!r}",
)
check(
    "a replayed event reports as delivered so the cursor still advances",
    asyncio.run(
        _deliver(
            watcher, replay, SUB, "heartbeat", _Event(1, {"note": NOTES[0]}),
            f"⏳ {HEADER}{NOTES[0]}", None, header=HEADER,
        )
    )
    is None,
    "reporting a failure here would stall the subscription on an old event",
)

# --- 7. Budgets ----------------------------------------------------------------
# A long-running card is the one this patch is for, so the trail is the thing
# most likely to grow past what the platform will take.
print("budgets:")
long_trail = [f"Milestone {i}: " + "detail " * 40 for i in range(60)]
rendered = render(HEADER, long_trail)
check(
    "a runaway trail stays inside the render budget",
    len(rendered) <= MAX_RENDER,
    f"rendered {len(rendered)} characters",
)
check(
    "the newest milestones are the ones kept",
    "Milestone 59" in rendered and "Milestone 0:" not in rendered,
)
check(
    "the reader is told the trail was elided",
    "[…]" in rendered,
    "a silently shortened log reads as a card that skipped work",
)
check(
    "the trail is capped by count as well as size",
    render(HEADER, [f"n{i}" for i in range(MAX_LINES + 5)]).count("\n") <= MAX_LINES + 1,
)
check(
    "the map is bounded",
    MAX_TRACKED > 0 and isinstance(MAX_TRACKED, int),
)

# --- 8. What rolls and what does not -------------------------------------------
print("routing:")
check(
    "the rolling kinds are the two non-terminal ones",
    ROLLING_KINDS == ("heartbeat", "status"),
    f"got {ROLLING_KINDS!r}",
)
check(
    "a noteless auto-heartbeat contributes nothing",
    rolling_line("heartbeat", None) == "" and rolling_line("heartbeat", {}) == "",
    "the live board carries ~2,100 of these; each one would be a bullet",
)
check(
    "a status transition renders as a trail entry",
    rolling_line("status", {"status": "in_progress"}) == "→ in_progress",
)
check(
    "a terminal kind contributes no trail entry",
    rolling_line("completed", {"note": "x"}) == "",
)

# The filter the render branch itself calls. Silence for a noteless heartbeat is
# the single most important behaviour in this patch — the live board carries
# 2,107 of them and delivering one line each would make the notifier the noisiest
# thing in the space.
check(
    "a payload with no note at all is silent",
    not (progress_note(None) or progress_note({}) or progress_note({"note": "  "})),
)
check(
    "a deliberate note is delivered verbatim",
    progress_note({"note": "scanned 3 of 7"}) == "scanned 3 of 7",
)
check(
    "a runaway note is clipped to the progress budget",
    len(progress_note({"note": "word " * 500})) <= DEFAULT_NOTE_LIMIT,
)

print()
if FAILURES:
    print(f"verify_kanban_progress_lines: {len(FAILURES)} check(s) FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_progress_lines: all checks passed")
