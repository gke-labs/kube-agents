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
``pending``. A row's title says where the card is (:func:`row_title`): its
latest note while it runs (with a step count past one) or after it failed,
its one-line result once complete, "waiting on you" while pending, and the
card's title when there is none of those; in a plan of several rows it leads
with the card's title, so the rows stay told apart. The plan carries no Stop
button yet: ``/stop`` interrupts only the Planning Agent's turn and would leave
the cards running. :func:`plan_text` is the same plan as plain text, for the
message's ``text`` field, with ``&``, ``<`` and ``>`` escaped since Slack
parses that field.

The session (:func:`session_status`, :func:`session_title`):
``agents.sessions.setStatus`` accepts ``processing``, ``suspended`` or
``closed`` and refuses free text with ``invalid_arguments``, which is what
Hermes's thread status sends it. ``agents.sessions.rename`` refuses a whole
title with ``invalid_name`` when it runs over 80 characters or holds a single
character outside a narrow set: letters, digits, combining marks, spaces, a
little ASCII punctuation and the common emoji blocks. ``:``, ``/``, ``#``,
``%``, the ellipsis, every dash but ``-`` and compatibility forms such as
``²`` or ``ﬁ`` are refused.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

# --- the session -----------------------------------------------------------

#: ``agents.sessions.setStatus`` values. ``processing`` shows Slack's Working…;
#: ``closed`` clears it.
SESSION_PROCESSING = "processing"
SESSION_SUSPENDED = "suspended"
SESSION_CLOSED = "closed"
SESSION_STATUSES = frozenset({SESSION_PROCESSING, SESSION_SUSPENDED, SESSION_CLOSED})

#: ``agents.sessions.rename`` limits, measured against Slack: a letter, a
#: decimal digit or a combining mark in any script passes, as does a symbol in
#: :data:`TITLE_EMOJI`, and of everything else only :data:`TITLE_KEPT`. The
#: limit counts code points. A letter with a compatibility decomposition (``ª``,
#: ``ﬁ``, ``𝐀``, Thai ``ำ``) is refused, so the title is NFKC-normalized first,
#: which also turns ``…`` into ``...`` and ``²`` into ``2``. A refused character
#: an ask is likely to hold becomes an ASCII stand-in from
#: :data:`TITLE_REPLACEMENTS`; a format character such as a zero-width joiner
#: is dropped, and any other becomes a space.
#: A slash between two word characters (:func:`_word_char`) becomes a hyphen, so
#: ``kube-system/coredns`` stays one name rather than two alternatives; one that
#: leads a path like ``/metrics`` becomes a space. A combining mark goes with
#: its base, so one on a refused character is dropped. A clipped title ends on
#: :data:`TITLE_ELLIPSIS`, since "…" is refused.
TITLE_MAX = 80
TITLE_KEPT = frozenset(" -'\"&|_!?.,()=“”")
TITLE_REPLACEMENTS = {
    ":": ",",
    ";": ",",
    "·": ",",
    "•": ",",
    "‒": " - ",
    "–": " - ",
    "—": " - ",
    "―": " - ",
    "−": "-",
    "‐": "-",
    "‘": "'",
    "’": "'",
    "[": "(",
    "]": ")",
    "{": "(",
    "}": ")",
    "%": " percent",
    "×": "x",
    "、": ",",
    "。": ".",
    "«": '"',
    "»": '"',
}
#: The symbol blocks Slack accepts, as inclusive code point ranges: Miscellaneous
#: Symbols and Dingbats (``⚠``, ``✅``), and the emoji planes from Miscellaneous
#: Symbols and Pictographs to Symbols and Pictographs Extended-A (``😀``, skin
#: tones). Regional indicators (flags) sit below them and are refused, as are
#: ``⭐``, ``⏰`` and ``🀄``.
TITLE_EMOJI = ((0x2600, 0x27BF), (0x1F300, 0x1FAFF))
TITLE_EMOJI_CATEGORIES = frozenset({"So", "Sk"})
TITLE_ELLIPSIS = "..."
ELLIPSIS = "…"
#: A word-aligned clip shorter than this share of the limit drops too much; it cuts hard instead.
CLIP_MIN_SHARE = 2

#: Slack markup in an ask: a user or channel mention, and a link with or
#: without its label. Mentions go; a channel keeps its name, a link its label,
#: and a bare link goes too, since every ``:`` and ``/`` in it would be replaced.
#: No set takes ``<``: Slack never nests it, and a set that did would rescan to the
#: end of the ask from every unclosed ``<``, quadratic on the gateway's event loop.
MENTION = re.compile(r"<[@!][^<>]*>")
CHANNEL = re.compile(r"<#[A-Z0-9]+\|([^<>]*)>")
LINK = re.compile(r"<([^<>|]+)(?:\|([^<>]*))?>")
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
#: A running row's step count, after its latest note, once it has more than one.
STEP_COUNT = "step {count} ▸"
#: A pending row's title: the card asked the user something, or its review waits on them.
WAITING_ON_YOU = "waiting on you"
#: What the text fallback escapes, ``&`` first, as Hermes's ``format_message``
#: does: Slack parses a message's ``text``, so a card title or note holding
#: ``<!here>`` would broadcast and ``<url|label>`` would post a link under any label.
TEXT_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

#: Size caps. Titles as Hermes clips its own task cards; the last few steps
#: per row, since the row is a status and the report carries the rest; a
#: bound on rows so a runaway fan-out cannot outgrow the message.
PLAN_TITLE_MAX = 256
ROW_TITLE_MAX = 256
#: A card's title ahead of its state in a plan of several, so a long one
#: cannot crowd the state out of the row.
ROW_NAME_MAX = 80
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


def _clip(text: str, limit: int, ellipsis: str = ELLIPSIS) -> str:
    """``text`` cut on a word to ``limit`` characters, ``ellipsis`` included."""
    if len(text) <= limit:
        return text
    hard = text[: limit - len(ellipsis)]
    cut = hard.rsplit(" ", 1)[0]
    # A long unbroken word would otherwise take everything after the last space with it.
    if len(cut) * CLIP_MIN_SHARE < len(hard):
        cut = hard
    return cut.rstrip(" ,") + ellipsis


def _word_char(char: str) -> bool:
    """Whether ``char`` is part of a word: a letter, a decimal digit, ``_`` or a combining mark."""
    return char.isalpha() or char.isdecimal() or char == "_" or unicodedata.category(char).startswith("M")


def _title_char(char: str) -> str:
    """``char`` if ``agents.sessions.rename`` accepts it, else its stand-in, nothing or a space."""
    category = unicodedata.category(char)
    if _word_char(char) or char in TITLE_KEPT:
        return char
    if category in TITLE_EMOJI_CATEGORIES and any(low <= ord(char) <= high for low, high in TITLE_EMOJI):
        return char
    return TITLE_REPLACEMENTS.get(char, "" if category == "Cf" else " ")


def _title_chars(title: str) -> str:
    """``title`` with each character through :func:`_title_char`, a slash between words joined.

    A combining mark is kept only on a letter, digit or emoji kept as itself, the bases Slack was
    measured to accept one on; on any other it is dropped with its base.
    """
    out: list[str] = []
    marks_kept = after_word = False
    for i, char in enumerate(title):
        if unicodedata.category(char).startswith("M"):
            out.append(char if marks_kept else "")
            continue
        joins = char == "/" and after_word and i + 1 < len(title) and _word_char(title[i + 1])
        stand_in = "-" if joins else _title_char(char)
        after_word = stand_in == char and _word_char(char)
        marks_kept = stand_in == char and char not in TITLE_KEPT
        out.append(stand_in)
    return "".join(out)


def session_title(text: Any) -> str:
    """A title ``agents.sessions.rename`` accepts, from the ask's words, or ``""``.

    Mentions and bare links are dropped, a channel keeps its name and a link its label, each
    refused character is replaced, and the result is clipped on a word to :data:`TITLE_MAX`,
    ending on :data:`TITLE_ELLIPSIS`. NFKC first, so a compatibility form becomes the plain
    character Slack accepts and an accent typed as a combining mark joins its letter.
    """
    title = MENTION.sub(" ", str(text or ""))
    title = CHANNEL.sub(r"\1", title)
    title = LINK.sub(lambda m: m.group(2) or " ", title)
    title = unicodedata.normalize("NFKC", title)
    title = _title_chars(title)
    title = WHITESPACE.sub(" ", title)
    title = REPEATED_COMMA.sub(", ", title).strip(" ,")
    return _clip(title, TITLE_MAX, TITLE_ELLIPSIS)


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
    """One plan row: its title and status, the card's notes as steps in ``details``."""
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


def _lead(row: Any) -> tuple[str, str]:
    """Where the card is and its step count, for its row's title; ``""`` for either it lacks.

    The row's ``note`` says which line is where the card is, so a line that
    only moved the card is never taken for it, whatever its wording.
    """
    if row.status == TASK_PENDING:
        return WAITING_ON_YOU, ""
    if row.status == TASK_COMPLETE:
        return str(getattr(row, "result", "") or "").strip(), ""
    note = _clip(str(getattr(row, "note", "") or "").strip(), STEP_TEXT_MAX)
    count = int(getattr(row, "steps", 0) or 0)
    if note and row.status == TASK_RUNNING and count > 1:
        return note, STEP_COUNT.format(count=count)
    return note, ""


def row_title(row: Any, several: bool = False) -> str:
    """A row's title: where its card is, after the card's title in a plan of ``several`` rows.

    A step count is kept whole when the note is clipped, and the card's title
    is clipped to :data:`ROW_NAME_MAX` ahead of it. With nothing to say the row
    shows the card's title, or its id when that is blank.
    """
    name = str(row.title or "").strip() or str(row.task_id)
    lead, count = _lead(row)
    if not lead:
        return _clip(name, ROW_TITLE_MAX)
    if several:
        lead = _clip(name, ROW_NAME_MAX) + NOTE_SEPARATOR + lead
    if not count:
        return _clip(lead, ROW_TITLE_MAX)
    tail = NOTE_SEPARATOR + count
    return _clip(lead, ROW_TITLE_MAX - len(tail)) + tail


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
    and optionally ``result`` (a completed card's one line), ``steps`` (how
    many notes the card has given, past the :data:`STEPS_MAX` kept, moves not
    counted) and ``note`` (the line a running or failed row's title shows;
    without it the row shows the card's title), in the
    order the cards first reported; past :data:`ROWS_MAX` the oldest are dropped.
    """
    rows = list(rows)[-ROWS_MAX:]
    several = len(rows) > 1
    return [
        {
            "type": "plan",
            "title": plan_title(title, rows),
            "tasks": [task_card(r.task_id, row_title(r, several), r.lines, r.status) for r in rows],
        }
    ]


def plan_text(title: str | None, rows: Sequence[Any]) -> str:
    """The plan as plain lines: the title, then a marker and :func:`row_title` per row, escaped."""
    rows = list(rows)[-ROWS_MAX:]
    several = len(rows) > 1
    out = [plan_title(title, rows)]
    for row in rows:
        out.append(f"{ROW_MARKERS.get(row.status, STEP_OPEN)} {row_title(row, several)}")
    text = "\n".join(out)
    for raw, escaped in TEXT_ESCAPES:
        text = text.replace(raw, escaped)
    return text
