"""Mid-run progress lines for the kanban notifier.

Installed into the image at ``/opt/hermes/gateway/kanban_progress_lines.py``
and wired into ``gateway/kanban_watchers_notifier.py`` (the notifier's
``_EVENT_FORMATTERS`` table, ``_KanbanNotification._send_event`` and, for
the flag's silent kinds, the skip loop in ``_send_pings``) by
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
event carrying that note (``hermes_cli/kanban_db_dispatch.py``); the notifier
simply does not deliver that kind. Two properties make delivering it nearly
free:

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
result posts as a message of its own, which is the one that should ping. With
``KAGE_SLACK_UX`` on, a Slack card settles to its last line only, a failure
the creator's wake will explain is held for the wake step rather than posted,
the card recovering drops a failure line it still holds, no line carries the
board tag or ``Kanban <id>``, a card's notes go on its row in the thread's
plan (``gateway/slack_ux_status.py``), the rolling line being the plan's
fallback, an opened pull request and a ``needs_input`` question post as
messages of their own (``gateway/slack_ux_moments.py``), and any later event
takes the buttons off the card's open question; :func:`silent_event` carries
``archived`` and ``unblocked``, which upstream never posts, to the plan and
the question. See :func:`deliver`.

Three properties of the surrounding code make this nearly free:

1. Every notifier line leaves through a single ``adapter.send`` call site, so
   the whole behaviour hangs off one anchor; only the flag's silent kinds need
   a second, in the skip loop.
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

import asyncio
import logging
import re
import unicodedata
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
#: terminal: it settles the rolling message and then posts separately (or,
#: for a failure the wake will explain under ``KAGE_SLACK_UX``, holds the post
#: for the wake step).
#:
#: ``status`` was listed for correctness before any code path wrote the kind;
#: since v2026.9.14 the dashboard's drag-drop path (``_set_status_direct`` in
#: ``plugins/kanban/dashboard/plugin_api.py``) records one per move, and
#: upstream's ``_EVENT_FORMATTERS["status"]`` renders it. Listing it here means
#: such a move folds into the card's trail as ``→ <status>`` rather than
#: posting upstream's ``🔄`` line as a message of its own.
#:
#: ``queued`` is the dispatcher's notice that a user card is waiting for a
#: worker slot (``hermes_cli/kanban_priority.py``). It rolls so the
#: card's first progress note joins the queued line instead of posting under
#: it, and so a card queued twice reads as one message.
ROLLING_KINDS = ("heartbeat", "status", "queued")

#: The rolling kinds whose trail entry is the ``note`` on the event payload.
NOTE_KINDS = ("heartbeat", "queued")

#: A queued line that opens a card's rolling message is posted bare, without
#: the card's header, because it is worded for the user as a whole sentence
#: (bnaylor). The header joins once the worker's first note arrives.
QUEUED_KIND = "queued"

#: Where a card's map entry remembers which of its trail lines were queued
#: notices, so the terminal settle can drop them.
QUEUED_LINES_KEY = "queued_lines"

#: With ``KAGE_SLACK_UX`` on, the blocked kind that may post a question, and
#: the terminal kind whose report may announce a PR (see ``slack_ux_moments``).
NEEDS_YOU_KIND = "blocked"
UNBLOCKED_KIND = "unblocked"
PR_REPORT_KIND = "completed"

#: With ``KAGE_SLACK_UX`` on, a card beneath a fan-out folds its report into
#: its row in the thread's plan rather than posting it, when its nearest
#: ancestor that created more than one card is still open and subscribed to the
#: same thread: that card's own completion is the answer, and
#: ``kanban_children_settled`` holds it until its children settle. That covers
#: a card a fanned-out card filed in turn, such as a Platform Agent card per
#: cluster handing its cluster to the Cluster Agent. A chain with no fan-out
#: above it, a single-cluster delegation, posts its whole report as before.
#: Only a completion folds; a failure or a question posts.
FOLDED_KIND = "completed"

#: How many creators up the fold looks for a fan-out. Delegation runs three
#: deep (Planning Agent, Platform Agent, Cluster Agent); the bound only stops a
#: malformed chain from walking the board.
FOLD_ANCESTOR_DEPTH = 8

#: A card parked by a give-up: still ``blocked``, with a ``gave_up`` as its
#: latest stop, as ``slack_ux_reactions.OPEN_CARDS_SQL`` reads it. ``{card}``
#: is the alias of its ``tasks`` row.
GAVE_UP_SQL = (
    "({card}.status = 'blocked' AND COALESCE((SELECT g.kind FROM task_events g "
    "WHERE g.task_id = {card}.id AND g.kind IN ('blocked', 'unblocked', 'gave_up') "
    "ORDER BY g.id DESC LIMIT 1), '') = 'gave_up')"
)

#: One row when the card's nearest ancestor that created more than one card is
#: still open and subscribed to the thread: the creators Hermes stamps on each
#: ``created`` event (``hermes_cli/kanban_db.py``), walked up from the card,
#: and the open statuses ``kanban_children_settled`` waits on. Parameters: the
#: card, the depth bound, then the subscription's platform, chat and thread.
#: The cards between are not required open: ``kanban_children_settled`` closes
#: each only after the card beneath it, and usually before its report is
#: delivered, so requiring them open would post the report again.
#: A card parked by a give-up, the fan-out or one between, is not open: its
#: worker will not run again to carry the report up, so the report posts. One
#: blocked on a question still counts; it resumes on the answer.
FANNED_OUT_ANCESTOR_SQL = (
    "WITH RECURSIVE up(id, depth) AS ("
    "SELECT json_extract(payload, '$.creator_task_id'), 1 FROM task_events "
    "WHERE task_id = ? AND kind = 'created' "
    "UNION ALL "
    "SELECT json_extract(e.payload, '$.creator_task_id'), up.depth + 1 FROM task_events e "
    "JOIN up ON e.task_id = up.id WHERE e.kind = 'created' AND up.depth < ?"
    "), fan(id, depth) AS ("
    "SELECT up.id, up.depth FROM up WHERE up.id IS NOT NULL AND (SELECT count(*) FROM task_events o "
    "WHERE o.kind = 'created' AND json_extract(o.payload, '$.creator_task_id') = up.id) > 1 "
    "ORDER BY up.depth LIMIT 1"
    ") "
    "SELECT 1 FROM fan JOIN tasks t ON t.id = fan.id "
    "JOIN kanban_notify_subs s ON s.task_id = t.id "
    "WHERE t.status NOT IN ('done', 'archived') "
    "AND NOT " + GAVE_UP_SQL.format(card="t") + " "
    "AND NOT EXISTS (SELECT 1 FROM up b JOIN tasks m ON m.id = b.id "
    "WHERE b.depth < fan.depth AND " + GAVE_UP_SQL.format(card="m") + ") "
    "AND lower(s.platform) = ? AND s.chat_id = ? AND COALESCE(s.thread_id, '') = ? "
    "LIMIT 1"
)

#: Leading marker on the rolling message. ``IN_PROGRESS`` while the card runs;
#: on a terminal event the message is re-rendered with one of the other two so
#: a finished card never leaves an hourglass sitting in the thread.
#:
#: Two settled markers rather than one, because the rolling message must not
#: imply success for a card that crashed. Which outcome it *was* is carried by
#: the terminal message posted directly beneath it, or by the creator's
#: explanation where ``KAGE_SLACK_UX`` held that message for the wake.
IN_PROGRESS = "⏳"
FINISHED = "✓"
STOPPED = "⏹"

#: Under ``KAGE_SLACK_UX``, the marker a card's rolling message settles to when
#: the thread's plan takes the card's notes over: the card still runs, and its
#: notes go on the plan below.
MOVED_TO_PLAN = "↓"

#: The kinds that make a failure line the card still holds stale: the card
#: recovered, and its report or review handoff is posting. So does a ``status``
#: event moving the card to :data:`SUPERSEDING_STATUS`, a card dragged to done.
#: A note or another failure leaves the line held for its wake to settle.
SUPERSEDING_KINDS = ("completed", "review_requested")
SUPERSEDING_STATUS = "done"

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

#: Upstream's card id in a notifier head, ``{board_tag}{@assignee }Kanban <id>``.
#: With ``KAGE_SLACK_UX`` on, a Slack line keeps only the ``@assignee``, which
#: is how the thread names the specialist; see :func:`slack_line`.
KANBAN_ID = "Kanban {task_id}"
BOARD_TAG = "[{board}] "

#: What may come before a head :func:`slack_line` trims: one glyph of this
#: Unicode category, trailed by at most one variation selector, and a space.
MARKER_CATEGORY = "So"
VARIATION_SELECTOR = "\ufe0f"
MARKER_MAX = 2

#: Kinds upstream claims but has no formatter for, so ``_send_pings`` skips
#: them before any send. With ``KAGE_SLACK_UX`` on they still move a Slack
#: card's row in the thread's plan; see :func:`silent_event`.
SILENT_PLAN_KINDS = ("archived", "unblocked")

#: Kinds that settle a plan row past an earlier ``unblocked`` in the same
#: batch: every kind ``slack_status.TASK_STATUS_BY_KIND`` maps to a status
#: other than running, plus ``archived``. ``crashed`` and ``timed_out`` are
#: not among them, since the dispatcher retries the card and its row stays
#: running; see :func:`_overtaken`.
SETTLING_KINDS = (
    "archived",
    "blocked",
    "block_loop_detected",
    "changes_requested",
    "completed",
    "gave_up",
    "review_requested",
)

#: A Markdown heading's marks, which a completed card's result line drops.
HEADING_MARKS = re.compile(r"^#{1,6}\s+")
#: A Markdown link or image, which the result line reduces to its text: the
#: row's title is plain text, so ``[PR #12](url)`` would land verbatim.
LINK_MARKS = re.compile(r"!?\[([^\]]+)\]\([^)\s]*\)")
#: A code span's backticks, which the result line drops.
CODE_MARKS = re.compile(r"(?<!`)`([^`]+)`(?!`)")
#: Bold and italic stars around a run of text, which the result line drops. A
#: star touching a word, a path or a dot is a glob or arithmetic and stays, as
#: do underscores, so ``logs/*/*.json``, ``2*3`` and ``seeded_a`` survive.
EMPHASIS_MARKS = re.compile(r"(?<![\w*./])(\*\*|\*)(?=[^\s./*_])(.+?)(?<=\S)\1(?![\w*/])")

#: Attribute the map hangs off on the watcher instance. Same lazily-initialised
#: pattern as upstream's ``_kanban_sub_fail_counts``.
_ATTR = "_kanban_progress_messages"


def result_line(kind: str, payload: object) -> str:
    """A completed card's one-line result for its plan row, or ``""``.

    Upstream's ``completed`` event carries the first line of the worker's
    handoff summary, or of its result when it gave no summary
    (``_completed_event_payload`` in ``hermes_cli/kanban_db.py``), so the row
    needs nothing new from the worker. The title it becomes is plain text, so
    a heading's ``#`` marks, a link's URL and bold, italic and code marks are
    dropped, a ``#1234`` kept; the line is clipped as a progress note is.
    """
    if kind != "completed" or not isinstance(payload, dict):
        return ""
    line = HEADING_MARKS.sub("", str(payload.get("summary") or "").strip())
    line = EMPHASIS_MARKS.sub(r"\2", CODE_MARKS.sub(r"\1", LINK_MARKS.sub(r"\1", line)))
    return progress_note({"note": line})


def rolling_line(kind: str, payload: object) -> str:
    """Return the trail entry for an event, or ``""`` if it contributes none.

    Only ever consulted for a kind in :data:`ROLLING_KINDS`; the empty return
    is for a rolling event that turned out to carry nothing worth showing (a
    ``status`` event with no status on it). Routing is by *kind*, not by this
    being non-empty — an event that rolls must not fall through and settle the
    message just because its payload was thin.
    """
    if kind in NOTE_KINDS:
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


def slack_header(header: str, board: Optional[str]) -> str:
    """The progress header without its board tag: ``"@assignee "``, or ``""``."""
    tag = BOARD_TAG.format(board=board) if board else ""
    return header[len(tag):] if tag and header.startswith(tag) else header


def _marker(prefix: str) -> bool:
    """Whether ``prefix`` is nothing, or one upstream marker glyph and a space.

    Upstream's markers are symbols (``✔``, ``✖``, ``⏸``, ``🛑``), a variation
    selector at most after them; a quote, bullet or dash is the worker's own.
    """
    if not prefix:
        return True
    glyph, space = prefix[:-1], prefix[-1:]
    return (
        space == " "
        and 0 < len(glyph) <= MARKER_MAX
        and unicodedata.category(glyph[0]) == MARKER_CATEGORY
        and all(ch == VARIATION_SELECTOR for ch in glyph[1:])
    )


def slack_line(message: str, header: str, board: Optional[str], task_id: str) -> str:
    """A notifier line with the board tag and ``Kanban <id>`` taken out of its head.

    Upstream heads a line ``{board_tag}{@assignee }Kanban <id>``, or for
    ``changes_requested`` ``{board_tag}Kanban <id>``; either becomes the
    ``@assignee`` alone, and a line with no assignee loses the head entirely.
    Only a head that opens the line, after nothing but its marker, is trimmed:
    a completion message whose head ``completion_text`` already dropped is
    the worker's handoff alone, and a head quoted in it is the worker's words.
    """
    tag = BOARD_TAG.format(board=board) if board else ""
    card = KANBAN_ID.format(task_id=task_id)
    agent = slack_header(header, board).strip()
    first = message.split("\n", 1)[0]
    for head in (f"{header}{card}", f"{tag}{card}"):
        at = first.find(head)
        if at < 0 or not _marker(first[:at]):
            continue
        rest = message[at + len(head):]
        if agent:
            return message[:at] + agent + rest
        return message[:at] + (rest[1:] if rest.startswith(" ") else rest)
    return message


def _slack_heads(
    quiet: Any, sub: dict, header: str, board: Optional[str], message: str,
) -> tuple:
    """``(header, message)`` as a ``KAGE_SLACK_UX`` Slack line shows them, else unchanged."""
    if quiet is None:
        return header, message
    try:
        task_id = str(sub.get("task_id") or "")
        return slack_header(header, board), slack_line(message, header, board, task_id)
    except Exception as exc:  # noqa: BLE001 — presentation must not fail a delivery
        logger.debug("kanban progress: trimming the head for %s failed: %s", sub.get("task_id"), exc)
        return header, message


async def _settle_reaction(adapter: Any, sub: dict, kind: str, board: Optional[str]) -> None:
    """With ``KAGE_SLACK_UX`` on, settle the Slack ask this card's thread is waiting on.

    Best-effort, for the reason the terminal path below settles its rolling
    message best-effort: a cosmetic failure must not reach the notifier's
    ``except``. With the flag off this returns before touching anything. See
    ``gateway/slack_ux_reactions.py``.
    """
    try:
        from gateway import slack_ux_reactions
    except ImportError:
        return
    try:
        if slack_ux_reactions.enabled():
            await slack_ux_reactions.settle_delegated(adapter, sub, kind, board)
    except Exception as exc:  # noqa: BLE001 — never fail a delivery on a reaction
        logger.debug(
            "kanban progress: settle reaction for %s failed: %s", sub.get("task_id"), exc,
        )


def _slack_quiet(sub: dict) -> Any:
    """``kanban_notifier`` when ``KAGE_SLACK_UX`` is on for this Slack card, else None.

    Imported when a delivery runs: ``gateway/kanban_notifier.py`` is copied into
    the image after this module, and this one's build-time check runs before it
    exists. Anything that goes wrong here reads as flag off.
    """
    try:
        from gateway import kanban_notifier
    except ImportError:
        try:  # Unit tests import the patch modules flat.
            import kanban_notifier
        except ImportError:
            return None
    try:
        return kanban_notifier if kanban_notifier.slack_ux_on(sub.get("platform")) else None
    except Exception as exc:  # noqa: BLE001 — presentation must not fail a delivery
        logger.debug("kanban progress: reading KAGE_SLACK_UX failed: %s", exc)
        return None


def _slack_plan(quiet: Any) -> Any:
    """``slack_ux_status`` when ``quiet`` says ``KAGE_SLACK_UX`` is on for this Slack card.

    Imported when a delivery runs, for the reason :func:`_slack_quiet` imports
    its module then; an image without it reads as flag off.
    """
    if quiet is None:
        return None
    try:
        from gateway import slack_ux_status
    except ImportError:
        return None
    try:
        return slack_ux_status if slack_ux_status.enabled() else None
    except Exception as exc:  # noqa: BLE001 — presentation must not fail a delivery
        logger.debug("kanban progress: reading slack_ux_status failed: %s", exc)
        return None


async def _plan_row(
    plan: Any, adapter: Any, sub: dict, event_id: int, title: str, line: str, moved: Optional[str],
) -> bool:
    try:
        return bool(await plan.deliver_row(adapter, sub, event_id, title, line, moved))
    except Exception as exc:  # noqa: BLE001 — fall back to the progress line
        logger.debug("kanban progress: the plan row for %s failed: %s", sub.get("task_id"), exc)
        return False


async def _settle_plan_row(
    plan: Any, adapter: Any, sub: dict, kind: str, result: str = "", title: str = "",
) -> bool:
    """Settle the card's plan row; True when the plan now shows it complete."""
    try:
        return bool(await plan.settle_row(adapter, sub, kind, result, title))
    except Exception as exc:  # noqa: BLE001 — cosmetic, like the rolling settle
        logger.debug("kanban progress: settling the plan row for %s failed: %s", sub.get("task_id"), exc)
        return False


def _fan_out_open(sub: dict, board: Optional[str]) -> bool:
    from hermes_cli import kanban_db_connect

    conn = kanban_db_connect.connect(board=board)
    try:
        params = (
            sub["task_id"], FOLD_ANCESTOR_DEPTH,
            str(sub.get("platform") or "").lower(), sub["chat_id"], sub.get("thread_id") or "",
        )
        return conn.execute(FANNED_OUT_ANCESTOR_SQL, params).fetchone() is not None
    finally:
        conn.close()


async def _folds(sub: dict, board: Optional[str]) -> bool:
    """Whether the report of a card beneath a fan-out folds into its plan row: see :data:`FOLDED_KIND`.

    A read that fails posts the report, as it did before the plan.
    """
    try:
        return await asyncio.to_thread(_fan_out_open, sub, board)
    except Exception as exc:  # noqa: BLE001 — fail towards posting the report
        logger.debug("kanban progress: reading the fan-out above %s failed: %s", sub.get("task_id"), exc)
        return False


def _slack_moments(quiet: Any) -> Any:
    """``slack_ux_moments`` when ``quiet`` says ``KAGE_SLACK_UX`` is on for this Slack card.

    Imported when a delivery runs, for the reason :func:`_slack_quiet` imports
    its module then; an image without it reads as flag off.
    """
    if quiet is None:
        return None
    try:
        from gateway import slack_ux_moments
    except ImportError:
        return None
    try:
        return slack_ux_moments if slack_ux_moments.enabled() else None
    except Exception as exc:  # noqa: BLE001 — presentation must not fail a delivery
        logger.debug("kanban progress: reading slack_ux_moments failed: %s", exc)
        return None


async def _needs_you(moments: Any, adapter: Any, sub: dict, ev: Any) -> bool:
    try:
        return bool(await moments.needs_you(
            adapter, sub, getattr(ev, "payload", None), int(getattr(ev, "id", 0) or 0),
        ))
    except Exception as exc:  # noqa: BLE001 — fall back to the blocked line
        logger.debug("kanban progress: the question for %s failed: %s", sub.get("task_id"), exc)
        return False


def _asked(moments: Any, sub: dict, event_id: int) -> bool:
    """Whether the card's open question was posted for this event: a replay of its ``blocked``."""
    if moments is None or not event_id:
        return False
    try:
        return bool(moments.asked(sub, event_id))
    except Exception as exc:  # noqa: BLE001 — fail towards settling and posting as before
        logger.debug("kanban progress: reading the question for %s failed: %s", sub.get("task_id"), exc)
        return False


async def _settle_question(moments: Any, adapter: Any, sub: dict, kind: str, event_id: int = 0) -> None:
    """Take the buttons off the card's open question: any event moves it on, and ``kind``
    says whether that event means it was answered. A new question posts after this."""
    if moments is None:
        return
    try:
        await moments.settle_question(adapter, sub, kind, event_id)
    except Exception as exc:  # noqa: BLE001 — cosmetic; the card has moved on
        logger.debug("kanban progress: settling the question for %s failed: %s", sub.get("task_id"), exc)


async def _pr_opened(moments: Any, adapter: Any, sub: dict, text: str, result: Any) -> None:
    if moments is None or getattr(result, "success", True) is False:
        return
    try:
        await moments.pr_opened(adapter, sub, text)
    except Exception as exc:  # noqa: BLE001 — the line or report already went out
        logger.debug("kanban progress: the PR message for %s failed: %s", sub.get("task_id"), exc)


def _replayed(notification: Any, ev: Any) -> bool:
    """Whether a silent event is at or behind the card's recorded ``last_ping_event_id``."""
    event_id = int(getattr(ev, "id", 0) or 0)
    return bool(event_id) and event_id <= int(notification.sub.get("last_ping_event_id") or 0)


def _overtaken(notification: Any, ev: Any) -> bool:
    """Whether a silent event is a redelivery, or one a later event in its batch settles past.

    A delivery that fails after its pings (a wake not accepted, a wake or a
    send that raised) rewinds the claim, and the batch is delivered again.
    Upstream skips a ping already sent by ``last_ping_event_id``, but a silent
    kind is never recorded there, so a replayed ``unblocked`` would undo the
    ``blocked`` after it: the row running and Working… held while the card
    waits on you. A later event that settles the card in the same batch
    (:data:`SETTLING_KINDS`) wins even when recording its ping failed and
    ``last_ping_event_id`` lags it; a note or a retried failure does not.
    """
    event_id = int(getattr(ev, "id", 0) or 0)
    if _replayed(notification, ev):
        return True
    batch = getattr(notification, "d", None)
    events = batch.get("events") if isinstance(batch, dict) else None
    return any(
        int(getattr(later, "id", 0) or 0) > event_id
        and str(getattr(later, "kind", "") or "") in SETTLING_KINDS
        for later in events or ()
    )


async def silent_event(notification: Any, ev: Any) -> None:
    """Move a Slack card's plan row on a kind upstream keeps silent.

    Called by ``_send_pings`` for an event whose ``format_event`` returned
    ``None``, before it skips the event. :func:`deliver` never sees these, so
    without this a card archived by hand would hold its row running, and the
    thread's Working…, and an unblocked card would stay waiting on you, with
    its question's buttons still live, until its next note, and its ask would
    keep its ⏸️ (``gateway/slack_ux_reactions.py``). Only
    :data:`SILENT_PLAN_KINDS`, never one replayed or overtaken
    (:func:`_overtaken`), only with ``KAGE_SLACK_UX`` on for a Slack card,
    and never raises: it runs inside the send loop. An ``unblocked`` overtaken
    in its batch, not replayed, still settles the question it answered, which
    the later event would settle as unanswered; one posted for that later
    event is newer than the unblock and left alone.
    """
    try:
        kind = str(getattr(ev, "kind", "") or "")
        if kind not in SILENT_PLAN_KINDS:
            return
        overtaken = _overtaken(notification, ev)
        if overtaken and (kind != UNBLOCKED_KIND or _replayed(notification, ev)):
            return
        sub = notification.sub
        adapter = getattr(notification, "adapter", None)
        quiet = _slack_quiet(sub)
        plan = _slack_plan(quiet)
        moments = _slack_moments(quiet)
    except Exception as exc:  # noqa: BLE001 — never fail a delivery on the plan
        logger.debug("kanban progress: reading a silent event failed: %s", exc)
        return
    if adapter is None:
        return
    if plan is not None and not overtaken:
        await _settle_plan_row(plan, adapter, sub, kind)
    await _settle_question(moments, adapter, sub, kind, int(getattr(ev, "id", 0) or 0))
    # An unblocked card no longer waits on the user, so its ask loses its pause.
    await _settle_reaction(adapter, sub, kind, getattr(notification, "board_slug", None))


def _explained_by_wake(quiet: Any, sub: dict, kind: str) -> bool:
    if quiet is None:
        return False
    try:
        return bool(quiet.explained_by_wake(sub, kind))
    except Exception as exc:  # noqa: BLE001 — fail towards posting the line
        logger.debug(
            "kanban progress: explained_by_wake for %s failed: %s", sub.get("task_id"), exc,
        )
        return False


def _supersedes(kind: str, payload: object) -> bool:
    """Whether an event says the card recovered, so a failure it still holds is stale."""
    return kind in SUPERSEDING_KINDS or _moved_to(kind, payload) == SUPERSEDING_STATUS


def _moved_to(kind: str, payload: object) -> Optional[str]:
    """The column a ``status`` event moved the card to, ``""`` if it names none; None for another kind."""
    if kind != "status":
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    return str(status or "").strip()


def _drop_superseded(watcher: Any, sub: dict, kind: str, ev: Any, event_id: int) -> None:
    """Drop failure lines this card still holds from before ``event_id``, if it recovered."""
    quiet = _slack_quiet(sub) if event_id else None
    if quiet is None or not _supersedes(kind, getattr(ev, "payload", None)):
        return
    try:
        quiet.drop_superseded(watcher, sub, event_id)
    except Exception as exc:  # noqa: BLE001 — presentation must not fail a delivery
        logger.debug(
            "kanban progress: dropping held lines for %s failed: %s", sub.get("task_id"), exc,
        )


def _hold(
    quiet: Any, watcher: Any, sub: dict, kind: str, event_id: int,
    message: str, metadata: Optional[dict],
) -> bool:
    try:
        quiet.hold_explained(watcher, sub, kind, event_id, message, metadata)
        return True
    except Exception as exc:  # noqa: BLE001 — fail towards posting the line
        logger.debug(
            "kanban progress: holding the %s line for %s failed: %s",
            kind, sub.get("task_id"), exc,
        )
        return False


async def deliver(
    watcher: Any,
    adapter: Any,
    sub: dict,
    kind: str,
    ev: Any,
    message: str,
    metadata: Optional[dict],
    header: str,
    board: Optional[str] = None,
    title: str = "",
) -> Any:
    """Deliver one notifier event, rolling progress into a single message.

    Drop-in for the ``await adapter.send(...)`` the notifier used to call, and
    returns what that call site expects: the adapter's ``SendResult``, or
    ``None`` on the suppressed-replay path (the notifier reads
    ``getattr(res, "success", True)``, so ``None`` means "delivered").

    Terminal events are unchanged from the caller's point of view — a new
    message, with the artifact upload and failure accounting that follow it
    untouched. Two things are added on that path, both best-effort: settling
    the rolling message first, and, once the terminal message has posted,
    the ``KAGE_SLACK_UX`` settle reaction on the ask
    (``slack_ux_reactions.settle_delegated``). Neither may reach the notifier's
    ``except``, where a failed cosmetic call would rewind the cursor and count
    against the subscription's send-failure budget.

    That is the flag-off behaviour. With ``KAGE_SLACK_UX`` on and a Slack card,
    the terminal path is quieter in three ways. The rolling message settles to its
    last line rather than the whole trail. And a failure the creator's wake will
    explain is held rather than posted, returning ``None`` like the replay path;
    the notifier's wake step drops it once the wake is admitted for the kind, and
    posts it if the wake raises or never covers the kind, so a failed wake does
    not leave the failure untold. A line that cannot be held is posted. And an
    event saying the card recovered (:func:`_supersedes`) drops a failure line
    it still holds, so the line never posts beneath the recovery. Section 6 of
    ``gateway/kanban_notifier.py`` has the retry and the gaps it leaves. Beyond
    the terminal path, the card's progress goes on its row in the thread's plan rather than in a
    rolling message of its own, with the rolling message as the fallback when
    the plan cannot be posted; ``title`` is the card's, which the row leads with
    in a plan of several or falls back to. See
    ``gateway/slack_ux_status.py``. A card beneath a fan-out still open on the
    same thread completes into its row and posts nothing, returning
    ``None``, once the plan shows that row complete (:data:`FOLDED_KIND`).
    Every line it posts, holds or edits keeps
    the ``@assignee`` and drops the board tag and ``Kanban <id>``
    (:func:`slack_line`). A card blocked on ``needs_input``
    posts its question instead of the blocked line, once however often the
    notifier replays the event, and loses that question's buttons at its next
    event; a note or report saying a PR was opened is
    followed by that PR as a message of its own. See
    ``gateway/slack_ux_moments.py``.
    """
    chat_id = sub["chat_id"]
    tracked = tracked_messages(watcher)
    key = sub_key(sub)
    entry = tracked.get(key)
    event_id = int(getattr(ev, "id", 0) or 0)
    _drop_superseded(watcher, sub, kind, ev, event_id)
    quiet = _slack_quiet(sub)
    header, message = _slack_heads(quiet, sub, header, board, message)

    if kind not in ROLLING_KINDS:
        plan = _slack_plan(quiet)
        shown = False
        if plan is not None:
            result = result_line(kind, getattr(ev, "payload", None))
            shown = await _settle_plan_row(plan, adapter, sub, kind, result, title)
        if entry and entry["message_id"] and entry["lines"]:
            # A queued line says the card is waiting; once it settles that is
            # stale, so the settled message keeps only the card's own notes.
            notes = [line for line in entry["lines"] if line not in entry.get(QUEUED_LINES_KEY, ())]
            settled = notes[-1:] if quiet else notes
            try:
                await adapter.edit_message(
                    chat_id,
                    entry["message_id"],
                    render(header, settled, settled_marker(kind)),
                )
            except Exception as exc:
                logger.debug(
                    "kanban progress: could not settle the rolling message "
                    "for %s: %s", sub.get("task_id"), exc,
                )
        tracked.pop(key, None)
        moments = _slack_moments(quiet)
        if kind == NEEDS_YOU_KIND and _asked(moments, sub, event_id):
            # An at-least-once replay of the block whose question is up: settling
            # it would take its buttons off and posting would repeat it.
            return None
        await _settle_question(moments, adapter, sub, kind, event_id)
        if kind == NEEDS_YOU_KIND and moments is not None and await _needs_you(moments, adapter, sub, ev):
            await _settle_reaction(adapter, sub, kind, board)
            return None
        if kind == FOLDED_KIND and shown and await _folds(sub, board):
            # The row says it; the creator's completion carries the answer.
            await _settle_reaction(adapter, sub, kind, board)
            await _pr_opened(moments, adapter, sub, message, None)
            return None
        if _explained_by_wake(quiet, sub, kind) and _hold(
            quiet, watcher, sub, kind, event_id, message, metadata,
        ):
            await _settle_reaction(adapter, sub, kind, board)
            return None
        result = await adapter.send(chat_id, message, metadata=metadata)
        # A failed post raises in the notifier, which rewinds its claim so the
        # next tick posts again, and that post settles. After the notifier's
        # MAX_SEND_FAILURES it drops the subscription instead, and the ask
        # never settles.
        if getattr(result, "success", True) is not False:
            await _settle_reaction(adapter, sub, kind, board)
        if kind == PR_REPORT_KIND:
            await _pr_opened(moments, adapter, sub, message, result)
        return result

    payload = getattr(ev, "payload", None)
    line = rolling_line(kind, payload) or message
    if kind == QUEUED_KIND and not entry:
        header = ""
    moments = _slack_moments(quiet)
    await _settle_question(moments, adapter, sub, kind, event_id)
    result = await _roll(
        adapter, sub, metadata, header, title, line, _moved_to(kind, payload),
        _slack_plan(quiet), event_id, tracked,
    )
    if kind == QUEUED_KIND and tracked.get(key):
        tracked[key].setdefault(QUEUED_LINES_KEY, []).append(line)
    # The whole note, not the clipped line: a url past the clip is cut whole.
    note = progress_note(payload, limit=0) if kind == "heartbeat" else ""
    await _pr_opened(moments, adapter, sub, note or line, result)
    return result


async def _roll(
    adapter: Any, sub: dict, metadata: Optional[dict], header: str, title: str,
    line: str, moved: Any, plan: Any, event_id: int, tracked: dict,
) -> Any:
    """Put one progress line on the card's plan row, or roll it into its message."""
    chat_id = sub["chat_id"]
    key = sub_key(sub)
    entry = tracked.get(key)
    if plan is not None and await _plan_row(plan, adapter, sub, event_id, title, line, moved):
        if moved is None and entry and entry["message_id"] and entry["lines"]:
            # The plan took over a card that rolled while it stood fallen back. A
            # move it took may have been dropped, so only a note hands over.
            try:
                await adapter.edit_message(
                    chat_id, entry["message_id"], render(header, entry["lines"][-1:], MOVED_TO_PLAN),
                )
            except Exception as exc:
                logger.debug(
                    "kanban progress: could not settle the rolling message "
                    "for %s: %s", sub.get("task_id"), exc,
                )
            tracked.pop(key, None)
        return None
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
