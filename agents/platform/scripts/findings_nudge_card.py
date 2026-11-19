"""The findings-nudge card: surfaced issues, collapsible queue breakdown, and hidden thread context.

``findings_nudge.py`` writes a sidecar card file beside its daily state whenever
it prints a nudge, and removes the file when a tick prints nothing. The relay
(``session_kv_server.relay_cron_report``) loads the card here when its ``text``
matches the relayed report:

* On a card platform (Slack via the relay as Block Kit, or Slack and Google Chat
  through the A2A gateway as ``kube-agents.chat/v1``), the card replaces the
  plain-text post with an answer-first headline, clean finding rows, a collapsed
  cluster breakdown table for the rest of the queue, and action buttons.
* On every leg that lands, ``thread_context`` is stored in Session KV's
  ``incidents`` table instead of the bare text, so a reply or button click in
  that thread receives the finding ids and item coverage counts without showing
  plumbing in chat.
"""

from __future__ import annotations

import json
import os
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
PLAIN_TEXT_TYPE = "plain_text"

HOME_OVERRIDE_ENV = "FINDINGS_NUDGE_HOME"
PLATFORM_HOME_ENV = "PLATFORM_AGENT_HOME"
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_GATEWAY_HOME = "/opt/data"
PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"

VALID_TONES = frozenset({_chat.TONE_SUCCESS, _chat.TONE_ATTENTION, _chat.TONE_CRITICAL})


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
    except OSError:
        pass


def _row_ok(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    text = row.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    for key in ("severity", "detail"):
        if key in row and not isinstance(row[key], str):
            return False
    return True


def _fold_ok(fold: Any) -> bool:
    if fold is None:
        return True
    if not isinstance(fold, dict):
        return False
    title = fold.get(FOLD_TITLE_KEY, "")
    if not isinstance(title, str):
        return False
    table = fold.get(FOLD_TABLE_KEY)
    if not isinstance(table, dict):
        return False
    columns = table.get(TABLE_COLUMNS_KEY)
    rows = table.get(TABLE_ROWS_KEY)
    if not isinstance(columns, list) or not (1 <= len(columns) <= FOLD_COLUMNS_MAX):
        return False
    if not all(isinstance(col, str) and col.strip() for col in columns):
        return False
    if not isinstance(rows, list) or not (1 <= len(rows) <= FOLD_TABLE_ROWS_MAX):
        return False
    return all(
        isinstance(row, list) and len(row) == len(columns) and all(isinstance(cell, str) for cell in row)
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
    title = _presenter._clip(_chat._line(fold.get(FOLD_TITLE_KEY) or ""), _chat.FOLD_TITLE_MAX) or _chat.FOLD_TITLE_DEFAULT
    return {
        FOLD_TITLE_KEY: title,
        FOLD_TABLE_KEY: {
            TABLE_COLUMNS_KEY: columns,
            TABLE_ROWS_KEY: rows,
        },
    }


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
        headline=chat.get("headline", ""),
        note=chat.get("note", ""),
        detail=chat.get("detail", ""),
        tone=tone if tone in VALID_TONES else "",
        rows=chat.get("rows", []),
        after=chat.get("after", []),
        fold=fold,
        choices=chat.get("choices", []),
        links=chat.get("links", []),
    )
    return ordered, card[TEXT_KEY]


def _fold_container_block(fold: dict) -> dict:
    """Slack Block Kit collapsible ``container`` holding a native ``table`` block."""
    table = fold[FOLD_TABLE_KEY]
    all_rows = [table[TABLE_COLUMNS_KEY], *table[TABLE_ROWS_KEY]]
    slack_rows = [
        [{"type": RAW_TEXT_CELL_TYPE, "text": cell} for cell in row]
        for row in all_rows
    ]
    return {
        "type": FOLD_BLOCK_TYPE,
        "title": {"type": PLAIN_TEXT_TYPE, "text": fold[FOLD_TITLE_KEY]},
        "is_collapsible": True,
        "default_collapsed": True,
        "child_blocks": [{"type": TABLE_BLOCK_TYPE, "rows": slack_rows}],
    }


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
    )
    fold = _clean_fold(card.get(FOLD_KEY))
    if fold is not None:
        container = _fold_container_block(fold)
        action_index = next(
            (idx for idx, block in enumerate(blocks) if block.get("type") == ACTIONS_BLOCK_TYPE),
            len(blocks),
        )
        blocks = [*blocks[:action_index], container, *blocks[action_index:]]
    return blocks, card[TEXT_KEY]


def thread_context(card: dict, fallback: str) -> str:
    """Return the hidden incident context to store for thread replies."""
    stored = (card.get(THREAD_CONTEXT_KEY) or "").strip()
    return stored if stored else fallback
