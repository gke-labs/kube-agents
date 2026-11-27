"""The findings-nudge card: surfaced issues, collapsible queue breakdown, and hidden thread context.

``findings_nudge.py`` writes a sidecar card file beside its daily state whenever
it prints a nudge, and removes the file when a tick prints nothing (a tick that
fails before it prints leaves the file, which no later report's text matches). The relay
(``session_kv_server.relay_cron_report``) loads the card here when its ``text``
matches the relayed report:

* On a card platform (Slack via the relay as Block Kit, or Slack and Google Chat
  through the A2A gateway as ``kube-agents.chat/v1``), the card replaces the
  plain-text post with an answer-first headline, clean finding rows, a cluster
  breakdown table for the rest of the queue (collapsed on Slack, an open
  monospace block on Google Chat), and action buttons.
* On every leg that lands, ``thread_context`` is stored in Session KV's
  ``incidents`` table instead of the bare text, so a reply or button click in
  that thread receives the finding ids and item coverage counts without showing
  plumbing in chat. A day's nudges share one thread, so the newest one's
  context keeps the earlier ones' items under its own
  (:func:`thread_context_with_earlier`).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import chat_message as _chat
import slack_presenter as _presenter

JOB_ID = "findings-morning-nudge"

CARD_FILE = ".findings_nudge_card.json"
CARD_TMP_SUFFIX = ".tmp"
CARD_SCHEMA_VERSION = 1

VERSION_KEY = "version"
TEXT_KEY = "text"
THREAD_CONTEXT_KEY = "thread_context"
HEADLINE_KEY = "headline"
NOTE_KEY = "note"
TONE_KEY = "tone"
ROWS_KEY = "rows"
AFTER_KEY = "after"
FOLD_KEY = "fold"
FOLD_TITLE_KEY = "title"
FOLD_TABLE_KEY = "table"
FOLD_TEXT_KEY = "text"
TABLE_COLUMNS_KEY = "columns"
TABLE_ROWS_KEY = "rows"
CHOICES_KEY = "choices"
REPLIES_KEY = "replies"
LINKS_KEY = "links"

FOLD_COLUMNS_MAX = 10
FOLD_COLUMN_MAX = 40
FOLD_TABLE_ROWS_MAX = 50
FOLD_CELL_MAX = 200

ACTION_ID_PREFIX = "kage"
ACTIONS_BLOCK_TYPE = "actions"
FOLD_BLOCK_TYPE = _presenter.FOLD_BLOCK
TABLE_BLOCK_TYPE = "table"
RAW_TEXT_CELL_TYPE = "raw_text"
SECTION_BLOCK_TYPE = "section"
MRKDWN_TYPE = "mrkdwn"
# What one section's text may hold (Slack's limit, as `slackChatSectionMax` in a2a/gateway/slack_chat.go has it).
SECTION_TEXT_MAX = 3000
MARKDOWN_BOLD, MRKDWN_BOLD = "**", "*"
MARKDOWN_BULLET, MRKDWN_BULLET = "- ", "• "
PLAIN_TEXT_TYPE = "plain_text"
BLOCK_TYPE_KEY = "type"
BLOCK_TEXT_KEY = "text"
BLOCK_TITLE_KEY = "title"
BLOCK_ROWS_KEY = "rows"
BLOCK_COLLAPSIBLE_KEY = "is_collapsible"
BLOCK_COLLAPSED_KEY = "default_collapsed"
BLOCK_CHILDREN_KEY = "child_blocks"
# The stored context of a thread, as findings_nudge.build_nudge writes it: the message's
# text, then this header, then one line per item with its id, in the rows' order.
METADATA_HEADER = "[Queued item metadata for thread replies]"
# What a row of the message's text starts with.
ROW_LINE_PREFIX = "- "
# A day's nudges share one thread and one stored context, and the message prints no id.
# So the newest nudge's context keeps, under it, the items of the nudges before it: this
# header, then per earlier message its position line and one line per item.
EARLIER_HEADER = "[Items of earlier messages in this thread, newest first. A reply or a click may mean one of them.]"
EARLIER_MESSAGE_LINE = "(message {position} before the newest)"
EARLIER_MESSAGE_RE = re.compile(r"\(message (\d+) before the newest\)")
# An earlier item line ends in its id and what it covers. The three parts below spell that
# tail once, for the line's format and for reading it back with plain string cuts: a pattern
# search over the stored text would take quadratic time on a line made to provoke it.
EARLIER_ID_OPEN = " [id="
EARLIER_COVERS_OPEN = " covers="
EARLIER_ITEM_CLOSE = "]"
EARLIER_ITEM_LINE = "{row}" + EARLIER_ID_OPEN + "{id}" + EARLIER_COVERS_OPEN + "{covers}" + EARLIER_ITEM_CLOSE
# An item whose row could not be read back keeps its id under these words.
EARLIER_ROW_UNREAD = ROW_LINE_PREFIX + "(its words were not kept)"
# How many earlier messages' items the newest context keeps; older ones drop off.
EARLIER_MESSAGES_MAX = 4
# The id of an item in a metadata line of the nudge (findings_nudge.METADATA_LINE); anchored at the line's start.
METADATA_ID_RE = re.compile(r"^\d+\. \[[^\]]*\] id=(\S+)")
ITEM_COVERS_RE = re.compile(r"\bcovers=(\d+)")
ITEM_COVERS_DEFAULT = "1"
# What ends a block of lines in a stored context.
BLOCK_END = "\n\n"
# The chat message's fields this module reads back to put `tone` and `fold` in spec order.
CHAT_HEADLINE, CHAT_NOTE, CHAT_DETAIL = "headline", "note", "detail"
CHAT_ROWS, CHAT_AFTER, CHAT_CHOICES, CHAT_LINKS = "rows", "after", "choices", "links"

HOME_OVERRIDE_ENV = "FINDINGS_NUDGE_HOME"
PLATFORM_HOME_ENV = "PLATFORM_AGENT_HOME"
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_GATEWAY_HOME = "/opt/data"
PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"

VALID_TONES = frozenset({_chat.TONE_SUCCESS, _chat.TONE_ATTENTION, _chat.TONE_CRITICAL})

ROW_TEXT_KEY = "text"
ROW_OPTIONAL_KEYS = ("severity", "detail")
LOG_PREFIX = "findings_nudge_card"


def profile_home(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the platform profile directory shared by the cron script and Session KV."""
    environ = env if env is not None else os.environ
    override = (environ.get(HOME_OVERRIDE_ENV) or "").strip()
    if override:
        base = Path(override)
    else:
        raw = (environ.get(PLATFORM_HOME_ENV) or environ.get(HERMES_HOME_ENV) or DEFAULT_GATEWAY_HOME).strip()
        base = Path(raw or DEFAULT_GATEWAY_HOME)
    nested = base / PROFILES_DIR / PLATFORM_PROFILE
    if nested.is_dir() or (environ.get(PLATFORM_HOME_ENV) or "").strip():
        return nested
    return base


def card_path(home: Path | str | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Path to the sidecar card file under the platform profile home."""
    target = Path(home) if home is not None else profile_home(env)
    nested = target / PROFILES_DIR / PLATFORM_PROFILE
    if nested.is_dir():
        target = nested
    return target / CARD_FILE


def save(path: Path, card: dict | None) -> None:
    """Atomically write ``card`` to ``path``, or remove ``path`` when ``card`` is None."""
    try:
        if card is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + CARD_TMP_SUFFIX)
        tmp.write_text(json.dumps(card, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # The text still posts; what is lost is the card and, with it, the ids
        # the thread's stored context would have carried.
        sys.stderr.write(f"{LOG_PREFIX}: could not write {path}: {exc}\n")


def _row_ok(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    text = row.get(ROW_TEXT_KEY)
    if not isinstance(text, str) or not text.strip():
        return False
    for key in ROW_OPTIONAL_KEYS:
        if key in row and not isinstance(row[key], str):
            return False
    return True


def _fold_ok(fold: Any) -> bool:
    if fold is None:
        return True
    if not isinstance(fold, dict):
        return False
    # A fold the renderer would have to title or clip itself is refused here,
    # so the report posts as its text, whole.
    title = fold.get(FOLD_TITLE_KEY)
    if not isinstance(title, str) or not title.strip() or len(title) > _chat.FOLD_TITLE_MAX:
        return False
    text = fold.get(FOLD_TEXT_KEY, "")
    if not isinstance(text, str) or len(text) > _chat.FOLD_TEXT_MAX:
        return False
    table = fold.get(FOLD_TABLE_KEY)
    if not isinstance(table, dict):
        return False
    columns = table.get(TABLE_COLUMNS_KEY)
    rows = table.get(TABLE_ROWS_KEY)
    if not isinstance(columns, list) or not (1 <= len(columns) <= FOLD_COLUMNS_MAX):
        return False
    if not all(isinstance(col, str) and col.strip() and len(col) <= FOLD_COLUMN_MAX for col in columns):
        return False
    if not isinstance(rows, list) or not (1 <= len(rows) <= FOLD_TABLE_ROWS_MAX):
        return False
    return all(
        isinstance(row, list)
        and len(row) == len(columns)
        and all(isinstance(cell, str) and len(cell) <= FOLD_CELL_MAX for cell in row)
        for row in rows
    )


def _links_ok(links: Any) -> bool:
    if links is None:
        return True
    if not isinstance(links, list):
        return False
    return all(
        isinstance(entry, (list, tuple))
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], str)
        for entry in links
    )


def load(home: Path | str | None, report: str, env: Mapping[str, str] | None = None) -> dict | None:
    """Return the sidecar card matching ``report``, or None when absent, stale, or malformed."""
    try:
        card = json.loads(card_path(home, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(card, dict) or card.get(VERSION_KEY) != CARD_SCHEMA_VERSION:
        return None
    if str(card.get(TEXT_KEY) or "").strip() != (report or "").strip():
        return None
    headline = card.get(HEADLINE_KEY)
    rows = card.get(ROWS_KEY)
    if not isinstance(headline, str) or not headline.strip():
        return None
    if not isinstance(rows, list) or not rows or not all(_row_ok(row) for row in rows):
        return None
    if not isinstance(card.get(NOTE_KEY, ""), str) or not isinstance(card.get(THREAD_CONTEXT_KEY, ""), str):
        return None
    tone = card.get(TONE_KEY, "")
    if tone and (not isinstance(tone, str) or tone not in VALID_TONES):
        return None
    after = card.get(AFTER_KEY, [])
    if not isinstance(after, list) or not all(isinstance(line, str) for line in after):
        return None
    choices = card.get(CHOICES_KEY, [])
    if not isinstance(choices, list) or not all(isinstance(item, str) for item in choices):
        return None
    replies = card.get(REPLIES_KEY, {})
    if not isinstance(replies, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in replies.items()
    ):
        return None
    if not _fold_ok(card.get(FOLD_KEY)) or not _links_ok(card.get(LINKS_KEY)):
        return None
    return card


def _clean_fold(fold: dict | None) -> dict | None:
    """Clip and sanitize ``fold`` to ``kube-agents.chat/v1`` table limits."""
    if not fold:
        return None
    table = fold.get(FOLD_TABLE_KEY) or {}
    raw_cols = table.get(TABLE_COLUMNS_KEY) or []
    raw_rows = table.get(TABLE_ROWS_KEY) or []
    columns = [_presenter._clip(_chat._line(col), FOLD_COLUMN_MAX) for col in raw_cols[:FOLD_COLUMNS_MAX]]
    if not columns or not all(columns):
        return None
    rows = [
        [_presenter._clip(_chat._line(cell), FOLD_CELL_MAX) for cell in row[: len(columns)]]
        for row in raw_rows[:FOLD_TABLE_ROWS_MAX]
        if isinstance(row, list) and len(row) >= len(columns)
    ]
    if not rows:
        return None
    title = _presenter._clip(_chat._line(fold.get(FOLD_TITLE_KEY) or ""), _chat.FOLD_TITLE_MAX)
    if not title:
        return None
    cleaned = {
        FOLD_TITLE_KEY: title,
        FOLD_TABLE_KEY: {
            TABLE_COLUMNS_KEY: columns,
            TABLE_ROWS_KEY: rows,
        },
    }
    text = str(fold.get(FOLD_TEXT_KEY) or "").strip()
    if text:
        # In spec key order: title, text, table.
        cleaned = {FOLD_TITLE_KEY: title, FOLD_TEXT_KEY: text, FOLD_TABLE_KEY: cleaned[FOLD_TABLE_KEY]}
    return cleaned


def chat_from_card(card: dict) -> tuple[dict, str]:
    """Build ``(chat, fallback_text)`` as a validated ``kube-agents.chat/v1`` payload."""
    after_text = "\n".join(str(line) for line in (card.get(AFTER_KEY) or ()) if str(line).strip())
    chat = _chat.report_message(
        headline=card[HEADLINE_KEY],
        note=card.get(NOTE_KEY) or "",
        rows=card[ROWS_KEY],
        choices=card.get(CHOICES_KEY) or (),
        links=card.get(LINKS_KEY) or (),
        after_rows=after_text,
        replies=card.get(REPLIES_KEY) or {},
    )
    tone = card.get(TONE_KEY) or ""
    fold = _clean_fold(card.get(FOLD_KEY))
    # Rebuild in spec key order so `tone` and `fold` sit in their canonical positions.
    ordered = _chat.message(
        headline=chat.get(CHAT_HEADLINE, ""),
        note=chat.get(CHAT_NOTE, ""),
        detail=chat.get(CHAT_DETAIL, ""),
        tone=tone if tone in VALID_TONES else "",
        rows=chat.get(CHAT_ROWS, []),
        after=chat.get(CHAT_AFTER, []),
        fold=fold,
        choices=chat.get(CHAT_CHOICES, []),
        links=chat.get(CHAT_LINKS, []),
    )
    return ordered, card[TEXT_KEY]


def _fold_container_block(fold: dict) -> dict:
    """Slack Block Kit collapsible ``container`` holding a native ``table`` block."""
    table = fold[FOLD_TABLE_KEY]
    all_rows = [table[TABLE_COLUMNS_KEY], *table[TABLE_ROWS_KEY]]
    slack_rows = [
        [{BLOCK_TYPE_KEY: RAW_TEXT_CELL_TYPE, BLOCK_TEXT_KEY: cell} for cell in row]
        for row in all_rows
    ]
    children = [
        {BLOCK_TYPE_KEY: SECTION_BLOCK_TYPE, BLOCK_TEXT_KEY: {BLOCK_TYPE_KEY: MRKDWN_TYPE, BLOCK_TEXT_KEY: chunk}}
        for chunk in _mrkdwn_chunks(fold.get(FOLD_TEXT_KEY) or "")
    ]
    children.append({BLOCK_TYPE_KEY: TABLE_BLOCK_TYPE, BLOCK_ROWS_KEY: slack_rows})
    return {
        BLOCK_TYPE_KEY: FOLD_BLOCK_TYPE,
        BLOCK_TITLE_KEY: {BLOCK_TYPE_KEY: PLAIN_TEXT_TYPE, BLOCK_TEXT_KEY: fold[FOLD_TITLE_KEY]},
        BLOCK_COLLAPSIBLE_KEY: True,
        BLOCK_COLLAPSED_KEY: True,
        BLOCK_CHILDREN_KEY: children,
    }


def _mrkdwn_chunks(text: str) -> list[str]:
    """A fold's text as Slack mrkdwn, in pieces of whole lines no longer than one section holds.

    The text is the emitter's: a bold lead (`**...**`), bullet lines (`- ...`)
    and plain lines. Only the markup changes; no word does.
    """
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        line = _presenter._escape(line)
        if line.startswith(MARKDOWN_BOLD) and line.endswith(MARKDOWN_BOLD) and len(line) > 2 * len(MARKDOWN_BOLD):
            line = MRKDWN_BOLD + line[len(MARKDOWN_BOLD) : -len(MARKDOWN_BOLD)] + MRKDWN_BOLD
        elif line.startswith(MARKDOWN_BULLET):
            line = MRKDWN_BULLET + line[len(MARKDOWN_BULLET) :]
        line = line[:SECTION_TEXT_MAX]
        if current and len(current) + 1 + len(line) > SECTION_TEXT_MAX:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current.strip():
        chunks.append(current)
    return chunks


def blocks_from_card(card: dict) -> tuple[list[dict], str]:
    """Build ``(blocks, fallback_text)`` as Slack Block Kit for the relay path."""
    after_text = "\n".join(str(line) for line in (card.get(AFTER_KEY) or ()) if str(line).strip())
    blocks = _presenter.blocks_report(
        headline=card[HEADLINE_KEY],
        note=card.get(NOTE_KEY) or "",
        rows=card[ROWS_KEY],
        choices=card.get(CHOICES_KEY) or (),
        links=card.get(LINKS_KEY) or (),
        action_id_prefix=ACTION_ID_PREFIX,
        after_rows=after_text,
        replies=card.get(REPLIES_KEY) or {},
    )
    fold = _clean_fold(card.get(FOLD_KEY))
    if fold is not None:
        container = _fold_container_block(fold)
        action_index = next(
            (idx for idx, block in enumerate(blocks) if block.get(BLOCK_TYPE_KEY) == ACTIONS_BLOCK_TYPE),
            len(blocks),
        )
        blocks = [*blocks[:action_index], container, *blocks[action_index:]]
    return blocks, card[TEXT_KEY]


def thread_context(card: dict, fallback: str) -> str:
    """Return the hidden incident context to store for thread replies."""
    stored = (card.get(THREAD_CONTEXT_KEY) or "").strip()
    return stored if stored else fallback


def _earlier_item_id(line: str) -> str:
    """The id at the end of an earlier item line, or "" when ``line`` does not end as one."""
    _, opened, tail = line.rpartition(EARLIER_ID_OPEN)
    finding_id, covered, covers = tail.partition(EARLIER_COVERS_OPEN)
    count = covers.removesuffix(EARLIER_ITEM_CLOSE)
    whole = opened and covered and covers.endswith(EARLIER_ITEM_CLOSE) and count.isdigit()
    return finding_id if whole and finding_id and not any(c.isspace() for c in finding_id) else ""


def _item_id(line: str) -> str:
    """The id of an item line: an earlier item's, or a metadata line's."""
    earlier = _earlier_item_id(line)
    if earlier:
        return earlier
    found = METADATA_ID_RE.match(line)
    return found.group(1) if found else ""


def _metadata_lines(context: str) -> tuple[str, list[str]]:
    """``(text above the metadata header, the metadata lines)`` of a stored context; no header, no lines."""
    visible, found, rest = context.partition(f"\n{METADATA_HEADER}\n")
    if not found:
        return context, []
    return visible, [line for line in rest.split(BLOCK_END, 1)[0].splitlines() if line.strip()]


def _message_items(context: str) -> list[str]:
    """The items of the message a stored context is for, a line each: the row's words, then its id."""
    visible, metadata = _metadata_lines(context)
    rows = [line for line in visible.splitlines() if line.startswith(ROW_LINE_PREFIX)]
    paired = len(rows) == len(metadata)
    items = []
    for index, line in enumerate(metadata):
        finding_id = _item_id(line)
        if not finding_id:
            continue
        covers = ITEM_COVERS_RE.search(line)
        items.append(
            EARLIER_ITEM_LINE.format(
                row=rows[index] if paired else EARLIER_ROW_UNREAD,
                id=finding_id,
                covers=covers.group(1) if covers else ITEM_COVERS_DEFAULT,
            )
        )
    return items


def _earlier_messages(context: str) -> list[tuple[int, list[str]]]:
    """``(position, item lines)`` of each earlier message a stored context already keeps, newest first."""
    _, found, section = context.partition(f"\n{EARLIER_HEADER}\n")
    messages: list[tuple[int, list[str]]] = []
    for line in section.splitlines() if found else ():
        position = EARLIER_MESSAGE_RE.fullmatch(line.strip())
        if position:
            messages.append((int(position.group(1)), []))
        elif messages and line.startswith(ROW_LINE_PREFIX):
            messages[-1][1].append(line)
    return messages


def thread_context_with_earlier(card: dict | None, fallback: str, stored: str) -> str:
    """The context to store for the newest nudge of a thread: its own, then the earlier nudges' items.

    ``stored`` is what the thread holds now, the context of the nudge before
    this one or nothing. A day's nudges share the thread and the card prints
    no id, so replacing the stored context would lose the ids of every card
    but the newest, and a reply or a click under an earlier card would be
    read against the newest card's items. The first nudge of a thread stores
    its context unchanged. An item the newest message names again is not
    repeated below it. Each earlier message keeps its own position, and one
    more than :data:`EARLIER_MESSAGES_MAX` messages back drops off.

    ``card`` is ``None`` for a nudge whose card is not to hand (a truncated
    report, a card file missing or refused): its text is stored as it is, and
    the earlier messages' items are still kept under it.
    """
    current = thread_context(card, fallback) if card is not None else fallback
    if not stored:
        return current
    # The same message again (a retried relay, a repeat of the hour before): what is
    # stored already holds it, with the earlier messages at their positions.
    if stored.partition(f"{BLOCK_END}{EARLIER_HEADER}\n")[0] == current:
        return stored
    named = {_item_id(line) for line in _metadata_lines(current)[1]}
    before = [(1, _message_items(stored)), *((position + 1, lines) for position, lines in _earlier_messages(stored))]
    kept: list[tuple[int, list[str]]] = []
    for position, message in before:
        lines = [line for line in message if _item_id(line) not in named]
        named.update(_item_id(line) for line in lines)
        if lines and position <= EARLIER_MESSAGES_MAX:
            kept.append((position, lines))
    if not kept:
        return current
    parts = [current, "", EARLIER_HEADER]
    for position, lines in kept:
        parts.append(EARLIER_MESSAGE_LINE.format(position=position))
        parts.extend(lines)
    return "\n".join(parts)
