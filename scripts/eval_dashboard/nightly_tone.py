"""The reply-shape line of the 11 AM digest's nightly section: how last
night's answers read, against their own recent past.

The nightly runs record each repetition's reply shape (``rep N shape:``,
measured by ``bench/kube_agents_bench/chat_shape.py`` on every case; SCHEMA.md),
and ``nightly.night_reports`` rolls it up per night. The Nightly page shows
the numbers; this compares them with a baseline, so a model or prompt change
that brings back preambles, apologies or walls of text reaches the space
instead of waiting for someone to open a page::

    🗣 Reply shape: 78% of 40 reps open on the answer ⚠ (typical 92%) · answer p50 62 words (typical 55) · messages p50 2 (typical 2) · 5% apologize (typical 0%)

Each number's typical is the median of the same number over the last
``nightly_latency.BASELINE_NIGHTS`` earlier whole nights that carry it, with
at least ``nightly_latency.MIN_BASELINE_NIGHTS`` of them, as the latency
line's are. A number gets the ⚠ mark when it moved the wrong way by the
threshold: the share opening on the answer down SHARE_DROP_POINTS or more,
the share apologizing up as much, or the answer or the message count at
GROWTH_RATIO times its typical or more.

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

# A share move of ten points is about four of the forty-odd repetitions the
# chat-voice and delegated cases give a night. Compared in whole points, the
# unit the line prints, so 30% against a typical 20% is a rise of ten.
SHARE_DROP_POINTS = 10
GROWTH_RATIO = 1.5

VERDICT_SHARE = "verdict_share"
APOLOGY_SHARE = "apology_share"
ANSWER_P50 = "answer_words_p50"
MESSAGES_P50 = "bot_messages_p50"

LINE = "🗣 *Reply shape*: {body}{note}"
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


def _whole(value: float) -> str:
    return f"{value:.0f}"


def _count(value) -> float | None:
    """A finite number at or above zero, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def numbers(night: dict) -> dict[str, float | None] | None:
    """The night's four compared numbers, or None when it measured no reply."""
    shape = night.get("shape")
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


def baselines(nights: list[dict]) -> dict[str, float | None]:
    """The typical of each number over ``nights[1:]`` (``nights`` newest
    first), whole nights only."""
    earlier = [numbers(n) for n in reversed(nights[1:]) if nightly_latency.whole(n)]
    earlier = [n for n in earlier if n is not None]
    return {key: nightly_latency.typical([n[key] for n in earlier if n[key] is not None])
            for key in (VERDICT_SHARE, APOLOGY_SHARE, ANSWER_P50, MESSAGES_P50)}


def regressions(nights: list[dict]) -> list[str]:
    """The numbers of ``nights[0]`` that moved the wrong way by the
    threshold, by key; empty without a night or a typical to compare."""
    now = numbers(nights[0]) if nights else None
    if now is None:
        return []
    base = baselines(nights)
    out = []
    if now[VERDICT_SHARE] is not None and base[VERDICT_SHARE] is not None and _points(now[VERDICT_SHARE]) <= _points(base[VERDICT_SHARE]) - SHARE_DROP_POINTS:
        out.append(VERDICT_SHARE)
    if now[APOLOGY_SHARE] is not None and base[APOLOGY_SHARE] is not None and _points(now[APOLOGY_SHARE]) >= _points(base[APOLOGY_SHARE]) + SHARE_DROP_POINTS:
        out.append(APOLOGY_SHARE)
    for key in (ANSWER_P50, MESSAGES_P50):
        if now[key] is not None and base[key] and now[key] >= GROWTH_RATIO * base[key]:
            out.append(key)
    return out


def night_line(nights: list[dict]) -> str | None:
    """The reply-shape line for ``nights[0]`` (``nights`` newest first), or
    None when last night measured no reply. A number the night did not
    measure is left out."""
    now = numbers(nights[0]) if nights else None
    if now is None:
        return None
    night = nights[0]
    base = baselines(nights)
    flagged = set(regressions(nights))

    def mark(key: str) -> str:
        return MARK if key in flagged else ""

    def typical(key: str, text) -> str:
        return TYPICAL.format(value=text(base[key])) if base[key] is not None else ""

    parts = []
    if now[VERDICT_SHARE] is not None:
        parts.append(VERDICT_TEXT.format(share=_share(now[VERDICT_SHARE]), reps=REPS.format(count=int(night["shape"]["n"])),
                                         mark=mark(VERDICT_SHARE), typical=typical(VERDICT_SHARE, _share)))
    if now[ANSWER_P50] is not None:
        parts.append(ANSWER_TEXT.format(words=_whole(now[ANSWER_P50]), mark=mark(ANSWER_P50), typical=typical(ANSWER_P50, _whole)))
    if now[MESSAGES_P50] is not None:
        parts.append(MESSAGES_TEXT.format(count=_whole(now[MESSAGES_P50]), mark=mark(MESSAGES_P50), typical=typical(MESSAGES_P50, _whole)))
    if now[APOLOGY_SHARE] is not None:
        parts.append(APOLOGY_TEXT.format(share=_share(now[APOLOGY_SHARE]), mark=mark(APOLOGY_SHARE), typical=typical(APOLOGY_SHARE, _share)))
    note = CUT_NOTE if night.get("truncated") else "" if nightly_latency.whole(night) else PARTIAL_NOTE
    return LINE.format(body=nightly.SEP.join(parts), note=note)
