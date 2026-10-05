"""Lead a failure reply with its fact in bold, and offer its closing question as a button.

Installed into the image at ``/opt/hermes/gateway/slack_ux_failure.py``.
``apply_slack_ux_failure.py`` wires three calls: the kanban notifier's
``build_wake_text`` calls :func:`note_wake`, ``send_final_ledgered`` brackets
its send with :func:`begin` and :func:`end`, and ``SlackAdapter._maybe_blocks``
hands its rendering to :func:`maybe_blocks`. With ``KAGE_SLACK_UX`` off
:func:`note_wake` records nothing, so no reply is marked and every call returns
what upstream would have.

Upstream, and why it changes
----------------------------
A card that blocked or failed wakes the Planning Agent, whose reply is the
thread's only word on it (``agents/chat/SOUL.md`` §2 step 5): it opens on what
did not happen ("I couldn't find seeded-z.") and, for a card nothing will run
again, ends on the retry as a question ("Check it there?"). Upstream renders
that reply like any other, so the failure reads as a paragraph and answering
it means typing.

With the flag on, the Slack reply to such a wake is drawn with its first
sentence in bold and, when it ends on one short question, a choice button
carrying that question ("check it there"). A click is the clicker answering in
the thread with the button's text (``gateway/slack_ux_clicks.py``), so the
Planning Agent reads it as the user saying yes. The words are the agent's; only
how they are drawn changes. The ``text`` Slack keeps for notifications and for
a later read of the thread is unchanged, question included.

Which reply is the wake's: :func:`note_wake` marks the subscription's thread
when a Slack wake carries ``blocked``, ``crashed``, ``timed_out`` or
``gave_up``, unless ``gateway/slack_ux_moments.py`` noted the question already
posted (that wake is answered with ``[SILENT]``). The next final reply sent for
an internal event in that thread takes the mark, within :data:`MARK_TTL_SECONDS`;
a reply the user's own message prompted never does. Kanban pings and moments are
not finals and never look. The marks are this process's, so a restart between a
wake and its reply drops the look, not the reply.

A reply that arrives as edits to a streamed message, rather than through
``send_final_ledgered``, is drawn as upstream draws it.

Fail-soft: anything that raises is logged and the reply goes out as upstream
renders it.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None

SLACK_PLATFORM = "slack"

#: The wake kinds whose reply is a failure's announcement. ``completed`` posts
#: the worker's own report, and the review kinds announce nothing.
FAILURE_KINDS = frozenset({"blocked", "crashed", "timed_out", "gave_up"})

#: How long a marked thread waits for its reply, and how many threads are held.
MARK_TTL_SECONDS = 15 * 60
MARKS_MAX = 256

#: The action id prefix of the offer button; ``slack_ux_clicks`` answers any
#: ``<prefix>.choice.<n>``.
ACTION_ID_PREFIX = "kage_failure"

#: What the button is cut from: the reply's last sentence, when it is one
#: question with no markup. ``[^.!?]`` keeps it one sentence.
TRAILING_QUESTION = re.compile(r"(?:^|(?<=[.!?])\s+)([^.!?\n]+)\?\s*$")
MARKUP = re.compile(r"[*_~`<>\[\]|]")

#: Slack's cap on a message's blocks; a reply already at it keeps its question as text.
MESSAGE_BLOCKS_MAX = 50

#: Marked threads, ``(chat_id, thread_id) -> monotonic time``.
_marks: "OrderedDict[tuple[str, str], float]" = OrderedDict()

#: Set for the length of one marked final's send.
_marked: contextvars.ContextVar[bool] = contextvars.ContextVar("kage_failure_reply", default=False)


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    return _presenter is not None and _presenter.enabled()


def _key(chat_id: Any, thread_id: Any) -> tuple[str, str]:
    return str(chat_id or ""), str(thread_id or "")


def _wake_note() -> str:
    """``slack_ux_moments.WAKE_NOTE``, the note on a wake whose question is already posted."""
    try:
        from gateway import slack_ux_moments
    except ImportError:  # loaded by path, beside it
        import slack_ux_moments
    return slack_ux_moments.WAKE_NOTE


def note_wake(sub: dict, wake_kinds: Iterable[str], text: str) -> None:
    """Mark ``sub``'s thread when this wake announces a failure on Slack."""
    try:
        if str(sub.get("platform") or "").strip().lower() != SLACK_PLATFORM:
            return
        if not FAILURE_KINDS & set(wake_kinds or ()) or not enabled():
            return
        if _wake_note() in (text or ""):
            return
        key = _key(sub.get("chat_id"), sub.get("thread_id"))
        _marks.pop(key, None)
        _marks[key] = time.monotonic()
        while len(_marks) > MARKS_MAX:
            _marks.popitem(last=False)
    except Exception:
        logger.warning("slack_ux_failure: marking the failure wake failed", exc_info=True)


def begin(event: Any) -> Optional[contextvars.Token]:
    """Mark this send when ``event`` is an internal turn in a marked thread; see :func:`end`."""
    try:
        source = getattr(event, "source", None)
        if not _marks or source is None or not getattr(event, "internal", False):
            return None
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
        if str(platform or "").lower() != SLACK_PLATFORM:
            return None
        noted = _marks.pop(_key(source.chat_id, getattr(source, "thread_id", None)), None)
        if noted is None or time.monotonic() - noted > MARK_TTL_SECONDS:
            return None
        return _marked.set(True)
    except Exception:
        logger.warning("slack_ux_failure: reading the final's thread failed", exc_info=True)
        return None


def end(token: Optional[contextvars.Token]) -> None:
    """Undo :func:`begin`."""
    if token is not None:
        _marked.reset(token)


def present(content: str) -> tuple[str, str]:
    """``(content with its first sentence in bold, offer label or "")``.

    The bold is left off when the first line opens with markup or its first
    sentence holds any, since a ``*`` inside would unpair. The offer is the last
    sentence when it is one question with no markup that fits a button, with its
    first letter lowered and the ``?`` dropped.
    """
    text = content or ""
    lead = text.lstrip()
    indent = text[: len(text) - len(lead)]
    first, newline, after = lead.partition("\n")
    sentence, rest = _presenter._first_sentence(first)
    if sentence and sentence[0].isalnum() and not MARKUP.search(sentence):
        first = f"**{sentence}**" + (f" {rest}" if rest else "")
    bolded = indent + first + newline + after
    match = TRAILING_QUESTION.search(text.rstrip())
    question = match.group(1).strip() if match else ""
    if not question or MARKUP.search(question) or len(question) > _presenter.BUTTON_TEXT_MAX:
        return bolded, ""
    return bolded, question[0].lower() + question[1:]


def maybe_blocks(content: str, render: Callable[[str], Optional[list]]) -> Optional[list]:
    """``render(content)``, or for a marked reply its :func:`present` form plus the offer."""
    if not _marked.get():
        return render(content)
    try:
        bolded, label = present(content)
        blocks = render(bolded)
        if blocks is None:
            return render(content)
        if label and len(blocks) < MESSAGE_BLOCKS_MAX:
            blocks = list(blocks) + _presenter.blocks_answer("", choices=[label], action_id_prefix=ACTION_ID_PREFIX)
        return blocks
    except Exception:
        logger.warning("slack_ux_failure: drawing the failure reply failed", exc_info=True)
        return render(content)
