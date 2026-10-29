"""The latency block of the 9 AM digest's nightly section: last night's
timing against its own recent past, and the cases that slowed down.

The nightly runs record each repetition's timing (``agent_s``, the agent
run's wall clock, and ``turn1_s``, the opening chat turn; SCHEMA.md), and
``nightly.night_reports`` rolls them up per night and per case. The Nightly
and Trend pages show the numbers; this compares them with a baseline, so a
spike reaches the space instead of waiting for someone to open a page::

    ⏱ Latency: turn 1 p50 9.5 s, p95 24.4 s (target p95 < 10 s; typical 9.5 s, 24.0 s) · agent time, median case 171 s (typical 175 s)
    🐢 Slow cases (agent time at least 2× the case's typical and 60 s more)
    • Observability: obtainability-direct-query 18 s → 428 s (23.7×), sustained 3 nights · @owner

The night line (``night_line``) is always there when last night carried
timing, a night cut off at the deadline included: a slowdown is one way a
night reaches its deadline, and the cases it ran have their timing. Its line
says it covers only those cases, and so does a night with a part missing or
still running. Each number's typical is the median of the same number over
the last BASELINE_NIGHTS earlier whole nights that carry it (not cut short,
no part missing or still running),
with at least MIN_BASELINE_NIGHTS of them; a number at NIGHT_SPIKE_RATIO
times its typical or more gets the 🐢 mark. "Agent time, median case" is the
median of the cases' agent p50: the night document carries per-case
percentiles, and a median over cases does not move when a night records
more or fewer cases.

A case is slow (``slow_cases``) when its agent p50 is at least
CASE_SPIKE_RATIO times its own typical and at least CASE_SPIKE_FLOOR_S more.
Its typical is the median of its agent p50 over its last BASELINE_NIGHTS
earlier nights with timing, at least MIN_BASELINE_NIGHTS of them; a night
cut short still counts here, because a case that ran in it ran whole. A case
slow on SUSTAINED_NIGHTS or more of its nights with timing in a row, last
night included, is "sustained"; the nights counted are the case's own nights
with timing, so a night it did not run in does not break the streak. A
case's typical is its own recent past, so a slowdown that lasts about half
of BASELINE_NIGHTS becomes the typical and the case drops off the list.
When more than BROAD_SHARE of the cases with a typical are slow, and more
than the digest's list holds, one line says so instead of naming them: a
shift that wide has one cause (the model, the mode, quota), not one per
case. A list's worth is always named, so a night that timed few cases
still names its slow one.

All of it is advisory, like the turn-1 target: no verdict, state or gate
reads it. A backtest on the history of 2026-10-15 to 10-28 set the case
rule: at 1.5× a night flagged 4 to 35 cases, too many to act on.
"""

from __future__ import annotations

import math
import statistics

try:
    from eval_dashboard import nightly
except ImportError:  # imported with scripts/eval_dashboard/ on sys.path
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import nightly

BASELINE_NIGHTS = 7
MIN_BASELINE_NIGHTS = 3
NIGHT_SPIKE_RATIO = 1.5
CASE_SPIKE_RATIO = 2.0
CASE_SPIKE_FLOOR_S = 60
SUSTAINED_NIGHTS = 2
BROAD_SHARE = 0.25

TURN1_P50 = "turn1_p50_s"
TURN1_P95 = "turn1_p95_s"
CASE_AGENT_P50 = "agent_p50_s"

LINE = "⏱ *Latency*: {turn1}{sep}{agent}{note}"
CUT_NOTE = " (cut short: over the cases it ran)"
PARTIAL_NOTE = " (partial: over the cases on record)"
TURN1_TEXT = "turn 1 p50 {p50}, p95 {p95} (target p95 < {target} s{typical})"
TURN1_TYPICAL = "; typical {p50}, {p95}"
TURN1_NOT_TIMED = "turn 1 not timed"
AGENT_TEXT = "agent time, median case {value}{typical}"
AGENT_TYPICAL = " (typical {value})"
AGENT_NOT_TIMED = "agent time not recorded"
SPIKE_MARK = " 🐢 {ratio:.1f}×"
SECONDS = "{value:.1f} s"
WHOLE_SECONDS = "{value:.0f} s"

SLOW_HEADING = f"🐢 *Slow cases* (agent time at least {CASE_SPIKE_RATIO:g}× the case's typical and {CASE_SPIKE_FLOOR_S} s more)"
SLOW_LINE = "{domain}: {case} {typical} → {value} ({ratio:.1f}×){sustained} · {owner}"
SUSTAINED_TEXT = ", sustained {nights} nights"
BROAD_LINE = (
    "🐢 *Broad slowdown*: {slow} of {timed} cases with a typical took at least {ratio:g}× their typical agent time"
    f" and {CASE_SPIKE_FLOOR_S} s more"
    " — likely one cause (the model, the mode, quota), not one per case · {page}"
)


def _seconds(value: float | None) -> str:
    return SECONDS.format(value=value)


def _whole_seconds(value: float | None) -> str:
    return WHOLE_SECONDS.format(value=value)


def _number(value) -> float | None:
    """A finite positive number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def whole(night: dict) -> bool:
    """A night with every part, none cut short: its numbers cover the matrix.
    The night's own ``truncated`` reads the main parts only, so each part's
    flag is read too: a writers part cut short leaves the night partial."""
    if night.get("truncated") or night.get("missing_parts") or night.get("running_parts"):
        return False
    return not any(part.get("truncated") for part in night.get("parts") or [])


def median_case_agent(night: dict) -> float | None:
    """The median of the night's cases' agent p50, None when no case has one."""
    values = [v for v in (_number(c.get(CASE_AGENT_P50)) for c in night.get("cases") or []) if v is not None]
    return statistics.median(values) if values else None


def typical(values: list[float]) -> float | None:
    """The median of the newest BASELINE_NIGHTS ``values`` (oldest first),
    None with fewer than MIN_BASELINE_NIGHTS."""
    recent = values[-BASELINE_NIGHTS:]
    return statistics.median(recent) if len(recent) >= MIN_BASELINE_NIGHTS else None


def night_baselines(nights: list[dict]) -> dict[str, float | None]:
    """The typical of each night number over ``nights[1:]`` (``nights``
    newest first, ``nights[0]`` last night), whole nights only."""
    earlier = [n for n in reversed(nights[1:]) if whole(n)]
    return {
        TURN1_P50: typical([v for v in (_number(n.get(TURN1_P50)) for n in earlier) if v is not None]),
        TURN1_P95: typical([v for v in (_number(n.get(TURN1_P95)) for n in earlier) if v is not None]),
        CASE_AGENT_P50: typical([v for v in (median_case_agent(n) for n in earlier) if v is not None]),
    }


def _marked(value: float, base: float | None, text: str) -> str:
    if base and value >= NIGHT_SPIKE_RATIO * base:
        return text + SPIKE_MARK.format(ratio=value / base)
    return text


def night_line(nights: list[dict]) -> str | None:
    """The night line for ``nights[0]`` (``nights`` newest first), or None
    when last night carried no timing at all."""
    if not nights:
        return None
    night = nights[0]
    p50, p95, agent = _number(night.get(TURN1_P50)), _number(night.get(TURN1_P95)), median_case_agent(night)
    if p50 is None and p95 is None and agent is None:
        return None
    base = night_baselines(nights)
    if p50 is not None and p95 is not None:
        typical_text = ""
        if base[TURN1_P50] and base[TURN1_P95]:
            typical_text = TURN1_TYPICAL.format(p50=_seconds(base[TURN1_P50]), p95=_seconds(base[TURN1_P95]))
        turn1 = TURN1_TEXT.format(
            p50=_marked(p50, base[TURN1_P50], _seconds(p50)),
            p95=_marked(p95, base[TURN1_P95], _seconds(p95)),
            target=nightly.TURN1_P95_TARGET_S,
            typical=typical_text,
        )
    else:
        turn1 = TURN1_NOT_TIMED
    if agent is not None:
        agent_typical = AGENT_TYPICAL.format(value=_whole_seconds(base[CASE_AGENT_P50])) if base[CASE_AGENT_P50] else ""
        agent_text = AGENT_TEXT.format(value=_marked(agent, base[CASE_AGENT_P50], _whole_seconds(agent)), typical=agent_typical)
    else:
        agent_text = AGENT_NOT_TIMED
    note = CUT_NOTE if night.get("truncated") else "" if whole(night) else PARTIAL_NOTE
    return LINE.format(turn1=turn1, sep=nightly.SEP, agent=agent_text, note=note)


def _case_history(nights: list[dict]) -> dict[str, list[float]]:
    """``{case: agent p50 per night with timing, oldest first}`` over ``nights`` (newest first)."""
    out: dict[str, list[float]] = {}
    for night in reversed(nights):
        for case in night.get("cases") or []:
            value = _number(case.get(CASE_AGENT_P50))
            if value is not None and isinstance(case.get("case"), str):
                out.setdefault(case["case"], []).append(value)
    return out


def _is_slow(value: float, base: float | None) -> bool:
    return bool(base) and value >= CASE_SPIKE_RATIO * base and value - base >= CASE_SPIKE_FLOOR_S


def slow_cases(nights: list[dict], keep: set[str] | None = None) -> tuple[list[dict], int]:
    """``(slow, timed)`` for ``nights[0]``: each slow case as ``{case,
    value, typical, ratio, sustained}`` (``sustained`` the nights in
    a row it was slow, last night included), largest ratio first; ``timed``
    the cases last night with timing and a typical. With ``keep``, only the
    cases it names are judged or counted."""
    if not nights:
        return [], 0
    history = _case_history(nights[1:])
    slow, timed = [], 0
    for case in nights[0].get("cases") or []:
        name, value = case.get("case"), _number(case.get(CASE_AGENT_P50))
        if not isinstance(name, str) or value is None or (keep is not None and name not in keep):
            continue
        past = history.get(name, [])
        base = typical(past)
        if base is None:
            continue
        timed += 1
        if not _is_slow(value, base):
            continue
        # Walk back through the case's own nights with timing: each earlier
        # value judged against the values before it, as that morning would.
        streak, series = 1, past + [value]
        for index in range(len(series) - 2, -1, -1):
            if not _is_slow(series[index], typical(series[:index])):
                break
            streak += 1
        slow.append({"case": name, "value": value, "typical": base, "ratio": value / base, "sustained": streak})
    slow.sort(key=lambda s: (-s["ratio"], s["case"]))
    return slow, timed


def is_broad(slow: list[dict], timed: int, listed: int) -> bool:
    """Whether one broad-slowdown line replaces the list: more slow cases
    than the ``listed`` lines the list holds, and more than BROAD_SHARE of
    ``timed``."""
    return len(slow) > listed and len(slow) > BROAD_SHARE * timed


def slow_text(entry: dict, domain: str, owner: str) -> str:
    sustained = SUSTAINED_TEXT.format(nights=entry["sustained"]) if entry["sustained"] >= SUSTAINED_NIGHTS else ""
    return SLOW_LINE.format(domain=domain, case=entry["case"], typical=_whole_seconds(entry["typical"]), value=_whole_seconds(entry["value"]),
                            ratio=entry["ratio"], sustained=sustained, owner=owner)


def broad_text(slow: list[dict], timed: int, page: str) -> str:
    return BROAD_LINE.format(slow=len(slow), timed=timed, ratio=CASE_SPIKE_RATIO, page=page)
