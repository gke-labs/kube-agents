"""Slack presentation for kube-agents: answer layout, buttons and reactions.

Nothing here imports the Hermes gateway, the Slack SDK or the network, and
every function is pure but :func:`ack_link_click`, the coroutine a caller
registers to acknowledge a link-button click, so any process that posts to
Slack can use it, and it can move with Slack ingress when it leaves the
gateway. Today its one caller is the
gateway's reactions patch (``slack_ux_reactions``), which the kanban notifier
also reaches.
Every caller reaches it through ``PYTHONPATH=/opt/defaults/scripts``, which the
operator sets on the agent container.

Everything a caller changes on screen is gated on :func:`enabled`, the
``KAGE_SLACK_UX`` environment variable, off by default. With it off, callers
take their upstream path unchanged; this module only answers questions.

Layout: :func:`split_answer` takes the headline off an agent's markdown
answer; url link buttons and choice buttons, whose value is the label, are
built by ``_button`` and wrapped into rows by ``_actions``; :func:`fallback_text`
is the headline, links and choices as plain mrkdwn, for the message's ``text``
field.

Reactions (:func:`arrival_reaction`, :func:`settle_reaction`): the first
reaction says what kind of ask arrived, chosen by keyword before any model
call; a second joins it when the work settles. The first is never removed; the
credential proxy refuses every Slack method ending in ``remove``.
"""

from __future__ import annotations

import os
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

#: The flag, and the values that turn it on. Anything else (unset included) is off.
FLAG_ENV = "KAGE_SLACK_UX"
FLAG_ON_VALUES = frozenset({"1", "true", "yes", "on"})

# --- reactions -------------------------------------------------------------

#: Slack emoji names (``reactions.add`` takes the name, not the glyph).
REACTION_QUESTION = "eyes"  # 👀
REACTION_CHANGE = "hammer_and_wrench"  # 🛠️
REACTION_BOARD = "clipboard"  # 📋
REACTION_INCIDENT = "rotating_light"  # 🚨
REACTION_DONE = "white_check_mark"  # ✅
REACTION_BLOCKED = "double_vertical_bar"  # ⏸️
REACTION_FAILED = "x"  # ❌

#: How work can settle, as the callers name it.
SETTLE_DONE = "done"
SETTLE_BLOCKED = "blocked"
SETTLE_FAILED = "failed"
SETTLE_REACTIONS = {
    SETTLE_DONE: REACTION_DONE,
    SETTLE_BLOCKED: REACTION_BLOCKED,
    SETTLE_FAILED: REACTION_FAILED,
}

#: Kanban notifier event kinds that settle delegated work. ``crashed`` and
#: ``timed_out`` are absent on purpose: the dispatcher retries both, so the work
#: has not settled. ``block_loop_detected`` sends the card back to triage, where
#: it can run again, so it settles provisionally, like ``blocked``. Bookkeeping
#: kinds (``status``, ``archived``, ``unblocked``) settle nothing.
SETTLE_BY_KANBAN_KIND = {
    "completed": SETTLE_DONE,
    "blocked": SETTLE_BLOCKED,
    "review_requested": SETTLE_BLOCKED,
    "changes_requested": SETTLE_BLOCKED,
    "gave_up": SETTLE_FAILED,
    "block_loop_detected": SETTLE_BLOCKED,
}

#: Settle outcomes after which a later settle can still follow on the same ask:
#: a blocked card is answered and runs on to done.
PROVISIONAL_SETTLES = frozenset({SETTLE_BLOCKED})

#: Keyword classes, tried in this order; the first match wins. A change request
#: outranks everything because it is the ask with consequences. A question
#: outranks the incident words, so "is checkout crashlooping?" is 👀 (a check),
#: while "checkout is down" is 🚨.
CHANGE_WORDS = re.compile(
    r"\bfix(es|ing)?\b"
    r"|\bbump\b"
    r"|\broll(ing)?[\s-]*back\b|\brollback\b"
    r"|\bopen\s+(a\s+|the\s+|an\s+)?(pr|pull\s+request)\b"
    r"|\bscale\b"
    r"|\bupgrade\b",
    re.IGNORECASE,
)
BOARD_WORDS = re.compile(
    r"\bboard\b|\bstatus\b|\bwhat'?s\s+running\b|\bwhat\s+is\s+running\b",
    re.IGNORECASE,
)
QUESTION_OPENERS = frozenset(
    {
        "is", "are", "was", "were", "why", "what", "whats", "what's", "how", "which",
        "who", "when", "where", "does", "do", "did", "can", "could", "should", "will",
        "would", "has", "have", "check", "show", "list", "tell", "explain", "describe",
    }
)
QUESTION_MARK = "?"
INCIDENT_WORDS = re.compile(
    r"\bdown\b|\bpag(e|ed|es|ing)\b|\boutage\b|\bsev\s?\d\b|\bsev\b|\bcrash\s?loop",
    re.IGNORECASE,
)
#: A Slack user or channel mention, stripped before the opener check so
#: "<@U123> is it down?" still reads as a question. A token holds no "<", so a
#: run of unclosed "<!" fails at the next one rather than scanning to the end.
MENTION = re.compile(r"<[@#!][^<>]*>")
FIRST_WORD = re.compile(r"[A-Za-z']+")

# --- layout ----------------------------------------------------------------

#: Slack's limits: button label, button value, button url. Slack refuses the whole message
#: for a url past its limit, so :func:`_safe_link_url` refuses one.
BUTTON_TEXT_MAX = 75
BUTTON_VALUE_MAX = 2000
BUTTON_URL_MAX = 3000
#: The schemes a link button or ``<url|label>`` may open; :func:`urlsplit` lowercases a url's.
LINK_SCHEMES = ("http", "https")
#: Buttons per actions block: Slack allows 25, but a row past 5 wraps badly on mobile.
BUTTONS_PER_ROW = 5
#: The headline's length, and what a clipped headline or label ends with.
HEADLINE_MAX = 150
ELLIPSIS = "…"

#: Action ids: ``<prefix>.link.<n>`` and ``<prefix>.choice.<n>``, which callers build
#: from these. Link buttons open their url client-side and Slack still sends a
#: block_actions request, which :func:`ack_link_click` acknowledges once a caller
#: registers it for :data:`LINK_ACTION_ID_PATTERN`.
LINK_ACTION = "link"
CHOICE_ACTION = "choice"
LINK_ACTION_ID_PATTERN = re.compile(r"\.link\.\d+$")
CHOICE_ACTION_ID_PATTERN = re.compile(r"\.choice\.\d+$")

CHOICES_LEAD = "Reply with one of: "
CHOICE_SEPARATOR = " · "

#: A code fence line; nothing between an opener and its closer is a heading, a bullet or markup.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
LIST_MARKER = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
#: Bold markers. An opener has no word character before it and a closer none after
#: it, so an in-word run such as ``DB__HOST`` or ``2**20`` is text.
BOLD_MARKERS = ("**", "__")
BOLD_OPEN = r"(?<!\w){0}(?=\S)"
BOLD_CLOSE = r"(?<=\S){0}(?!\w)"
#: As in CommonMark, ``**`` may also bold part of a word (``**Pod**s``, ``re**start**ed``),
#: but only a run without spaces when the opener is in-word, so ``2**20 and 2**30`` stays text.
#: ``__`` keeps the word-boundary rule. A bold ends at the nearest marker, so a run of three or
#: more markers (``___x___``, ``******a``) or an inner ``__`` that cannot close (``__foo__bar__``) stays text;
#: a body that ran past markers retried to the line's end from every opener.
STAR_BOLD = BOLD_OPEN.format(re.escape("**")) + r"((?:(?!\*\*).)+?)(?<=\S)\*\*"
STAR_BOLD_IN_WORD = r"(?<=\w)\*\*([^\s*]+)\*\*"
UNDERSCORE_BOLD = BOLD_OPEN.format(re.escape("__")) + "((?:(?!__).)+?)" + BOLD_CLOSE.format(re.escape("__"))
MD_BOLD = re.compile(f"{STAR_BOLD}|{STAR_BOLD_IN_WORD}|{UNDERSCORE_BOLD}")
#: A markdown link; the url may hold balanced parentheses, as a Logs Explorer query does.
MD_LINK_URL = r"[^()\s]+(?:\([^()\s]*\)[^()\s]*)*"
MD_LINK = re.compile(rf"\[([^\[\]]+)\]\(({MD_LINK_URL})\)")
#: ``*italic*`` and ``_italic_``; a ``*`` inside a word (``2*3``) or unpaired (``*.tmp``) is text.
MD_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s.])([^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=[^\s.])([^_\n]+?)(?<=\S)_(?![\w_])")
#: A backtick run, which opens or closes a code span (see ``_code_spans``).
BACKTICK_RUN = re.compile(r"`+")
#: Holds a code span's place while the other markup is stripped; input NULs are dropped first.
CODE_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
MRKDWN_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))
#: The characters that would end a Slack mrkdwn link early, percent-encoded so the button and
#: the fallback text carry the same url; ``%`` is left alone, so an encoded url stays as it is.
MRKDWN_URL_ESCAPES = str.maketrans({"<": "%3C", ">": "%3E", "|": "%7C"})
#: A sentence ends at ``.``, ``!`` or ``?``, or just after the emphasis that closes on one,
#: then space, then anything but a lowercase letter ("in ns. prod" runs on).
SENTENCE_END = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][*_])|(?<=[.!?]\*\*)|(?<=[.!?]__))\s+(?=[^\sa-z])")
#: Per bold marker, its opener and closer, which a split can leave unpaired; the first
#: sentence closes one and the rest reopens it.
BOLD_EDGES = {
    marker: (re.compile(BOLD_OPEN.format(re.escape(marker))), re.compile(BOLD_CLOSE.format(re.escape(marker))))
    for marker in BOLD_MARKERS
}
#: The same for an italic marker, with :data:`MD_ITALIC`'s edges: an opener not before a
#: space or ``.``, a closer after no space, neither beside a word character or another marker.
ITALIC_EDGES = {
    marker: (
        re.compile(rf"(?<![\w{re.escape(marker)}]){re.escape(marker)}(?=[^\s.])"),
        re.compile(rf"(?<=\S){re.escape(marker)}(?![\w{re.escape(marker)}])"),
    )
    for marker in ("*", "_")
}
#: A ``*``, ``_`` or ``~`` at a word's edge, which Slack mrkdwn can read as markup; one inside
#: a word (``2*3``, ``DB_HOST``) is text. ``_`` is markup only as a pair, :data:`MD_ITALIC`'s.
FALLBACK_LIVE_MARKER = re.compile(r"(?<!\w)[*~]+|[*~]+(?!\w)")
#: A text ending in one of these abbreviations has not ended its sentence.
ABBREVIATION_END = re.compile(
    r"(?:^|[\s(\[])(?:e\.g|i\.e|vs|approx|incl|cf|etc|esp|ex|fig|rev|ver|cont|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.$",
    re.IGNORECASE,
)
#: "No.", "max." and "min." abbreviate only before a number ("Ticket No. 3", "at its max. 4
#: pods"); "The answer is no." and "Replicas are at max." end.
NUMBER_ABBREVIATION_END = re.compile(r"(?:^|[\s(\[#])(?:no|max|min)\.$", re.IGNORECASE)
NUMBER_NEXT = re.compile(r"\d")
#: How far back from a candidate end an abbreviation can start; bounds the check to the tail.
ABBREVIATION_TAIL = 16
#: A space-aligned clip shorter than this share of the limit drops too much; it cuts hard instead.
CLIP_MIN_SHARE = 2


def enabled() -> bool:
    """Whether ``KAGE_SLACK_UX`` is on in this process's environment."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in FLAG_ON_VALUES


# --- reactions -------------------------------------------------------------


def _is_question(text: str) -> bool:
    if text.rstrip().endswith(QUESTION_MARK):
        return True
    first = FIRST_WORD.search(text)
    return bool(first) and first.group(0).lower() in QUESTION_OPENERS


def arrival_reaction(text: str | None) -> str:
    """The emoji name for an ask, from its words alone (no model call)."""
    clean = MENTION.sub(" ", text or "").strip()
    if CHANGE_WORDS.search(clean):
        return REACTION_CHANGE
    if BOARD_WORDS.search(clean):
        return REACTION_BOARD
    if _is_question(clean):
        return REACTION_QUESTION
    if INCIDENT_WORDS.search(clean):
        return REACTION_INCIDENT
    return REACTION_QUESTION


def settle_reaction(outcome: str) -> str | None:
    """The emoji name for ``done``/``blocked``/``failed``; None for anything else."""
    return SETTLE_REACTIONS.get(outcome)


def settle_for_kanban_kind(kind: str) -> str | None:
    """``done``/``blocked``/``failed`` for a notifier event kind that settles work, else None."""
    return SETTLE_BY_KANBAN_KIND.get(kind)


# --- markdown --------------------------------------------------------------


def next_fence(line: str, fence: str | None) -> str | None:
    """The fence open after ``line``, given the one open before it.

    As in CommonMark, only a line of the opener's character, at least as long, closes it, an
    unclosed fence runs to the end, and a backtick run followed by another backtick is a code span.
    """
    match = FENCE.match(line)
    if not match:
        return fence
    mark = match.group(1)
    if fence is None:
        return None if mark[0] == "`" and "`" in line[match.end():] else mark
    closes = mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip().strip(mark[0])
    return None if closes else fence


def _opens_fence(line: str) -> bool:
    return next_fence(line, None) is not None


def _paragraphs(markdown: str) -> list[str]:
    """Blank-line separated blocks, never splitting inside a fenced code block."""
    out: list[str] = []
    current: list[str] = []
    fence = None
    for line in markdown.split("\n"):
        fence = next_fence(line, fence)
        if fence is None and not line.strip():
            if current:
                out.append("\n".join(current).strip("\n"))
                current = []
            continue
        current.append(line)
    if current:
        out.append("\n".join(current).strip("\n"))
    return [p for p in out if p.strip()]


def _plain(markdown: str) -> str:
    """One line of markdown as plain text: no heading, list marker, emphasis or link syntax.

    Code spans are held out first, as a renderer resolves them first, so ``__init__`` in one stays.
    """
    text, spans = _hold_code(markdown.replace("\x00", "").strip())
    text = HEADING.sub("", text)
    text = LIST_MARKER.sub("", text)
    text = MD_LINK.sub(r"\1", text)
    text = MD_BOLD.sub(lambda m: next(g for g in m.groups() if g is not None), text)
    text = MD_ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return CODE_PLACEHOLDER.sub(lambda m: spans[int(m.group(1))][1].strip(), text).strip()


def _code_spans(text: str) -> list[tuple[int, int, int]]:
    """``(start, end, opener length)`` of each code span in ``text``, left to right.

    A span opens on a backtick run and closes on the first later run of exactly that length
    on the same line; a run with no such closer is literal backticks (CommonMark). Each
    line's runs are indexed by length and the closing run bisected for.
    """
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for line in text.split("\n"):
        runs = [(m.start(), m.end() - m.start()) for m in BACKTICK_RUN.finditer(line)]
        starts: dict[int, list[int]] = {}
        for start, length in runs:
            starts.setdefault(length, []).append(start)
        end = 0
        for start, length in runs:
            if start < end:
                continue
            closers = starts[length]
            after = bisect_right(closers, start)
            if after < len(closers):
                end = closers[after] + length
                spans.append((offset + start, offset + end, length))
        offset += len(line) + 1
    return spans


def _hold_code(text: str) -> tuple[str, list[tuple[str, str]]]:
    """``text`` with each code span swapped for a placeholder, and each span's text and content."""
    held: list[str] = []
    spans: list[tuple[str, str]] = []
    last = 0
    for start, end, opener in _code_spans(text):
        held += [text[last:start], f"\x00{len(spans)}\x00"]
        spans.append((text[start:end], text[start + opener : end - opener]))
        last = end
    return "".join(held) + text[last:], spans


def _restore_code(text: str, spans: list[tuple[str, str]]) -> str:
    """``text`` with each placeholder put back as its code span, backticks included."""
    return CODE_PLACEHOLDER.sub(lambda m: spans[int(m.group(1))][0], text)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    hard = text[: limit - len(ELLIPSIS)]
    cut = hard.rsplit(" ", 1)[0]
    # A long unbroken word would otherwise take everything after the last space with it.
    if len(cut) * CLIP_MIN_SHARE < len(hard):
        cut = hard
    return cut.rstrip() + ELLIPSIS


def _escape(text: str) -> str:
    for raw, escaped in MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


def _link_url(url: str) -> str:
    """``url`` as the target of a mrkdwn ``<url|label>`` that opens what a button with ``url`` opens.

    ``&`` goes first, as ``&amp;``, which Slack decodes back, so an entity already in the url
    (``&lt;``) reaches the browser as written.
    """
    return url.replace("&", "&amp;").translate(MRKDWN_URL_ESCAPES)


def _first_sentence(line: str) -> tuple[str, str]:
    """``(first sentence, the rest)`` of ``line``, not cut after an abbreviation."""
    for match in SENTENCE_END.finditer(line):
        sentence = line[: match.start()]
        stem = sentence.rstrip("*_")
        tail = max(0, len(stem) - ABBREVIATION_TAIL)
        numbered = NUMBER_ABBREVIATION_END.search(stem, tail) and NUMBER_NEXT.match(line, match.end())
        if not (numbered or ABBREVIATION_END.search(stem, tail)):
            rest = line[match.end() :].strip()
            # "**One. Two.**" splits inside the bold, which would leave both halves unpaired.
            for marker, (opener, closer) in BOLD_EDGES.items():
                if sentence.count(marker) % 2 and opener.search(sentence) and closer.search(rest):
                    sentence, rest = sentence + marker, marker + rest
            # "*One. Two.*" likewise, counting only the markers no bold marker holds.
            for marker, (opener, closer) in ITALIC_EDGES.items():
                lone = sentence.replace(marker * 2, "").count(marker)
                if lone % 2 and opener.search(sentence) and closer.search(rest):
                    sentence, rest = sentence + marker, marker + rest
            return sentence, rest
    return line, ""


def split_answer(markdown: str) -> tuple[str, list[str]]:
    """``(headline, body_sections)`` for an agent's markdown answer.

    The headline is the first sentence of the first paragraph, as plain text
    capped at ``HEADLINE_MAX``. The rest of that paragraph and every later
    paragraph are the body sections, still markdown, in order. Empty input
    gives ``("", [])``.
    """
    paragraphs = _paragraphs((markdown or "").replace("\x00", ""))
    if not paragraphs:
        return "", []
    first, rest = paragraphs[0], paragraphs[1:]
    if _opens_fence(first.split("\n", 1)[0]):
        return "", paragraphs
    # A soft-wrapped sentence continues onto the next line; a list item, heading or fence does not,
    # and nothing continues a heading.
    lines = first.split("\n")
    wrapped = 1
    while wrapped < len(lines) and not HEADING.match(lines[0]) and not (
        LIST_MARKER.match(lines[wrapped]) or HEADING.match(lines[wrapped]) or _opens_fence(lines[wrapped])
    ):
        wrapped += 1
    joined = " ".join(line.strip() for line in lines[:wrapped])
    more_lines = "\n".join(lines[wrapped:])
    # The list marker goes first, or "1. Checkout is down." would end at "1.".
    # A period inside a code span does not end the sentence.
    held, spans = _hold_code(LIST_MARKER.sub("", joined))
    sentence, remainder = (_restore_code(part, spans) for part in _first_sentence(held))
    headline = _clip(_plain(sentence), HEADLINE_MAX)
    tail = "\n".join(part for part in (remainder, more_lines.strip("\n")) if part)
    body = ([tail] if tail.strip() else []) + rest
    return headline, body


# --- blocks ----------------------------------------------------------------


def _link_pairs(links: Iterable[Any]) -> list[tuple[str, str]]:
    """``(label, url)`` from tuples or ``{"text", "url"}`` mappings; entries without a :func:`_safe_link_url` dropped."""
    pairs = []
    for link in links or ():
        if isinstance(link, Mapping):
            label, url = link.get("text") or link.get("label") or "", link.get("url") or ""
        else:
            label, url = link
        if url and _safe_link_url(str(url)):
            pairs.append((str(label or url), str(url)))
    return pairs


def _safe_link_url(url: str) -> bool:
    """Whether ``url`` may back a link: :data:`LINK_SCHEMES`, a host, no userinfo, no whitespace.

    In ``https://console.cloud.google.com@evil.example/`` the host is ``evil.example``, so a
    url with a user or password is refused, as is a ``\\`` in its host part, which a browser
    reads as ``/``. No longer than :data:`BUTTON_URL_MAX`, checked first.
    """
    if len(url) > BUTTON_URL_MAX or any(char.isspace() for char in url):
        return False
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in LINK_SCHEMES
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and "\\" not in parts.netloc
        )
    except ValueError:
        return False


def _button(label: str, action_id: str, *, url: str | None = None, value: str | None = None) -> dict:
    button: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": _clip(label, BUTTON_TEXT_MAX), "emoji": True},
        "action_id": action_id,
    }
    if url is not None and _safe_link_url(url):
        button["url"] = url
    if value is not None:
        button["value"] = value[:BUTTON_VALUE_MAX]
    return button


def _actions(buttons: Sequence[dict]) -> list[dict]:
    return [
        {"type": "actions", "elements": list(buttons[i : i + BUTTONS_PER_ROW])}
        for i in range(0, len(buttons), BUTTONS_PER_ROW)
    ]


def fallback_text(headline: str, links: Iterable[Any] = (), choices: Iterable[str] = ()) -> str:
    """The same layout as plain mrkdwn, with no buttons.

    Used as the ``text`` of a blocks message (notifications, screen readers).
    ``headline`` is already plain, as :func:`split_answer` gives it; a second
    plain pass would strip markup a code span had kept. Links become inline
    ``<url|label>``, the url escaped by :func:`_link_url` so the link opens what
    its button opens; choices become one "Reply with one of:" line. Every label
    is escaped, so none can mention anyone, and a ``*``, ``_`` or ``~`` left in the headline
    that Slack would read as markup is dropped.
    """
    parts: list[str] = []
    title = _clip((headline or "").strip(), HEADLINE_MAX)
    # Markup left in the headline would end or nest inside the bold it is wrapped in.
    title = FALLBACK_LIVE_MARKER.sub("", MD_ITALIC.sub(lambda m: m.group(1) or m.group(2), title)).strip()
    if title:
        parts.append(f"*{_escape(title)}*")
    pairs = _link_pairs(links)
    if pairs:
        parts.append(CHOICE_SEPARATOR.join(f"<{_link_url(url)}|{_escape(label)}>" for label, url in pairs))
    labels = [_escape(str(c)) for c in choices or () if str(c).strip()]
    if labels:
        parts.append(CHOICES_LEAD + CHOICE_SEPARATOR.join(labels))
    return "\n".join(parts)


# --- link-button ack -------------------------------------------------------


async def ack_link_click(ack: Any, body: Any = None, action: Any = None) -> None:
    """Acknowledge a link-button click and do nothing else; Slack already opened the url."""
    await ack()
