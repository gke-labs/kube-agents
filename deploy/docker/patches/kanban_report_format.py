"""One home for what a card's ``result`` is supposed to look like.

Installed into the image at ``/opt/hermes/tools/kanban_report_format.py``. Two
callers wire it in:

``apply_kanban_report_format.py``
    appends :data:`REPORT_FORMAT_STANZA` to a new card's ``body`` at
    ``kanban_create``, so the worker is told the shape before it starts.

``gateway/kanban_notifier.py``
    reads :func:`result_shape_defects` and :func:`serious_defects` to record the
    shape of a report as it is delivered — a WARNING naming the edit to make for
    the two defects that always render wrongly, INFO for the two that are
    matters of taste. It imports this module lazily and inside a blanket
    ``except``, so a mistake here can never wedge the delivery path.

Nothing refuses a completion over shape. ``kanban_result_required.py`` briefly
did, and removing it is why :func:`serious_defects` is named for severity rather
than for a gate; that module's docstring records the measurement that settled it.
The predicate lives here rather than in its callers because the stanza, the
schema wording and the delivery log must not answer "is this report well-shaped?"
three different ways.

Why
---
On 2026-08-08 a fan-out of four near-identical sleep cards (``t_4867e94b`` and
its children) produced four different report formats. All four wrote valid
standard Markdown — the persona contract added in the previous commit worked, and
the Block Kit renderer rendered every one of them without error. They still did
not look alike, because none of the card bodies said anything about *shape* and
the persona only says "use standard Markdown", not how much structure:

``t_c781d6b0`` (read as fine)
    312 chars. One ``###`` heading, a lead sentence, three bullets, every epoch
    value in backticks.

``t_88cdceb1`` (read as wrong)
    240 chars. An ``#`` H1 *and* a ``###``, so Slack drew two banner ``header``
    blocks over four lines of content — and the H1 restates the card title the
    notifier has already put in the message. Raw floats, no backticks.

``t_c60439af`` (read as wrong)
    189 chars. A ``###`` immediately followed by three bullets, no prose at all:
    a banner over a bare list. Raw floats, no backticks.

The difference between the one that read as fine and the two that did not is
small, mechanical, and entirely expressible as a rule — which is what this module
is. Prose in a persona competes with the immediate task text and loses; the
stanza below travels *in* the task text.

What is serious and what is only taste
--------------------------------------
:func:`result_shape_defects` reports four defects. Only the two in
:data:`SERIOUS_DEFECTS` are worth a WARNING, because a log line that argues
about taste trains its reader to ignore it:

``top-level-heading`` (serious — WARNING)
    An ``#`` H1. Always wrong here and mechanically fixable — the chat message
    already carries the card title as its heading, so an H1 is a duplicate
    banner. Demoting it to ``##`` is a one-character edit. Fenced code is
    exempt; an *unfenced* manifest or diff is not, and its leading ``#``
    comment reads as an H1. That report is defective anyway — unfenced, it
    renders as a wall of text — so it still warns, and the advice names the
    fence as the edit, because demoting a YAML comment marker would break it.

``ascii-substitute`` (serious — WARNING)
    ``=== Title ===`` or an ALL-CAPS numbered section, with no real Markdown
    anywhere. The report meant to have structure and expressed it in the one
    form Slack cannot see, so it renders flat.

``heading-without-prose`` (taste — INFO)
    A heading whose section is nothing but list items. Usually ugly, sometimes
    correct — a card that genuinely only has three values to report is not
    improved by a manufactured sentence.

``unquoted-numerics`` (taste — INFO)
    Bare high-precision numbers outside backticks. The most reliable signal
    separating the good card from the bad ones above, and still not worth
    waking anybody over: it is cosmetic, and a false positive on a number that
    was meant to be prose would be maddening.
"""

from __future__ import annotations

import re

__all__ = [
    "REPORT_FORMAT_STANZA",
    "FORMAT_MARKER",
    "has_format_directive",
    "with_report_format",
    "result_shape_defects",
    "serious_defects",
    "SERIOUS_DEFECTS",
    "DEFECT_ADVICE",
]

#: Substring that marks a body as already carrying the stanza. Matched instead of
#: the whole block so a caller who pasted the stanza and then edited a bullet
#: still counts as having one — the point is not to append a second copy.
FORMAT_MARKER = "## Report format"

#: Appended to a new card's ``body`` when it says nothing about report shape.
#: Written as instructions to the worker, in the second person, because that is
#: what the rest of a card body is and the model reads the whole thing as one
#: brief. Every rule here is one the detector below can actually measure, so the
#: card, the schema wording and the delivery log all describe the same contract.
REPORT_FORMAT_STANZA = """\
## Report format

Put the full answer in `result` as standard Markdown — the gateway posts it
verbatim into the requester's chat thread, where Slack renders it as blocks:

- Open with one sentence saying what happened, then give the detail.
- Use `##` and `###` for sections. Never `#` — the chat message already shows
  the card title, so an H1 renders as a second, duplicate banner.
- Put tabular data in a Markdown pipe table with a `---` separator row.
- Wrap raw values — ids, paths, epochs, durations, counts — in backticks.
- Do not use `=== Title ===`, `1. SECTION`, or hand-aligned columns. Slack
  renders those as flat text.\
"""

#: Phrases that mean the caller has already said something about shape. A body
#: carrying any of these is left alone: an explicit instruction from the
#: orchestrator outranks a generic stanza, and stapling ours underneath it would
#: give the worker two briefs to reconcile.
_EXISTING_DIRECTIVE = re.compile(
    r"\b(?:report|output|response) format\b"
    r"|\bformat (?:your |the )?(?:result|report|answer|output|response)\b",
    re.IGNORECASE,
)

#: An ATX heading of any level.
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6}) +(\S.*)$", re.MULTILINE)

#: An H1 specifically.
_H1 = re.compile(r"^ {0,3}# +\S", re.MULTILINE)

#: Block-level Markdown Block Kit turns into structure. Kept in step with
#: ``gateway/kanban_notifier.py``'s copy — see that module's note on why the
#: notifier holds its own.
_BLOCK_MARKDOWN = re.compile(
    r"^ {0,3}(?:#{1,6} +\S|\|.*\||(?:-{3,}|\*{3,}|_{3,}) *$|```)",
    re.MULTILINE,
)

#: ``=== Title ===`` or an ALL-CAPS numbered section — structure Slack cannot see.
_ASCII_STRUCTURE = re.compile(
    r"^ *(?:={2,}[^=\n]+={2,} *|\d+[.)] +[A-Z][A-Z0-9 _/&'\"-]{3,}) *$",
    re.MULTILINE,
)

#: A list item.
_LIST_ITEM = re.compile(r"^ *(?:[-*+] |\d+[.)] )")

#: A fenced code block, removed before the numeric scan so a code sample full of
#: floats is not read as unquoted prose.
_FENCE = re.compile(r"```.*?```", re.DOTALL)

#: Inline code, removed for the same reason — a backticked value is exactly what
#: we are asking for and must not be counted against the report.
_INLINE_CODE = re.compile(r"`[^`\n]*`")

#: A high-precision bare number: six or more significant digits, which is a raw
#: epoch, duration or id rather than a quantity a human wrote on purpose. "3
#: pods" and "99.9%" are left alone deliberately.
_LONG_NUMBER = re.compile(r"(?<![\w.`])\d[\d,]*\.?\d*(?![\w`])")

#: How many bare long numbers before it counts as a defect. Two, not one, so a
#: single figure quoted in a sentence does not trip it.
UNQUOTED_NUMERIC_MIN = 2

#: Significant digits that make a number "raw" rather than written-by-hand.
LONG_NUMBER_MIN_DIGITS = 6

#: Below this a result is a one-liner and has no shape to get wrong. The old
#: floor here was 600, which is why the two cards that prompted this module — 240
#: and 189 characters — could never be measured at all.
SHAPE_MIN_CHARS = 150

#: The defects worth a WARNING rather than an INFO. See the module docstring.
SERIOUS_DEFECTS = ("top-level-heading", "ascii-substitute")

#: What to tell a worker about each defect. Phrased as the edit to make, not as a
#: complaint, because this text is handed straight back to a model that has to
#: act on it.
DEFECT_ADVICE = {
    "top-level-heading": (
        "`result` starts a line with `#`. If that is a heading, use `##` or "
        "`###` instead — the chat message already carries this card's title, so "
        "an H1 renders as a second banner saying the same thing. If it is a "
        "comment in a manifest, diff or script, put that block in a ``` fence: "
        "unfenced, it renders as a wall of text and its first comment reads as "
        "a heading. Do not delete the `#` in that case."
    ),
    "ascii-substitute": (
        "`result` marks its sections with `=== Title ===` or ALL-CAPS numbering "
        "and has no real Markdown. Slack renders that as flat text. Use `##` "
        "headings, and a pipe table with a `---` separator row for tabular data."
    ),
    "heading-without-prose": (
        "`result` is a heading over a bare list. Open with one sentence saying "
        "what happened, then the list."
    ),
    "unquoted-numerics": (
        "`result` has raw values outside backticks. Wrap ids, paths, epochs, "
        "durations and counts in backticks."
    ),
}


def has_format_directive(body: object) -> bool:
    """Whether ``body`` already tells the worker how to shape its report."""
    if body is None:
        return False
    text = str(body)
    if FORMAT_MARKER in text:
        return True
    return bool(_EXISTING_DIRECTIVE.search(text))


def with_report_format(body: object) -> object:
    """Return ``body`` with the report-format stanza appended, if it needs one.

    Idempotent, and a no-op for a caller who already gave format instructions —
    see :data:`_EXISTING_DIRECTIVE`. A card created with no body at all gets the
    stanza as its whole body: an empty brief is the case where the worker has the
    least to go on and the default shape matters most.

    ``body`` is returned unchanged, and with its own type, whenever there is
    nothing to add, so a ``None`` body stays ``None`` for callers downstream that
    distinguish it from ``""``.
    """
    if body is not None and not str(body).strip():
        # Whitespace-only. Treat as absent, but keep returning a string so the
        # handler's own `body` semantics do not change shape underneath it.
        return REPORT_FORMAT_STANZA
    if body is None:
        return REPORT_FORMAT_STANZA
    text = str(body)
    if has_format_directive(text):
        return body
    return text.rstrip() + "\n\n" + REPORT_FORMAT_STANZA


def _strip_code(body: str) -> str:
    """Remove fenced and inline code so their contents are not scanned."""
    return _INLINE_CODE.sub("", _FENCE.sub("", body))


def _has_unquoted_numerics(body: str) -> bool:
    """Whether ``body`` shows raw high-precision numbers outside code."""
    bare = _strip_code(body)
    hits = 0
    for match in _LONG_NUMBER.finditer(bare):
        digits = sum(ch.isdigit() for ch in match.group())
        if digits >= LONG_NUMBER_MIN_DIGITS:
            hits += 1
            if hits >= UNQUOTED_NUMERIC_MIN:
                return True
    return False


def _has_heading_without_prose(body: str) -> bool:
    """Whether every heading in ``body`` is followed only by list items.

    True when the report carries at least one heading and not one line of
    ordinary prose anywhere outside a heading, a list item or a code block.
    """
    if not _ATX_HEADING.search(body):
        return False
    in_fence = False
    for line in _strip_code(body).splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if _ATX_HEADING.match(line) or _LIST_ITEM.match(line):
            continue
        if set(stripped) <= set("-*_| "):
            # A divider or a table rule, not a sentence.
            continue
        return False
    return True


def result_shape_defects(
    result: object,
    min_chars: int = SHAPE_MIN_CHARS,
) -> tuple[str, ...]:
    """Name every shape defect in ``result``, most serious first.

    Returns an empty tuple for a result that is fine, too short to have a shape,
    or absent. Pure and total: it never raises, because both callers are on a
    path where an exception would cost more than a missed warning.
    """
    if result is None:
        return ()
    try:
        body = str(result).strip()
    except Exception:  # pragma: no cover - defensive
        return ()
    if len(body) < min_chars:
        return ()

    defects: list[str] = []
    # Fenced code is removed first: a shell comment inside a code block is not a
    # heading, and this defect is the serious tier, so reading one as an H1 would
    # warn about a well-shaped report for an edit the worker cannot make. Inline
    # code stays — an H1 has to open a line, so it can never hide inside a
    # backtick pair, and removing one would let ``\`--dry-run\` # note`` collapse
    # onto its hash and read as a heading nobody wrote.
    #
    # An *unfenced* manifest or diff still trips this: ``# Managed by
    # kube-agents`` at column 0 is a YAML comment, not a heading. That report is
    # defective either way — unfenced YAML renders as a wall of text in Slack —
    # so it still warns, and ``DEFECT_ADVICE`` names the fence as the other edit
    # rather than sending the worker off to demote a comment marker.
    if _H1.search(_FENCE.sub("", body)):
        defects.append("top-level-heading")
    if not _BLOCK_MARKDOWN.search(body) and _ASCII_STRUCTURE.search(body):
        defects.append("ascii-substitute")
    if _has_heading_without_prose(body):
        defects.append("heading-without-prose")
    if _has_unquoted_numerics(body):
        defects.append("unquoted-numerics")
    return tuple(defects)


def serious_defects(
    result: object, min_chars: int = SHAPE_MIN_CHARS
) -> tuple[str, ...]:
    """The subset of :func:`result_shape_defects` worth a WARNING."""
    found = result_shape_defects(result, min_chars=min_chars)
    return tuple(d for d in found if d in SERIOUS_DEFECTS)
