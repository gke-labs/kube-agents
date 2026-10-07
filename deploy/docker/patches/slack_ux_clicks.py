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
  thread with the button's text as Slack showed it. Not its ``value``: the
  presenter clips the text to ``BUTTON_TEXT_MAX`` but keeps up to
  ``BUTTON_VALUE_MAX`` in the value, and a click must not send words the clicker did not see.
  The one exception keeps that rule: a value that is the label, ": ", and a
  whole line the message itself shows ("Fix the first one: <the first row>",
  markup and a row's severity aside) is the turn, so a session that never
  read the message is told what the click is about. Part of a line is not
  enough: it can say the opposite of the line it came from. The answered line still shows the label.
  An incident option button is the other exception: it shows the option's
  title (`` (recommended)`` after the recommended one's) and its value is the
  reply the report's call to action asks for, ``apply Option B: <that
  title>``, which is the turn when its title is the one shown, whole or as
  the clip shows it. The shown title, without the suffix, is what the
  answered line says and the thread is offered as its ask. A
  label that starts like a command (``/`` or ``!``) is sent as text, since a
  choice is an answer. The click goes through the adapter's own interactive
  authorization; an unlisted user's click is logged and changes nothing, and
  so does one in a channel or DM the adapter would ignore a typed message in:
  ``allowed_channels`` gates channels and group DMs, never a 1:1 DM, which
  only ``disable_dms`` gates, as upstream's message handler does.
  Then the message is rewritten with the choice buttons replaced by
  a line naming who chose what ("✓ <name>: label", by the clicker's display
  name, then real name, then handle, never a mention or an id; the same line
  goes above the message's text, which is kept: an incident report is in that
  text and nowhere a later read of the thread looks); only when Slack refuses
  that rewrite is the same line posted in the thread instead, since a bot token
  cannot post as the user. The message's first line of text (a question's
  headline; a failure reply's bold lead) is then offered as the thread's first
  ask (the label for an incident option or a message with no text), so a thread
  nobody titled yet is named for the message rather than for the button or the
  card note in the turn. The label is fed
  to the adapter's message handler as that user's message in that thread, the
  path a reaction trigger already takes. That path applies the channel and user checks a typed message gets, so a click
  can do nothing its clicker could not do by typing the label.
* A link button (``<prefix>.link.<n>``) is acknowledged and nothing else;
  Slack has already opened the url.

When the clicked message is a card's question (``gateway/slack_ux_moments.py``),
the turn also names the card, after the label or the shown line its value
names, so with two cards blocked in one thread the
answer reaches the right one, and the rewrite drops its "waiting on you" line.

A message is answered once: the first authorized click wins, and a second
click on the same message, before the rewrite lands, is dropped in this
process and logged. If the rewrite fails, the buttons stay on the message but
the click still counts: its turn runs, and a later click on them is dropped and
logged rather than running a second apply. That memory is this process's and
holds the last ``ANSWERED_MAX`` answers, so only a failed rewrite followed by a
gateway restart, or by that many later answers, lets the leftover buttons run
again.
:func:`answered` reports a message only once its rewrite landed, so a question
whose rewrite failed is still settled when its card moves on.

The rewrite sends back the blocks Slack echoed in the payload, clamped as
upstream clamps every ``chat.update``: Slack stores ``< > &`` escaped, so an
echoed text can come back longer than the send path budgeted for. A section or
context text past ``SECTION_TEXT_MAX`` is clipped and the message is cut to
``MESSAGE_BLOCKS_MAX`` blocks, keeping the answered note last. A message with a
side bar (``slack_presenter.with_side_bar``) echoes its blocks split between
its own and its attachment's; the rewrite reads both and puts them back beside
the same colour, since ``chat.update`` keeps an attachment it is not sent.

An incident alert's option buttons (``kage_incident.choice.<n>``) can also be
answered by typing: someone replies ``apply Option B`` in the thread, the agent
applies it, and the buttons are still there. So before such a click counts,
the thread is read once, plus the cached thread-root lookup the adapter's
channel gate makes, and if a person the adapter would answer (not a bot by
its own test, its interactive authorization, the check the clicker passed, and
its channel gate with the mention rule the click skips) has replied with one of the call to
action's forms (``apply``, ``apply Option B``, ``apply B`` (a letter the alert offers, when its options are lettered), any of them ending
in a please or a thanks, or a button's whole text, ``apply Option B: <that
option's text>``; a colon before anything else is not one) since the buttons appeared, the buttons are replaced with
"answered in the thread" and the click is dropped. Any option typed counts, not only the one
clicked: a typed ``apply A`` drops a click on B, and the clicker sees only
"answered in the thread", since the agent is already applying A and a second
apply would run on top of it. The buttons appear when the alert is edited
into its triage, so that edit's time, which the click's payload carries, is
the start; a reply typed during the diagnosis does not count. The match is a
heuristic: the agent reads a typed reply as free text, so this guesses what it
will apply. Those checks are made when the button is clicked, not when the
reply was typed, so a channel or user setting changed in between is read as it
stands at the click. A read that fails runs the click as if nothing had been
typed; an adapter check that answers no means that reply does not count, and
one that raises counts it, since the agent may already be applying it. The bot
test is the exception: the adapter makes it before any other, so one that
raises took no turn and that reply does not count. Other choice buttons are
not checked.

A click that runs is fail-soft: a rewrite, or the answered line posted in its place, that fails is logged and the
turn still runs, because the click was the user's answer. A click dropped for a
typed apply runs no turn, whether or not its rewrite lands.
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

#: The line that replaces the answered buttons, posted in the thread instead
#: when that rewrite fails, so a click always shows once; naming the clicker as plain text.
CLICKED = "✓ {name}: {label}"
#: The clicker when Slack names them nowhere: never their raw id.
NAMELESS_CLICKER = "Someone"

#: Joins a label to the shown line its value names.
TURN_JOIN = ": "

#: A choice label starting with one of these would run as a gateway command;
#: the guard in front keeps it an answer. Zero-width, so the agent reads the label.
COMMAND_PREFIXES = ("/", "!")
COMMAND_GUARD = "\u200b"

#: Added to the turn when the clicked message is a card's question.
CARD_NOTE = "(Clicked on the question from card {card}.)"

#: Paired markup a shown line or a clicked value may carry, stripped from both
#: before they are compared: ``code``, **bold**, *bold*, _italic_ and ~strike~.
#: Paired only, so a glob (``app=web-*``) or a dunder name keeps its characters.
PAIRED_MARKUP = re.compile(
    r"`([^`]+)`|\*\*([^*]+)\*\*"
    r"|(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])"
    r"|(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])"
    r"|(?<![\w~])~(?=\S)([^~]+?)(?<=\S)~(?![\w~])"
)

#: The severities a row leads with as inline code (``findings_queue.SEVERITIES``).
ROW_SEVERITIES = frozenset({"critical", "major", "minor"})

#: A DM channel id's first letter, as the adapter's message handler reads it.
DM_CHANNEL_PREFIX = "D"

#: A group DM's name in a click's payload, which carries no ``channel_type`` to read ``mpim`` from.
#: Not yet seen in a live click: a payload naming it otherwise reads as a channel, as before.
GROUP_DM_NAME_PREFIX = "mpdm-"

#: A synthetic message's ts when the payload carries no ``action_ts``.
FALLBACK_TS = "kage-click-{ts}-{action}-{user}"

#: Bound on the answered-message map, oldest evicted first.
ANSWERED_MAX = 512

#: An incident alert's option buttons: ``slack_ux_incident.ACTION_PREFIX`` and the
#: presenter's choice segment, copied because that module is not imported here.
INCIDENT_CHOICE_PREFIX = "kage_incident.choice."

#: ``slack_ux_incident.RECOMMENDED_SUFFIX``, copied for the same reason.
INCIDENT_RECOMMENDED_SUFFIX = " (recommended)"

#: Struck-through text, ``~like this~``: taken back, so removed before a reply is matched.
#: Each tilde sits at a word's edge, as Slack's own strike needs: ``~3 to ~5`` is not struck.
TYPED_STRUCK = re.compile(r"(?<![\w~])~(?=\S)[^~\n]+?(?<=\S)~(?![\w~])")

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

#: An incident option button's value, ``slack_ux_incident.OPTION_REPLY`` or ``SINGLE_REPLY``
#: (and its shown text too, on an alert edited before the button showed only the title): its
#: capital letter, if it has one, and the option's own text.
BUTTON_FORM = re.compile(r"apply(?: Option ([A-Z]))?: (.+)", re.DOTALL)

#: What ``slack_presenter._clip`` ends a button's clipped shown text with.
CLIPPED_END = "…"

#: Slack's escapes in a message's text, undone before typed option text is compared.
SLACK_ESCAPES = (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"))

#: Trailing characters typed option text may end in and still be the option's own.
OPTION_TEXT_END = ".! "

#: A link Slack made of a typed url or hostname: ``<url|as typed>``, or ``<url>`` when the url was typed.
#: Neither part runs past a ``<``: an unclosed link is given up at the next one, not at the end of the reply.
SLACK_LINK = re.compile(r"<(https?://[^|>\s<]+)(?:\|([^>\n<]*))?>")

#: The reply subtype the adapter's inbound filter drops outright; a bot's post, ``bot_message``
#: among them, it drops by ``_event_declares_bot_sender``, asked of each reply too. Every other
#: subtype reaches the agent as a turn: "also send to channel", a file with a comment, a ``/me``.
GATEWAY_DROPPED_SUBTYPES = frozenset({"message_deleted"})

#: The line that replaces an alert's buttons when someone typed the apply first.
ANSWERED_IN_THREAD = "✓ answered in the thread"

#: Slack's most replies one ``conversations.replies`` page returns.
REPLIES_READ_MAX = 1000

#: Slack truncates a message's ``text`` past this many characters.
SLACK_TEXT_MAX = 40000

#: Slack's caps on a section or context text and on a message's blocks; past
#: either, ``chat.update`` fails whole with ``invalid_blocks``.
SECTION_TEXT_MAX = 3000
MESSAGE_BLOCKS_MAX = 50

#: ``(channel, ts, kind)`` a click answered.
_answered: OrderedDict[tuple, None] = OrderedDict()
#: The keys of ``_answered`` whose rewrite landed.
_rewritten: OrderedDict[tuple, None] = OrderedDict()
#: The keys of ``_answered`` whose rewrite failed; one in neither is still rewriting.
_unrewritten: OrderedDict[tuple, None] = OrderedDict()
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
    """Add the choice and link listeners to ``adapter._app``.

    A choice click's turn is the label Slack showed, or the value when it is
    that label naming a whole line the message shows; either way, a click on a
    card's question then names the card.
    """

    async def on_choice(ack, body, action):
        await answer(adapter, ack, body, action, CHOICE_KIND)

    adapter._app.action(_presenter.CHOICE_ACTION_ID_PATTERN)(on_choice)
    adapter._app.action(_presenter.LINK_ACTION_ID_PATTERN)(_presenter.ack_link_click)
    logger.info("slack_ux_clicks: choice and link button handlers registered")


def _unescape(text: str) -> str:
    """``text`` with Slack's three entities decoded, ``&amp;`` last so ``&amp;lt;`` stays ``&lt;``."""
    for raw, escaped in reversed(_presenter.MRKDWN_ESCAPES):
        text = text.replace(escaped, raw)
    return text


def answered(channel_id: str, msg_ts: str) -> bool:
    """Whether a choice click in this process answered the message and rewrote it."""
    return (str(channel_id), str(msg_ts), CHOICE_KIND) in _rewritten


def clicked(channel_id: str, msg_ts: str) -> bool:
    """Whether a choice click in this process answered the message, whether or not its rewrite landed."""
    return (str(channel_id), str(msg_ts), CHOICE_KIND) in _answered


def rewriting(channel_id: str, msg_ts: str) -> bool:
    """Whether a choice click answered the message and its rewrite has neither landed nor failed yet."""
    key = (str(channel_id), str(msg_ts), CHOICE_KIND)
    return key in _answered and key not in _rewritten and key not in _unrewritten


def _answered_by(other: str) -> bool:
    """Whether ``other`` is one of the buttons a choice click answers: every choice."""
    return bool(_presenter.CHOICE_ACTION_ID_PATTERN.search(other))


def answered_blocks(blocks: Any, answered: Any, note: str) -> list[dict]:
    """``blocks`` with the answered buttons dropped, and ``note`` as a context line after them.

    An actions block left with no buttons is dropped; one that still holds a
    link keeps it. So is the "waiting on you" line, now that it is answered.
    Section and context texts are clipped to Slack's cap and the message to its
    block cap, the note kept. Section fields and header texts are not clipped:
    the presenter lays out neither.
    """
    out: list[dict] = []
    for block in blocks or ():
        if isinstance(block, dict) and block.get("block_id") == _presenter.WAITING_BLOCK_ID:
            continue
        if isinstance(block, dict) and block.get("type") == "actions":
            elements = block.get("elements") or []
            kept = [e for e in elements if not answered(str((e or {}).get("action_id") or ""))]
            if not kept:
                continue
            if len(kept) != len(elements):
                block = {**block, "elements": kept}
        out.append(_clamped(block) if isinstance(block, dict) else block)
    note_text = {"type": "mrkdwn", "text": note}
    return out[: MESSAGE_BLOCKS_MAX - 1] + [{"type": "context", "elements": [_clamped_text(note_text)]}]


async def clicker_name(adapter: Any, body: dict, user_id: str, channel_id: str, team_id: str) -> str:
    """The clicker's name as an answered line shows it, escaped: never a mention or their id.

    The adapter's ``_resolve_user_name`` reads ``users.info`` once per user and
    caches the answer, preferring the display name, then the real name, then the
    handle. It answers with the id when the call fails, so the click's own
    handle stands in then.
    """
    try:
        name = str(await adapter._resolve_user_name(user_id, chat_id=channel_id, team_id=team_id) or "").strip()
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.debug("slack_ux_clicks: could not name %s: %s", user_id, exc)
        name = ""
    if not name or name == user_id:
        user = body.get("user") or {}
        name = str(user.get("username") or user.get("name") or "").strip()
    return _presenter._escape(name) if name and name != user_id else NAMELESS_CLICKER


def _clamped_text(obj: Any) -> Any:
    """A text object clipped to ``SECTION_TEXT_MAX``; anything else unchanged."""
    if not isinstance(obj, dict) or obj.get("type") not in ("mrkdwn", "plain_text"):
        return obj
    text = str(obj.get("text") or "")
    return {**obj, "text": _presenter._clip(text, SECTION_TEXT_MAX)} if len(text) > SECTION_TEXT_MAX else obj


def _clamped(block: dict) -> dict:
    """``block`` with its section text, or each context text, clipped to ``SECTION_TEXT_MAX``."""
    if block.get("type") == "section" and "text" in block:
        return {**block, "text": _clamped_text(block["text"])}
    if block.get("type") == "context":
        return {**block, "elements": [_clamped_text(e) for e in block.get("elements") or []]}
    return block


def _without_choices_line(text: str, question: bool = False) -> str:
    """``text`` without the fallback's "Reply with one of:" line, which an incident
    alert carries in its first paragraph; the same words further down are the report's
    own and stay. A card's ``question`` carries it as its last line, after a detail
    that can hold blank lines."""
    head, _sep, rest = text.partition("\n\n")
    head = "\n".join(line for line in head.split("\n") if not line.startswith(_presenter.CHOICES_LEAD))
    body, _nl, last = rest.rpartition("\n")
    if question and last.startswith(_presenter.CHOICES_LEAD):
        rest = body.rstrip("\n")
    return "\n\n".join(part for part in (head, rest) if part)


def _answered_text(note: str, message: dict, question: bool = False) -> str:
    """``note`` with the message's own text under it, clipped to ``SLACK_TEXT_MAX``.

    The adapter reads a thread back from ``text`` and top-level blocks, and an
    incident alert's report is in its ``text`` only, so replacing the text with
    the note would leave the click's own turn, and every later read of the
    thread, without the report. The "Reply with one of:" line goes: it asks for
    an answer the note already records.
    """
    original = _without_choices_line(str(message.get("text") or ""), question)
    return _presenter._clip(f"{note}\n\n{original}", SLACK_TEXT_MAX) if original else note


def _rewrite(message: dict, answered: Any, note: str, text: str) -> dict:
    """The ``text`` and ``blocks``, and ``attachments`` for a message with a side bar, that answer ``message``."""
    blocks = answered_blocks(_presenter.message_blocks(message), answered, note)
    color = _presenter.side_bar_color(message)
    return {"text": text, **(_presenter.with_side_bar(blocks, color, text) if color else {"blocks": blocks})}


def _shown_text(action: dict) -> str:
    """The clicked button's text as Slack displayed it, with Slack's entities decoded."""
    text = action.get("text") or {}
    return _unescape(str(text.get("text") or "").strip()) if isinstance(text, dict) else ""


def _section_lines(elements: list[dict]) -> list[str | None]:
    """Each line ``elements`` show, their text joined; None for a line the joined text misrepresents.

    That is a line holding struck text, which a turn would carry unstruck, or
    an element with no text (a mention, an emoji, a bare link), which Slack
    shows and the join drops.
    """
    lines: list[str | None] = [""]
    for element in elements:
        text, style = element.get("text"), element.get("style")
        opaque = not isinstance(text, str) or (isinstance(style, dict) and bool(style.get("strike")))
        for i, part in enumerate(str(text or "").split("\n")):
            if i:
                lines.append("")
            lines[-1] = None if opaque or lines[-1] is None else lines[-1] + part
    return lines


def _shown_lines(blocks: Any) -> list[str]:
    """Each line the rich_text in ``blocks`` shows, its elements' text joined.

    A section led by a severity as inline code (a row's) also gives its first
    line without it, since a row's button value leaves the severity out. Only
    a severity: any other leading code span stays part of the line. A line
    holding struck text or an element with no text is left out, so a value
    naming it sends the label.
    """
    lines: list[str] = []
    for block in blocks or ():
        if not (isinstance(block, dict) and block.get("type") == "rich_text"):
            continue
        for section in block.get("elements") or ():
            raw = (section or {}).get("elements") or () if isinstance(section, dict) else ()
            elements = [e for e in raw if isinstance(e, dict)]
            lines.extend(line for line in _section_lines(elements) if line is not None)
            style = elements[0].get("style") if elements else None
            if isinstance(style, dict) and style.get("code") and str(elements[0].get("text") or "").strip().lower() in ROW_SEVERITIES:
                first = _section_lines(elements[1:])[0]
                if first is not None:
                    lines.append(first)
    return lines


def _comparable(text: str) -> str:
    """``text`` without paired markup, its whitespace collapsed."""
    return " ".join(PAIRED_MARKUP.sub(lambda m: next(g for g in m.groups() if g is not None), text).split())


def _turn(label: str, value: Any, message: dict) -> str:
    """The clicker's turn: ``label`` and the line it names when ``value`` names a whole line the message shows, else ``label``.

    The named line must equal a shown line, not sit inside one: a card showing
    "Do not drain node-pool-a" must not pass a value naming "drain node-pool-a".
    The turn carries the line as shown, not the value's own markup, which the
    match ignores: "~Do not~ drain node-pool-a" would read as striking "Do not".
    """
    prefix = label + TURN_JOIN
    if not (isinstance(value, str) and value.startswith(prefix)):
        return label
    named = value[len(prefix):].strip()
    want = _comparable(named)
    if not want or "\n" in named:
        return label
    shown = next((line for line in _shown_lines(_presenter.message_blocks(message)) if _comparable(line) == want), None)
    return label if shown is None else label + TURN_JOIN + " ".join(shown.split())


def _incident_turn(picked: str, value: Any) -> str:
    """An incident option's turn: ``value`` when it is a reply naming the title ``picked`` shows, else ``picked``."""
    form = BUTTON_FORM.fullmatch(value) if isinstance(value, str) else None
    if form is None:
        return picked
    title = form.group(2)
    clipped = picked.endswith(CLIPPED_END) and title.startswith(picked.removesuffix(CLIPPED_END))
    return value if picked in (value, title) or clipped else picked


def _gated_out(adapter: Any, channel_id: str, body: dict) -> bool:
    """Whether the adapter would ignore a typed message in ``channel_id``: an ignored
    channel or group DM outside ``allowed_channels``, or a DM, 1:1 or group, with
    DMs disabled. A 1:1 DM skips ``allowed_channels``, as upstream's message
    handler does. Checked before anything is shown."""
    if adapter._is_ignored_channel(channel_id):
        return True
    group_dm = _is_group_dm(body)
    one_to_one = channel_id.startswith(DM_CHANNEL_PREFIX) and not group_dm
    allowed = adapter._slack_allowed_channels()
    if allowed and not one_to_one and channel_id not in allowed:
        return True
    return (one_to_one or group_dm) and bool(adapter._slack_disable_dms())


def _as_answer(label: str) -> str:
    """``label`` as message text that cannot be parsed as a gateway command."""
    return COMMAND_GUARD + label if label.startswith(COMMAND_PREFIXES) else label


def _question_card(channel_id: str, msg_ts: str) -> str:
    """The card whose question this process posted as the message, else ""."""
    try:
        from gateway import slack_ux_moments
    except ImportError:
        return ""
    try:
        return str(slack_ux_moments.question_card(channel_id, msg_ts) or "")
    except Exception:  # noqa: BLE001 — the label alone still answers
        return ""


def _asked(message: dict) -> str:
    """The first line of ``message``'s text without the "Reply with one of:" line (a
    question's headline; a failure reply's bold lead), or ``""`` when it has none.

    Decoded as the button label is, and without the ``*`` a question's headline is
    stored in, so a title reads "Scale replicas > 3?" rather than ``*…&gt; 3?*``."""
    line = _without_choices_line(str(message.get("text") or ""), True).split("\n", 1)[0].strip()
    if len(line) > 2 and line[0] == line[-1] == "*":
        line = line[1:-1].strip()
    return _unescape(line)


async def _offer_title(adapter: Any, channel_id: str, team_id: str, thread_ts: str, title: str) -> None:
    """Offer ``title`` as the thread's first ask before the turn runs, so a session
    titled from the click never shows the card note in the turn.

    A channel thread is titled through ``slack_ux_status``; a DM thread only by
    upstream, once, so a DM already titled from its first message keeps it."""
    if not channel_id.startswith(DM_CHANNEL_PREFIX):
        try:
            from gateway import slack_ux_status

            slack_ux_status.note_ask(channel_id, thread_ts, title)
        except Exception:  # noqa: BLE001 — the title is cosmetic; the click still answers
            pass
    elif hasattr(adapter, "_set_assistant_thread_title"):
        try:
            await adapter._set_assistant_thread_title(channel_id, thread_ts, title, team_id=team_id)
        except Exception:  # noqa: BLE001 — as above
            pass


def _turn_text(label: str, card: str) -> str:
    text = _as_answer(label)
    return f"{text}\n\n{CARD_NOTE.format(card=card)}" if card else text


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
    button's ``apply:``) and its own text: whole, from the value, and as the button shows it,
    which may be clipped, with and without the clip's ellipsis and the recommended suffix."""
    found = set()
    for block in message.get("blocks") or ():
        if not isinstance(block, dict) or block.get("type") != "actions":
            continue
        for element in block.get("elements") or ():
            if not isinstance(element, dict) or not str(element.get("action_id") or "").startswith(
                INCIDENT_CHOICE_PREFIX
            ):
                continue
            shown = element.get("text")
            shown = str(shown.get("text") or "") if isinstance(shown, dict) else ""
            for label in (str(element.get("value") or ""), shown, shown.removesuffix(CLIPPED_END)):
                form = BUTTON_FORM.match(label)
                if form:
                    found.add((form.group(1) or "", _option_text(form.group(2))))
            value_form = BUTTON_FORM.fullmatch(str(element.get("value") or ""))
            if value_form and not BUTTON_FORM.match(shown):
                title = shown.removesuffix(INCIDENT_RECOMMENDED_SUFFIX)
                for text in (shown, title, title.removesuffix(CLIPPED_END)):
                    found.add((value_form.group(1) or "", _option_text(text)))
    return frozenset(found)


def _typed_apply(text: str, options: frozenset[tuple[str, str]]) -> bool:
    """Whether ``text`` is one of the call to action's forms: bare, or a button's, where the text
    after the colon is that option's own in ``options``. A guess at what the agent applies."""
    text = TYPED_STRUCK.sub("", text).translate(TYPED_MARKUP)
    typed = TYPED_APPLY.match(text, TYPED_LEAD.match(text).end())
    if not typed:
        return False
    if not typed.group(0).endswith(":"):
        # A letter the alert does not offer applies nothing. A single fix (no letters) or no
        # option read: any letter counts, since the agent may apply the one fix anyway.
        letters = {letter for letter, _ in options if letter}
        return not (typed.group(1) and letters) or typed.group(1).upper() in letters
    tail = SLACK_LINK.sub(lambda link: link.group(2) or link.group(1), text[typed.end():])
    return ((typed.group(1) or "").upper(), _option_text(tail)) in options


def _mention_text(adapter: Any, reply: dict) -> str:
    """The text the gateway reads mentions in: adapter.py's ``_slack_mention_detection_text``,
    the flat text plus a mention only in the blocks. Found through the gate's own globals, which
    hold it however the plugin loader named the module."""
    detect = type(adapter)._channel_gate_allows.__globals__["_slack_mention_detection_text"]
    return str(detect(reply))


def _is_group_dm(body: dict) -> bool:
    """Whether the click came from a group DM, which the gateway asks its gate about as a DM."""
    return str((body.get("channel") or {}).get("name") or "").startswith(GROUP_DM_NAME_PREFIX)


async def _gateway_hears(
    adapter: Any, reply: dict, channel_id: str, team_id: str, thread_ts: str, is_dm: bool,
) -> bool:
    """Whether the adapter's channel gate passes ``reply`` as it would a typed message: with
    the mention rules a click skips. A 1:1 DM, or no bot id yet, skips the gate there too."""
    bot_uid = adapter._team_bot_user_ids.get(team_id, adapter._bot_user_id)
    if channel_id.startswith(DM_CHANNEL_PREFIX) or not bot_uid:
        return True
    text = _mention_text(adapter, reply)
    return await adapter._channel_gate_allows(
        channel_id=channel_id, routing_text=text, bot_uid=bot_uid,
        is_mentioned=f"<@{bot_uid}>" in text or bool(adapter._slack_message_matches_mention_patterns(text)),
        is_thread_reply=True, event_thread_ts=thread_ts, user_id=reply["user"], team_id=team_id, is_dm=is_dm,
        force_process=False,
    )


async def _applied_by_typing(
    adapter: Any, client: Any, channel_id: str, team_id: str, thread_ts: str, since: str,
    options: frozenset[tuple[str, str]], is_dm: bool,
) -> bool:
    """Whether a person the adapter would answer typed an apply in the thread after ``since``.
    One read, plus the cached thread-root lookup the adapter's channel gate makes. A failed read
    answers no, so the click runs as it would without the check; an adapter check that raises on
    a reply that typed an option answers yes, since the gateway may already be applying it and
    running the click as well would apply two. The bot test is the exception: the adapter makes it
    first, so one that raises took no turn there and the reply does not count. The channel gate is
    asked only of a reply that passes everything else, and at click time, not as it stood when the
    reply was typed. Not mirrored: the users.info probe the adapter makes of a reply with no
    ``client_msg_id``, and ``allow_bots``, which can let a bot's post through as a turn; neither is
    a person typing."""
    try:
        response = await client.conversations_replies(
            channel=channel_id, ts=thread_ts, oldest=since, limit=REPLIES_READ_MAX,
        )
        for reply in response.get("messages") or []:
            if not (
                isinstance(reply, dict)
                and reply.get("user")
                and reply.get("subtype") not in GATEWAY_DROPPED_SUBTYPES
                and _after(reply.get("ts"), since)
                and _typed_apply(str(reply.get("text") or ""), options)
            ):
                continue
            try:
                if adapter._event_declares_bot_sender(reply):
                    continue
            except Exception as exc:  # noqa: BLE001 — the adapter asks it first, so no turn ran
                logger.warning(
                    "slack_ux_clicks: could not tell whether a reply in %s is a bot's; not counting it: %s",
                    thread_ts, exc,
                )
                continue
            try:
                if adapter._is_interactive_user_authorized(
                    reply["user"], channel_id=channel_id, team_id=team_id,
                ) and await _gateway_hears(adapter, reply, channel_id, team_id, thread_ts, is_dm):
                    return True
            except Exception as exc:  # noqa: BLE001 — a typed apply is already in the thread
                logger.warning(
                    "slack_ux_clicks: could not check the thread of %s; counting the typed apply: %s", thread_ts, exc,
                )
                return True
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning("slack_ux_clicks: could not check the thread of %s; running the click: %s", thread_ts, exc)
    return False


async def answer(adapter: Any, ack: Any, body: dict, action: dict, kind: str) -> None:
    """Authorize a choice click, mark it answered, title its thread from the label, and run it as the clicker's turn."""
    started = await adapter._begin_interaction(ack, body, action, kind)
    if started is None:
        return
    team_id, action_id, value, message, msg_ts, channel_id, _user_name, user_id = started
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
    if _gated_out(adapter, channel_id, body):
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
        _is_group_dm(body),
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
                channel=channel_id, ts=msg_ts,
                **_rewrite(message, _answered_by, ANSWERED_IN_THREAD, _answered_text(ANSWERED_IN_THREAD, message)),
            )
        except Exception as exc:  # noqa: BLE001 — the click is dropped either way
            logger.warning("slack_ux_clicks: could not mark %s answered in the thread: %s", msg_ts, exc)
        return

    incident = action_id.startswith(INCIDENT_CHOICE_PREFIX)
    if incident:
        label = label.removesuffix(INCIDENT_RECOMMENDED_SUFFIX)
    shown = _presenter._escape(label)
    # Before the name lookup and the rewrite: a card that moves on during either settles its question and forgets it.
    card = _question_card(channel_id, msg_ts)
    note = CLICKED.format(name=await clicker_name(adapter, body, user_id, channel_id, team_id), label=shown)
    rewritten = False
    try:
        await client.chat_update(
            channel=channel_id, ts=msg_ts,
            **_rewrite(message, _answered_by, note, _answered_text(note, message, not incident)),
        )
        rewritten = True
        if key in _answered:
            _rewritten[key] = None
            while len(_rewritten) > ANSWERED_MAX:
                _rewritten.popitem(last=False)
    except Exception as exc:  # noqa: BLE001 — the click still answers
        logger.warning(
            "slack_ux_clicks: could not mark %s answered; its buttons stay but further clicks are dropped: %s",
            msg_ts, exc,
        )
        if key in _answered:
            _unrewritten[key] = None
            while len(_unrewritten) > ANSWERED_MAX:
                _unrewritten.popitem(last=False)
    if not rewritten:
        try:
            await client.chat_postMessage(
                channel=channel_id, thread_ts=thread_ts, text=note,
            )
        except Exception as exc:  # noqa: BLE001 — the click still answers
            logger.warning("slack_ux_clicks: could not post the answered line for %s: %s", msg_ts, exc)
    await _offer_title(adapter, channel_id, team_id, thread_ts, label if incident else _asked(message) or label)

    synthetic = {
        "type": "message",
        "user": user_id,
        "text": _turn_text(_incident_turn(label, value) if incident else _turn(label, value, message), card),
        "channel": channel_id,
        # The click's own ts keeps the deduplicator from conflating this turn
        # with the answered line or the clicked message, as a reaction trigger's does.
        "ts": str(action.get("action_ts") or FALLBACK_TS.format(ts=msg_ts, action=action_id, user=user_id)),
        "thread_ts": thread_ts,
        # Skips the mention requirement only; channel and user checks still apply.
        "_hermes_force_process": True,
    }
    if team_id:
        synthetic["team"] = team_id
    await adapter._handle_slack_message(synthetic)
