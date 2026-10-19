"""The two infrastructure shapes ``nightly_bugs.py`` files an `Infra` issue
for, read from the nightly runs in ``data.json``.

**Nights lost.** Each part of the nightly (main, writers: ``nightly.night_part``)
is judged on its own: a night of a part is lost when Prow ended the build
before the eval loop's verdict (``nightly.run_truncated``: the job deadline,
an abort) or its pod went away (``classify.is_lost_pod``). A part that lost
JOB_LOST_MIN of its last JOB_LOST_WINDOW nights gets one issue for its job.

**A case kept from running.** A case is kept from running on a night when
the part that runs it built to its verdict and the case graded nothing:
every repetition classified as infrastructure (``classify.rep_kind``: an
``infra`` verdict, a never-ran phrasing, the delegation ceiling), or no line
for the case at all. INFRA_STREAK_NIGHTS such nights in a row file one issue
for the case. A night is not counted, either way, when the case's part did
not run or was lost (the job itself did not run), when the night predates
the case's first recorded night (a case new to the matrix is not "never
graded" on the nights before it existed), or when infrastructure took at
least INFRA_EVENT_FRACTION of the part's cases (a pool-wide outage, which is
not one case's problem). A graded night ends the streak.

Neither shape touches the domain streak: ``nightly_bugs.graded_nights``
still drops every infrastructure repetition before it calls a night failing.

Only stdlib, like ``nightly.py``.
"""

from __future__ import annotations

import datetime

try:
    from eval_dashboard import classify, nightly
except ImportError:  # run as a script from scripts/eval_dashboard/
    import classify  # type: ignore[no-redef]
    import nightly  # type: ignore[no-redef]

# How many of a part's newest nights are judged, and how many of them lost
# file the job's issue.
JOB_LOST_WINDOW = 3
JOB_LOST_MIN = 2
# Nights in a row a case must be kept from running.
INFRA_STREAK_NIGHTS = 3
# A part night whose recorded cases graded nothing at this fraction or more
# is a pool-wide infrastructure event: skipped for every case. The worst
# nightly night of 2026-10-10..19 had 4 of 81 cases ungraded.
INFRA_EVENT_FRACTION = 0.5
# Verdicts of a case on an eligible night.
STATE_INFRA = "infra"
STATE_ABSENT = "absent"
STATE_GRADED = "graded"
KEPT_STATES = (STATE_INFRA, STATE_ABSENT)
# How a night stopped, as the issue says it.
STOPPED_POD = "the pod was lost (node {node}, last event {event}); nothing recorded"
STOPPED_CUT = "ended before its verdict after {took} (result {result}); {recorded} cases recorded"
STOPPED_CLEAN = "ran to its verdict (result {result}); {recorded} cases recorded"
UNKNOWN = "unknown"
ABSENT_REASON = "no line for the case: the {part} part ran to its verdict and recorded nothing for it"
INFRA_REASON = "{infra} of {reps} repetitions infrastructure{ceiling}: {reason}"
CEILING_NOTE = " ({ceiling} at the delegation ceiling)"
NO_REASON = "(no reason recorded)"
REASON_MAX_CHARS = nightly.REASON_MAX_CHARS


def window_dates(data: dict, lookback: int) -> set[datetime.date]:
    """The newest ``lookback`` nightly dates on record."""
    dates = {nightly.night_date(run) for run in nightly.sorted_nightly_runs(data)} - {None}
    return set(sorted(dates)[-lookback:])


def stopped(run: dict) -> tuple[bool, str]:
    """``(lost, how it stopped)`` for one build of one part."""
    recorded = len(classify.run_tasks(run))
    result = str(run.get("result") or "").upper() or UNKNOWN
    if classify.is_lost_pod(run):
        return True, STOPPED_POD.format(node=run.get("pod_node") or UNKNOWN, event=run.get("pod_last_event") or UNKNOWN)
    if nightly.run_truncated(run):
        seconds = nightly.run_duration(run)
        took = nightly.duration_text(seconds) if seconds is not None else "an unknown wall clock"
        return True, STOPPED_CUT.format(took=took, result=result, recorded=recorded)
    return False, STOPPED_CLEAN.format(result=result, recorded=recorded)


def part_nights(data: dict, lookback: int) -> dict[str, list[dict]]:
    """``{part: [night, ...]}``, oldest first, one per date inside the
    window: the newest build of that part that date. A night is ``{date,
    started, build, job, part, run_url, lost, how, run}``."""
    window = window_dates(data, lookback)
    by_part: dict[str, dict[datetime.date, dict]] = {}
    for run in nightly.sorted_nightly_runs(data):
        day = nightly.night_date(run)
        if day not in window:
            continue
        lost, how = stopped(run)
        part = nightly.night_part(run)
        by_part.setdefault(part, {})[day] = {
            "date": day.isoformat(),
            "started": run.get("started"),
            "build": run.get("build_id"),
            "job": run.get("job") or nightly.DEFAULT_NIGHTLY_JOB,
            "part": part,
            "run_url": nightly.build_url(run),
            "lost": lost,
            "how": how,
            "run": run,
        }
    return {part: [nights[d] for d in sorted(nights)] for part, nights in by_part.items()}


def lost_jobs(parts: dict[str, list[dict]]) -> list[dict]:
    """Each part that lost JOB_LOST_MIN of its last JOB_LOST_WINDOW nights:
    ``{job, part, nights}`` with those nights, oldest first."""
    out = []
    for part in nightly.PARTS:
        tail = (parts.get(part) or [])[-JOB_LOST_WINDOW:]
        if len(tail) == JOB_LOST_WINDOW and sum(n["lost"] for n in tail) >= JOB_LOST_MIN:
            out.append({"job": tail[-1]["job"], "part": part, "nights": tail})
    return out


def first_clean_after_lost(nights: list[dict]) -> dict | None:
    """The first clean night after the part's last lost one, when its newest
    night is clean; None otherwise, or when no night in the window was lost."""
    if not nights or nights[-1]["lost"]:
        return None
    last = max((i for i, n in enumerate(nights) if n["lost"]), default=None)
    return None if last is None else nights[last + 1]


def infra_event(run: dict) -> bool:
    tasks = classify.run_tasks(run)
    ungraded = sum(1 for t in tasks if classify.outcome_of(classify.rep_counts(t)) == classify.OUTCOME_INFRA)
    return bool(tasks) and ungraded / len(tasks) >= INFRA_EVENT_FRACTION


def case_homes(data: dict) -> dict[str, tuple[str, datetime.date | None]]:
    """``{case: (the part that last recorded it, the first date it was
    recorded)}`` over every nightly run on record."""
    out: dict[str, tuple[str, datetime.date | None]] = {}
    for run in nightly.sorted_nightly_runs(data):
        day = nightly.night_date(run)
        for task in classify.run_tasks(run):
            name = task.get("name")
            if isinstance(name, str):
                first = out[name][1] if name in out else day
                out[name] = (nightly.night_part(run), first)
    return out


def infra_reason(task: dict) -> str:
    counts = classify.rep_counts(task)
    reps = sum(counts.values())
    reason = classify.first_reason(task)[:REASON_MAX_CHARS] or NO_REASON
    ceiling = CEILING_NOTE.format(ceiling=counts["ceiling"]) if counts["ceiling"] else ""
    return INFRA_REASON.format(infra=counts["infra"] + counts["ceiling"], reps=reps, ceiling=ceiling, reason=reason)


def case_verdicts(data: dict, parts: dict[str, list[dict]], cases: list[str]) -> dict[str, list[dict]]:
    """``{case: [verdict, ...]}``, oldest first, over the eligible nights
    (module docstring). A verdict is ``{date, started, build, run_url,
    transcript_url, state, reason}``; ``state`` is infra, absent or graded."""
    homes = case_homes(data)
    eligible = {}
    for part, nights in parts.items():
        eligible[part] = [n for n in nights if not n["lost"] and not infra_event(n["run"])]
    out: dict[str, list[dict]] = {}
    for case in cases:
        if case not in homes:
            continue
        part, first = homes[case]
        for night in eligible.get(part, []):
            if first and datetime.date.fromisoformat(night["date"]) < first:
                continue
            task = next((t for t in classify.run_tasks(night["run"]) if t.get("name") == case), None)
            if task is None:
                state, reason = STATE_ABSENT, ABSENT_REASON.format(part=part)
            elif classify.outcome_of(classify.rep_counts(task)) == classify.OUTCOME_INFRA:
                state, reason = STATE_INFRA, infra_reason(task)
            elif classify.outcome_of(classify.rep_counts(task)) is None:
                continue
            else:
                state, reason = STATE_GRADED, None
            out.setdefault(case, []).append({
                "date": night["date"],
                "started": night["started"],
                "build": night["build"],
                "run_url": night["run_url"],
                "transcript_url": nightly.transcript_url(night["run"], case),
                "state": state,
                "reason": reason,
            })
    return out


def kept_streak(verdicts: list[dict]) -> list[dict] | None:
    """The last INFRA_STREAK_NIGHTS verdicts when none graded, else None."""
    tail = verdicts[-INFRA_STREAK_NIGHTS:]
    if len(tail) == INFRA_STREAK_NIGHTS and all(v["state"] in KEPT_STATES for v in tail):
        return tail
    return None


def first_graded_after_kept(verdicts: list[dict]) -> dict | None:
    """The first graded night after the case's last kept one, when its
    newest eligible night graded; None otherwise."""
    if not verdicts or verdicts[-1]["state"] != STATE_GRADED:
        return None
    last = max((i for i, v in enumerate(verdicts) if v["state"] in KEPT_STATES), default=None)
    return None if last is None else verdicts[last + 1]
