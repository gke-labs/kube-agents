"""Slack presentation for kube-agents: answer layout, buttons, reactions and the flag.

Pure functions, plus ``ack_link_click``, the no-op handler a caller registers
for link buttons. Nothing here imports the Hermes gateway, the Slack SDK or
the network, so any process that posts to Slack can use it, and it can move
with Slack ingress when it leaves the gateway. Its callers are the gateway
patches for reactions (``slack_ux_reactions``, which the kanban notifier
also reaches), plan and session status (``slack_ux_status``, which reads only
:func:`enabled`), harness messages (``slack_boilerplate``, which reads only
:func:`enabled` too), moments (``slack_ux_moments``, through ``slack_moments``)
and button clicks (``slack_ux_clicks``, which uses the action ids and the
link ack); the Chat Agent's ``bootstrap_delivery``, which lays out the first
inventory report through ``inventory_presenter``; and ``session_kv_server``'s
cron relay, which gates the fleet-audit report on :func:`enabled` and lays it
out through ``slack_audit_report``.
Every caller reaches it through ``PYTHONPATH=/opt/defaults/scripts``, which the
operator sets on the agent container.

Everything a caller changes on screen is gated on :func:`enabled`, the
``KAGE_SLACK_UX`` environment variable, off by default. With it off, callers
take their upstream path unchanged; this module only answers questions.

Layout (:func:`blocks_answer`): a bold headline, url link buttons, and choice
buttons whose value is the label; ``slack_moments`` lays out the messages
``slack_ux_moments`` posts with it. :func:`split_answer` takes the headline off
an agent's markdown answer. :func:`fallback_text` is the headline, links and
choices as plain mrkdwn, for the message's ``text`` field, with any report rows
led by their severity as inline code (a bullet when they have none).

Reports (:func:`blocks_report`): a headline, the top findings as one group
between dividers, then the choice buttons and the link buttons. Block Kit has no bordered box a message
can draw, so the dividers stand in for a border. :func:`names_gap` and
:func:`as_line` find and set out a report's line saying what was not scanned.

Reactions (:func:`arrival_reaction`, :func:`settle_reaction`): the first
reaction says what kind of ask arrived, chosen by keyword before any model
call; a second joins it when the work settles. The first is never removed; the
credential proxy refuses every Slack method ending in ``remove``.
"""

from __future__ import annotations

import itertools
import os
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

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

#: Slack's limits: button label, button value, buttons shown per actions block
#: before mobile wraps badly (Slack allows 25; Hermes uses 5).
BUTTON_TEXT_MAX = 75
BUTTON_VALUE_MAX = 2000
BUTTONS_PER_ROW = 5
#: Ours, not Slack's: the longest headline, and the mark a clip ends with.
HEADLINE_MAX = 150
ELLIPSIS = "…"

#: Action ids: ``<prefix>.link.<n>`` and ``<prefix>.choice.<n>``. Link buttons
#: open their url client-side and Slack still sends a block_actions request,
#: which the no-op handler acknowledges; a choice click is answered as the
#: clicker's reply (the gateway's ``slack_ux_clicks``).
LINK_ACTION = "link"
CHOICE_ACTION = "choice"
LINK_ACTION_ID_PATTERN = re.compile(r"\.link\.\d+$")
CHOICE_ACTION_ID_PATTERN = re.compile(r"\.choice\.\d+$")
#: The block that says a message waits on an answer; a choice click drops it.
WAITING_BLOCK_ID = "kage_waiting"

CHOICES_LEAD = "Reply with one of: "
CHOICE_SEPARATOR = " · "
BULLET = "• "

#: A row's severity, as inline code ahead of its text: "`critical` text".
SEVERITY_TAG = "`{severity}` {text}"
BACKTICK = "`"

#: Characters per report row.
ROW_TEXT_MAX = 300
PRIMARY_STYLE = "primary"

CODE_FENCE = "```"

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
#: A sentence ends at ``.``, ``!`` or ``?``, or just after the emphasis that closes on one,
#: then space, then anything but a lowercase letter ("in ns. prod" runs on).
SENTENCE_END = re.compile(r"(?:(?<=[.!?])|(?<=[.!?][*_])|(?<=[.!?]\*\*)|(?<=[.!?]__))\s+(?=[^\sa-z])")
#: Per bold marker, its opener and closer, which a split can leave unpaired; the first
#: sentence closes one and the rest reopens it.
BOLD_EDGES = {
    marker: (re.compile(BOLD_OPEN.format(re.escape(marker))), re.compile(BOLD_CLOSE.format(re.escape(marker))))
    for marker in BOLD_MARKERS
}
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
#: Slack refuses a button whose url is longer than this.
URL_MAX = 3000
#: A url a link button or ``<url|label>`` may carry: http(s) in any case, no userinfo (an ``@``
#: before the path, query or fragment, which would show one host and open another), nothing that
#: ends or splits the link, and short enough for a button. Use ``fullmatch``: ``$`` would admit a
#: trailing newline.
SAFE_URL = re.compile(
    rf"https?://(?=[^\s<>|]{{1,{URL_MAX - len('https://')}}}\Z)[^\s<>|/?#@]*(?:[/?#][^\s<>|]*)?", re.IGNORECASE
)
#: A space-aligned clip shorter than this share of the limit drops too much; it cuts hard instead.
CLIP_MIN_SHARE = 2
INLINE_CODE = re.compile(r"`[^`]*`")
#: :func:`to_mrkdwn` holds a code span's place with this; :data:`CODE_PLACEHOLDER` finds it again.
CODE_HOLD = "\x00{}\x00"
#: A bold takes :data:`STAR_BOLD`'s and :data:`STAR_BOLD_IN_WORD`'s edges, so a card row and its text
#: fallback agree on ``2**20 and 2**30``.
RICH_SPAN = re.compile(
    rf"`(?P<code>[^`]+)`|{BOLD_OPEN.format(re.escape('**'))}(?P<bold>(?:(?!\*\*).)+?)(?<=\S)\*\*"
    r"|(?<=\w)\*\*(?P<inbold>[^\s*]+)\*\*"
    rf"|\[(?P<label>[^\[\]]+)\]\((?P<url>{MD_LINK_URL})\)"
    r"|(?<![\w*])\*(?=[^\s.])(?P<star>[^*\n]+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=[^\s.])(?P<under>[^_\n]+?)(?<=\S)_(?![\w_])"
    r"|(?<![\w~])~(?=\S)(?P<strike>[^~\n]+?)(?<=\S)~(?![\w~])"
)
#: A bare url in :func:`to_mrkdwn`'s input, held out of the bold pass like a code span. Its last
#: character is not markup or punctuation, so ``**https://x.io**`` still bolds the url.
BARE_URL = re.compile(r"https?://[^\s<>|\x00]*[^\s<>|\x00*_.,;:!?'\"]", re.IGNORECASE)

#: A report naming something that was not scanned (the prioritization SOP's Step 1).
GAP = re.compile(
    r"\b(?:could\s+not|couldn't|cannot|can't|unable\s+to|was\s+not|were\s+not|wasn't|weren't|not)"
    r"\s+(?:be\s+)?(?:scanned|reached|checked|accessed|read)\b"
    r"|\b(?:unreachable|unavailable|inaccessible|skipped|did\s+not\s+run)\b"
    r"|\b(?:permission|access)\s+denied\b(?!\s+(?:events?|requests?)\b)"
    r"|\b(?:clusters?|namespaces?|projects?|nodes?)\s+(?:(?:were|was)\s+)?(?:denied|forbidden)\b(?!\s+(?:by|from)\b)",
    re.IGNORECASE,
)
#: "No clusters were unreachable.", "0 clusters unreachable", "There were 0
#: clusters that were unreachable", "Not a single cluster was unreachable": a
#: part of a clause whose subject is one of these names no gap.
NO_GAP = re.compile(
    r"^\s*(?:there\s+(?:were|was|are|is)\s+)?(?:no|none|nothing|zero|0|not\s+(?:a\s+single|one|any))\b", re.IGNORECASE
)
#: "with no unreachable nodes": the gap word negated right before it.
NEGATED = re.compile(r"\b(?:no|zero)\s+$", re.IGNORECASE)
#: How far back from a gap word NEGATED reads: "zero" and a run of spaces. Farther
#: than that, the clause errs toward naming a gap rather than hiding one.
NEGATION_REACH = 100
#: "Clusters skipped: none", "unreachable: 0": the gap word negated right after
#: it, by a word ending the clause; "skipped: no credentials" is a reason.
NONE_AFTER = re.compile(r"\s*[:=—–]\s*(?:none|nothing|zero|no|0)\s*(?:[.;,!?)]|$)", re.IGNORECASE)
#: Where a clause's parts divide, so "No drift, but 2 clusters were
#: unreachable" keeps its gap.
CLAUSE_PART = re.compile(r"[,:]|\b(?:but|and|while|although|though)\b", re.IGNORECASE)
#: Where a line's parts divide, for the one that names a gap; kept, so a
#: part's "skipped" can still be read against the ": none" after it.
#: ``(?<!\s)`` here and below starts a whitespace run's match only at the run's
#: first character: the same matches, but a long run is not retried from every
#: position inside it, which made a 96 KB run of spaces take minutes.
LINE_PART = re.compile(r"([;:,]\s+|[.!?]\s+|(?<!\s)\s+[—–]\s+)")
#: Taken out with the space before it, so no double space is left behind.
PARENTHETICAL = re.compile(r"(?<!\s)\s*\(([^()]*)\)")
#: A parenthetical held out of a line while it is split, so its commas do not divide it.
HELD = "\0{}\0"
HELD_REF = re.compile(r"\0(\d+)\0")
#: Fills a parenthetical's span so a join inside it is not found.
HELD_MASK = "\0"
#: A conjunction that starts a second count inside a gap part: "2 new and 1 skipped".
COUNT_JOIN = re.compile(r"(?<!\s)\s+(?:and|but|while|although|though)\s+(?=\d)", re.IGNORECASE)
#: A conjunction that can start a gap after the counts: "7 findings across 3 clusters but seeded-c was not reached".
GAP_JOIN = re.compile(r"(?<!\s)\s+(?:and|but|while|although|though)\s+", re.IGNORECASE)
#: The most joins a gap part is searched at; each reads the whole text before it,
#: so a part of thousands of "and"s would otherwise take seconds. Past it the part stays whole.
GAP_JOINS_MAX = 20
#: A count, not a digit inside a name such as seeded-3.
COUNT = re.compile(r"(?<![\w.-])\d")
#: A name and a plural verb after "and": "2 clusters and seeded-c were unreachable" is one subject.
SHARED_SUBJECT = re.compile(
    r"^(?!(?:they|others|some|both|all|these|those|rest)\s)(?=[\w.-]*[a-z])[\w.-]+\s+(?:were|are|have)(?:n['’]t)?\b",
    re.IGNORECASE,
)
#: One name in the list a gap's colon opens ("skipped: seeded-d (no credentials), seeded-e"),
#: as against a reason ("unreachable: no response from the control plane") or a count.
LISTED_NAME = re.compile(r"\s*(?!\d)[\w.-]+(?:\s*\([^()]*\))?\s*[.!?]?\s*")
#: The word joining a gap part to the clean part before it: "..., but 2 clusters were skipped".
JOINING_WORD = re.compile(r"^(?:but|and|while|although|though|yet)\s+", re.IGNORECASE)
COLON = ":"
COMMA = ","
CLAUSE_ENDS = ".!?"
CLAUSE_STOP = "."
#: A first word :func:`as_line` capitalizes; "seeded-c" or "kube-system" is a name and keeps its case.
LOWER_WORD = re.compile(r"[a-z]+(?![\w-])")


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

    A span opens on a backtick run, or on as long a start of one as a later run on the
    same line matches, and closes on the first later run of exactly that length
    (CommonMark). Each line's runs are indexed by length and the closing run bisected for,
    so a long run is not rescanned once per opening length.
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
            for opener in range(length, 0, -1):
                closers = starts.get(opener, [])
                after = bisect_right(closers, start)
                if after < len(closers):
                    end = closers[after] + opener
                    spans.append((offset + start, offset + end, opener))
                    break
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


def names_gap(clause: str, after: str = "") -> bool:
    """True when ``clause`` says something was not scanned, and no part of it says nothing was.

    ``after`` is the text that followed ``clause`` in its line, read only for a
    negation of a gap word that ends the clause; a URL's path is not a clause.
    """
    clause = BARE_URL.sub(" ", clause)
    # Each gap word starts at a word boundary, so no part divider spans it.
    starts = [0] + [m.end() for m in CLAUSE_PART.finditer(clause)]
    for match in GAP.finditer(clause):
        part = clause[starts[bisect_right(starts, match.start()) - 1] : match.start()]
        if NO_GAP.match(part) or NEGATED.search(clause, max(0, match.start() - NEGATION_REACH), match.start()):
            continue
        if not NONE_AFTER.match(clause[match.end() :] + after):
            return True
    return False


def _held_split(pattern: re.Pattern, text: str) -> list[str]:
    """``pattern.split(text)`` with no split inside a parenthetical.

    A NUL in ``text`` is dropped first: a literal ``\\0N\\0`` would otherwise read
    as a held reference and be swapped for a parenthetical, or index past them.
    """
    text = text.replace("\0", "")
    held: list[str] = []

    def hold(match: re.Match) -> str:
        held.append(match.group(0))
        return HELD.format(len(held) - 1)

    pieces = pattern.split(PARENTHETICAL.sub(hold, text))
    return [HELD_REF.sub(lambda m: held[int(m.group(1))], piece) for piece in pieces]


def _gap_counts(piece: str) -> list[str]:
    """``piece`` as the counts in it that name a gap: "2 new and 1 skipped" is ["1 skipped"]."""
    counts = _held_split(COUNT_JOIN, piece)
    kept = [count for count in counts if names_gap(PARENTHETICAL.sub("", count))]
    if not kept or len(kept) == len(counts):
        return _after_counts(piece)
    return [part for count in kept for part in _after_counts(count)]


def _after_counts(piece: str) -> list[str]:
    """``piece`` from the gap joined to a count before it: "2 new and seeded-c skipped" is ["seeded-c skipped"].

    "seeded-d and seeded-e were skipped" has no count before its join and stays whole, and
    "2 clusters and seeded-c were unreachable" shares one subject and stays whole.
    """
    masked = PARENTHETICAL.sub(lambda match: HELD_MASK * len(match.group(0)), piece)
    for join in itertools.islice(GAP_JOIN.finditer(masked), GAP_JOINS_MAX):
        before = PARENTHETICAL.sub("", piece[: join.start()])
        after = piece[join.end() :]
        if join.group(0).strip().lower() == "and" and SHARED_SUBJECT.match(after):
            continue
        if COUNT.search(before) and not names_gap(before) and names_gap(PARENTHETICAL.sub("", after)):
            return _gap_counts(after)
    return [piece]


def gap_parts(text: str) -> list[str]:
    """Every part of ``text`` that names what was not scanned, as written, in order.

    ``text`` is split at :data:`LINE_PART` outside parentheses. A part naming a
    gap keeps its parentheticals and, after a colon, the names the colon lists
    ("skipped: seeded-d, seeded-e") but not a reason; a count beside it joined
    by "and" is left behind. A part that names none is searched inside its parentheticals.
    """
    pieces = _held_split(LINE_PART, text)
    parts, separators = pieces[::2], pieces[1::2]
    found: list[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        after = PARENTHETICAL.sub("", separators[i] + parts[i + 1]) if i < len(separators) else ""
        if not names_gap(PARENTHETICAL.sub("", part), after):
            for inner in PARENTHETICAL.findall(part):
                found += gap_parts(inner)
            i += 1
            continue
        piece, named = part, False
        while i < len(separators):
            separator, following = separators[i], parts[i + 1]
            opens = separator.strip() == COLON and not named
            if not (opens or named and separator.strip() == COMMA) or not LISTED_NAME.fullmatch(following):
                break
            named = True
            piece += separator + following
            i += 1
        found += [JOINING_WORD.sub("", count.strip()) for count in _gap_counts(piece)]
        i += 1
    return found


def as_line(clause: str) -> str:
    """``clause`` as a sentence of its own: ending in a full stop, capitalized unless it opens on a name."""
    clause = clause.strip()
    if clause[-1:] not in CLAUSE_ENDS:
        clause += CLAUSE_STOP
    return clause[:1].upper() + clause[1:] if LOWER_WORD.match(clause) else clause


def _escape(text: str) -> str:
    for raw, escaped in MRKDWN_ESCAPES:
        text = text.replace(raw, escaped)
    return text


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
    """``(label, url)`` from tuples or ``{"text", "url"}`` mappings; entries without a ``SAFE_URL`` dropped."""
    pairs = []
    for link in links or ():
        if isinstance(link, Mapping):
            label, url = link.get("text") or link.get("label") or "", link.get("url") or ""
        else:
            label, url = link
        if url and SAFE_URL.fullmatch(str(url)):
            pairs.append((str(label or url), str(url)))
    return pairs


def _button(label: str, action_id: str, *, url: str | None = None, value: str | None = None) -> dict:
    button: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": _clip(label, BUTTON_TEXT_MAX), "emoji": True},
        "action_id": action_id,
    }
    if url is not None:
        button["url"] = url
    if value is not None:
        button["value"] = value[:BUTTON_VALUE_MAX]
    return button


def _actions(buttons: Sequence[dict]) -> list[dict]:
    return [
        {"type": "actions", "elements": list(buttons[i : i + BUTTONS_PER_ROW])}
        for i in range(0, len(buttons), BUTTONS_PER_ROW)
    ]


def to_mrkdwn(markdown: str) -> str:
    """Standard markdown to Slack mrkdwn: bold, links and headings; code and urls left alone.

    The input must already be escaped (``&``, ``<``, ``>``), as :func:`_row_line`
    does: this adds brackets but escapes nothing. Only a link to a ``SAFE_URL``
    is then written as ``<url|label>``; any other target, ``@U…``, ``!channel``,
    ``#C…`` or a url Slack would refuse, keeps its label alone.
    """
    spans: list[str] = []

    def keep(text: str) -> str:
        spans.append(text)
        return CODE_HOLD.format(len(spans) - 1)

    def hold(m: re.Match) -> str:
        return keep(m.group(0))

    def link(m: re.Match) -> str:
        return f"<{keep(m.group(2))}|{m.group(1)}>" if SAFE_URL.fullmatch(m.group(2)) else m.group(1)

    def restore(text: str) -> str:
        return CODE_PLACEHOLDER.sub(lambda m: restore(spans[int(m.group(1))]), text)

    lines = []
    in_fence = False
    for line in markdown.replace("\x00", "").split("\n"):
        if line.strip().startswith(CODE_FENCE):
            in_fence = not in_fence
            lines.append(line)
            continue
        if in_fence:
            lines.append(line)
            continue
        held = INLINE_CODE.sub(hold, line)
        heading = HEADING.match(held)
        if heading:
            held = "**" + held[heading.end():].strip() + "**"
        held = MD_LINK.sub(link, held)
        held = BARE_URL.sub(hold, held)
        held = MD_BOLD.sub(lambda m: "*" + next(g for g in m.groups() if g is not None) + "*", held)
        lines.append(restore(held))
    return "\n".join(lines)


def _row_parts(row: Any) -> tuple[str, str]:
    """``(severity, text)`` from a ``{"text", "severity"?}`` mapping or a plain string."""
    if isinstance(row, Mapping):
        text, severity = str(row.get("text") or ""), row.get("severity")
    else:
        text, severity = str(row), None
    return str(severity or "").replace(BACKTICK, "").strip().lower(), text


def _row_detail(row: Any) -> str:
    """A row's optional second line, from a mapping's ``detail``; empty for a plain string."""
    return str(row.get("detail") or "").strip() if isinstance(row, Mapping) else ""


def _shown(rows: Iterable[Any]) -> list[Any]:
    """``rows`` without those that have neither text nor a severity, which would render empty."""
    return [row for row in rows or () if any(part.strip() for part in _row_parts(row))]


def severity_row(severity: str, text: str) -> str:
    """``text`` led by ``severity`` as inline code; the same in markdown and mrkdwn."""
    return SEVERITY_TAG.format(severity=severity, text=text)


def _row_line(row: Any) -> str:
    """A row as one mrkdwn line, so a row can no more mention anyone than a label can.

    Escaping neutralises the brackets already there; ``to_mrkdwn`` then writes
    new ones only for a link to a ``SAFE_URL``.
    """
    severity, text = _row_parts(row)
    line = to_mrkdwn(_escape(_clip(text.strip(), ROW_TEXT_MAX)))
    line = severity_row(_escape(severity), line) if severity else f"{BULLET}{line}"
    detail = _row_detail(row)
    return f"{line}\n{to_mrkdwn(_escape(_clip(detail, ROW_TEXT_MAX)))}" if detail else line


def blocks_answer(
    headline: str,
    links: Iterable[Any] = (),
    choices: Iterable[str] = (),
    action_id_prefix: str = "kage",
) -> list[dict]:
    """Block Kit for a message: headline, link buttons, choice buttons.

    ``links`` are ``(label, url)`` pairs or ``{"text", "url"}`` mappings.
    ``choices`` are labels; each button's ``value`` is its label. Blocks are
    emitted in that order and any part left empty is omitted.
    """
    blocks: list[dict] = []
    title = _clip(_plain(headline or ""), HEADLINE_MAX)
    if title:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{_escape(title)}*"}})
    link_buttons = [
        _button(label, f"{action_id_prefix}.{LINK_ACTION}.{i}", url=url)
        for i, (label, url) in enumerate(_link_pairs(links))
    ]
    blocks.extend(_actions(link_buttons))
    choice_buttons = [
        _button(label, f"{action_id_prefix}.{CHOICE_ACTION}.{i}", value=label)
        for i, label in enumerate(str(c) for c in choices or () if str(c).strip())
    ]
    blocks.extend(_actions(choice_buttons))
    return blocks


def fallback_text(
    headline: str, links: Iterable[Any] = (), choices: Iterable[str] = (), rows: Sequence[Any] = ()
) -> str:
    """The same layout as plain mrkdwn, with no buttons.

    Used as the ``text`` of a blocks message (notifications, screen readers).
    ``headline`` is already plain, as :func:`split_answer` gives it; a second
    plain pass would strip markup a code span had kept. ``rows`` follow the
    headline, one line each, led by their severity as inline code or, with
    none, a bullet, and any ``detail`` on the line under its row, each clipped
    as the rich view clips it; links become inline ``<url|label>``; choices
    become one "Reply with one of:" line. Every label and row is escaped and every url,
    in a row or a link, is a ``SAFE_URL``, so none can mention anyone.
    """
    parts: list[str] = []
    title = _clip((headline or "").strip(), HEADLINE_MAX)
    if title:
        parts.append(f"*{_escape(title)}*")
    parts.extend(_row_line(row) for row in _shown(rows))
    pairs = _link_pairs(links)
    if pairs:
        parts.append(CHOICE_SEPARATOR.join(f"<{url}|{_escape(label)}>" for label, url in pairs))
    labels = [_escape(str(c)) for c in choices or () if str(c).strip()]
    if labels:
        parts.append(CHOICES_LEAD + CHOICE_SEPARATOR.join(labels))
    return "\n".join(parts)


def _rich_elements(markdown: str) -> list[dict]:
    """One line of markdown as rich_text elements: code spans, bold, italic, strike and links kept, the rest text."""
    elements: list[dict] = []
    at = 0
    for span in RICH_SPAN.finditer(markdown):
        if span.start() > at:
            elements.append({"type": "text", "text": markdown[at : span.start()]})
        if span.group("code") is not None:
            elements.append({"type": "text", "text": span.group("code"), "style": {"code": True}})
        elif span.group("bold") is not None or span.group("inbold") is not None:
            elements.append({"type": "text", "text": span.group("bold") or span.group("inbold"), "style": {"bold": True}})
        elif span.group("star") is not None or span.group("under") is not None:
            elements.append({"type": "text", "text": span.group("star") or span.group("under"), "style": {"italic": True}})
        elif span.group("strike") is not None:
            elements.append({"type": "text", "text": span.group("strike"), "style": {"strike": True}})
        elif SAFE_URL.fullmatch(span.group("url")):
            elements.append({"type": "link", "url": span.group("url"), "text": span.group("label")})
        else:
            elements.append({"type": "text", "text": span.group("label")})
        at = span.end()
    if at < len(markdown):
        elements.append({"type": "text", "text": markdown[at:]})
    return elements


def _rich_row(row: Any) -> dict:
    """A report row: its severity as inline code, then its text, with no bullet; any detail below it."""
    severity, text = _row_parts(row)
    elements = _rich_elements(_clip(text.strip(), ROW_TEXT_MAX))
    if severity:
        elements = [{"type": "text", "text": severity, "style": {"code": True}}, {"type": "text", "text": " "}, *elements]
    detail = _row_detail(row)
    if detail:
        elements += [{"type": "text", "text": "\n"}, *_rich_elements(_clip(detail, ROW_TEXT_MAX))]
    return {"type": "rich_text_section", "elements": elements}


def blocks_report(
    headline: str,
    note: str = "",
    rows: Sequence[Any] = (),
    choices: Iterable[Any] = (),
    links: Iterable[Any] = (),
    action_id_prefix: str = "kage",
    detail: str = "",
    after_rows: str = "",
) -> list[dict]:
    """Block Kit for a report: headline, the top rows between dividers, buttons.

    ``headline`` is bold and ``note`` follows it plain, both one line;
    ``detail``, when given, is one more plain line under them, clipped like a
    row, and ``after_rows`` one plain line below the rows' group, clipped the
    same. ``rows``
    are ``{"text": <markdown>, "severity"?: str, "detail"?:
    <markdown>}`` mappings or plain strings, a ``detail`` being a second line
    under its row, clipped like it; ``rows`` sit between two dividers, with no
    header above them. The first choice is the primary button and the links
    follow the choices. A choice is clipped to the button label before it
    becomes the value too, so a click posts only what the button showed.
    Any part left empty, a row with no text and no severity included, is
    omitted.
    """
    rows = _shown(rows)
    blocks: list[dict] = []
    head: list[dict] = []
    title = _clip(_plain(headline or ""), HEADLINE_MAX)
    if title:
        head.append({"type": "text", "text": title, "style": {"bold": True}})
    if note and note.strip():
        head.append({"type": "text", "text": " " + _plain(note)})
    sections = [{"type": "rich_text_section", "elements": head}] if head else []
    detail_line = _clip(_plain(detail or "").strip(), ROW_TEXT_MAX)
    if detail_line:
        sections.append({"type": "rich_text_section", "elements": [{"type": "text", "text": detail_line}]})
    if sections:
        blocks.append({"type": "rich_text", "elements": sections})
    if rows:
        blocks.append({"type": "divider"})
        blocks.append({"type": "rich_text", "elements": [_rich_row(row) for row in rows]})
        blocks.append({"type": "divider"})
    after_line = _clip(_plain(after_rows or "").strip(), ROW_TEXT_MAX)
    if after_line:
        blocks.append({"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": after_line}]}]})
    choice_buttons = [
        _button(label, f"{action_id_prefix}.{CHOICE_ACTION}.{i}", value=label)
        for i, label in enumerate(_choice_labels(choices))
    ]
    if choice_buttons:
        choice_buttons[0]["style"] = PRIMARY_STYLE
    link_buttons = [
        _button(label, f"{action_id_prefix}.{LINK_ACTION}.{i}", url=url)
        for i, (label, url) in enumerate(_link_pairs(links))
    ]
    blocks.extend(_actions(choice_buttons + link_buttons))
    return blocks


def _choice_labels(choices: Iterable[Any]) -> list[str]:
    """Each non-empty choice clipped to a button label."""
    return [_clip(str(choice), BUTTON_TEXT_MAX) for choice in choices or () if str(choice).strip()]


# --- link-button ack -------------------------------------------------------


async def ack_link_click(ack: Any, body: Any = None, action: Any = None) -> None:
    """Acknowledge a link-button click and do nothing else; Slack already opened the url."""
    await ack()
