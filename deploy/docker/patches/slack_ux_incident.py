"""Turn an incident alert into its triage when the report lands.

Installed into the image at ``/opt/hermes/gateway/slack_ux_incident.py``.
``apply_slack_ux_incident.py`` makes the kanban notifier hand the terminal
send the adapter :func:`adapter_for` returns. With ``KAGE_SLACK_UX`` off, or
for anything but an incident report bound for its own alert's thread on
Slack, that is the notifier's own adapter, so the delivery is upstream's.

Upstream, and why it changes
----------------------------
The event watcher posts the alert ("🚨 ... Digging down to the root cause")
and the triage card's report arrives as a reply under it, so the channel
keeps showing the alert while the diagnosis sits in the thread. With the flag
on, the report replaces the alert in place instead: the alert message is
edited into the report's "What's wrong" sentence, one button per option, the
report's own links as link buttons, and the whole report in a collapsed fold,
rendered by the Slack plugin's own ``block_kit.render_blocks`` so it reads as
the threaded reply would.

A button's text is ``apply Option B: <title>`` (``apply: <title>`` for the
single-fix shape), so a click, which ``slack_ux_clicks`` sends as the
clicker's message in the thread, is the same reply the report's call to
action asks for. Typing ``apply`` still works: the ``incidents`` row the
notifier stores after the send is keyed to the same thread, and the report
text is untouched, only laid out.

Only the first report in a thread takes the alert. ``POST /v1/incidents``
keeps the first report per thread, and a second one edited over it would
show options the stored row does not have and erase the first from Slack; a
thread that already has a row gets the reply upstream posts. That row is a
best-effort write after the send, so this process also remembers the alerts
it edited and never edits one twice; only a failed write followed by a
gateway restart leaves a thread open to a second edit. A report with no "What's wrong" sentence, an
option named but not parsed, a fold ``block_kit`` cannot render, or any failure
to edit, also falls back to that reply.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import OrderedDict
from contextlib import closing
from importlib import util as importlib_util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None

#: The Slack plugin's markdown renderer, beside this module's ``gateway/``
#: directory. It imports only ``re`` and ``typing``, so it loads by path.
BLOCK_KIT = Path(__file__).resolve().parents[1] / "plugins" / "platforms" / "slack" / "block_kit.py"

SLACK = "slack"
COMPLETED = "completed"

#: The routing database the event watcher writes, read here as
#: ``kanban_event_routing`` reads it: read-only, through a URI.
DB_PATH_ENV = "SESSION_KV_DB_PATH"
DEFAULT_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"
DB_TIMEOUT_SECONDS = 2.0
#: Event sessions, as ``session_kv_server`` names them. Cron reports register
#: threads too, under other ids, and must never be edited.
ALERT_SESSION_LIKE = "k8s-evt-%"

ACTION_PREFIX = "kage_incident"
FOLD_TITLE_OPTIONS = "why · what each option does"
FOLD_TITLE_SINGLE = "why · what the fix does"
OPTION_LABEL = "apply Option {letter}: {title}"
SINGLE_LABEL = "apply: {title}"
LINK_LABEL = "{label} ↗"
PRIMARY = "primary"
#: A report longer than this keeps the threaded reply; triage reports are a
#: few hundred words, and Slack's limit on a container's content is not published.
FOLD_TEXT_MAX = 12000

HEADING = re.compile(r"^ {0,3}#{1,6} +(.+?)[ #]*$")
WHATS_WRONG = re.compile(r"what(?:['’]s| is) wrong", re.IGNORECASE)
WHAT_TO_DO = re.compile(r"what to do", re.IGNORECASE)
#: A parenthesised title, one level of nested parentheses allowed: ``(Scale to 4 (from 2))``.
TITLE = r"\(((?:[^()\n]|\([^()\n]*\))+)\)"
#: An option bullet: ``- **Option A (<title>):** ...``.
OPTION_LINE = re.compile(r"^\s*[-*]\s+[*_]*Option ([A-Z])\s*" + TITLE)
#: The single-fix bullet: ``- **Proposed fix (<title>):** ...``.
PROPOSED_FIX_LINE = re.compile(r"^\s*[-*]\s+[*_]*Proposed fix\s*" + TITLE)
#: The report's links line, 🔗 first; links elsewhere in the prose stay in the fold only.
LINKS_LINE = re.compile(r"^[^\w\n]*🔗")
#: Any option the section names, parsed or not.
OPTION_NAMED = re.compile(r"\bOption ([A-Z])\b")
#: The recommendation line, ``- ✅ **Recommended: Option B**``; only markup may precede the
#: word, so ``Not Recommended: Option A`` is not one.
RECOMMENDED = re.compile(r"^[^\w\n]*Recommended:?[*_\s]*Option ([A-Z])\b", re.MULTILINE)
#: A markdown link; the url may hold balanced parentheses, as a Logs Explorer query does.
MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^()\s]+(?:\([^()\s]*\)[^()\s]*)*)\)")
#: A code fence line; nothing between two is a heading, a bullet or markup.
FENCE = re.compile(r"^\s*(```|~~~)")
#: How many edited alerts this process remembers.
EDITED_MAX = 512

#: ``(chat_id, thread_id)`` of the alerts this process has edited, oldest first.
_edited: OrderedDict[tuple[str, str], None] = OrderedDict()


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    return _presenter is not None and _presenter.enabled()


def _sections(report: str) -> list[tuple[str, str]]:
    """``(heading, body)`` for each markdown heading in ``report``, in order."""
    out: list[tuple[str, str]] = []
    heading, body = None, []
    fenced = False
    for line in report.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        match = None if fenced else HEADING.match(line)
        if match:
            if heading is not None:
                out.append((heading, "\n".join(body).strip()))
            heading, body = match.group(1).strip(), []
        elif heading is not None:
            body.append(line)
    if heading is not None:
        out.append((heading, "\n".join(body).strip()))
    return out


def _section(sections: list[tuple[str, str]], name: re.Pattern) -> str:
    return next((body for heading, body in sections if name.search(heading)), "")


def _unfenced(text: str) -> list[str]:
    """The lines of ``text`` outside code fences, fence lines dropped."""
    out, fenced = [], False
    for line in text.split("\n"):
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            out.append(line)
    return out


def parse_triage(report: str) -> dict | None:
    """The headline, choices and links of a triage report, or None if it has no option.

    ``choices`` are ``(label, recommended)`` pairs in the report's order.
    """
    sections = _sections(report)
    starts = [i for i, (heading, _body) in enumerate(sections) if WHAT_TO_DO.search(heading)]
    if len(starts) > 1:
        # Buttons from one section under a fold showing both would misstate the report.
        return None
    # "What to do" runs to the end of the report, so a stray `#` line cannot cut it short, and a
    # fenced block may quote the option shape, so only prose lines count.
    rest = "\n".join(body for _heading, body in sections[starts[0]:]) if starts else ""
    lines = _unfenced(rest)
    what_to_do = "\n".join(lines)
    choices: list[tuple[str, bool]] = []
    recommended = RECOMMENDED.search(what_to_do)
    seen = set()
    for line in lines:
        option = OPTION_LINE.match(line)
        if option and option.group(1) not in seen:
            letter = option.group(1)
            seen.add(letter)
            label = OPTION_LABEL.format(letter=letter, title=_presenter._plain(option.group(2)))
            choices.append((label, bool(recommended) and recommended.group(1) == letter))
    if not set(OPTION_NAMED.findall(what_to_do)) <= seen:
        # A button row missing an option the report offers would misstate it.
        return None
    if not choices:
        single = next((m for m in map(PROPOSED_FIX_LINE.match, lines) if m), None)
        if single:
            choices.append((SINGLE_LABEL.format(title=_presenter._plain(single.group(1))), True))
    if not choices:
        return None
    headline, _body = _presenter.split_answer(_section(sections, WHATS_WRONG))
    if not headline:
        return None
    return {
        "headline": headline,
        "choices": choices,
        "links": [link for line in lines if LINKS_LINE.match(line) for link in MD_LINK.findall(line)],
        "fold_title": FOLD_TITLE_OPTIONS if len(choices) > 1 else FOLD_TITLE_SINGLE,
    }


def _text(text: str, **style: bool) -> dict:
    element: dict = {"type": "text", "text": text}
    if style:
        element["style"] = style
    return element


_block_kit = None


def _load_block_kit() -> Any:
    global _block_kit
    if _block_kit is None:
        spec = importlib_util.spec_from_file_location("slack_ux_incident_block_kit", BLOCK_KIT)
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _block_kit = module
    return _block_kit


def render_fold(report: str, mrkdwn_fn: Any = None) -> list[dict] | None:
    """``report`` as the blocks the adapter's own send would render, or None if it cannot."""
    block_kit = _load_block_kit()
    return block_kit.sanitize_blocks(block_kit.render_blocks(report, mrkdwn_fn=mrkdwn_fn))


def blocks_triage(triage: dict, fold_blocks: list[dict]) -> list[dict]:
    """Headline, option and link buttons, and ``fold_blocks`` folded, as Block Kit."""
    buttons = []
    for i, (label, recommended) in enumerate(triage["choices"]):
        button = _presenter._button(label, f"{ACTION_PREFIX}.{_presenter.CHOICE_ACTION}.{i}", value=label)
        if recommended:
            button["style"] = PRIMARY
        buttons.append(button)
    for i, (label, url) in enumerate(triage["links"]):
        buttons.append(
            _presenter._button(LINK_LABEL.format(label=label), f"{ACTION_PREFIX}.{_presenter.LINK_ACTION}.{i}", url=url)
        )
    headline = {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": [_text(triage["headline"], bold=True)]}],
    }
    fold = {
        "type": "container",
        "title": {"type": "plain_text", "text": triage["fold_title"]},
        "is_collapsible": True,
        "default_collapsed": True,
        "child_blocks": fold_blocks,
    }
    return [headline, *_presenter._actions(buttons), fold]


def fallback_text(triage: dict) -> str:
    """The edited message's ``text``: the headline and the choices, as mrkdwn."""
    return _presenter.fallback_text(triage["headline"], choices=[label for label, _ in triage["choices"]])


def _db_path() -> str:
    return os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH


def is_open_alert(chat_id: str, thread_id: str, db_path: str | None = None) -> bool:
    """Whether ``thread_id`` is an event alert on Slack that no stored report owns yet."""
    db_path = db_path or _db_path()
    if not os.path.exists(db_path):
        return False
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS)) as conn:
        if conn.execute(
            "SELECT 1 FROM incidents WHERE chat_id = ? AND thread_id = ?", (chat_id, thread_id)
        ).fetchone():
            return False
        # The LIKE on metadata narrows the scan; the JSON check below decides.
        rows = conn.execute(
            "SELECT metadata FROM session_metadata WHERE session_id LIKE ? AND metadata LIKE '%' || ? || '%'",
            (ALERT_SESSION_LIKE, thread_id),
        ).fetchall()
    for (blob,) in rows:
        try:
            meta = json.loads(blob or "")
        except ValueError:
            continue
        if (
            isinstance(meta, dict)
            and str(meta.get("platform") or "").lower() == SLACK
            and str(meta.get("thread_id") or "") == thread_id
            and str(meta.get("chat_id") or "") == chat_id
        ):
            return True
    return False


class _AlertEditor:
    """The notifier's adapter, with ``send`` editing the alert instead of replying under it."""

    def __init__(self, adapter: Any, chat_id: str, thread_id: str, triage: dict, fold_blocks: list[dict]):
        self._adapter = adapter
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._triage = triage
        self._fold_blocks = fold_blocks

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def send(self, chat_id: Any, content: Any, metadata: Any = None, **kwargs: Any) -> Any:
        key = (self._chat_id, self._thread_id)
        if str(chat_id) == self._chat_id and key not in _edited:
            _edited[key] = None
            while len(_edited) > EDITED_MAX:
                _edited.popitem(last=False)
            try:
                await self._adapter._get_client(self._chat_id).chat_update(
                    channel=self._chat_id,
                    ts=self._thread_id,
                    text=fallback_text(self._triage),
                    blocks=blocks_triage(self._triage, self._fold_blocks),
                )
                logger.info("slack_ux_incident: edited alert %s into its triage", self._thread_id)
                return SimpleNamespace(success=True, message_id=self._thread_id, error=None)
            except Exception as exc:  # noqa: BLE001 — the threaded reply still delivers it
                _edited.pop(key, None)
                logger.warning(
                    "slack_ux_incident: could not edit alert %s, replying under it: %s", self._thread_id, exc
                )
        return await self._adapter.send(chat_id, content, metadata=metadata, **kwargs)


def adapter_for(adapter: Any, platform: str, event: Any, task: Any, sub: Any) -> Any:
    """``adapter``, or one whose ``send`` edits the alert this report answers.

    The editor is returned only with the flag on, on Slack, for a ``completed``
    event whose result is an actionable report with at least one option,
    bound for the thread of an event alert that has no stored report yet.
    Everything else, including any error deciding, gets ``adapter`` itself.
    """
    try:
        if not enabled() or str(platform or "").lower() != SLACK:
            return adapter
        if getattr(event, "kind", None) != COMPLETED or not isinstance(sub, dict):
            return adapter
        from gateway.kanban_notifier import (
            RESULT_LIMIT,
            actionable_report,
            clip_handoff,
        )

        result = getattr(task, "result", None)
        if not actionable_report(result):
            return adapter
        report = clip_handoff(str(result).strip(), RESULT_LIMIT)
        if len(report) > FOLD_TEXT_MAX:
            return adapter
        chat_id = str(sub.get("chat_id") or "").strip()
        thread_id = str(sub.get("thread_id") or "").strip()
        if not (chat_id and thread_id):
            return adapter
        triage = parse_triage(report)
        if triage is None or (chat_id, thread_id) in _edited or not is_open_alert(chat_id, thread_id):
            return adapter
        fold_blocks = render_fold(report, getattr(adapter, "format_message", None))
        if not fold_blocks:
            return adapter
        return _AlertEditor(adapter, chat_id, thread_id, triage, fold_blocks)
    except Exception as exc:  # noqa: BLE001 — never fail a delivery on presentation
        logger.warning("slack_ux_incident: not editing the alert: %s", exc)
        return adapter
