"""Today's Slack presentation as ``kube-agents.chat/v1`` messages, for ``spec.mode: next``.

Pure functions only, like the presenters they port: nothing here imports the
Hermes gateway, the Slack SDK or the network. Under ``next`` the chat gateway
renders a chat message and composes nothing (``docs/designs/spec-a2a-payloads.md``,
"Chat messages"), so the composing that ``slack_status``, ``slack_presenter`` and
``slack_moments`` do for Block Kit happens here instead, with the same rules and
caps, and the result is a dict the notify route carries beside its text
fallback. The Block Kit functions are untouched: ``today`` keeps them, byte for
byte, and every function here is new.

Each builder returns the chat message, or None when today posts the plain text
(an answer that opens on a code fence, a rest past the fold's cap). ``*_text``
builds the fallback where the caller has no text of its own; it is plain, since
the gateway escapes the fallback itself. Every field carries the raw text: the
renderer escapes, so none can mention anyone.

Where the spec refuses what today clips, the emitter clips: the newest
:data:`slack_status.ROWS_MAX` plan items, the last :data:`slack_status.STEPS_MAX`
steps of each, and the first :data:`AFTER_MAX` ``after`` lines, which today
leaves uncapped. A choice whose label or reply the gateway would act on as a
command (:func:`command_word`) is left in ``detail`` instead of becoming a
button. The shared cases in ``tests/conformance/chat/`` are the contract;
``deploy/docker/patches/test_chat_conformance.py`` runs them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import chat_notify as _notify
import slack_moments as _moments
import slack_presenter as _presenter
import slack_status as _status

#: The schema every message carries.
SCHEMA = "kube-agents.chat/v1"
#: ``tone`` values: green for done and yours to act on, yellow for waiting on you.
TONE_SUCCESS = "success"
TONE_ATTENTION = "attention"
#: ``mark.settle`` values, spelled as ``slack_presenter`` names the settles.
SETTLE_DONE = _presenter.SETTLE_DONE
SETTLE_BLOCKED = _presenter.SETTLE_BLOCKED
SETTLE_FAILED = _presenter.SETTLE_FAILED

#: The spec's caps that today does not already apply.
NOTE_MAX = 300
DETAIL_MAX = 2000
ROWS_MAX = 20
AFTER_MAX = 5
FOLD_TITLE_DEFAULT = "why"
FOLD_TITLE_MAX = 40
FOLD_TEXT_MAX = 12000
CHOICES_MAX = 10
LINKS_MAX = 10
REPLY_MAX = 400
PLAN_RESULT_MAX = 256
PLAN_ID_MAX = 64
#: A ``severity`` or ``mark`` token: 1-16 characters of ``[a-z0-9-]``.
TOKEN = re.compile(r"[a-z0-9-]{1,16}")

#: ``slack_presenter.arrival_reaction``'s emoji names, as ``mark.ask`` tokens.
ASK_BY_REACTION = {
    _presenter.REACTION_QUESTION: "question",
    _presenter.REACTION_INVESTIGATE: "investigate",
    _presenter.REACTION_FLEET: "fleet",
    _presenter.REACTION_UPGRADE: "upgrade",
    _presenter.REACTION_COST: "cost",
    _presenter.REACTION_SECURITY: "security",
    _presenter.REACTION_CHANGE: "change",
    _presenter.REACTION_BOARD: "board",
    _presenter.REACTION_INCIDENT: "incident",
}

#: A needs-you choice's reply: the label, then the card it answers, as a click
#: posts it today (``slack_ux_clicks.CARD_NOTE`` after a blank line).
NEEDS_YOU_REPLY = "{label}\n\n(Clicked on the question from card {card}.)"
#: The ``update`` keys a card's notifies carry: the thread's plan (a later plan
#: in the same thread numbered after it), a card's question, and the alert
#: the event watcher posted, which the triage edits.
PLAN_UPDATE = "plan"
PLAN_UPDATE_NEXT = "plan-{number}"
QUESTION_UPDATE = "question-{card}"
ALERT_UPDATE = _notify.ALERT_UPDATE_KEY
#: Plan item statuses whose title shows the note, so the note rides beside it.
NOTE_STATUSES = frozenset({_status.TASK_RUNNING, _status.TASK_ERROR})
#: Today's suffix on the recommended incident option's label; on next the
#: choice is primary instead.
RECOMMENDED_SUFFIX = " (recommended)"
#: An incident choice's reply starts with its label, which the spec requires
#: of every reply: the option's title, then what the report's call to action
#: asks for. Today's reply ("apply Option B: <title>") does not.
TRIAGE_REPLY = "{label} — {action}"
TRIAGE_OPTION_ACTION = "apply Option {letter}"
TRIAGE_SINGLE_ACTION = "apply"
#: Today's option reply, ``slack_ux_incident.OPTION_REPLY``, read back for its letter.
TRIAGE_OPTION_REPLY = re.compile(r"^apply Option ([A-Z]):")
#: A triage link's label, ``slack_ux_incident.LINK_LABEL``.
TRIAGE_LINK_LABEL = "{label} ↗"
#: The failure reply's offer, ``slack_ux_failure.OFFER_LABEL``.
OFFER_LABEL = "Try again"

# --- the gateway's command words (a2a/lib/chattext/commands.go) ----------

#: ``isStop``'s words, matched on the normalized text.
STOP_WORDS = frozenset({"stop", "cancel", "abort"})
#: ``isStatusQuery``'s exact phrases.
STATUS_QUERIES = frozenset({
    "what is it doing", "whats it doing", "what are you doing", "whats happening", "what is happening",
    "whats going on", "what is going on", "status", "progress", "any progress", "any update",
    "any updates", "where are we", "hows it going", "how is it going",
})
#: ``isStatusQuery``'s wide rule: a status word and an interrogative opening, in a short text.
WIDE_MATCH_LEN_CAP = 48
STATUS_WORDS = ("doing", "happening", "going on", "update")
STATUS_OPENERS = ("what", "how", "any", "is ", "are ")
#: ``isDelegate``: the word, a separator after it, then something to do.
DELEGATE_WORD = "delegate"
DELEGATE_SEPARATORS = frozenset(" \t\n:,-—")
#: ``isSessionCommand``: a leading slash and the word, then the end or whitespace.
SESSION_COMMAND = re.compile(r"/session(?:\s|$)", re.IGNORECASE)
NORMALIZE_SPACES = frozenset(" \t\n")


def normalize(text: str) -> str:
    """The gateway's ``normalize``: lower case, letters and digits, single spaces, apostrophes dropped."""
    out: list[str] = []
    last_space = True
    for char in str(text or "").strip().lower():
        if "a" <= char <= "z" or "0" <= char <= "9":
            out.append(char)
            last_space = False
        elif char == "'":
            last_space = False
        elif char in NORMALIZE_SPACES:
            if not last_space:
                out.append(" ")
            last_space = True
    return "".join(out).strip()


def _status_query(text: str) -> bool:
    words = normalize(text)
    if words in STATUS_QUERIES:
        return True
    if len(words) > WIDE_MATCH_LEN_CAP:
        return False
    return any(w in words for w in STATUS_WORDS) and words.startswith(STATUS_OPENERS)


def _delegate(text: str) -> bool:
    trimmed = str(text or "").strip()
    word = len(DELEGATE_WORD)
    if len(trimmed) <= word or trimmed[:word].lower() != DELEGATE_WORD or trimmed[word] not in DELEGATE_SEPARATORS:
        return False
    return bool(trimmed[word:].lstrip(":,-— \t\n").strip())


def command_word(text: str) -> bool:
    """Whether the gateway would act on ``text`` as a command, whatever the route or state.

    The superset the spec judges a choice against: a stop word, a status ask in
    its wide form, ``/session`` or the ``delegate`` prefix.
    """
    raw = str(text or "")
    return (
        normalize(raw) in STOP_WORDS
        or _status_query(raw)
        or bool(SESSION_COMMAND.match(raw.strip()))
        or _delegate(raw)
    )


# --- shared ----------------------------------------------------------------


def message(**fields: Any) -> dict:
    """A chat message: ``schema`` and every field that is not empty, in the spec's order."""
    out: dict = {"schema": SCHEMA}
    for name, value in fields.items():
        if value or (name == "mark" and value is not None):
            out[name] = value
    return out


def _line(text: Any) -> str:
    """``text`` on one line: its whitespace runs, newlines included, as single spaces."""
    return " ".join(str(text or "").split())


def _headline(text: Any) -> str:
    return _presenter._clip(_line(_presenter._plain(str(text or ""))), _presenter.HEADLINE_MAX)


def _choice(label: str, reply: str = "", primary: bool = False) -> dict | None:
    """One choice, or None when it cannot be one: empty, too long to reply, or a command word."""
    text = _presenter._clip(_line(label), _presenter.BUTTON_TEXT_MAX)
    if not text:
        return None
    reply = str(reply or "")
    if reply and (text not in reply or len(reply) > REPLY_MAX):
        return None
    if command_word(text) or (reply and command_word(reply)):
        return None
    choice: dict = {"text": text}
    if reply and reply != text:
        choice["reply"] = reply
    if primary:
        choice["primary"] = True
    return choice


def _links(pairs: Iterable[Any], primary_first: bool = False) -> list[dict]:
    """``(label, url)`` pairs as links, refused urls dropped as today drops them, the first maybe primary."""
    out = []
    for label, url in _presenter._link_pairs(pairs)[:LINKS_MAX]:
        link: dict = {"text": _presenter._clip(_line(label), _presenter.BUTTON_TEXT_MAX), "url": url}
        if primary_first and not out:
            link["primary"] = True
        out.append(link)
    return out


def ask_kind(text: str | None) -> str:
    """The ``mark.ask`` token for an ask, by ``slack_presenter.arrival_reaction``'s rules."""
    return ASK_BY_REACTION[_presenter.arrival_reaction(text)]


def mark(ask: str = "", settle: str = "") -> dict:
    """A whole ``mark`` state: ``{}`` takes the ask's reaction off and settles nothing."""
    out: dict = {}
    if ask and TOKEN.fullmatch(ask):
        out["ask"] = ask
    if settle in (SETTLE_DONE, SETTLE_BLOCKED, SETTLE_FAILED):
        out["settle"] = settle
    return out


# --- the plan --------------------------------------------------------------


def _plan_rows(rows: Sequence[Any]) -> list[Any]:
    return list(rows)[-_status.ROWS_MAX:]


def _item(row: Any, several: bool) -> dict:
    status = row.status if row.status in _status.TASK_STATUSES else _status.TASK_RUNNING
    notes = _status._notes(getattr(row, "lines", ()) or ())
    count = getattr(row, "steps", None)
    item: dict = {
        "id": str(row.task_id)[:PLAN_ID_MAX],
        "title": _status.row_title(row, several),
        "status": status,
    }
    steps = notes[-_status.STEPS_MAX:]
    if steps:
        item["steps"] = steps
    step_count = int(count) if count is not None else len(notes)
    if step_count > 0:
        item["stepCount"] = step_count
    # The note rides beside the title only where the title shows it: a running
    # or failed row's. A complete row shows its result, a pending one "waiting on you".
    note = _presenter._clip(_line(getattr(row, "note", "")), NOTE_MAX)
    if note and status in NOTE_STATUSES:
        item["note"] = note
    result = _presenter._clip(_line(getattr(row, "result", "")), PLAN_RESULT_MAX) if status == _status.TASK_COMPLETE else ""
    if result:
        item["result"] = result
    return item


def plan_message(title: str | None, rows: Sequence[Any]) -> dict:
    """The thread's plan: :func:`slack_status.plan_blocks`'s rows, each title composed by ``row_title``.

    ``rows`` are the objects ``plan_blocks`` takes; past ``ROWS_MAX`` the oldest
    are dropped, and each keeps its last ``STEPS_MAX`` notes as plain steps.
    """
    rows = _plan_rows(rows)
    several = len(rows) > 1
    return message(plan={"title": _status.plan_title(title, rows), "items": [_item(r, several) for r in rows]})


def plan_mark(ask: str, rows: Sequence[Any]) -> dict | None:
    """The ask's reaction state the plan's rows leave, by ``slack_ux_reactions``' settle rules.

    A row waiting on the user pauses the ask (⏸️ beside its kind); one running
    keeps the kind alone; once every row has settled the kind comes off, with
    ``failed`` if a card gave up (an archived one is not a failure) and ``done``
    otherwise. None for no rows. ``ask`` is the kind the conversation's route
    recorded, "" when none was.
    """
    rows = list(rows)
    if not rows:
        return None
    statuses = {row.status for row in rows}
    if _status.TASK_PENDING in statuses:
        return mark(ask, SETTLE_BLOCKED)
    if _status.TASK_RUNNING in statuses:
        return mark(ask)
    failed = any(row.status == _status.TASK_ERROR and not getattr(row, "archived", False) for row in rows)
    return mark(settle=SETTLE_FAILED if failed else SETTLE_DONE)


def plan_text(title: str | None, rows: Sequence[Any]) -> str:
    """:func:`slack_status.plan_text` unescaped: the gateway escapes a fallback itself."""
    rows = _plan_rows(rows)
    several = len(rows) > 1
    lines = [_status.plan_title(title, rows)]
    lines += [f"{_status.ROW_MARKERS.get(r.status, _status.STEP_OPEN)} {_status.row_title(r, several)}" for r in rows]
    return "\n".join(lines)


# --- answers and reports ---------------------------------------------------


def answer_message(markdown: str) -> dict | None:
    """An answer laid out: its first sentence as the headline, the rest as the detail.

    ``slack_presenter.split_answer``'s rules. A rest longer than a detail holds
    goes under "why" instead. None when today posts it plain: no headline (an
    answer that opens on a code fence) or a rest past :data:`FOLD_TEXT_MAX`.
    """
    headline, body = _presenter.split_answer(markdown)
    if not headline:
        return None
    text = "\n\n".join(body).strip()
    if len(text) > FOLD_TEXT_MAX:
        return None
    if len(text) > DETAIL_MAX:
        return message(headline=headline, fold={"title": FOLD_TITLE_DEFAULT, "text": text})
    return message(headline=headline, detail=text)


def _row(row: Any) -> dict:
    severity, text = _presenter._row_parts(row)
    text = _presenter._clip(_line(text), _presenter.ROW_TEXT_MAX)
    out: dict = {}
    if severity and not TOKEN.fullmatch(severity):
        # Not a token the spec takes: kept as the text's lead, as the fallback shows it.
        text, severity = _presenter._clip(_presenter.severity_row(severity, text), _presenter.ROW_TEXT_MAX), ""
    out["text"] = text
    if severity:
        out["severity"] = severity
    detail = _presenter._clip(_line(_presenter._row_detail(row)), _presenter.ROW_TEXT_MAX)
    if detail:
        out["detail"] = detail
    return out


def _usable(choices: Iterable[Any]) -> tuple[list[str], list[str]]:
    """``(labels that can be buttons, labels left for the detail)``, each clipped to a label."""
    kept, left = [], []
    for label in _presenter._choice_labels(choices):
        (left if _choice(label) is None else kept).append(label)
    return kept, left


def report_message(
    headline: str,
    note: str = "",
    rows: Sequence[Any] = (),
    choices: Iterable[Any] = (),
    links: Iterable[Any] = (),
    detail: str = "",
    after_rows: str = "",
) -> dict:
    """:func:`slack_presenter.blocks_report`'s report: the first choice primary, links after.

    ``after`` keeps its first :data:`AFTER_MAX` lines. A choice the
    gateway would read as a command is left out of the buttons and named in
    ``detail``, as a needs-you question keeps an option it cannot offer.
    """
    shown = _presenter._shown(rows)[:ROWS_MAX]
    kept, left = _usable(choices)
    detail_line = _presenter._clip(_line(_presenter._plain(detail or "")), _presenter.ROW_TEXT_MAX)
    if left:
        detail_line = "\n".join(part for part in (detail_line, *left) if part)
    after = [_presenter._clip(_line(line), _presenter.ROW_TEXT_MAX) for line in _presenter._after_lines(after_rows)]
    return message(
        headline=_headline(headline),
        note=_presenter._clip(_line(_presenter._plain(note or "")), NOTE_MAX),
        detail=detail_line,
        rows=[_row(row) for row in shown],
        after=after[:AFTER_MAX],
        choices=[_choice(label, primary=not i) for i, label in enumerate(kept[:CHOICES_MAX])],
        links=_links(links),
    )


# --- the moments -----------------------------------------------------------


def _evidence(url: str, number: str, line: str) -> str:
    """The worker's line with the url shortened to "PR #n", as :func:`slack_moments.pr_opened` shows it."""
    ref = _moments.PR_REF_URL.format(url=re.escape(url), tail=_moments.PR_URL_TAIL)
    span = re.compile(_moments.PR_REF_SPAN.format(number=number, ref=ref), re.IGNORECASE)
    shortened = span.sub(lambda m: _moments.PR_REF.format(number=number) + _moments._ref_markup(m), line)
    return _presenter._clip(_line(shortened), _moments.EVIDENCE_MAX)


def pr_message(url: str, repo: str, number: str, line: str) -> dict:
    """:func:`slack_moments.pr_opened`'s card: the headline, the worker's line, Open PR primary."""
    return message(
        headline=_moments.PR_HEADLINE.format(number=number, repo=repo),
        note=_evidence(url, number, line),
        tone=TONE_SUCCESS,
        links=_links([(_moments.OPEN_PR, url), (_moments.FILES_CHANGED, url + _moments.FILES_PATH)], primary_first=True),
    )


def pr_text(url: str, repo: str, number: str, line: str) -> str:
    """The PR card's fallback: the headline, the worker's line, the url."""
    return "\n".join(part for part in (_moments.PR_HEADLINE.format(number=number, repo=repo), line.strip(), url) if part)


def needs_you_message(reason: str, card: str = "", buttons: bool = True) -> dict | None:
    """:func:`slack_moments.needs_you`'s question, or None for an empty reason.

    The options become choices whose reply names ``card``; one the gateway
    would read as a command keeps every option in ``detail``, as today keeps
    them there when they cannot all be buttons.
    """
    headline, rest, options = _moments._question(reason, buttons)
    if options and any(_choice(o, NEEDS_YOU_REPLY.format(label=o, card=card) if card else "") is None for o in options):
        headline, rest, options = _moments._question(reason, False)
    if not headline:
        return None
    title = _presenter._clip(_line(_moments._headline_text(headline)), _presenter.HEADLINE_MAX)
    choices = [
        _choice(option, NEEDS_YOU_REPLY.format(label=option, card=card) if card else "") for option in options
    ]
    return message(headline=title, detail=_moments._detail(rest), tone=TONE_ATTENTION, choices=choices)


def needs_you_settled(chat: Mapping[str, Any]) -> dict:
    """The question once its card has moved on: the same message with no choices."""
    return {name: value for name, value in chat.items() if name != "choices"}


def failure_message(reply: str, offer: bool) -> dict | None:
    """A failure wake's reply: its first sentence as the headline, the rest as detail, Try again with ``offer``.

    ``offer`` is whether ``slack_ux_failure.present`` found the closing yes/no
    question it turns into the button. None when the rest is past
    :data:`DETAIL_MAX`, or there is no headline, and the reply posts as text.
    """
    headline, body = _presenter.split_answer(reply)
    detail = "\n\n".join(body).strip()
    if not headline or len(detail) > DETAIL_MAX:
        return None
    return message(headline=headline, detail=detail, choices=[_choice(OFFER_LABEL)] if offer else None)


def _triage_reply(label: str, reply: str) -> str:
    """An incident choice's reply led by its label: "<label> — apply Option B"."""
    letter = TRIAGE_OPTION_REPLY.match(str(reply))
    action = TRIAGE_OPTION_ACTION.format(letter=letter.group(1)) if letter else TRIAGE_SINGLE_ACTION
    return TRIAGE_REPLY.format(label=label, action=action)


def triage_message(triage: Mapping[str, Any], fold_text: str) -> dict | None:
    """``slack_ux_incident.parse_triage``'s result as the alert's edit, or None when it cannot fold.

    Each label is the option's title, the recommended one primary rather than suffixed, and its reply
    starts with that label: "<label> — apply Option B".
    """
    if len(fold_text) > FOLD_TEXT_MAX:
        return None
    choices = []
    for label, reply, recommended in triage["choices"]:
        text = _presenter._clip(_line(str(label).removesuffix(RECOMMENDED_SUFFIX)), _presenter.BUTTON_TEXT_MAX)
        choice = _choice(text, _triage_reply(text, reply), primary=bool(recommended))
        if choice is None:
            return None
        choices.append(choice)
    if sum(1 for choice in choices if choice.get("primary")) > 1 or len(choices) > CHOICES_MAX:
        return None
    return message(
        headline=_headline(triage["headline"]),
        fold={"title": str(triage.get("fold_title") or FOLD_TITLE_DEFAULT)[:FOLD_TITLE_MAX], "text": fold_text},
        choices=choices,
        links=_links([(TRIAGE_LINK_LABEL.format(label=label), url) for label, url in triage.get("links") or ()]),
    )


def triage_text(triage: Mapping[str, Any], report: str) -> str:
    """The alert edit's fallback: the headline, what each button sends, then the report."""
    replies = [_triage_reply(_presenter._clip(_line(str(label).removesuffix(RECOMMENDED_SUFFIX)), _presenter.BUTTON_TEXT_MAX), reply)
               for label, reply, _rec in triage["choices"]]
    head = "\n".join([_headline(triage["headline"]), *replies])
    return f"{head}\n\n{report}"
