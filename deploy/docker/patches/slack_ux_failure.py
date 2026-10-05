"""Lead a failure reply with its fact in bold, and offer its closing question as a button.

Installed into the image at ``/opt/hermes/gateway/slack_ux_failure.py``.
``apply_slack_ux_failure.py`` wires four calls: the kanban notifier's
``build_wake_text`` calls :func:`note_wake`, ``send_final_ledgered`` brackets
its send with :func:`begin` and :func:`end`, ``SlackAdapter._maybe_blocks``
hands its rendering to :func:`maybe_blocks`, and ``_run_agent_queued_followup``
calls :func:`drop`. With ``KAGE_SLACK_UX`` off
:func:`note_wake` records nothing, so no reply is marked and every call returns
what upstream would have.

Upstream, and why it changes
----------------------------
A card that blocked or failed wakes the Planning Agent, whose reply is the
thread's only word on it. Mock 06's reply opens on what did not happen ("I
couldn't find seeded-z.") and ends on the retry as a question ("Check it
there?"). Upstream renders that reply like any other, so the failure reads as a
paragraph and answering it means typing. Nothing here depends on the reply
taking that shape: any reply to such a wake gets the bold lead, and the button
only when it ends on a yes/no question.

With the flag on, the Slack reply to such a wake is drawn with its first
sentence in bold and, when it ends on one short yes/no question, a choice button
carrying that question ("check it there"). An open question ("Which namespace
should it use?") or an either/or stays text, since it needs a word, not a click. A click is the clicker answering in
the thread with the button's text (``gateway/slack_ux_clicks.py``), so the
Planning Agent reads it as the user saying yes. The words are the agent's; only
how they are drawn changes. The ``text`` Slack keeps for notifications and for
a later read of the thread is unchanged, question included.

Which reply is the wake's: :func:`note_wake` marks the subscription's thread
when a Slack wake carries ``blocked``, ``crashed``, ``timed_out`` or
``gave_up``, unless ``gateway/slack_ux_moments.py`` noted the question already
posted (that wake is answered with ``[SILENT]``, so it clears the thread's mark
instead). The next final reply sent for an internal event in that thread takes
the mark, within :data:`MARK_TTL_SECONDS`; a reply the user's own message
prompted never does. A message queued behind a running turn runs as a follow-up
whose reply is sent under that turn's event: a queued wake's reply is the wake's
and keeps the mark, while a user's message drops it (:func:`drop`), so neither
reply is drawn as the failure's. Outside a thread the reply keeps its question
as text, since a click answers in the thread it was clicked in. Kanban pings and moments are
not finals and never look. The marks are this process's, so a restart between a
wake and its reply drops the look, not the reply.

A reply that arrives as edits to a streamed message, rather than through
``send_final_ledgered``, is drawn as upstream draws it, and its mark waits for
the TTL or the thread's next failure wake.

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
#: A question that asks for a word rather than a yes: an open one, by its first
#: word, or an either/or. Its click would post the question back as the answer.
OPEN_OPENERS = frozenset({"what", "what's", "whats", "which", "who", "whom", "whose", "when", "where", "why", "how"})
EITHER_OR = re.compile(r"\bor\b", re.IGNORECASE)
#: How a sentence ends, so a soft-wrapped lead is not joined past its end.
SENTENCE_ENDS = (".", "!", "?")
#: A first word the button may lower: capitalised only for starting the
#: sentence, so not "I", "I'll", or an acronym.
LOWERABLE = re.compile(r"^(?!I(?:'|$))[A-Z](?:[a-z]|$)")

#: Slack's cap on a message's blocks; a reply already at it keeps its question as text.
MESSAGE_BLOCKS_MAX = 50

#: Marked threads, ``(chat_id, thread_id) -> monotonic time``.
_marks: "OrderedDict[tuple[str, str], float]" = OrderedDict()

#: The marked thread's id ("" outside a thread) for the length of one marked final's send.
_marked: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("kage_failure_reply", default=None)


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
        key = _key(sub.get("chat_id"), sub.get("thread_id"))
        _marks.pop(key, None)
        if _wake_note() in (text or ""):
            return
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
        key = _key(source.chat_id, getattr(source, "thread_id", None))
        noted = _marks.pop(key, None)
        if noted is None or time.monotonic() - noted > MARK_TTL_SECONDS:
            return None
        return _marked.set(key[1])
    except Exception:
        logger.warning("slack_ux_failure: reading the final's thread failed", exc_info=True)
        return None


def drop(source: Any, pending_event: Any) -> None:
    """Clear the mark on ``source``'s thread when the queued follow-up is a user's message."""
    try:
        if _marks and source is not None and not getattr(pending_event, "internal", False):
            _marks.pop(_key(source.chat_id, getattr(source, "thread_id", None)), None)
    except Exception:
        logger.warning("slack_ux_failure: clearing the follow-up's thread failed", exc_info=True)


def end(token: Optional[contextvars.Token]) -> None:
    """Undo :func:`begin`."""
    if token is not None:
        _marked.reset(token)


def present(content: str) -> tuple[str, str]:
    """``(content with its first sentence in bold, offer label or "")``.

    A first sentence soft-wrapped onto the next lines is joined onto one line
    and bolded whole, as ``slack_presenter.split_answer`` reads it; a list item,
    heading or fence does not continue it. The bold is left off when the first
    line opens with markup or a list marker, or the first sentence holds markup,
    since a ``*`` inside would unpair. The offer is the last sentence when it is
    one yes/no question with no markup that fits a button, with the ``?``
    dropped and its first letter lowered unless that would change a word that
    is capitalised anyway ("I", "OK", "API").
    """
    text = content or ""
    lead = text.lstrip()
    indent = text[: len(text) - len(lead)]
    lines = lead.split("\n")
    first, taken = lines[0], 1
    while (
        taken < len(lines)
        and not first.rstrip().endswith(SENTENCE_ENDS)
        and not _presenter._first_sentence(first)[1]
        and _continues(lines[taken])
    ):
        first = f"{first.rstrip()} {lines[taken].strip()}"
        taken += 1
    sentence, rest = _presenter._first_sentence(first)
    if sentence and sentence[0].isalnum() and not MARKUP.search(sentence) and not _presenter.LIST_MARKER.match(lines[0]):
        bolded = indent + "\n".join([f"**{sentence}**" + (f" {rest}" if rest else "")] + lines[taken:])
    else:
        bolded = text
    match = TRAILING_QUESTION.search(text.rstrip())
    question = match.group(1).strip() if match else ""
    if not question or MARKUP.search(question) or len(question) > _presenter.BUTTON_TEXT_MAX:
        return bolded, ""
    word = question.split()[0]
    if word.lower() in OPEN_OPENERS or EITHER_OR.search(question):
        return bolded, ""
    if LOWERABLE.match(word):
        question = question[0].lower() + question[1:]
    return bolded, question


def _continues(line: str) -> bool:
    """Whether ``line`` carries on the sentence above it rather than starting a block."""
    return bool(line.strip()) and not (
        _presenter.LIST_MARKER.match(line) or _presenter.HEADING.match(line) or _presenter._opens_fence(line)
    )


def maybe_blocks(content: str, render: Callable[[str], Optional[list]]) -> Optional[list]:
    """``render(content)``, or for a marked reply its :func:`present` form plus the offer."""
    thread = _marked.get()
    if thread is None:
        return render(content)
    try:
        bolded, label = present(content)
        blocks = render(bolded)
        if blocks is None:
            return render(content)
        if label and thread and len(blocks) < MESSAGE_BLOCKS_MAX:
            blocks = list(blocks) + _presenter.blocks_answer("", choices=[label], action_id_prefix=ACTION_ID_PREFIX)
        return blocks
    except Exception:
        logger.warning("slack_ux_failure: drawing the failure reply failed", exc_info=True)
        return render(content)
