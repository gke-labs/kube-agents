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
links on the report's 🔗 line as link buttons, and the whole report in a
collapsed fold, rendered by the Slack plugin's own ``block_kit.render_blocks``,
the renderer the threaded reply goes through when the adapter sends rich
blocks. The message's ``text`` is the headline, the choices and then the
whole report, because the adapter reads a thread back from ``text`` and
top-level blocks, never a fold's. That holds on a cold read of the thread
only: a session already open on it fetches at most the newer replies, which
skips the edited alert, and a typed ``apply`` there finds the report through
the ``incidents`` row below.

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
gateway restart leaves a thread open to a second edit. A report with no
"What's wrong" sentence, an option named but not parsed, a fold
``block_kit`` cannot render or that would hold a block outside
``FOLD_CHILD_TYPES``, or any failure to edit, also falls back to that reply.
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
#: Slack refuses a button url longer than this, and with it the whole message.
BUTTON_URL_MAX = 3000
#: A report longer than this keeps the threaded reply; triage reports are a
#: few hundred words, Slack's limit on a container's content is not published,
#: and the message ``text``, which carries the report too, stays far inside
#: Slack's 40,000 characters.
FOLD_TEXT_MAX = 12000
#: The block types Slack has been seen to keep inside a collapsible
#: ``container`` (read back from ``conversations.replies`` on a live alert).
#: The plugin also renders ``table`` and ``divider``; a fold holding one keeps
#: the threaded reply until a live run shows Slack accepts it there.
FOLD_CHILD_TYPES = frozenset({"header", "section", "rich_text"})

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
#: A proposed fix the section names in any shape: bullet, numbered, bold label, heading, any case.
PROPOSED_FIX_NAMED = re.compile(r"\bproposed fix\b", re.IGNORECASE)
#: The recommendation line, ``- ✅ **Recommended: Option B**``; only markup may precede the
#: word, so ``Not Recommended: Option A`` is not one.
RECOMMENDED = re.compile(r"^[^\w\n]*Recommended[*_]*:?[*_\s]*Option ([A-Z])\b", re.MULTILINE)
#: A markdown link; the url may hold balanced parentheses, as a Logs Explorer query does.
MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^()\s]+(?:\([^()\s]*\)[^()\s]*)*)\)")
#: How many edited alerts this process remembers.
EDITED_MAX = 512
#: The characters that would end a Slack mrkdwn link early, percent-encoded so the button and
#: the fallback text carry the same url; ``%`` is left alone, so an encoded url stays as it is.
MRKDWN_URL_ESCAPES = str.maketrans({"<": "%3C", ">": "%3E", "|": "%7C"})

#: ``(chat_id, thread_id)`` of the alerts this process has edited, oldest first.
_edited: OrderedDict[tuple[str, str], None] = OrderedDict()


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    return _presenter is not None and _presenter.enabled()


def _next_fence(line: str, fence: str | None) -> str | None:
    """The presenter's fence rule, so the sections and the headline read fences alike."""
    return _presenter.next_fence(line, fence)


def _sections(report: str) -> list[tuple[str, str]]:
    """``(heading, body)`` for each markdown heading in ``report``, in order."""
    out: list[tuple[str, str]] = []
    heading, body = None, []
    fence = None
    for line in report.split("\n"):
        was, fence = fence, _next_fence(line, fence)
        match = None if (was or fence) else HEADING.match(line)
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
    out, fence = [], None
    for line in text.split("\n"):
        was, fence = fence, _next_fence(line, fence)
        if was is None and fence is None:
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
    # Fenced lines and headings count here: neither may hide an option from the guard.
    named = "\n".join(f"{heading}\n{body}" for heading, body in sections[starts[0]:]) if starts else ""
    if not set(OPTION_NAMED.findall(named)) <= seen:
        # A button row missing an option the report offers would misstate it.
        return None
    if choices and PROPOSED_FIX_NAMED.search(named):
        # So would lettered buttons beside a "Proposed fix" bullet that has none.
        return None
    if not choices:
        # One button stands for one fix; a second fix named anywhere would be left out.
        fixes = [m for m in map(PROPOSED_FIX_LINE.match, lines) if m]
        if len(fixes) == 1 and len(PROPOSED_FIX_NAMED.findall(named)) == 1:
            choices.append((SINGLE_LABEL.format(title=_presenter._plain(fixes[0].group(1))), True))
    if not choices:
        return None
    headline, _body = _presenter.split_answer(_section(sections, WHATS_WRONG))
    if not headline:
        return None
    return {
        "headline": headline,
        "choices": choices,
        # A clipped url is a dead one, so an overlong link is dropped; the fold still has it.
        "links": [
            (_presenter._plain(label), url) for line in lines if LINKS_LINE.match(line)
            for label, raw in MD_LINK.findall(line)
            for url in (raw.translate(MRKDWN_URL_ESCAPES),) if len(url) <= BUTTON_URL_MAX
        ],
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
    blocks = block_kit.sanitize_blocks(block_kit.render_blocks(report, mrkdwn_fn=mrkdwn_fn))
    if not blocks or any(block.get("type") not in FOLD_CHILD_TYPES for block in blocks):
        return None
    return blocks


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
    """The headline, the links and the choices, as mrkdwn; :func:`message_text` starts with it."""
    return _presenter.fallback_text(
        triage["headline"], links=triage["links"], choices=[label for label, _ in triage["choices"]]
    )


def message_text(triage: dict, report: str, mrkdwn_fn: Any = None) -> str:
    """The edited message's ``text``: :func:`fallback_text`, then the whole report as mrkdwn."""
    return "\n\n".join((fallback_text(triage), mrkdwn_fn(report) if callable(mrkdwn_fn) else report))


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

    def __init__(
        self, adapter: Any, chat_id: str, thread_id: str, triage: dict, fold_blocks: list[dict], text: str
    ):
        self._adapter = adapter
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._triage = triage
        self._fold_blocks = fold_blocks
        self._text = text

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def send(self, chat_id: Any, content: Any, metadata: Any = None, **kwargs: Any) -> Any:
        key = (self._chat_id, self._thread_id)
        if str(chat_id) == self._chat_id and key not in _edited:
            _edited[key] = None
            while len(_edited) > EDITED_MAX:
                _edited.popitem(last=False)
            try:
                await self._adapter._client_for(self._chat_id, metadata).chat_update(
                    channel=self._chat_id,
                    ts=self._thread_id,
                    text=self._text,
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
        mrkdwn_fn = getattr(adapter, "format_message", None)
        fold_blocks = render_fold(report, mrkdwn_fn)
        if not fold_blocks:
            return adapter
        return _AlertEditor(adapter, chat_id, thread_id, triage, fold_blocks, message_text(triage, report, mrkdwn_fn))
    except Exception as exc:  # noqa: BLE001 — never fail a delivery on presentation
        logger.warning("slack_ux_incident: not editing the alert: %s", exc)
        return adapter
