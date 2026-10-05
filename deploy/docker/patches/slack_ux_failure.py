"""Lead a failure reply with its fact in bold, and offer its closing question as a button.

Installed into the image at ``/opt/hermes/gateway/slack_ux_failure.py``.
``apply_slack_ux_failure.py`` wires five calls: the kanban notifier's
``build_wake_text`` calls :func:`note_wake`, ``_process_message_background``
calls :func:`start`, ``send_final_ledgered`` brackets its send with
:func:`begin` and :func:`end`, ``SlackAdapter._maybe_blocks`` hands its
rendering to :func:`maybe_blocks`, and ``_run_agent_queued_followup`` calls
:func:`drop`. With ``KAGE_SLACK_UX`` off :func:`note_wake` records nothing, so
no reply is marked and every call returns what upstream would have.

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
should it use?"), a request ("Could you share the namespace?") or an either/or
stays text, since it needs a word, not a click. A click is the clicker answering in the thread with the button's text
(``gateway/slack_ux_clicks.py``), so the Planning Agent reads it as the user
saying yes. The words are the agent's; only how they are drawn changes. The
``text`` Slack keeps for notifications and for a later read of the thread is
unchanged, question included.

Which reply is the wake's: :func:`note_wake` marks the subscription's thread
when a Slack wake carries ``blocked``, ``crashed``, ``timed_out`` or
``gave_up``, unless ``gateway/slack_ux_moments.py`` noted the question already
posted (that wake is answered with ``[SILENT]``, so it clears the mark its own
card left instead). The wake's turn claims the mark when it starts (:func:`start`, or
:func:`drop` for a follow-up drained from behind a running turn), and the next
final reply sent for that turn takes the claim within :data:`MARK_TTL_SECONDS`.
Hermes sends that reply under the wake's internal event, under the empty event
it builds for a reply with a follow-up queued behind it, or, when the wake ran
as a follow-up, under the outer turn's event, which may be the user's. A turn
that does not claim never looks: a user's turn starting after the mark was
noted clears it, a turn that was already running or queued when the mark was
noted leaves it for the wake's own turn, and a reply the user's own message
prompted never takes a claim. A claim the turn never sent (a ``[SILENT]`` or
streamed reply) is dropped at the thread's next turn start or by its next final
rather than drawing a later message of the user's. Outside a thread the reply
keeps its question as text, since a click answers in the thread it was clicked
in. Kanban pings and moments are not finals and never look. The marks are this
process's, so a restart between a wake and its reply drops the look, not the
reply.

A reply that arrives as edits to a streamed message, rather than through
``send_final_ledgered``, is drawn as upstream draws it, and its claim is
dropped as an unsent one is.

Fail-soft: anything that raises is logged and the reply goes out as upstream
renders it.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime
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
#: An inline code span as upstream's ``format_message`` protects one: kept as code
#: inside a bold lead, with the words around it bolded and the span itself not.
CODE_SPAN = re.compile(r"`[^`\n]+`")
#: A question that asks for a word rather than a yes: an open one, by its first
#: word or a question word anywhere in it ("Can you tell me which cluster?"), a
#: request for something ("Could you share the namespace?"), or an either/or. Its
#: click would post the question back as the answer.
OPEN_OPENERS = frozenset(
    {"what", "whats", "which", "who", "whom", "whose", "when", "where", "why", "how", "anything"}
)
OPEN_WORD = re.compile(r"\b(?:what|which|who|whom|whose|when|where|why|how)\b", re.IGNORECASE)
REQUEST = re.compile(
    r"^(?:(?:could|can|would|will) you (?:please )?(?:share|tell|give|send|provide|paste|"
    r"point|list|name|let me know)|do you (?:know|have))\b",
    re.IGNORECASE,
)
#: Words skipped before the opener ("So, which one?"), and what is stripped off it.
OPENER_FILLERS = frozenset({"so", "and", "then", "ok", "okay"})
OPENER_PUNCTUATION = ",;:"
OPENER_CONTRACTION = "'s"
CURLY_APOSTROPHE = "\u2019"
EITHER_OR = re.compile(r"\bor\b", re.IGNORECASE)
#: How a sentence ends, so a soft-wrapped lead is not joined past its end.
SENTENCE_ENDS = (".", "!", "?")
#: A first word the button may lower: capitalised only for starting the
#: sentence, so not "I", "I'll", or an acronym.
LOWERABLE = re.compile(r"^(?!I(?:'|$))[A-Z](?:[a-z]|$)")

#: Slack's cap on a message's blocks; a reply already at it keeps its question as text.
MESSAGE_BLOCKS_MAX = 50

#: Marked threads, ``(chat_id, thread_id) -> (monotonic time, wall time, card id)``.
_marks: "OrderedDict[tuple[str, str], tuple[float, datetime, str]]" = OrderedDict()
#: Marks a queued wake carried to its follow-up's reply, ``(chat_id, thread_id) ->
#: (mark time, when carried)``: taken under any event that arrived before the carry.
_carried: "OrderedDict[tuple[str, str], tuple[float, datetime]]" = OrderedDict()

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
        card = str(sub.get("task_id") or "")
        if _wake_note() in (text or ""):
            # Only this card's own mark: a sibling's failure wake may still be queued.
            if key in _marks and _marks[key][2] == card:
                del _marks[key]
            return
        _marks.pop(key, None)
        _marks[key] = (time.monotonic(), datetime.now(), card)
        while len(_marks) > MARKS_MAX:
            _marks.popitem(last=False)
    except Exception:
        logger.warning("slack_ux_failure: marking the failure wake failed", exc_info=True)


def begin(event: Any) -> Optional[contextvars.Token]:
    """Mark this send when its turn claimed a mark (:func:`start`); see :func:`end`.

    The claim is taken by the wake turn's own reply: under its internal event, under
    the fresh event Hermes sends it under when a follow-up is queued behind it, or,
    for a wake queued behind a user's turn, under that turn's event.
    """
    try:
        source = getattr(event, "source", None)
        if not _carried or source is None:
            return None
        key = _slack_key(source)
        if key is None:
            return None
        carried = _carried.pop(key, None)
        if carried is None:
            return None
        if not (getattr(event, "internal", False) or _queued_reply(event) or _arrived_by(event, carried[1])):
            return None
        if time.monotonic() - carried[0] > MARK_TTL_SECONDS:
            return None
        return _marked.set(key[1])
    except Exception:
        logger.warning("slack_ux_failure: reading the final's thread failed", exc_info=True)
        return None


def start(event: Any) -> None:
    """A turn for ``event`` is starting: a wake claims its thread's mark for its reply,
    and a user's message clears a mark noted before it arrived.

    Any earlier claim is dropped, since the turn that held it is over. A mark noted
    after ``event`` arrived belongs to a wake still to come, and stays.
    """
    try:
        source = getattr(event, "source", None)
        if not (_marks or _carried) or source is None:
            return
        key = _slack_key(source)
        if key is None:
            return
        _carried.pop(key, None)
        mark = _marks.get(key)
        if mark is None:
            return
        if not getattr(event, "internal", False):
            if not _arrived_by(event, mark[1]):
                del _marks[key]
            return
        if not _noted_by(event, mark[1]):
            return
        del _marks[key]
        _carried[key] = (mark[0], datetime.now())
        while len(_carried) > MARKS_MAX:
            _carried.popitem(last=False)
    except Exception:
        logger.warning("slack_ux_failure: claiming or clearing the turn's mark failed", exc_info=True)


def drop(source: Any, pending_event: Any) -> None:
    """:func:`start` for a follow-up drained from behind the turn on ``source``.

    A follow-up with no queued event (a ``/steer``) still ends the turn before it,
    so it drops that turn's claim.
    """
    if pending_event is not None:
        start(pending_event)
        return
    try:
        key = _slack_key(source) if _carried and source is not None else None
        if key is not None:
            _carried.pop(key, None)
    except Exception:
        logger.warning("slack_ux_failure: dropping the turn's claim failed", exc_info=True)


def _slack_key(source: Any) -> Optional[tuple[str, str]]:
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", ""))
    if str(platform or "").lower() != SLACK_PLATFORM:
        return None
    return _key(source.chat_id, getattr(source, "thread_id", None))


def _queued_reply(event: Any) -> bool:
    """Whether ``event`` is the empty one Hermes sends a finished turn's reply under when
    a follow-up is queued behind it, for a turn with no inbound message."""
    return not (
        getattr(event, "internal", False)
        or getattr(event, "message_id", None)
        or getattr(event, "ledger_message_id", None)
        or getattr(event, "text", "")
    )


def _arrived_by(event: Any, moment: datetime) -> bool:
    """Whether ``event`` arrived by ``moment``, as ``MessageEvent.timestamp`` records it."""
    arrived = getattr(event, "timestamp", None)
    return isinstance(arrived, datetime) and arrived.tzinfo is None and arrived <= moment


def _noted_by(event: Any, moment: datetime) -> bool:
    """Whether ``moment`` is no later than ``event``'s arrival; true when it has none."""
    arrived = getattr(event, "timestamp", None)
    if not isinstance(arrived, datetime) or arrived.tzinfo is not None:
        return True
    return moment <= arrived


def end(token: Optional[contextvars.Token]) -> None:
    """Undo :func:`begin`."""
    if token is not None:
        _marked.reset(token)


def present(content: str) -> tuple[str, str]:
    """``(content with its first sentence in bold, offer label or "")``.

    A first sentence soft-wrapped onto the next lines is joined onto one line
    and bolded whole, as ``slack_presenter.split_answer`` reads it; a list item,
    heading or fence does not continue it. A code span in it stays code, with
    the words on either side bolded and the span not (``**Couldn't find**
    `seeded-z`.``). The bold is left off when the first line opens with other
    markup, a heading or a list marker, or the first sentence holds markup
    outside its code spans, since a ``*`` inside would unpair. The offer is the
    last sentence when it is one yes/no question with no markup, not on a list
    item or heading line, that fits a button, with the ``?`` dropped and its
    first letter lowered unless that would change a word that is capitalised
    anyway ("I", "OK", "API").
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
    strong = _bold(sentence) if sentence else None
    if strong and not (_presenter.LIST_MARKER.match(lines[0]) or _presenter.HEADING.match(lines[0])):
        bolded = indent + "\n".join([strong + (f" {rest}" if rest else "")] + lines[taken:])
    else:
        bolded = text
    match = TRAILING_QUESTION.search(text.rstrip())
    question = match.group(1).strip() if match else ""
    if not question or MARKUP.search(question) or len(question) > _presenter.BUTTON_TEXT_MAX:
        return bolded, ""
    last = text.rstrip().rsplit("\n", 1)[-1]
    if _presenter.LIST_MARKER.match(last) or _presenter.HEADING.match(last):
        return bolded, ""
    word = question.split()[0]
    if (
        _opener(question) in OPEN_OPENERS
        or OPEN_WORD.search(question)
        or REQUEST.match(question)
        or EITHER_OR.search(question)
    ):
        return bolded, ""
    if LOWERABLE.match(word):
        question = question[0].lower() + question[1:]
    return bolded, question


def _opener(question: str) -> str:
    """The word ``question`` opens on, lowered, past a filler ("So, which") and without a trailing 's."""
    for word in question.replace(CURLY_APOSTROPHE, "'").lower().split():
        word = word.strip(OPENER_PUNCTUATION)
        if word not in OPENER_FILLERS:
            return word.removesuffix(OPENER_CONTRACTION)
    return ""


def _bold(sentence: str) -> Optional[str]:
    """``sentence`` with each run of words outside its code spans in bold, or None
    when markup sits outside a code span, a bolded run touches a span without a
    space between (Slack would show the ``*`` as typed), or no run holds a word."""
    runs, spans = CODE_SPAN.split(sentence), CODE_SPAN.findall(sentence)
    if any(MARKUP.search(run) for run in runs):
        return None
    out = []
    for i, run in enumerate(runs):
        core = run.strip()
        if any(c.isalnum() for c in core):
            if (i > 0 and not run[:1].isspace()) or (i < len(spans) and not run[-1:].isspace()):
                return None
            start = run.index(core)
            run = f"{run[:start]}**{core}**{run[start + len(core):]}"
        out.append(run + (spans[i] if i < len(spans) else ""))
    strong = "".join(out)
    return strong if "**" in strong else None


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
