"""Terminal table primitives: a box table, a palette, OSC 8 links, width fitting.

Domain-free on purpose. Two terminal tables in one repository that disagree
about how to measure a coloured cell is two bugs, and a renderer that knows
what a finding or an audit stream is cannot be shared. Everything here takes
strings and `Column` specs and returns lines.

Measurement is the part that is easy to get wrong. `display_width()` is the
only correct way to ask how wide a cell is, and it corrects for two things a
raw `len()` gets wrong in opposite directions: SGR colour codes and OSC 8
hyperlinks occupy zero columns while counting as many characters, and an
emoji or a CJK character occupies two columns while counting as one. Callers
are expected to have scrubbed control characters out of untrusted text before
it reaches a cell -- `scrub()` in `fleet_audit_status_view` is where this
repository does it -- because an escape sequence arriving from a model-written
field would measure as zero columns here and defeat the very assertions that
exist to catch misalignment.
"""

from __future__ import annotations

import collections
import datetime as _dt
import os
import re
import sys
import textwrap
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

# The narrowest a wrapping column shrinks unless its spec says otherwise.
DEFAULT_MIN_WIDTH = 12
# The least content width a table is ever fitted to, however narrow the
# terminal: below it every wrapping column is one or two characters wide.
MIN_CONTENT_WIDTH = 10
# Unicode categories a terminal draws in no column: nonspacing and enclosing
# marks, and format characters such as the zero-width joiner and space.
ZERO_WIDTH_CATEGORIES = ("Mn", "Me", "Cf")
# The one format character terminals draw anyway: the soft hyphen, as a hyphen.
SOFT_HYPHEN = "\u00ad"
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400
HOURS_PER_DAY = 24
_PR_URL = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$")


_ANSI = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\")

RESET = "\033[0m"

STYLES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "crit": "\033[1;31m",
    "head": "\033[1;4m",
}

_LINKABLE_URL = re.compile(r"https?://", re.IGNORECASE)

BOX_UNICODE = {
    "h": "─", "v": "│",
    "tl": "┌", "tm": "┬", "tr": "┐",
    "ml": "├", "mm": "┼", "mr": "┤",
    "bl": "└", "bm": "┴", "br": "┘",
}

BOX_ASCII = {
    "h": "-", "v": "|",
    "tl": "+", "tm": "+", "tr": "+",
    "ml": "+", "mm": "+", "mr": "+",
    "bl": "+", "bm": "+", "br": "+",
}

class Palette:
    """Applies or discards styles, so no renderer has to know which."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, style: Optional[str]) -> str:
        if not self.enabled or not style or not text:
            return text
        code = STYLES.get(style)
        return "%s%s%s" % (code, text, RESET) if code else text

def want_colour(choice: str, stream=None) -> bool:
    """`--color` plus the two conventions a terminal tool is expected to honour.

    NO_COLOR is checked after the explicit flag, because a flag typed on the
    command line is a stronger statement than a variable inherited from a shell
    profile, and before the TTY test, because its whole point is that a user
    who sets it means it on an interactive terminal too.
    """
    if choice == "always":
        return True
    if choice == "never":
        return False
    # no-color.org: set *and non-empty*, as the installer's shell gate reads it.
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    stream = stream if stream is not None else sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)())

def plain(text: str) -> str:
    """Colour-off view of a string: the two escape forms this file emits, gone.

    This strips those two and nothing else, which is only safe because
    `scrub()` in `fleet_audit_status_view` has already taken the control
    characters out of everything else. An escape sequence that reached a cell
    from the ledger would measure here as zero columns wide, so the table would
    both misalign and pass the assertions that exist to catch misalignment.

    Use `display_width` to measure. `len(plain(...))` is a character count, and
    the characters most likely to arrive from a model-written finding or a
    GitHub pull-request title are exactly the ones where a character is not a
    column.
    """
    return _ANSI.sub("", text)

def display_width(text: str) -> int:
    """Terminal columns `text` occupies once colour is stripped.

    `len()` is wrong here in both directions, and each direction was reachable:
    an emoji in a pull-request title or a CJK cluster name counts one character
    and draws two columns, and a combining accent counts one and draws none. A
    single one of either shifted every border below its row -- and the test
    that exists to catch that measured with `len(plain(...))` too, so it agreed
    with the renderer and reported the table aligned. Zero-width is decided by
    category, not by combining class, in both directions: the zero-width
    joiner, a variation selector and many vowel signs have combining class 0
    and still draw nothing, and a spacing mark such as a Javanese virama or a
    Hangul tone mark has a non-zero class and still draws.
    """
    return sum(
        0
        if ch != SOFT_HYPHEN
        and unicodedata.category(ch) in ZERO_WIDTH_CATEGORIES
        else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        for ch in plain(text)
    )

def pr_ref(url: str) -> str:
    """`owner/repo#123` for a GitHub pull-request URL, else the URL unchanged.

    A 50-character URL in a table column wraps, and a wrapped URL is no longer
    one a terminal will make clickable or a reader will copy in one go. The
    short form is half the width, and `hyperlink` restores the full link on any
    terminal that supports OSC 8.
    """
    match = _PR_URL.match((url or "").strip())
    return "%s/%s#%s" % match.groups() if match else url

def hyperlink(text: str, url: str, palette: Palette, link_id: str = "") -> str:
    """OSC 8, gated on the same signal as colour.

    Terminals that do not implement it ignore the sequence, but a pipe or a
    file keeps the bytes, so this follows `--color`: that flag already means
    "a human is looking at this in a terminal".

    `link_id` is OSC 8's `id=` parameter, which exists to say that two
    separately-emitted runs are one hyperlink. Anything that wraps across table
    rows needs it: without it a terminal treats each row as its own link and
    highlights only the line under the pointer, and with it the whole text
    lights up as one.
    """
    if not palette.enabled or not url or not _LINKABLE_URL.match(url):
        return text
    return "\x1b]8;%s;%s\x1b\\%s\x1b]8;;\x1b\\" % ("id=%s" % link_id if link_id else "", url, text)

class Column:
    """One column.

    `wrap` marks a column that gives up width first, down to `min_width`.
    `expendable` is the next concession after that: a positive value means the
    column may be dropped entirely on a terminal too narrow to hold the table
    even at its minimums, highest value first. Zero -- the default -- means the
    column is load-bearing and the table runs wide instead.
    """

    def __init__(
        self,
        title: str,
        align: str = "l",
        wrap: bool = False,
        min_width: int = DEFAULT_MIN_WIDTH,
        expendable: int = 0,
    ) -> None:
        self.title = title
        self.align = align
        self.wrap = wrap
        self.min_width = min_width
        self.expendable = expendable

def _pad(text: str, width: int, align: str) -> str:
    gap = max(0, width - display_width(text))
    if align == "r":
        return " " * gap + text
    if align == "c":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap

def _cell_lines(text: str, width: int) -> List[Tuple[str, int]]:
    """Wrap one cell to `width`, as `(line, source paragraph index)` pairs.

    Deliberate newlines are preserved, and the paragraph index rides along so a
    cell that stacks several facts -- a title over a location, say -- can
    colour each one differently even after wrapping has turned
    them into an indeterminate number of lines.

    `break_long_words` is on because the cells most likely to overflow are file
    paths and fingerprints, which have no spaces to break at -- left unbroken
    they push the column past its allotment and every border below misaligns.
    """
    out: List[Tuple[str, int]] = []
    for index, para in enumerate((text or "").split("\n")):
        if not para:
            out.append(("", index))
            continue
        for line in (
            # `textwrap` counts characters and `width` is columns: widen it by
            # the zero-width characters so a paragraph that fits is not split.
            textwrap.wrap(
                para,
                width=max(1, width) + max(0, len(plain(para)) - display_width(para)),
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [""]
        ):
            for piece in _to_width(line, max(1, width)):
                out.append((piece, index))
    return out or [("", 0)]

def _to_width(line: str, width: int) -> List[str]:
    """Break a line `textwrap` left too wide, measuring in columns not characters.

    `textwrap` counts characters, so a paragraph of double-width text comes back
    at or under `width` characters and up to twice `width` columns -- which is
    the one overflow `break_long_words` cannot catch, because as far as it is
    concerned the line already fits. Only a line that actually overruns is
    touched, so an ASCII cell takes the identical path it always has.
    """
    if display_width(line) <= width:
        return [line]
    out: List[str] = []
    current: List[str] = []
    used = 0
    for ch in line:
        step = display_width(ch)
        if used + step > width and current:
            out.append("".join(current))
            current, used = [], 0
        current.append(ch)
        used += step
    if current:
        out.append("".join(current))
    return out

def _natural_widths(columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]]) -> List[int]:
    """The width each column would take if nothing had to give."""
    natural = []
    for index, column in enumerate(columns):
        widest = display_width(column.title)
        for row in rows:
            text = row[index][0] if index < len(row) else ""
            for line in str(text).split("\n"):
                widest = max(widest, display_width(line))
        natural.append(widest)
    return natural

def _overhead(count: int) -> int:
    """Borders and padding: `│ ` before each cell, a space after each, and the
    closing `│`, so three columns a cell and one more."""
    return 3 * count + 1

def _minimum_width(columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]]) -> int:
    """The narrowest this table can be drawn without dropping a column."""
    natural = _natural_widths(columns, rows)
    return _overhead(len(columns)) + sum(
        min(column.min_width, natural[index]) if column.wrap else natural[index]
        for index, column in enumerate(columns)
    )

def _fit_columns(
    columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]], total: int
) -> Tuple[List[Column], List[List[Sequence[Any]]], List[str]]:
    """Drop expendable columns until the table fits, worst-value first.

    An eighty-column terminal cannot hold every wide table: each column costs
    three characters of border before its content, and a table left to run
    wide is hard-wrapped by the terminal into something less readable than the
    JSON it renders. So the least load-bearing columns come out first, and the
    caller is told which -- a table that silently drops a column is a table
    that lies about what its source holds. When no amount of dropping would
    fit, nothing is dropped.
    """
    kept = list(columns)
    trimmed = [list(row) for row in rows]
    dropped: List[str] = []
    while _minimum_width(kept, trimmed) > total:
        candidates = [i for i, column in enumerate(kept) if column.expendable > 0]
        if not candidates:
            break
        victim = max(candidates, key=lambda i: (kept[i].expendable, i))
        dropped.append(kept[victim].title)
        kept.pop(victim)
        for row in trimmed:
            if victim < len(row):
                row.pop(victim)
    if dropped and _minimum_width(kept, trimmed) > total:
        # Dropping every candidate still leaves it too wide: the terminal
        # wraps it either way, so keep what the columns said and claim no fit.
        return list(columns), [list(row) for row in rows], []
    return kept, trimmed, dropped

def _resolve_widths(columns: Sequence[Column], rows: Sequence[Sequence[Sequence[Any]]], total: int) -> List[int]:
    natural = _natural_widths(columns, rows)

    overhead = _overhead(len(columns))
    available = max(total - overhead, MIN_CONTENT_WIDTH)
    if sum(natural) <= available:
        return natural

    flex = [i for i, c in enumerate(columns) if c.wrap]
    if not flex:
        return natural

    fixed = sum(w for i, w in enumerate(natural) if i not in flex)
    room = available - fixed
    # A column whose content is narrower than its minimum needs only its
    # content: padding it up would take the width from a column that wraps.
    minimum = {i: min(columns[i].min_width, natural[i]) for i in flex}
    floor = sum(minimum.values())
    if room < floor:
        # Nothing left to give. Honour the minimums and let the table run wide:
        # a table one column too wide is legible, a table with three-character
        # title cells is not.
        return [minimum[i] if i in flex else natural[i] for i in range(len(columns))]

    share = float(sum(natural[i] for i in flex)) or 1.0
    widths = list(natural)
    for i in flex:
        widths[i] = max(minimum[i], int(room * (natural[i] / share)))
    # Integer division loses a column or two of the budget; hand the remainder
    # out a column at a time, widest first, rather than leaving the table
    # short. Never past a column's natural width: the columns share the
    # truncation, so a lump on one would push it wider than its content.
    drift = room - sum(widths[i] for i in flex)
    while drift > 0:
        short = [i for i in flex if widths[i] < natural[i]]
        if not short:
            break
        widths[max(short, key=lambda i: widths[i])] += 1
        drift -= 1
    # Clamping a narrow column up to its minimum overspends the budget; take
    # the excess back from the columns above theirs, widest first. `room >=
    # floor` above guarantees there is enough slack to cover it.
    while drift < 0:
        donor = max(flex, key=lambda i: widths[i] - minimum[i])
        give = min(-drift, widths[donor] - minimum[donor])
        widths[donor] -= give
        drift += give
    return widths

def render_table(
    columns: Sequence[Column],
    rows: Sequence[Sequence[Sequence[Any]]],
    palette: Palette,
    width: int,
    box: Dict[str, str],
    separator: str = "none",
    wider_hint: str = "",
) -> List[str]:
    """Render `rows` into a bordered table.

    A cell is a tuple of up to five parts: the text, a style for all of it, a
    URL to hyperlink it with, a `{paragraph index: style}` override for a cell
    whose newline-separated parts want colouring individually, and a
    `{paragraph index: URL}` for one that wants them linked individually.

    The two URL forms differ in what has to be unwrapped for the link to be
    drawn -- the whole cell for the plain one, only the paragraph itself for the
    per-paragraph one. A cell stacking a title over a location has no single-line
    form to reach, so a whole-cell URL on it would never render at all.

    `separator` puts a `blank` line or a `rule` between rows, for a table whose
    rows are several lines tall: a short cell leaves the lines under it blank,
    so without one there is nothing to say where one record stops and the next
    starts. A table of one-line rows wants `none`, which is the default.

    `wider_hint` finishes the note under a table that dropped columns to fit:
    how the caller's own interface asks for more width, which this module
    cannot know.
    """
    columns, rows, dropped = _fit_columns(columns, rows, width)
    widths = _resolve_widths(columns, rows, width)

    def rule(left: str, mid: str, right: str) -> str:
        return palette(left + mid.join(box["h"] * (w + 2) for w in widths) + right, "dim")

    vertical = palette(box["v"], "dim")

    # Distinguishes one wrapped link from another, so that two links in the
    # same table are never fused into one hyperlink by a shared `id=`.
    link_seq = [0]

    def emit(cells: Sequence[Sequence[Any]]) -> List[str]:
        wrapped = [
            _cell_lines(str(cells[i][0]) if i < len(cells) else "", widths[i])
            for i in range(len(columns))
        ]
        height = max(len(w) for w in wrapped)
        # How many lines each paragraph of each cell ended up occupying, which
        # is what decides whether a per-paragraph link needs an `id=` to hold
        # its pieces together.
        spans = [collections.Counter(para for _, para in w) for w in wrapped]
        link_seq[0] += 1
        row_seq = link_seq[0]
        lines = []
        for line_no in range(height):
            pieces = []
            for i, column in enumerate(columns):
                raw, para = wrapped[i][line_no] if line_no < len(wrapped[i]) else ("", -1)
                cell = cells[i] if i < len(cells) else ("",)
                style = cell[1] if len(cell) > 1 else None
                url = cell[2] if len(cell) > 2 else None
                per_line = cell[3] if len(cell) > 3 else None
                per_line_url = cell[4] if len(cell) > 4 else None
                if per_line and para in per_line:
                    style = per_line[para]
                # A whole-cell URL is drawn only on an unwrapped cell, because
                # it has no way to say which of several paragraphs it belongs
                # to. A blank line is never linked either -- the padding
                # beneath a short cell, which a taller neighbouring column
                # produces on nearly every row, would otherwise carry a
                # zero-width link with nothing for a reader to click.
                linkable = bool(url) and len(wrapped[i]) == 1
                link_id = ""
                if per_line_url and para in per_line_url:
                    url = per_line_url[para]
                    # A per-paragraph link is drawn even when its paragraph
                    # wraps, joined across the rows by `id=`. Dropping it
                    # instead would lose every link too long for its column,
                    # which at an 80-column terminal is most of them.
                    linkable = True
                    if spans[i][para] > 1:
                        link_id = "%d.%d.%d" % (row_seq, i, para)
                rendered = palette(raw, style)
                if url and raw.strip() and linkable:
                    rendered = hyperlink(rendered, url, palette, link_id)
                pieces.append(_pad(rendered, widths[i], column.align))
            lines.append(vertical + " " + (" " + vertical + " ").join(pieces) + " " + vertical)
        return lines

    # Built from the resolved widths rather than by blanking a rule, so that a
    # `--color` run's dim escapes around the borders survive into it.
    spacer = vertical + " " + (" " + vertical + " ").join(" " * w for w in widths) + " " + vertical

    out = [rule(box["tl"], box["tm"], box["tr"])]
    out.extend(emit([(c.title, "head") for c in columns]))
    out.append(rule(box["ml"], box["mm"], box["mr"]))
    for index, row in enumerate(rows):
        if index and separator == "rule":
            out.append(rule(box["ml"], box["mm"], box["mr"]))
        elif index and separator == "blank":
            out.append(spacer)
        out.extend(emit(row))
    out.append(rule(box["bl"], box["bm"], box["br"]))
    if dropped:
        note = "  %s dropped to fit %d columns" % (", ".join(dropped), width)
        out.append(palette(note + ("; " + wider_hint if wider_hint else ""), "dim"))
    return out

def humanise_delta(seconds: float) -> str:
    seconds = abs(int(seconds))
    if seconds < SECONDS_PER_MINUTE:
        return "%ds" % seconds
    if seconds < SECONDS_PER_HOUR:
        return "%dm" % (seconds // SECONDS_PER_MINUTE)
    if seconds < SECONDS_PER_DAY:
        hours, minutes = divmod(seconds // SECONDS_PER_MINUTE, MINUTES_PER_HOUR)
        return "%dh%02dm" % (hours, minutes) if minutes else "%dh" % hours
    days, hours = divmod(seconds // SECONDS_PER_HOUR, HOURS_PER_DAY)
    return "%dd%dh" % (days, hours) if hours else "%dd" % days

def ago(when: Optional[_dt.datetime], now: _dt.datetime) -> str:
    if when is None:
        return "never"
    delta = (now - when).total_seconds()
    return "in %s" % humanise_delta(delta) if delta < 0 else "%s ago" % humanise_delta(delta)
