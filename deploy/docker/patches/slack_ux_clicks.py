"""Answer clicks on kube-agents' own Slack buttons.

Installed into the image at ``/opt/hermes/gateway/slack_ux_clicks.py``.
``apply_slack_ux_clicks.py`` makes the Slack adapter call :func:`register`
when it wires its Bolt listeners, only when ``KAGE_SLACK_UX`` is on. With the
flag off nothing here is registered and the adapter's listeners are upstream's.

Upstream, and why it changes
----------------------------
Hermes handles clicks on its own buttons (approvals, clarify, the model
picker) and on buttons a plugin registered. Nothing handles the buttons
``slack_presenter`` lays out, so Slack delivers the ``block_actions`` and the
click does nothing.

With the flag on, :func:`register` adds two listeners:

* A choice button (``<prefix>.choice.<n>``) is the clicker answering in the
  thread with the button's text as Slack showed it. Never its ``value``: the
  presenter clips the text to ``BUTTON_TEXT_MAX`` but keeps up to
  ``BUTTON_VALUE_MAX`` in the value, and a click must not send words the clicker did not see. A
  label that starts like a command (``/`` or ``!``) is sent as text, since a
  choice is an answer. The click goes through the adapter's own interactive
  authorization; an unlisted user's click is logged and changes nothing, and
  so does one in a channel or DM the adapter would ignore a typed message in.
  Then the message is rewritten with the choice buttons replaced by
  a line naming who chose what (the same line goes above the message's text,
  which is kept: an incident report is in that text and nowhere a later read
  of the thread looks), a short echo ("↳ @user: label") is posted in
  the thread, since a bot token cannot post as the user, and the label is fed
  to the adapter's message handler as that user's message in that thread, the
  path a reaction trigger already takes. That path applies the channel and user checks a typed message gets, so a click
  can do nothing its clicker could not do by typing the label.
* A link button (``<prefix>.link.<n>``) is acknowledged and nothing else;
  Slack has already opened the url.

A message is answered once: the first authorized click wins, and a second
click on the same message, before the rewrite lands, is dropped in this
process and logged. If the rewrite fails, the buttons stay on the message but
the click still counts: its turn runs, and a later click on them is dropped and
logged rather than running a second apply. That memory is this process's and
holds the last ``ANSWERED_MAX`` answers, so only a failed rewrite followed by a
gateway restart, or by that many later answers, lets the leftover buttons run
again.

An incident alert's option buttons (``kage_incident.choice.<n>``) can also be
answered by typing: someone replies ``apply Option B`` in the thread, the agent
applies it, and the buttons are still there. So before such a click counts,
the thread is read once, and if a person the adapter would answer (its own
interactive authorization, the check the clicker passed, and its channel gate
with the mention rule the click skips) has replied with one of the call to
action's forms (``apply``, ``apply Option B``, ``apply B``, any of them ending
in a please or a thanks, or a button's whole text, ``apply Option B: <that
option's text>``; a colon before anything else is not one) since the buttons appeared, the buttons are replaced with
"answered in the thread" and the click is dropped. Any option typed counts, not only the one
clicked: a typed ``apply A`` drops a click on B, and the clicker sees only
"answered in the thread", since the agent is already applying A and a second
apply would run on top of it. The buttons appear when the alert is edited
into its triage, so that edit's time, which the click's payload carries, is
the start; a reply typed during the diagnosis does not count. The match is a
heuristic: the agent reads a typed reply as free text, so this guesses what it
will apply. A read, or one of the adapter's checks, that fails runs the click
as if nothing had been typed. Other choice buttons are not checked.

Fail-soft throughout: a rewrite or echo that fails is logged and the turn
still runs, because the click was the user's answer.
"""

from __future__ import annotations

import logging
import os
import re
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

try:
    import slack_presenter as _presenter
except ImportError:  # the scripts directory is not on PYTHONPATH
    _presenter = None

#: The flag, read here only to word the warning when the presenter is missing.
FLAG_ENV = "KAGE_SLACK_UX"

#: ``slack_presenter.FLAG_ON_VALUES``, copied because the warning below fires
#: exactly when that module cannot be imported.
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

#: What the adapter's authorization log calls each kind of click.
CHOICE_KIND = "kage choice"

#: The echo posted in the thread, and the line that replaces the answered buttons.
ECHO = "↳ <@{user}>: {label}"
ANSWERED = "✓ <@{user}>: {label}"

#: Slack mrkdwn control characters in a label, escaped before it is echoed so a
#: label cannot mention a user or a channel.
MRKDWN_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

#: A choice label starting with one of these would run as a gateway command;
#: the guard in front keeps it an answer. Zero-width, so the agent reads the label.
COMMAND_PREFIXES = ("/", "!")
COMMAND_GUARD = "\u200b"

#: A DM channel id's first letter, as the adapter's message handler reads it.
DM_CHANNEL_PREFIX = "D"

#: A synthetic message's ts when the payload carries no ``action_ts``.
FALLBACK_TS = "kage-click-{ts}-{action}-{user}"

#: Bound on the answered-message map, oldest evicted first.
ANSWERED_MAX = 512

#: An incident alert's option buttons: ``slack_ux_incident.ACTION_PREFIX`` and the
#: presenter's choice segment, copied because that module is not imported here.
INCIDENT_CHOICE_PREFIX = "kage_incident.choice."

#: Inline markup a reply can wrap the word in, dropped before it is matched.
TYPED_MARKUP = str.maketrans("", "", "*_~`'\"‘’“”")

#: What can come before a typed apply and is not part of it: mentions (Slack's
#: ``<@U…>`` or a plain ``@name``), a blockquote (``&gt;`` as Slack sends it), emoji
#: codes, punctuation, and a yes or a please.
TYPED_LEAD = re.compile(
    r"^(?:\s+|<@[UWB][A-Z0-9]+>|@\S+|&gt;|:[\w+-]+:|[^\w\s]|(?:yes|ok|okay|sure|please)\b)*",
    re.IGNORECASE,
)

#: The call to action's own forms, ``apply``, ``apply Option B`` or ``apply B``, ending
#: the reply, or ending in a courtesy (``please``, ``thanks``, ``thank you``, ``ty``), or
#: followed by ``:`` as a button's text is. A colon counts only before that option's own
#: text: :func:`_typed_apply`.
TYPED_APPLY = re.compile(
    r"apply(?:\s+(?:option\s+)?([A-Z])\b)?"
    r"(?::|[,.!]*(?:\s*(?:please|thanks|thank\s+you|ty)[.!]*)?\s*$)",
    re.IGNORECASE,
)

#: An incident option button's value, its whole label, ``slack_ux_incident.OPTION_LABEL``
#: or ``SINGLE_LABEL``: its capital letter, if it has one, and the option's own text.
BUTTON_FORM = re.compile(r"apply(?: Option ([A-Z]))?: (.+)", re.DOTALL)

#: Slack's escapes in a message's text, undone before typed option text is compared.
SLACK_ESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))

#: Trailing characters typed option text may end in and still be the option's own.
OPTION_TEXT_END = ".! "

#: The one reply subtype that is still a person typing: "also send to channel".
TYPED_SUBTYPES = frozenset({"thread_broadcast"})

#: The line that replaces an alert's buttons when someone typed the apply first.
ANSWERED_IN_THREAD = "✓ answered in the thread"

#: Slack's most replies one ``conversations.replies`` page returns.
REPLIES_READ_MAX = 1000

#: Slack truncates a message's ``text`` past this many characters.
SLACK_TEXT_MAX = 40000

#: ``(channel, ts, kind)`` a click answered.
_answered: OrderedDict[tuple, None] = OrderedDict()
_warned_missing = False


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on and the presenter is importable."""
    global _warned_missing
    if _presenter is not None:
        return _presenter.enabled()
    if os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES and not _warned_missing:
        _warned_missing = True
        logger.warning(
            "slack_ux_clicks: %s is set but slack_presenter is not importable; "
            "treating the flag as off", FLAG_ENV,
        )
    return False


def register(adapter: Any) -> None:
    """Add the choice and link listeners to ``adapter._app``."""

    async def on_choice(ack, body, action):
        await answer(adapter, ack, body, action, CHOICE_KIND)

    adapter._app.action(_presenter.CHOICE_ACTION_ID_PATTERN)(on_choice)
    adapter._app.action(_presenter.LINK_ACTION_ID_PATTERN)(_presenter.ack_link_click)
    logger.info("slack_ux_clicks: choice and link button handlers registered")


def _escape(text: str) -> str:
    for raw, escaped in MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _answered_by(other: str) -> bool:
    """Whether ``other`` is one of the buttons a choice click answers: every choice."""
    return bool(_presenter.CHOICE_ACTION_ID_PATTERN.search(other))


def answered_blocks(blocks: Any, answered: Any, note: str) -> list[dict]:
    """``blocks`` with the answered buttons dropped, and ``note`` as a context line after them.

    An actions block left with no buttons is dropped; one that still holds a
    link keeps it.
    """
    out: list[dict] = []
    for block in blocks or ():
        if isinstance(block, dict) and block.get("type") == "actions":
            elements = block.get("elements") or []
            kept = [e for e in elements if not answered(str((e or {}).get("action_id") or ""))]
            if not kept:
                continue
            if len(kept) != len(elements):
                block = {**block, "elements": kept}
        out.append(block)
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note}]})
    return out


def _without_choices_line(text: str) -> str:
    """``text`` without the fallback's "Reply with one of:" line, which only its first
    paragraph carries; the same words further down are the report's own and stay."""
    head, _sep, rest = text.partition("\n\n")
    head = "\n".join(line for line in head.split("\n") if not line.startswith(_presenter.CHOICES_LEAD))
    return "\n\n".join(part for part in (head, rest) if part)


def _answered_text(note: str, message: dict) -> str:
    """``note`` with the message's own text under it, clipped to ``SLACK_TEXT_MAX``.

    The adapter reads a thread back from ``text`` and top-level blocks, and an
    incident alert's report is in its ``text`` only, so replacing the text with
    the note would leave the click's own turn, and every later read of the
    thread, without the report. The "Reply with one of:" line goes: it asks for
    an answer the note already records.
    """
    original = _without_choices_line(str(message.get("text") or ""))
    return _presenter._clip(f"{note}\n\n{original}", SLACK_TEXT_MAX) if original else note


def _shown_text(action: dict) -> str:
    """The clicked button's text as Slack displayed it."""
    text = action.get("text") or {}
    return str(text.get("text") or "").strip() if isinstance(text, dict) else ""


def _gated_out(adapter: Any, channel_id: str) -> bool:
    """Whether the adapter would ignore a typed message in ``channel_id``: an ignored
    channel, outside ``allowed_channels``, or a DM with DMs disabled. Checked before
    anything is shown."""
    if adapter._is_ignored_channel(channel_id):
        return True
    allowed = adapter._slack_allowed_channels()
    if allowed and channel_id not in allowed:
        return True
    return channel_id.startswith(DM_CHANNEL_PREFIX) and bool(adapter._slack_disable_dms())


def _as_answer(label: str) -> str:
    """``label`` as message text that cannot be parsed as a gateway command."""
    return COMMAND_GUARD + label if label.startswith(COMMAND_PREFIXES) else label


def _thread_ts(body: dict, message: dict, msg_ts: str) -> str:
    container = body.get("container") or {}
    return str(message.get("thread_ts") or container.get("thread_ts") or msg_ts)


def _after(ts: Any, msg_ts: str) -> bool:
    try:
        return float(ts) > float(msg_ts)
    except (TypeError, ValueError):
        return False


def _buttons_shown(message: dict, msg_ts: str) -> str:
    """When the clicked message got its buttons: its last edit, or its post if it was never edited."""
    edited = message.get("edited")
    edited_ts = str(edited.get("ts") or "") if isinstance(edited, dict) else ""
    return edited_ts if _after(edited_ts, msg_ts) else msg_ts


def _option_text(text: str) -> str:
    """``text`` as option text is compared: without markup, Slack's escapes, case or extra spaces."""
    for escaped, char in SLACK_ESCAPES:
        text = text.replace(escaped, char)
    return " ".join(text.translate(TYPED_MARKUP).split()).casefold().rstrip(OPTION_TEXT_END)


def _option_texts(message: dict) -> frozenset[tuple[str, str]]:
    """The incident option buttons on ``message``, each as its letter (``""`` for the single
    button's ``apply:``) and its own text, read from the value: the shown text may be clipped."""
    found = set()
    for block in message.get("blocks") or ():
        if not isinstance(block, dict) or block.get("type") != "actions":
            continue
        for element in block.get("elements") or ():
            if not isinstance(element, dict) or not str(element.get("action_id") or "").startswith(
                INCIDENT_CHOICE_PREFIX
            ):
                continue
            form = BUTTON_FORM.match(str(element.get("value") or ""))
            if form:
                found.add((form.group(1) or "", _option_text(form.group(2))))
    return frozenset(found)


def _typed_apply(text: str, options: frozenset[tuple[str, str]]) -> bool:
    """Whether ``text`` is one of the call to action's forms: bare, or a button's, where the text
    after the colon is that option's own in ``options``. A guess at what the agent applies."""
    text = text.translate(TYPED_MARKUP)
    typed = TYPED_APPLY.match(text, TYPED_LEAD.match(text).end())
    if not typed or not typed.group(0).endswith(":"):
        return bool(typed)
    return ((typed.group(1) or "").upper(), _option_text(text[typed.end():])) in options


async def _gateway_hears(adapter: Any, reply: dict, channel_id: str, team_id: str, thread_ts: str) -> bool:
    """Whether the adapter's channel gate passes ``reply`` as it would a typed message: with
    the mention rules a click skips. A 1:1 DM, or no bot id yet, skips the gate there too."""
    bot_uid = adapter._team_bot_user_ids.get(team_id, adapter._bot_user_id)
    if channel_id.startswith(DM_CHANNEL_PREFIX) or not bot_uid:
        return True
    text = str(reply.get("text") or "")
    return await adapter._channel_gate_allows(
        channel_id=channel_id, routing_text=text, bot_uid=bot_uid,
        is_mentioned=f"<@{bot_uid}>" in text or bool(adapter._slack_message_matches_mention_patterns(text)),
        is_thread_reply=True, event_thread_ts=thread_ts, user_id=reply["user"], team_id=team_id, is_dm=False,
        force_process=False,
    )


async def _applied_by_typing(
    adapter: Any, client: Any, channel_id: str, team_id: str, thread_ts: str, since: str,
    options: frozenset[tuple[str, str]],
) -> bool:
    """Whether a person the adapter would answer typed an apply in the thread after ``since``.
    One read; a read or an adapter check that fails answers no, so the click runs as it would
    without the check. The channel gate is asked only of a reply that passes everything else."""
    try:
        response = await client.conversations_replies(
            channel=channel_id, ts=thread_ts, oldest=since, limit=REPLIES_READ_MAX,
        )
        for reply in response.get("messages") or []:
            if (
                isinstance(reply, dict)
                and reply.get("user")
                and not reply.get("bot_id")
                and (not reply.get("subtype") or reply.get("subtype") in TYPED_SUBTYPES)
                and _after(reply.get("ts"), since)
                and _typed_apply(str(reply.get("text") or ""), options)
                and adapter._is_interactive_user_authorized(reply["user"], channel_id=channel_id, team_id=team_id)
                and await _gateway_hears(adapter, reply, channel_id, team_id, thread_ts)
            ):
                return True
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning("slack_ux_clicks: could not check the thread of %s; running the click: %s", thread_ts, exc)
    return False


async def answer(adapter: Any, ack: Any, body: dict, action: dict, kind: str) -> None:
    """Authorize a choice click, mark it answered, echo it, and run it as the clicker's turn."""
    started = await adapter._begin_interaction(ack, body, action, kind)
    if started is None:
        return
    team_id, action_id, _value, message, msg_ts, channel_id, _user_name, user_id = started
    label = _shown_text(action)
    missing = [
        name
        for name, field in (("button text", label), ("message ts", msg_ts), ("channel", channel_id), ("user", user_id))
        if not field
    ]
    if missing:
        logger.warning(
            "slack_ux_clicks: dropping a %s click on %s with no %s",
            kind, msg_ts or "an unknown message", ", ".join(missing),
        )
        return
    if _gated_out(adapter, channel_id):
        logger.info("slack_ux_clicks: ignoring a %s click in %s, which the adapter ignores", kind, channel_id)
        return
    key = (channel_id, msg_ts, kind)
    if key in _answered:
        logger.info("slack_ux_clicks: dropping a second %s click on %s, already answered", kind, msg_ts)
        return
    thread_ts = _thread_ts(body, message, msg_ts)
    client = adapter._get_client(channel_id, team_id=team_id)
    typed = action_id.startswith(INCIDENT_CHOICE_PREFIX) and await _applied_by_typing(
        adapter, client, channel_id, team_id, thread_ts, _buttons_shown(message, msg_ts), _option_texts(message),
    )
    # Checked again: another click on this message may have landed during the read.
    if key in _answered:
        logger.info("slack_ux_clicks: dropping a second %s click on %s, already answered", kind, msg_ts)
        return
    _answered[key] = None
    while len(_answered) > ANSWERED_MAX:
        _answered.popitem(last=False)

    if typed:
        logger.info("slack_ux_clicks: dropping a %s click on %s, already applied in the thread", kind, msg_ts)
        try:
            await client.chat_update(
                channel=channel_id, ts=msg_ts, text=_answered_text(ANSWERED_IN_THREAD, message),
                blocks=answered_blocks(message.get("blocks"), _answered_by, ANSWERED_IN_THREAD),
            )
        except Exception as exc:  # noqa: BLE001 — the click is dropped either way
            logger.warning("slack_ux_clicks: could not mark %s answered in the thread: %s", msg_ts, exc)
        return

    shown = _escape(label)
    note = ANSWERED.format(user=user_id, label=shown)
    try:
        await client.chat_update(
            channel=channel_id, ts=msg_ts, text=_answered_text(note, message),
            blocks=answered_blocks(message.get("blocks"), _answered_by, note),
        )
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning(
            "slack_ux_clicks: could not mark %s answered; its buttons stay but further clicks are dropped: %s",
            msg_ts, exc,
        )
    try:
        await client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=ECHO.format(user=user_id, label=shown),
        )
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning("slack_ux_clicks: could not echo the click on %s: %s", msg_ts, exc)

    synthetic = {
        "type": "message",
        "user": user_id,
        "text": _as_answer(label),
        "channel": channel_id,
        # The click's own ts keeps the deduplicator from conflating this turn
        # with the echo or the clicked message, as a reaction trigger's does.
        "ts": str(action.get("action_ts") or FALLBACK_TS.format(ts=msg_ts, action=action_id, user=user_id)),
        "thread_ts": thread_ts,
        # Skips the mention requirement only; channel and user checks still apply.
        "_hermes_force_process": True,
    }
    if team_id:
        synthetic["team"] = team_id
    await adapter._handle_slack_message(synthetic)
