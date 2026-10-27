"""The board-health card: the stuck tasks with Retry and Drop on each row.

``kanban_board_health.py`` says the stuck tasks in plain words and, when they
are all it says, leaves a card file beside its record: the headline, each row
as said with the card ids it stands for, and the text it was said with. The
relay (``session_kv_server.relay_cron_report``) reads it here for the job's
Slack leg and posts it in place of the text, so each row gets two buttons:

* under today, as Block Kit (:func:`blocks_from_card`) through the credential
  proxy's Slack relay; a tap reaches ``slack_ux_clicks.py``;
* under ``spec.mode: next``, as a ``kube-agents.chat/v1`` message
  (:func:`chat_from_card`) through the gateway, whose rows carry ``tasks`` and
  ``actions``; the gateway draws the buttons and takes the tap.

Either way the button's value is the row's ids, Retry unblocks them and Drop
archives them (``session_kv_server`` ``POST /v1/board/actions``, which touches
only a card still blocked), and the row is edited in place to say who did it
and when. Nothing here reads the board: a card file whose text is not the
report being relayed is stale, and the report posts as text.
"""

from __future__ import annotations

import json
from pathlib import Path

import chat_message as _chat
import kanban_board_health as _health

#: The two buttons, the same ids under both modes: ``slack_ux_clicks.py``
#: (today) and ``a2a/gateway/slack_board.go`` (next) match them.
ACTION_PREFIX = "kage_board"
ACTION_RETRY = "retry"
ACTION_DROP = "drop"
ACTIONS = (ACTION_RETRY, ACTION_DROP)
ACTION_ID = ACTION_PREFIX + ".{action}"
BUTTON_LABELS = {ACTION_RETRY: "Retry", ACTION_DROP: "Drop"}
#: The row's text and its buttons, by the row's index: a tap edits the row by
#: its buttons' block id.
ROW_TEXT_BLOCK_ID = ACTION_PREFIX + ".text.{index}"
ROW_BLOCK_ID = ACTION_PREFIX + ".row.{index}"
#: A button's value is the row's ids joined, and Slack caps a value at 2000
#: characters; ``POST /v1/board/actions`` takes at most 50 ids. A row past
#: either is drawn without buttons.
VALUE_SEPARATOR = ","
VALUE_MAX = 2000
ROW_TASKS_MAX = 50


def load(home: Path | str, report: str, env=None) -> dict | None:
    """The card file for ``report``, or None: missing, unreadable, another version, said with
    other text, or a row the builders cannot lay out (``_row_ok``)."""
    try:
        card = json.loads(_health.card_path(Path(home), env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(card, dict) or card.get(_health.STATE_VERSION_KEY) != _health.CARD_SCHEMA_VERSION:
        return None
    if str(card.get("text") or "").strip() != (report or "").strip():
        return None
    rows = card.get("rows")
    if not isinstance(card.get("headline"), str) or not card["headline"].strip():
        return None
    if not isinstance(rows, list) or not rows or not all(_row_ok(row) for row in rows):
        return None
    if not isinstance(card.get("more", ""), str):
        return None
    return card


def _row_ok(row) -> bool:
    """A row as ``kanban_board_health.stuck_rows`` writes it: a titled dict, a whole
    count, text lines, and a list of ids."""
    if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"].strip():
        return False
    count = row.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int):
        return False
    if not all(isinstance(row.get(key, ""), str) for key in ("status", "held")):
        return False
    tasks = row.get("tasks", [])
    return isinstance(tasks, list) and all(isinstance(task, str) for task in tasks)


def row_value(row: dict) -> str:
    """The row's ids as a button's value, or "" when the row gets no buttons."""
    tasks = [str(task) for task in row.get("tasks") or () if str(task)]
    value = VALUE_SEPARATOR.join(tasks)
    if not tasks or len(tasks) > ROW_TASKS_MAX or len(value) > VALUE_MAX:
        return ""
    return value


def _title(row: dict) -> tuple[str, str]:
    count = int(row.get("count") or 1)
    return str(row["title"]), _health.STUCK_GROUP_SUFFIX.format(count=count) if count > 1 else ""


def _rich_text(text: str, bold: bool = False) -> dict:
    element: dict = {"type": "text", "text": text}
    if bold:
        element["style"] = {"bold": True}
    return element


def _rich(elements: list[dict], block_id: str = "") -> dict:
    """A rich_text block: its text is shown as written, so a title can mention no one."""
    block: dict = {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": elements}]}
    if block_id:
        block["block_id"] = block_id
    return block


def row_buttons(index: int, value: str) -> dict:
    """The row's Retry and Drop, as one actions block keyed by the row."""
    return {
        "type": "actions",
        "block_id": ROW_BLOCK_ID.format(index=index),
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": BUTTON_LABELS[action], "emoji": True},
                "action_id": ACTION_ID.format(action=action),
                "value": value,
            }
            for action in ACTIONS
        ],
    }


def blocks_from_card(card: dict) -> tuple[list[dict], str]:
    """The card as Block Kit ``(blocks, text)``: the headline, then each row's text over its buttons."""
    blocks = [_rich([_rich_text(card["headline"], bold=True)])]
    for index, row in enumerate(card["rows"]):
        title, suffix = _title(row)
        elements = [_rich_text(title, bold=True)]
        if suffix:
            elements.append(_rich_text(suffix))
        for line in (row.get("status"), row.get("held")):
            if line:
                elements.append(_rich_text("\n" + str(line)))
        blocks.append(_rich(elements, ROW_TEXT_BLOCK_ID.format(index=index)))
        value = row_value(row)
        if value:
            blocks.append(row_buttons(index, value))
    if card.get("more"):
        blocks.append(_rich([_rich_text(str(card["more"]))]))
    return blocks, card["text"]


def chat_from_card(card: dict) -> tuple[dict, str]:
    """The card as a ``kube-agents.chat/v1`` message for ``spec.mode: next``: ``(chat, text)``.

    Each row's text is its title in bold, its detail the status and holding-back
    lines, and a row with buttons carries its ids as ``tasks`` and both
    ``actions``; the gateway labels the buttons itself. ``text`` is the report,
    the fallback the gateway posts when it does not lay the message out.
    """
    rows = []
    for row in card["rows"]:
        title, suffix = _title(row)
        entry: dict = {"text": _health.STUCK_ROW_TITLE.format(title=title) + suffix}
        detail = " ".join(str(line) for line in (row.get("status"), row.get("held")) if line)
        if detail:
            entry["detail"] = detail
        value = row_value(row)
        if value:
            entry["tasks"] = value.split(VALUE_SEPARATOR)
            entry["actions"] = list(ACTIONS)
        rows.append(entry)
    chat = _chat.message(headline=card["headline"], rows=rows, after=[card["more"]] if card.get("more") else [])
    return chat, card["text"]
