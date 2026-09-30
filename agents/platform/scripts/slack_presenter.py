"""Slack presentation for kube-agents: answer layout, buttons and reactions.

Pure functions only. Nothing here imports the Hermes gateway, the Slack SDK or
the network, so any process that posts to Slack can use it, and it can move
with Slack ingress when it leaves the gateway. Today its callers are three
gateway patches: reactions (``slack_ux_reactions``, which the kanban notifier
also reaches), incident triage (``slack_ux_incident``) and button clicks
(``slack_ux_clicks``).
Every caller reaches it through ``PYTHONPATH=/opt/defaults/scripts``, which the
operator sets on the agent container.

Everything a caller changes on screen is gated on :func:`enabled`, the
``KAGE_SLACK_UX`` environment variable, off by default. With it off, callers
take their upstream path unchanged; this module only answers questions.

Layout: :func:`split_answer` takes the headline off an agent's markdown
answer; url link buttons and choice buttons, whose value is the label, are
built by ``_button`` and wrapped into rows by ``_actions``; :func:`fallback_text`
is the headline and choices as plain mrkdwn, for the message's ``text`` field.

Reactions (:func:`arrival_reaction`, :func:`settle_reaction`): the first
reaction says what kind of ask arrived, chosen by keyword before any model
call; a second joins it when the work settles. The first is never removed; the
credential proxy refuses every Slack method ending in ``remove``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: The flag, and the values that turn it on. Anything else (unset included) is off.
FLAG_ENV = "KAGE_SLACK_UX"
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

# --- reactions -------------------------------------------------------------

#: Slack emoji names (``reactions.add`` takes the name, not the glyph).
REACTION_QUESTION = "eyes"  # 👀
REACTION_CHANGE = "hammer_and_wrench"  # 🛠️
REACTION_BOARD = "clipboard"  # 📋
REACTION_INCIDENT = "rotating_light"  # 🚨
REACTION_DONE = "white_check_mark"  # ✅
REACTION_BLOCKED = "double_vertical_bar"  # ⏸️
REACTION_FAILED = "x"  # ❌

#: How work can settle, as the callers name it.
SETTLE_DONE = "done"
SETTLE_BLOCKED = "blocked"
SETTLE_FAILED = "failed"
SETTLE_REACTIONS = {
    SETTLE_DONE: REACTION_DONE,
    SETTLE_BLOCKED: REACTION_BLOCKED,
    SETTLE_FAILED: REACTION_FAILED,
}

#: Kanban notifier event kinds that settle delegated work. ``crashed`` and
#: ``timed_out`` are absent on purpose: the dispatcher retries both, so the work
#: has not settled. ``block_loop_detected`` sends the card back to triage, where
#: it can run again, so it settles provisionally, like ``blocked``. Bookkeeping
#: kinds (``status``, ``archived``, ``unblocked``) settle nothing.
SETTLE_BY_KANBAN_KIND = {
    "completed": SETTLE_DONE,
    "blocked": SETTLE_BLOCKED,
    "review_requested": SETTLE_BLOCKED,
    "changes_requested": SETTLE_BLOCKED,
    "gave_up": SETTLE_FAILED,
    "block_loop_detected": SETTLE_BLOCKED,
}

#: Settle outcomes after which a later settle can still follow on the same ask:
#: a blocked card is answered and runs on to done.
PROVISIONAL_SETTLES = frozenset({SETTLE_BLOCKED})

#: Keyword classes, tried in this order; the first match wins. A change request
#: outranks everything because it is the ask with consequences. A question
#: outranks the incident words, so "is checkout crashlooping?" is 👀 (a check),
#: while "checkout is down" is 🚨.
CHANGE_WORDS = re.compile(
    r"\bfix(es|ing)?\b"
    r"|\bbump\b"
    r"|\broll(ing)?[\s-]*back\b|\brollback\b"
    r"|\bopen\s+(a\s+|the\s+|an\s+)?(pr|pull\s+request)\b"
    r"|\bscale\b"
    r"|\bupgrade\b",
    re.IGNORECASE,
)
BOARD_WORDS = re.compile(
    r"\bboard\b|\bstatus\b|\bwhat'?s\s+running\b|\bwhat\s+is\s+running\b",
    re.IGNORECASE,
)
QUESTION_OPENERS = frozenset(
    {
        "is", "are", "was", "were", "why", "what", "whats", "what's", "how", "which",
        "who", "when", "where", "does", "do", "did", "can", "could", "should", "will",
        "would", "has", "have", "check", "show", "list", "tell", "explain", "describe",
    }
)
QUESTION_MARK = "?"
INCIDENT_WORDS = re.compile(
    r"\bdown\b|\bpag(e|ed|es|ing)\b|\boutage\b|\bsev\s?\d\b|\bsev\b|\bcrash\s?loop",
    re.IGNORECASE,
)
#: A Slack user or channel mention, stripped before the opener check so
#: "<@U123> is it down?" still reads as a question.
MENTION = re.compile(r"<[@#!][^>]*>")
FIRST_WORD = re.compile(r"[A-Za-z']+")

# --- layout ----------------------------------------------------------------

#: Slack's limits: button label, button value, buttons shown per actions block
#: before mobile wraps badly (Slack allows 25; Hermes uses 5).
BUTTON_TEXT_MAX = 75
BUTTON_VALUE_MAX = 2000
BUTTONS_PER_ROW = 5
HEADLINE_MAX = 150
ELLIPSIS = "…"

#: Action ids: ``<prefix>.link.<n>`` and ``<prefix>.choice.<n>``. Link buttons
#: open their url client-side and Slack still sends a block_actions request,
#: which the no-op handler acknowledges; a choice click is answered as the
#: clicker's reply (the gateway's ``slack_ux_clicks``).
LINK_ACTION = "link"
CHOICE_ACTION = "choice"
LINK_ACTION_ID_PATTERN = re.compile(r"\.link\.\d+$")
CHOICE_ACTION_ID_PATTERN = re.compile(r"\.choice\.\d+$")

CHOICES_LEAD = "Reply with one of: "
CHOICE_SEPARATOR = " · "

CODE_FENCE = "```"
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
MD_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
MRKDWN_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on in this process's environment."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES


# --- reactions -------------------------------------------------------------


def _is_question(text: str) -> bool:
    if text.rstrip().endswith(QUESTION_MARK):
        return True
    first = FIRST_WORD.search(text)
    return bool(first) and first.group(0).lower() in QUESTION_OPENERS


def arrival_reaction(text: str | None) -> str:
    """The emoji name for an ask, from its words alone (no model call)."""
    clean = MENTION.sub(" ", text or "").strip()
    if CHANGE_WORDS.search(clean):
        return REACTION_CHANGE
    if BOARD_WORDS.search(clean):
        return REACTION_BOARD
    if _is_question(clean):
        return REACTION_QUESTION
    if INCIDENT_WORDS.search(clean):
        return REACTION_INCIDENT
    return REACTION_QUESTION


def settle_reaction(outcome: str) -> str | None:
    """The emoji name for ``done``/``blocked``/``failed``; None for anything else."""
    return SETTLE_REACTIONS.get(outcome)


def settle_for_kanban_kind(kind: str) -> str | None:
    """``done``/``blocked``/``failed`` for a notifier event kind that settles work, else None."""
    return SETTLE_BY_KANBAN_KIND.get(kind)


# --- markdown --------------------------------------------------------------


def _paragraphs(markdown: str) -> list[str]:
    """Blank-line separated blocks, never splitting inside a fenced code block."""
    out: list[str] = []
    current: list[str] = []
    in_fence = False
    for line in markdown.split("\n"):
        if line.strip().startswith(CODE_FENCE):
            in_fence = not in_fence
        if not in_fence and not line.strip():
            if current:
                out.append("\n".join(current).strip("\n"))
                current = []
            continue
        current.append(line)
    if current:
        out.append("\n".join(current).strip("\n"))
    return [p for p in out if p.strip()]


def _plain(markdown: str) -> str:
    """One line of markdown as plain text: no heading, list marker, emphasis or link syntax."""
    text = HEADING.sub("", markdown.strip())
    text = LIST_MARKER.sub("", text)
    text = MD_LINK.sub(r"\1", text)
    text = MD_BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    return text.replace("*", "").replace("`", "").strip()


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - len(ELLIPSIS)].rsplit(" ", 1)[0] or text[: limit - len(ELLIPSIS)]
    return cut.rstrip() + ELLIPSIS


def _escape(text: str) -> str:
    for raw, escaped in MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def split_answer(markdown: str) -> tuple[str, list[str]]:
    """``(headline, body_sections)`` for an agent's markdown answer.

    The headline is the first sentence of the first paragraph, as plain text
    capped at ``HEADLINE_MAX``. The rest of that paragraph and every later
    paragraph are the body sections, still markdown, in order. Empty input
    gives ``("", [])``.
    """
    paragraphs = _paragraphs(markdown or "")
    if not paragraphs:
        return "", []
    first, rest = paragraphs[0], paragraphs[1:]
    if first.lstrip().startswith(CODE_FENCE):
        return "", paragraphs
    first_line, _, more_lines = first.partition("\n")
    sentences = SENTENCE_END.split(first_line.strip(), maxsplit=1)
    headline = _clip(_plain(sentences[0]), HEADLINE_MAX)
    remainder = " ".join(s for s in sentences[1:]).strip()
    tail = "\n".join(part for part in (remainder, more_lines.strip("\n")) if part)
    body = ([tail] if tail.strip() else []) + rest
    return headline, body


# --- blocks ----------------------------------------------------------------


def _link_pairs(links: Iterable[Any]) -> list[tuple[str, str]]:
    """``(label, url)`` from tuples or ``{"text", "url"}`` mappings; entries without a url dropped."""
    pairs = []
    for link in links or ():
        if isinstance(link, Mapping):
            label, url = link.get("text") or link.get("label") or "", link.get("url") or ""
        else:
            label, url = link
        if url:
            pairs.append((str(label or url), str(url)))
    return pairs


def _button(label: str, action_id: str, *, url: str | None = None, value: str | None = None) -> dict:
    button: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": _clip(label, BUTTON_TEXT_MAX), "emoji": True},
        "action_id": action_id,
    }
    if url is not None:
        button["url"] = url
    if value is not None:
        button["value"] = value[:BUTTON_VALUE_MAX]
    return button


def _actions(buttons: Sequence[dict]) -> list[dict]:
    return [
        {"type": "actions", "elements": list(buttons[i : i + BUTTONS_PER_ROW])}
        for i in range(0, len(buttons), BUTTONS_PER_ROW)
    ]


def fallback_text(headline: str, links: Iterable[Any] = (), choices: Iterable[str] = ()) -> str:
    """The same layout as plain mrkdwn, with no buttons.

    Used as the ``text`` of a blocks message (notifications, screen readers).
    Links become inline ``<url|label>``; choices become one "Reply with one
    of:" line. Every label is escaped, so none can mention anyone.
    """
    parts: list[str] = []
    title = _clip(_plain(headline or ""), HEADLINE_MAX)
    if title:
        parts.append(f"*{_escape(title)}*")
    pairs = _link_pairs(links)
    if pairs:
        parts.append(CHOICE_SEPARATOR.join(f"<{url}|{_escape(label)}>" for label, url in pairs))
    labels = [_escape(str(c)) for c in choices or () if str(c).strip()]
    if labels:
        parts.append(CHOICES_LEAD + CHOICE_SEPARATOR.join(labels))
    return "\n".join(parts)


# --- link-button ack -------------------------------------------------------


async def ack_link_click(ack: Any, body: Any = None, action: Any = None) -> None:
    """Acknowledge a link-button click and do nothing else; Slack already opened the url."""
    await ack()
