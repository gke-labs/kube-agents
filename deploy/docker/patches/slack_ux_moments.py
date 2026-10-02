"""Post the moments a Slack thread should not miss as messages of their own.

Installed into the image at ``/opt/hermes/gateway/slack_ux_moments.py``.
``gateway/kanban_progress_lines.py`` calls it for a Slack card when
``KAGE_SLACK_UX`` is on, and ``apply_slack_ux_moments.py`` has every notifier
wake's text pass through :func:`wake_text`, flag on or off. That function does
not read the flag: with it off no question was posted, so the wake text comes
back unchanged. What the messages look like is
``agents/platform/scripts/slack_moments.py``.

Upstream, and why it changes
----------------------------
**A pull request opened.** A worker that opens a PR says so in a progress note
or its report, so the link sits on a plan row or inside the report, where it
reads like any other step. With the flag on, :func:`pr_opened` also posts it
as its own message with "Open PR" and "Files changed" buttons, once per PR per
thread for the life of the process (a restart forgets), since a note and the
report often both carry it.

**A question the work waits on.** A card blocked with ``kind: needs_input``
reaches the thread as "⏸ <head> blocked: <reason>", clipped to 160
characters, and the ``blocked`` wake has the Planning Agent explain it, so
the thread gets the question twice, once as a paraphrase. With the flag on,
:func:`needs_you` instead posts the reason as a question: the first line in
bold (plain if bold would drop a ``*``), the rest below (clipped at 2,000
characters), a choice button per option the question ends with, and "waiting
on you". Buttons need the card's thread, since a click answers in the thread
it was clicked in; a card with no thread keeps its options as text.
The wake still runs, since it is how the Planning Agent learns which card an
answer belongs to, but :func:`wake_text` adds a note that the question is
already posted, so it says nothing now and only takes the answer, typed or
clicked (``gateway/slack_ux_clicks.py``), to the card with ``kanban_comment``
and ``kanban_unblock``. A click's turn names the card, which
:func:`question_card` looks up. A typed answer can open a session the wake
never reached, which knows the thread only by reading it back, so the
question's ``text`` names the card too, above its buttons' "Reply with one
of:" line: the adapter reads a thread back through ``text``, and Slack does
not render ``text`` beside the blocks; it is the fallback for notifications
and anywhere blocks cannot render. Other block kinds keep the line.

When the card moves on, any event of it, :func:`settle_question` takes the
buttons and "waiting on you" off the question, so a typed answer does not
leave them live. A question a click already answered was rewritten by the
click and is left alone; one whose rewrite failed is settled here. The
notifier delivers at least once, so a ``blocked`` event replayed after its
question posted (:func:`asked`) neither settles nor reposts it. A question
whose settle failed when the card asked again is kept and retried with the
card's next settle. The open questions are held in process, so a restart
leaves the buttons of any it forgot.

Each moment carries a side bar, green for a PR and yellow for a question: a
legacy attachment holding every block below the headline. A settle sends the
attachment again, since ``chat.update`` would otherwise keep the old one, buttons
and all.

Fail-soft: a moment that cannot be posted is logged, and the caller falls back
to what it did before.
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_moments as _moments
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _moments = None
    _presenter = None

#: The flag, read here only to word the warning when the renderer is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: ``slack_presenter.FLAG_ON_VALUES``, copied because the warning below fires
#: exactly when that module cannot be imported.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: The block kind that asks the user something; the others are not questions.
NEEDS_INPUT = "needs_input"

#: The event kind a posted question arrives as, and the one its wake is for.
BLOCKED_KIND = "blocked"

#: Added to the ``blocked`` wake when the question is already posted.
WAKE_NOTE = (
    "The specialist's question is already posted to the user as its own message, in the "
    "specialist's words. Do not restate, paraphrase or acknowledge it. If nothing else "
    "in this notification needs saying, reply with exactly [SILENT]. When the user answers, "
    "typed or clicked, carry the answer to the card with kanban_comment, then kanban_unblock."
)

#: Added to a question's ``text``, so a session the wake never reached reads its card in the thread.
QUESTION_CARD_NOTE = "(Question from card {card}.)"

#: Bound on each in-process map, oldest evicted first.
ANNOUNCED_MAX = 512

#: ``(channel, thread, pr url)`` already posted.
_announced: OrderedDict[tuple, None] = OrderedDict()
#: Subscription -> ``(event id, channel, ts, blocks, text)`` of its open question.
_questions: OrderedDict[tuple, tuple] = OrderedDict()
#: ``(subscription, ts)`` -> the entry of a question asked again before its settle succeeded.
_unsettled: OrderedDict[tuple, tuple] = OrderedDict()
_warned_missing = False


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter and renderer are importable."""
    global _warned_missing
    if _presenter is not None and _moments is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_ux_moments: %s is set but slack_presenter or slack_moments is not "
            "importable; treating the flag as off",
            FLAG_ENV,
        )
    return False


def _sub_key(sub: dict) -> tuple:
    """The subscription's identity, as upstream's ``_KanbanNotification.sub_key``."""
    return (
        str(sub.get("task_id") or ""),
        str(sub.get("platform") or ""),
        str(sub.get("chat_id") or ""),
        str(sub.get("thread_id") or ""),
    )


def _remember(mapping: OrderedDict, key: tuple, value: Any) -> None:
    mapping[key] = value
    mapping.move_to_end(key)
    while len(mapping) > ANNOUNCED_MAX:
        mapping.popitem(last=False)


async def _post(adapter: Any, sub: dict, blocks: list[dict], text: str, color: str) -> str | None:
    """Post in the card's thread beside a ``color`` side bar; the message's ts ("" if
    Slack gave none), None on failure."""
    chat_id = str(sub.get("chat_id") or "")
    if not (chat_id and hasattr(adapter, "_get_client")):
        return None
    try:
        client = adapter._get_client(chat_id, team_id=sub.get("team_id") or None)
        response = await client.chat_postMessage(
            channel=chat_id,
            thread_ts=sub.get("thread_id") or None,
            text=text,
            **_presenter.with_side_bar(blocks, color),
        )
    except Exception as exc:  # noqa: BLE001 — the caller falls back
        logger.warning("slack_ux_moments: posting in %s failed: %s", chat_id, exc)
        return None
    try:
        return str(response.get("ts") or "")
    except Exception:  # noqa: BLE001 — posted; only the settle needs the ts
        return ""


async def pr_opened(adapter: Any, sub: dict, text: str) -> bool:
    """Post the PR ``text`` says was opened, once per thread; True when posted."""
    found = _moments.opened_pr(text)
    if found is None:
        return False
    url, repo, number, line = found
    key = (str(sub.get("chat_id") or ""), str(sub.get("thread_id") or ""), url)
    if key in _announced:
        return False
    blocks, fallback = _moments.pr_opened(url, repo, number, line)
    if await _post(adapter, sub, blocks, fallback, _moments.PR_SIDE_BAR) is None:
        return False
    _remember(_announced, key, None)
    return True


async def needs_you(adapter: Any, sub: dict, payload: Any, event_id: int = 0) -> bool:
    """Post a ``needs_input`` block's reason as a question; True when posted.

    False for any other block, an empty reason, or a post that failed, and the
    caller delivers the blocked line as before. ``event_id`` is the ``blocked``
    event's, which :func:`wake_text` matches against the wake's events.
    """
    if not isinstance(payload, dict) or payload.get("kind") != NEEDS_INPUT:
        return False
    moment = _moments.needs_you(
        str(payload.get("reason") or ""), buttons=bool(sub.get("thread_id"))
    )
    if moment is None:
        return False
    if asked(sub, event_id):
        return True
    # A card blocks again only after it was unblocked, so an earlier question is answered.
    await settle_question(adapter, sub)
    key = _sub_key(sub)
    earlier = _questions.get(key)
    if earlier is not None:
        # Still open: its settle failed or never ran. The new question takes the slot, so keep this one for a retry.
        _remember(_unsettled, (key, earlier[2]), earlier)
    blocks, text = moment
    text = _with_card(text, key[0], any(b.get("type") == "actions" for b in blocks))
    ts = await _post(adapter, sub, blocks, text, _moments.NEEDS_YOU_SIDE_BAR)
    if ts is None:
        return False
    entry = (int(event_id or 0), str(sub.get("chat_id") or ""), ts, blocks, text)
    _remember(_questions, key, entry)
    return True


def _with_card(text: str, card: str, buttons: bool) -> str:
    """``text`` naming ``card`` on the line above its buttons' "Reply with one of:" line,
    or last without buttons: a click's rewrite drops that line only while it is the last."""
    if not card:
        return text
    note = QUESTION_CARD_NOTE.format(card=card)
    body, _nl, last = text.rpartition("\n")
    if buttons and body and last.startswith(_presenter.CHOICES_LEAD):
        return f"{body}\n{note}\n{last}"
    return f"{text}\n{note}"


def asked(sub: dict, event_id: int) -> bool:
    """Whether the card's open question was posted for the ``blocked`` event ``event_id``:
    a notifier replay of that event, which must not settle or repost it."""
    entry = _questions.get(_sub_key(sub))
    return bool(event_id) and entry is not None and entry[0] == int(event_id)


def question_card(channel: str, ts: str) -> str | None:
    """The card whose open question this process posted as ``ts`` in ``channel``, else None.

    A question kept in ``_unsettled`` for a retry still has live buttons, so it counts too.
    """
    entries = [*_questions.items(), *((key[0], entry) for key, entry in _unsettled.items())]
    for key, (_event_id, posted_channel, posted_ts, _blocks, _text) in entries:
        if ts and posted_ts == str(ts) and posted_channel == str(channel):
            return key[0] or None
    return None


def _clicked(channel: str, ts: str) -> bool:
    """Whether a click in this process already answered the message."""
    try:
        from gateway import slack_ux_clicks
    except ImportError:
        return False
    try:
        return bool(slack_ux_clicks.answered(channel, ts))
    except Exception:  # noqa: BLE001 — read as unanswered; the settle is cosmetic
        return False


async def _settled(adapter: Any, sub: dict, entry: tuple) -> bool:
    """Rewrite one question without its buttons; True once it needs nothing more."""
    _event_id, channel, ts, blocks, text = entry
    if not ts or _clicked(channel, ts):
        return True
    try:
        client = adapter._get_client(channel, team_id=sub.get("team_id") or None)
        await client.chat_update(
            channel=channel, ts=ts, text=text,
            **_presenter.with_side_bar(_moments.needs_you_settled(blocks), _moments.NEEDS_YOU_SIDE_BAR),
        )
    except Exception as exc:  # noqa: BLE001 — cosmetic; the next event retries
        logger.warning("slack_ux_moments: settling the question %s failed: %s", ts, exc)
        return False
    return True


async def settle_question(adapter: Any, sub: dict) -> None:
    """Take the buttons and "waiting on you" off the card's open question, if it has one.

    The question is forgotten only once the rewrite succeeds (or a click has
    answered it), so a failed rewrite is retried on the card's next event and
    a click in between still names the card. Earlier questions of the card
    whose settle failed are retried too.
    """
    key = _sub_key(sub)
    for stale_key, stale in [item for item in _unsettled.items() if item[0][0] == key]:
        if await _settled(adapter, sub, stale) and _unsettled.get(stale_key) is stale:
            _unsettled.pop(stale_key, None)
    entry = _questions.get(key)
    if entry is None:
        return
    if await _settled(adapter, sub, entry) and _questions.get(key) is entry:
        _questions.pop(key, None)


def wake_text(sub: dict, events: Iterable[Any], wake_kinds: Any, text: str) -> str:
    """``text`` with :data:`WAKE_NOTE` added when the wake is for a question this posted.

    Matched on the ``blocked`` event's id, so a notifier retry of the same
    delivery gets the note again and a later block that posted no question
    does not. Anything else returns ``text`` unchanged.
    """
    try:
        if BLOCKED_KIND not in set(wake_kinds or ()):
            return text
        entry = _questions.get(_sub_key(sub))
        if entry is None or not entry[0]:
            return text
        blocked = {
            int(getattr(ev, "id", 0) or 0)
            for ev in events or ()
            if getattr(ev, "kind", None) == BLOCKED_KIND
        }
        return f"{text}\n\n{WAKE_NOTE}" if entry[0] in blocked else text
    except Exception:
        logger.debug("slack_ux_moments: reading the posted question failed", exc_info=True)
        return text
