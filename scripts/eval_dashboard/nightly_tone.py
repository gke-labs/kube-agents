"""The reply-shape line of the 11 AM digest's nightly section: how last
night's answers read, against their own recent past.

The nightly runs record each repetition's reply shape (``rep N shape:``,
measured by ``bench/kube_agents_bench/chat_shape.py`` on every case; SCHEMA.md),
and ``nightly.night_reports`` rolls it up per night. The Nightly page shows
the numbers; this compares them with a baseline, so a model or prompt change
that brings back preambles, apologies or walls of text reaches the space
instead of waiting for someone to open a page::

    🗣 Reply shape: 86% of 512 reps open on the answer ⚠ (typical 92%) · answer p50 62 words (typical 55) · messages p50 2 (typical 2) · 1% apologize (typical 0%)
    🗣 Reply shape, chat and routing: 78% of 108 reps open on the answer ⚠ (typical 94%) · 2% apologize (typical 0%)

Each number's typical is the median of the same number over the last
``nightly_latency.BASELINE_NIGHTS`` earlier whole nights that carry it, with
at least ``nightly_latency.MIN_BASELINE_NIGHTS`` of them, as the latency
line's are. A number gets the ⚠ mark when it moved the wrong way by the
threshold: the share opening on the answer down SHARE_DROP_POINTS or more
over every case (FOCUS_DROP_POINTS on the focused line), the share
apologizing up as much, or the answer or the message count at GROWTH_RATIO
times its typical or more.

All of it is advisory: no verdict, state or gate reads it. Making it gate is
a later change, once the record shows what a clean night looks like.
"""

from __future__ import annotations

import math

try:
    from eval_dashboard import nightly, nightly_latency
except ImportError:  # imported with scripts/eval_dashboard/ on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import nightly, nightly_latency

# Shares compare in whole points, the unit the line prints, so 30% against a
# typical 20% is a rise of ten. Every nightly case is measured, about 500
# repetitions a night (177 cases, three each), so five points is ~25 of them:
# a shift across the suite, not one case.
SHARE_DROP_POINTS = 5
# The chat-and-routing domain holds the cases whose whole point is how a
# reply reads (the chat-voice cases, the delegated-answer ones): ~36 cases,
# ~108 repetitions, where ten points is ~11 of them. A regression confined to
# them moves the whole-suite share by about two points, so they get their
# own line.
FOCUS_DOMAIN = "chat-and-routing"
FOCUS_DROP_POINTS = 10
GROWTH_RATIO = 1.5

VERDICT_SHARE = "verdict_share"
APOLOGY_SHARE = "apology_share"
ANSWER_P50 = "answer_words_p50"
MESSAGES_P50 = "bot_messages_p50"

LINE = "🗣 *Reply shape{label}*: {body}{note}"
WHOLE_LABEL = ""
FOCUS_LABEL = ", chat and routing"
VERDICT_TEXT = "{share} of {reps} open on the answer{mark}{typical}"
REPS = "{count} reps"
ANSWER_TEXT = "answer p50 {words} words{mark}{typical}"
MESSAGES_TEXT = "messages p50 {count}{mark}{typical}"
APOLOGY_TEXT = "{share} apologize{mark}{typical}"
TYPICAL = " (typical {value})"
MARK = " ⚠"
CUT_NOTE = nightly_latency.CUT_NOTE
PARTIAL_NOTE = nightly_latency.PARTIAL_NOTE


def _points(value: float) -> int:
    """Whole percentage points, a half rounded up as the Nightly page's
    ``pct`` (``Math.round``) does, so the line and the tile agree."""
    return math.floor(value * 100 + 0.5)


def _share(value: float) -> str:
    return f"{_points(value)}%"


def _rounded(value: float) -> int:
    """A count rounded half up, as the line prints it."""
    return math.floor(value + 0.5)


def _whole(value: float) -> str:
    return str(_rounded(value))


def _count(value) -> float | None:
    """A finite number at or above zero, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def whole_shape(night: dict) -> dict | None:
    """The night's shape over every case it measured."""
    shape = night.get("shape")
    return shape if isinstance(shape, dict) else None


def focus_shape(night: dict) -> dict | None:
    """The night's shape over its FOCUS_DOMAIN cases alone: the reps, and
    how many opened on the answer and apologized, summed from each case's
    rollup. The medians do not sum, so the focused line carries the shares
    only; None when no such case measured a reply."""
    shapes = [c["shape"] for c in night.get("cases") or []
              if isinstance(c, dict) and c.get("domain") == FOCUS_DOMAIN and isinstance(c.get("shape"), dict)]
    totals = {key: sum(_count(s.get(key)) or 0 for s in shapes) for key in (nightly.SHAPE_REPS, *nightly.SHAPE_FLAGS)}
    return totals if totals[nightly.SHAPE_REPS] else None


def numbers(shape: dict | None) -> dict[str, float | None] | None:
    """The four compared numbers of one shape rollup, or None when it
    measured no reply."""
    reps = _count(shape.get("n")) if isinstance(shape, dict) else None
    if not reps:
        return None
    verdict, apology = _count(shape.get("opens_on_verdict")), _count(shape.get("apology"))
    return {
        VERDICT_SHARE: verdict / reps if verdict is not None else None,
        APOLOGY_SHARE: apology / reps if apology is not None else None,
        ANSWER_P50: _count(shape.get(ANSWER_P50)),
        MESSAGES_P50: _count(shape.get(MESSAGES_P50)),
    }


def baselines(nights: list[dict], shape_of=whole_shape) -> dict[str, float | None]:
    """The typical of each number over ``nights[1:]`` (``nights`` newest
    first), whole nights only."""
    earlier = [numbers(shape_of(n)) for n in reversed(nights[1:]) if nightly_latency.whole(n)]
    earlier = [n for n in earlier if n is not None]
    return {key: nightly_latency.typical([n[key] for n in earlier if n[key] is not None])
            for key in (VERDICT_SHARE, APOLOGY_SHARE, ANSWER_P50, MESSAGES_P50)}


def regressions(nights: list[dict], shape_of=whole_shape, drop_points: int = SHARE_DROP_POINTS) -> list[str]:
    """The numbers of ``nights[0]`` that moved the wrong way by the
    threshold, by key; empty without a night or a typical to compare."""
    now = numbers(shape_of(nights[0])) if nights else None
    if now is None:
        return []
    base = baselines(nights, shape_of)
    out = []
    if now[VERDICT_SHARE] is not None and base[VERDICT_SHARE] is not None and _points(now[VERDICT_SHARE]) <= _points(base[VERDICT_SHARE]) - drop_points:
        out.append(VERDICT_SHARE)
    if now[APOLOGY_SHARE] is not None and base[APOLOGY_SHARE] is not None and _points(now[APOLOGY_SHARE]) >= _points(base[APOLOGY_SHARE]) + drop_points:
        out.append(APOLOGY_SHARE)
    for key in (ANSWER_P50, MESSAGES_P50):
        # Against the typical as the line prints it, so a mark never
        # disagrees with the two numbers the reader is shown.
        typical = _rounded(base[key]) if base[key] is not None else None
        if now[key] is not None and typical and now[key] >= GROWTH_RATIO * typical:
            out.append(key)
    return out


def _line(nights: list[dict], label: str, shape_of, drop_points: int) -> str | None:
    # One night per date, the newest run of each, as the latency line counts
    # them (nightly.one_per_date): a same-date re-run is not a second
    # earlier night in the typicals.
    nights = nightly.one_per_date(nights)
    now = numbers(shape_of(nights[0])) if nights else None
    if now is None:
        return None
    night = nights[0]
    base = baselines(nights, shape_of)
    flagged = set(regressions(nights, shape_of, drop_points))

    def mark(key: str) -> str:
        return MARK if key in flagged else ""

    def typical(key: str, text) -> str:
        return TYPICAL.format(value=text(base[key])) if base[key] is not None else ""

    parts = []
    if now[VERDICT_SHARE] is not None:
        parts.append(VERDICT_TEXT.format(share=_share(now[VERDICT_SHARE]), reps=REPS.format(count=int(shape_of(night)["n"])),
                                         mark=mark(VERDICT_SHARE), typical=typical(VERDICT_SHARE, _share)))
    if now[ANSWER_P50] is not None:
        parts.append(ANSWER_TEXT.format(words=_whole(now[ANSWER_P50]), mark=mark(ANSWER_P50), typical=typical(ANSWER_P50, _whole)))
    if now[MESSAGES_P50] is not None:
        parts.append(MESSAGES_TEXT.format(count=_whole(now[MESSAGES_P50]), mark=mark(MESSAGES_P50), typical=typical(MESSAGES_P50, _whole)))
    if now[APOLOGY_SHARE] is not None:
        parts.append(APOLOGY_TEXT.format(share=_share(now[APOLOGY_SHARE]), mark=mark(APOLOGY_SHARE), typical=typical(APOLOGY_SHARE, _share)))
    note = CUT_NOTE if night.get("truncated") else "" if nightly_latency.whole(night) else PARTIAL_NOTE
    return LINE.format(label=label, body=nightly.SEP.join(parts), note=note)


def night_line(nights: list[dict]) -> str | None:
    """The reply-shape line over every case for ``nights[0]`` (``nights``
    newest first), or None when last night measured no reply. A number the
    night did not measure is left out."""
    return _line(nights, WHOLE_LABEL, whole_shape, SHARE_DROP_POINTS)


def focus_line(nights: list[dict]) -> str | None:
    """The same for the FOCUS_DOMAIN cases alone, shares only, marked at
    FOCUS_DROP_POINTS; None when none of them measured a reply."""
    return _line(nights, FOCUS_LABEL, focus_shape, FOCUS_DROP_POINTS)
