"""Slack status for kube-agents: the plan of a thread's cards, the session title and status.

Pure functions only, like ``slack_presenter``: nothing here imports the Hermes
gateway, the Slack SDK or the network, so the renderer can move with Slack
ingress when it leaves the gateway. Its caller is the gateway's status patch
(``slack_ux_status``), which the kanban notifier and the Slack adapter reach
with ``KAGE_SLACK_UX`` on. Nothing here reads the flag.

The plan (:func:`plan_blocks`): one message per thread holding a Block Kit
``plan`` with a ``task_card`` row per kanban card the thread follows. A row's
``details`` are the card's progress notes as steps, the last one open while
the card runs. Slack's task statuses are ``pending``, ``in_progress``,
``complete`` and ``error`` and nothing else, so a card waiting on the user is
``pending``. The plan carries no Stop button yet: ``/stop`` interrupts only
the Planning Agent's turn and would leave the cards running. :func:`plan_text`
is the same plan as plain text, for the message's ``text`` field.

The session (:func:`session_status`, :func:`session_title`):
``agents.sessions.setStatus`` accepts ``processing``, ``suspended`` or
``closed`` and refuses free text with ``invalid_arguments``, which is what
Hermes's thread status sends it. A title is at most 80 characters and
``agents.sessions.rename`` refuses ``:``, ``/`` and ``·``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

# --- the session -----------------------------------------------------------

#: ``agents.sessions.setStatus`` values. ``processing`` shows Slack's Working…;
#: ``closed`` clears it.
SESSION_PROCESSING = "processing"
SESSION_SUSPENDED = "suspended"
SESSION_CLOSED = "closed"
SESSION_STATUSES = frozenset({SESSION_PROCESSING, SESSION_SUSPENDED, SESSION_CLOSED})

#: ``agents.sessions.rename`` limits, and what each refused character becomes.
#: A slash becomes the division slash (U+2215), which reads the same, so
#: ``kube-system/coredns`` stays one name rather than two alternatives.
TITLE_MAX = 80
TITLE_REPLACEMENTS = ((":", ","), ("·", ","), ("/", "\u2215"))
ELLIPSIS = "…"

#: Slack markup in an ask: a user or channel mention, and a link with or
#: without its label. Mentions go; a channel keeps its name, a link its label,
#: and a bare link goes too, since every ``:`` and ``/`` in it would be replaced.
MENTION = re.compile(r"<[@!][^>]*>")
CHANNEL = re.compile(r"<#[A-Z0-9]+\|([^>]*)>")
LINK = re.compile(r"<([^>|]+)(?:\|([^>]*))?>")
WHITESPACE = re.compile(r"\s+")
REPEATED_COMMA = re.compile(r"\s*,[\s,]*")

# --- the plan --------------------------------------------------------------

#: Block Kit task statuses.
TASK_PENDING = "pending"
TASK_RUNNING = "in_progress"
TASK_COMPLETE = "complete"
TASK_ERROR = "error"
TASK_STATUSES = frozenset({TASK_PENDING, TASK_RUNNING, TASK_COMPLETE, TASK_ERROR})

#: Kanban notifier kinds, by the row status each leaves. A kind not listed
#: leaves the row as it was: ``crashed`` and ``timed_out``, which the
#: dispatcher retries, and ``archived`` and ``status`` (a dashboard move),
#: which the runtime handles itself (``gateway/slack_ux_status.py``).
#: ``block_loop_detected`` waits on the user, as
#: ``slack_presenter.SETTLE_BY_KANBAN_KIND`` reads it.
TASK_STATUS_BY_KIND = {
    "heartbeat": TASK_RUNNING,
    "completed": TASK_COMPLETE,
    "blocked": TASK_PENDING,
    "unblocked": TASK_RUNNING,
    "review_requested": TASK_PENDING,
    "changes_requested": TASK_PENDING,
    "gave_up": TASK_ERROR,
    "block_loop_detected": TASK_PENDING,
}

#: Step markers inside a row: done, open. Row markers for the text fallback.
STEP_DONE = "✓"
STEP_OPEN = "◌"
ROW_MARKERS = {
    TASK_PENDING: "○",
    TASK_RUNNING: STEP_OPEN,
    TASK_COMPLETE: STEP_DONE,
    TASK_ERROR: "✗",
}
NOTE_SEPARATOR = " · "

#: Size caps. Titles as Hermes clips its own task cards; the last few steps
#: per row, since the row is a status and the report carries the rest; a
#: bound on rows so a runaway fan-out cannot outgrow the message.
PLAN_TITLE_MAX = 256
ROW_TITLE_MAX = 256
STEPS_MAX = 6
ROWS_MAX = 20
STEP_TEXT_MAX = 300

#: The plan's title when the caller gave none and more than one card reports.
CARDS_TITLE = "{count} cards"


def session_status(text: Any) -> str:
    """The ``agents.sessions.setStatus`` value for a Hermes thread status.

    Hermes sets a phrase ("is thinking...") while a turn runs and ``""`` to
    clear. A valid value passes through; empty is ``closed``; any other text
    is ``processing``.
    """
    status = str(text or "").strip()
    if status in SESSION_STATUSES:
        return status
    return SESSION_PROCESSING if status else SESSION_CLOSED


def _clip(text: str, limit: int) -> str:
    """``text`` cut on a word to ``limit`` characters, ellipsis included."""
    if len(text) <= limit:
        return text
    cut = text[: limit - len(ELLIPSIS)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,") + ELLIPSIS


def session_title(text: Any) -> str:
    """A title ``agents.sessions.rename`` accepts, from the ask's words, or ``""``.

    Mentions and bare links are dropped, a channel keeps its name and a link its label, the
    refused characters are replaced, and the result is clipped on a word to
    :data:`TITLE_MAX`.
    """
    title = MENTION.sub(" ", str(text or ""))
    title = CHANNEL.sub(r"#\1", title)
    title = LINK.sub(lambda m: m.group(2) or " ", title)
    for refused, replacement in TITLE_REPLACEMENTS:
        title = title.replace(refused, replacement)
    title = WHITESPACE.sub(" ", title)
    title = REPEATED_COMMA.sub(", ", title).strip(" ,")
    return _clip(title, TITLE_MAX)


def task_status(kind: str) -> str | None:
    """The row status a notifier event kind leaves, or None to leave the row alone."""
    return TASK_STATUS_BY_KIND.get(kind)


def _notes(lines: Iterable[Any]) -> list[str]:
    return [_clip(str(line).strip(), STEP_TEXT_MAX) for line in lines if str(line or "").strip()]


def _steps(lines: Sequence[str], status: str) -> list[str]:
    kept = _notes(lines)[-STEPS_MAX:]
    last = len(kept) - 1
    return [
        f"{STEP_OPEN if status == TASK_RUNNING and i == last else STEP_DONE} {line}"
        for i, line in enumerate(kept)
    ]


def task_card(task_id: str, title: str, lines: Sequence[str], status: str) -> dict:
    """One plan row: the card's title and status, its notes as steps in ``details``."""
    card: dict = {
        "type": "task_card",
        "task_id": str(task_id),
        "title": _clip(str(title or "").strip() or str(task_id), ROW_TITLE_MAX),
        "status": status if status in TASK_STATUSES else TASK_RUNNING,
    }
    steps = _steps(lines, card["status"])
    if steps:
        card["details"] = {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "elements": [
                        {"type": "rich_text_section", "elements": [{"type": "text", "text": step}]}
                        for step in steps
                    ],
                }
            ],
        }
    return card


def plan_title(title: str | None, rows: Sequence[Any]) -> str:
    """The plan's title: the one given, else the one card's title, else a count."""
    given = str(title or "").strip()
    if given:
        return _clip(given, PLAN_TITLE_MAX)
    if len(rows) == 1:
        return _clip(str(rows[0].title or "").strip() or str(rows[0].task_id), PLAN_TITLE_MAX)
    return CARDS_TITLE.format(count=len(rows))


def running(rows: Iterable[Any]) -> bool:
    """Whether any row is still in progress."""
    return any(row.status == TASK_RUNNING for row in rows)


def plan_blocks(title: str | None, rows: Sequence[Any]) -> list[dict]:
    """The status message's blocks: one ``plan``.

    ``rows`` are objects with ``task_id``, ``title``, ``lines`` and ``status``,
    in the order the cards first reported; past :data:`ROWS_MAX` the oldest
    are dropped.
    """
    rows = list(rows)[-ROWS_MAX:]
    return [
        {
            "type": "plan",
            "title": plan_title(title, rows),
            "tasks": [task_card(r.task_id, r.title, r.lines, r.status) for r in rows],
        }
    ]


def plan_text(title: str | None, rows: Sequence[Any]) -> str:
    """The plan as plain lines: the title, then a marker, title and latest note per row."""
    rows = list(rows)[-ROWS_MAX:]
    out = [plan_title(title, rows)]
    for row in rows:
        line = f"{ROW_MARKERS.get(row.status, STEP_OPEN)} {str(row.title or '').strip() or row.task_id}"
        notes = _notes(row.lines)
        if notes:
            line += NOTE_SEPARATOR + notes[-1]
        out.append(line)
    return "\n".join(out)
