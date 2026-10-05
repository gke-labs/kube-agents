"""Lead a finished card's answer with its first sentence and fold the rest.

Installed into the image at ``/opt/hermes/gateway/slack_ux_answer.py``.
``apply_slack_ux_answer.py`` makes the kanban notifier hand the terminal send
the adapter :func:`adapter_for` returns, inside the one
``slack_ux_incident.adapter_for`` wraps. With ``KAGE_SLACK_UX`` off, or for
anything but a ``completed`` card on Slack, that is the notifier's own
adapter, so the delivery is upstream's.

Upstream, and why it changes
----------------------------
With the flag on, the completion message on Slack is the worker's answer alone
(``kanban_notifier.completion_text``), and the Chat agent's SOUL has it lead
with the answer. Upstream still posts it as one run of prose, so the answer
sits at the same weight as the reasoning under it. With the flag on, the
message is instead the answer's first sentence in bold, then everything after
it in a collapsed fold titled :data:`FOLD_TITLE`, rendered by the Slack
plugin's own ``block_kit.render_blocks``, the renderer the upstream post goes
through. The message's ``text`` is the whole answer as mrkdwn, as upstream's
is, because the adapter reads a thread back from ``text`` and top-level
blocks, never a fold's.

The first sentence is ``slack_presenter.split_lead``'s, the sentence
``slack_presenter.split_answer`` takes its headline from. A bold headline is
plain text, so an answer it cannot carry whole keeps the upstream post: one
whose first line is a heading, a list item or a code fence, or whose first
sentence is longer than ``HEADLINE_MAX`` or holds a link or a mention. So does
one with nothing after that sentence, one longer than :data:`FOLD_TEXT_MAX`, a
fold ``block_kit`` cannot render or that would hold a block outside
:data:`FOLD_CHILD_TYPES` (a table or a divider), and an adapter not rendering
``rich_blocks``, since upstream would then post text alone. A refused fold
logs why; a failed post falls back to the upstream send. An incident report
that edits its alert never reaches this send, and one whose edit fails opens
with a heading, so it keeps the upstream post here too.
"""

from __future__ import annotations

import logging
import re
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
FOLD_TITLE = "why"
#: The adapter's ``config.extra`` switches for its own Block Kit: the local
#: renderer the fold is rendered with, and Slack's native ``markdown`` block,
#: which upstream prefers over it.
RICH_BLOCKS = "rich_blocks"
MARKDOWN_BLOCKS = "markdown_blocks"
#: The label upstream's send passes ``_outbound_blocked``.
OUTBOUND_LABEL = "outbound generic send to"
#: As ``slack_ux_incident``'s: an answer longer than this keeps the upstream
#: post, and the message ``text`` stays far inside Slack's 40,000 characters.
FOLD_TEXT_MAX = 12000
#: As ``slack_ux_incident``'s: the block types Slack has been seen to keep
#: inside a collapsible ``container``.
FOLD_CHILD_TYPES = frozenset({"header", "section", "rich_text"})
#: A first line that is not prose: a heading, a list item or a fence.
NOT_PROSE = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|`{3,}|~{3,})")
#: What a plain-text headline would lose: a markdown link, a url, a Slack mention or link.
LOSES_CONTENT = re.compile(r"\]\(|https?://|<[@#!]", re.IGNORECASE)


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    return _presenter is not None and _presenter.enabled()


def _refuse(reason: str) -> None:
    logger.info("slack_ux_answer: keeping the upstream post, the fold is refused: %s", reason)


_block_kit = None


def _load_block_kit() -> Any:
    global _block_kit
    if _block_kit is None:
        spec = importlib_util.spec_from_file_location("slack_ux_answer_block_kit", BLOCK_KIT)
        module = importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _block_kit = module
    return _block_kit


def split(answer: str) -> tuple[str, str] | None:
    """``(headline, rest)``: the first sentence as plain text, everything after it as markdown.

    None, with the reason logged, when the headline cannot carry that sentence whole or
    nothing follows it.
    """
    if len(answer) > FOLD_TEXT_MAX:
        return _refuse(f"longer than {FOLD_TEXT_MAX} characters")
    if NOT_PROSE.match(answer.lstrip("\n")):
        return _refuse("it opens with a heading, a list item or a code fence")
    lead, body = _presenter.split_lead(answer)
    if LOSES_CONTENT.search(lead):
        return _refuse("the first sentence holds a link or a mention")
    headline = _presenter._plain(lead)
    if not headline:
        return _refuse("it does not open with a sentence")
    if len(headline) > _presenter.HEADLINE_MAX:
        return _refuse("the first sentence is longer than HEADLINE_MAX")
    rest = "\n\n".join(section for section in body if section.strip())
    if not rest:
        return _refuse("nothing follows the first sentence")
    return headline, rest


def render_fold(rest: str, mrkdwn_fn: Any = None) -> list[dict] | None:
    """``rest`` as the blocks the adapter's own send would render; None, with the reason logged, if it cannot fold."""
    block_kit = _load_block_kit()
    blocks = block_kit.sanitize_blocks(block_kit.render_blocks(rest, mrkdwn_fn=mrkdwn_fn))
    if not blocks:
        return _refuse("the Slack plugin rendered no blocks")
    outside = sorted({str(block.get("type")) for block in blocks} - FOLD_CHILD_TYPES)
    if outside:
        return _refuse("block types outside FOLD_CHILD_TYPES: " + ", ".join(outside))
    return blocks


def blocks_answer(headline: str, fold_blocks: list[dict]) -> list[dict]:
    """``headline`` in bold, then ``fold_blocks`` folded under :data:`FOLD_TITLE`, as Block Kit."""
    bold = {"type": "text", "text": headline, "style": {"bold": True}}
    return [
        {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [bold]}]},
        {
            "type": "container",
            "title": {"type": "plain_text", "text": FOLD_TITLE},
            "is_collapsible": True,
            "default_collapsed": True,
            "child_blocks": fold_blocks,
        },
    ]


def _renders_rich_blocks(adapter: Any) -> bool:
    flag = getattr(adapter, "_extra_flag", None)
    return callable(flag) and bool(flag(RICH_BLOCKS)) and not flag(MARKDOWN_BLOCKS)


class _AnswerFolder:
    """The notifier's adapter, with ``send`` posting the answer folded."""

    def __init__(self, adapter: Any, chat_id: str):
        self._adapter = adapter
        self._chat_id = chat_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def send(self, chat_id: Any, content: Any, metadata: Any = None, **kwargs: Any) -> Any:
        if str(chat_id) == self._chat_id and isinstance(content, str) and not kwargs:
            try:
                posted = await self._post(content.strip(), metadata)
            except Exception as exc:  # noqa: BLE001 — the upstream post still delivers it
                logger.warning("slack_ux_answer: could not post the answer folded, posting it whole: %s", exc)
                posted = None
            if posted is not None:
                return posted
        return await self._adapter.send(chat_id, content, metadata=metadata, **kwargs)

    async def _post(self, content: str, metadata: Any) -> Any:
        """The folded post's result, or None when it is refused and the upstream send should run."""
        parts = split(content)
        if parts is None:
            return None
        headline, rest = parts
        adapter = self._adapter
        mrkdwn_fn = getattr(adapter, "format_message", None)
        fold_blocks = render_fold(rest, mrkdwn_fn)
        if not fold_blocks:
            return None
        if adapter._outbound_blocked(self._chat_id, OUTBOUND_LABEL):
            # Upstream's send refuses it as well, and says so in its result.
            return None
        channel = await adapter._dm_target(self._chat_id, metadata)
        team_id = adapter._metadata_team_id(metadata)
        thread_ts = adapter._resolve_thread_ts(None, metadata)
        response = await adapter._client_for(channel, metadata).chat_postMessage(
            channel=channel,
            text=mrkdwn_fn(content) if callable(mrkdwn_fn) else content,
            blocks=blocks_answer(headline, fold_blocks),
            **({"thread_ts": thread_ts} if thread_ts else {}),
        )
        ts = str(response.get("ts") or "")
        try:
            if ts:
                # As upstream's send does, so a reply to it is answered without an @mention.
                adapter._bot_message_ts.add(adapter._workspace_message_marker(team_id, ts))
        except Exception as exc:  # noqa: BLE001 — posted; only reply tracking is lost
            logger.debug("slack_ux_answer: could not track %s: %s", ts, exc)
        logger.info("slack_ux_answer: posted the answer folded in %s", channel)
        return SimpleNamespace(success=True, message_id=ts, error=None)


def adapter_for(adapter: Any, platform: str, event: Any, task: Any, sub: Any) -> Any:
    """``adapter``, or one whose ``send`` posts a finished card's answer folded.

    The folder is returned only with the flag on, on Slack, for a ``completed``
    event bound for a chat, through an adapter rendering ``rich_blocks``. Whether
    an answer folds is decided when it is sent, since that is when its text is
    known. Everything else, including any error deciding, gets ``adapter`` itself.
    """
    try:
        if not enabled() or str(platform or "").lower() != SLACK:
            return adapter
        if getattr(event, "kind", None) != COMPLETED or not isinstance(sub, dict):
            return adapter
        chat_id = str(sub.get("chat_id") or "").strip()
        if not chat_id or not _renders_rich_blocks(adapter):
            return adapter
        return _AnswerFolder(adapter, chat_id)
    except Exception as exc:  # noqa: BLE001 — never fail a delivery on presentation
        logger.warning("slack_ux_answer: not folding the answer: %s", exc)
        return adapter
