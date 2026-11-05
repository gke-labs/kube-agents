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
not run or was lost (the job itself did not run, or one job of the part
lost its build and no clean build of that job followed), when the night
predates the case's first recorded night in the window (a case new to the
matrix, or back on it, is not "never graded" on the nights it was not on
it), or when infrastructure took at least INFRA_EVENT_FRACTION of the
part's cases, counting the cases with no line against its fullest night in
the window (a pool-wide outage or a short run, which is not one case's
problem). A graded night ends the streak.

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
# A part night where this fraction or more of the part's cases (its fullest
# night in the window) graded nothing -- every repetition infra, or no line
# at all -- is a pool-wide infrastructure event: skipped for every case. The
# worst nightly night of 2026-10-10..19 had 4 of 81 cases ungraded.
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
UNKNOWN_WALL_CLOCK = "an unknown wall clock"
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
        took = nightly.duration_text(seconds) if seconds is not None else UNKNOWN_WALL_CLOCK
        return True, STOPPED_CUT.format(took=took, result=result, recorded=recorded)
    return False, STOPPED_CLEAN.format(result=result, recorded=recorded)


def part_nights(data: dict, lookback: int) -> dict[str, list[dict]]:
    """``{part: [night, ...]}``, oldest first, one per date inside the
    window: the newest build of that part that date that was not lost, else
    the newest (a clean re-run rescues its night; a lost re-run does not undo
    a clean build). A night is ``{date, started, finished, build, job, part,
    run_url, lost, how, run, tasks}``; ``tasks`` is ``{case: (task, run)}``
    over every clean build of the part that date (the newest graded line
    wins, else the newest line), so
    two builds with different case sets, such as two halves of one part,
    both count; a lost night's are its own build's. ``unrescued`` is true
    when a job of the part lost its build that date and no clean build of
    the same job followed: a clean sibling job does not vouch for the cases
    the lost one was running, so the kept-case rule skips the night."""
    window = window_dates(data, lookback)
    by_part: dict[str, dict[datetime.date, dict]] = {}
    clean_tasks: dict[tuple[str, datetime.date], dict[str, tuple[dict, dict]]] = {}
    jobs_lost: dict[tuple[str, datetime.date], set[str]] = {}
    jobs_clean: dict[tuple[str, datetime.date], set[str]] = {}
    for run in nightly.sorted_nightly_runs(data):
        day = nightly.night_date(run)
        if day not in window:
            continue
        lost, how = stopped(run)
        part = nightly.night_part(run)
        (jobs_lost if lost else jobs_clean).setdefault((part, day), set()).add(run.get("job") or nightly.DEFAULT_NIGHTLY_JOB)
        if not lost:
            held_tasks = clean_tasks.setdefault((part, day), {})
            for task in classify.run_tasks(run):
                name = task.get("name")
                if not isinstance(name, str):
                    continue
                # A later line stands unless it graded nothing and an
                # earlier one graded the case: a re-run lost to infra does
                # not undo a verdict (as nightly_bugs.graded_nights reads it).
                earlier = held_tasks.get(name)
                if earlier and graded(earlier[0]) and not graded(task):
                    continue
                held_tasks[name] = (task, run)
        held = by_part.get(part, {}).get(day)
        if held and lost and not held["lost"]:
            continue
        by_part.setdefault(part, {})[day] = {
            "date": day.isoformat(),
            "started": run.get("started"),
            "finished": run.get("finished"),
            "build": run.get("build_id"),
            "job": run.get("job") or nightly.DEFAULT_NIGHTLY_JOB,
            "part": part,
            "run_url": nightly.build_url(run),
            "lost": lost,
            "how": how,
            "run": run,
        }
    for part, nights in by_part.items():
        for day, n in nights.items():
            own = {t["name"]: (t, n["run"]) for t in classify.run_tasks(n["run"]) if isinstance(t.get("name"), str)}
            n["tasks"] = own if n["lost"] else clean_tasks.get((part, day), {})
            n["unrescued"] = bool(jobs_lost.get((part, day), set()) - jobs_clean.get((part, day), set()))
    return {part: [nights[d] for d in sorted(nights)] for part, nights in by_part.items()}


def graded(task: dict) -> bool:
    """Whether the case's line is a verdict: not every repetition infra, and
    not unrecorded."""
    return classify.outcome_of(classify.rep_counts(task)) not in (classify.OUTCOME_INFRA, None)


def lost_jobs(parts: dict[str, list[dict]]) -> list[dict]:
    """Each part that lost JOB_LOST_MIN of the newest JOB_LOST_WINDOW nightly
    dates on record: ``{job, part, nights}`` with its nights on those dates,
    oldest first. Only those dates count, so a job that stopped running is
    not judged on its stale tail, and one paused and re-enabled is not filed
    from losses before the pause."""
    recent = set(sorted({n["date"] for ns in parts.values() for n in ns})[-JOB_LOST_WINDOW:])
    out = []
    for part in nightly.PARTS:
        tail = [n for n in parts.get(part) or [] if n["date"] in recent]
        if sum(n["lost"] for n in tail) >= JOB_LOST_MIN:
            out.append({"job": tail[-1]["job"], "part": part, "nights": tail})
    return out


def first_clean_after_lost(nights: list[dict], news=lambda n: True) -> dict | None:
    """The first clean night after the part's last lost one that ``news``
    accepts (the caller's "after the issue opened"), when its newest night
    is clean; None otherwise, or when no night in the window was lost."""
    if not nights or nights[-1]["lost"]:
        return None
    last = max((i for i, n in enumerate(nights) if n["lost"]), default=None)
    return None if last is None else next((n for n in nights[last + 1:] if news(n)), None)


def infra_event(tasks: list[dict], fullest: int) -> bool:
    """Whether infra took INFRA_EVENT_FRACTION of the part's cases: the
    all-infra lines plus the cases with no line, over the fullest night
    (so a night half infra and half missing is not read as a quarter)."""
    ungraded = sum(1 for t in tasks if classify.outcome_of(classify.rep_counts(t)) == classify.OUTCOME_INFRA)
    size = max(fullest, len(tasks))
    return bool(size) and (ungraded + size - len(tasks)) / size >= INFRA_EVENT_FRACTION


def case_homes(parts: dict[str, list[dict]]) -> dict[str, tuple[str, datetime.date]]:
    """``{case: (the part that last recorded it, the first date it was
    recorded)}`` over the window's nights only: a case dropped from the
    matrix and re-listed is new again, not kept from running on the nights
    it was off the list."""
    nights = sorted((n for ns in parts.values() for n in ns), key=lambda n: (n["date"], nightly.PARTS.index(n["part"])))
    out: dict[str, tuple[str, datetime.date]] = {}
    for night in nights:
        day = datetime.date.fromisoformat(night["date"])
        for name in night["tasks"]:
            first = out[name][1] if name in out else day
            out[name] = (night["part"], first)
    return out


def infra_reason(task: dict) -> str:
    counts = classify.rep_counts(task)
    reps = sum(counts.values())
    reason = classify.first_reason(task)[:REASON_MAX_CHARS] or NO_REASON
    ceiling = CEILING_NOTE.format(ceiling=counts["ceiling"]) if counts["ceiling"] else ""
    return INFRA_REASON.format(infra=counts["infra"] + counts["ceiling"] + counts["fixture"], reps=reps, ceiling=ceiling, reason=reason)


def case_verdicts(parts: dict[str, list[dict]], cases: list[str], tracked: dict[str, str] | None = None) -> dict[str, list[dict]]:
    """``{case: [verdict, ...]}``, oldest first, over the eligible nights
    (module docstring). A verdict is ``{date, started, finished, build, job,
    run_url, transcript_url, state, reason}``, the build being the one that
    recorded the case (the night's own when none did); ``state`` is infra,
    absent or graded.

    Each date reads the case from whichever part recorded it (a case that
    changed lanes, or one both parts ran), and is skipped when that part's
    night is not eligible; a date no part recorded it on is judged on the
    part that last did. ``tracked`` is ``{case: part}`` for cases with an
    open kept-case issue: one with no line anywhere in the window still has
    its nights read (on that part), from the window's start."""
    homes = case_homes(parts)
    # The cases each date's inject lane dropped before the fan-out
    # (runs[].lane_excluded, SCHEMA.md): a next-mode night was never going to
    # run them, so their absence that date is not a night they were kept from.
    lane_excluded: dict[str, set[str]] = {}
    for nights in parts.values():
        for n in nights:
            names = n["run"].get("lane_excluded") or []
            lane_excluded.setdefault(n["date"], set()).update(x for x in names if isinstance(x, str))
    eligible: dict[str, dict[str, dict]] = {}
    for part, nights in parts.items():
        # The fullest night as this date's lane would have run it: each
        # night's cases less the ones this date's inject lane drops, so a
        # pre-flip night does not count cases a next-mode night never runs.
        def fullest(day: str, nights: list[dict] = nights) -> int:
            dropped = lane_excluded.get(day, set())
            return max((len(set(n["tasks"]) - dropped) for n in nights), default=0)
        eligible[part] = {n["date"]: n for n in nights if not n["lost"] and not n["unrescued"]
                          and not infra_event([t for t, _ in n["tasks"].values()], fullest(n["date"]))}
    every = {part: {n["date"]: n for n in nights} for part, nights in parts.items()}
    dates = sorted({n["date"] for nights in parts.values() for n in nights})
    out: dict[str, list[dict]] = {}
    for case in cases:
        if case in (tracked or {}):
            home, first = homes.get(case, (tracked[case], None))[0], None
        elif case in homes:
            home, first = homes[case]
        else:
            continue
        for i, day in enumerate(dates):
            if first and datetime.date.fromisoformat(day) < first:
                continue
            recorders = [p for p in nightly.PARTS if case in every.get(p, {}).get(day, {}).get("tasks", {})]
            if not recorders and case in lane_excluded.get(day, ()):
                continue
            # A graded line is direct evidence the case ran: it claims the
            # date even on a pool-wide or unrescued night (not a lost
            # one, whose lines the domain reader skips too), so "a graded
            # night ends the streak" holds whatever the night's other cases did.
            graders = [p for p in recorders if not every[p][day]["lost"] and graded(every[p][day]["tasks"][case][0])]
            usable = [p for p in recorders if day in eligible.get(p, {})]
            if recorders and not usable and not graders:
                continue
            # No part recorded it: "absent" only when no other part may be
            # where the case runs now (a lane change): skip the date when
            # another part's night is lost or unrescued, or missing although
            # that part ran the date before (its build has not landed yet).
            if not recorders and any(
                    (every[p][day]["lost"] or every[p][day]["unrescued"]) if day in every[p] else i > 0 and dates[i - 1] in every[p]
                    for p in every if p != home):
                continue
            # A verdict in either part claims the night, as graded_nights
            # reads it: an ungraded line in one part does not hide the other
            # part's grade.
            part = graders[0] if graders else usable[0] if usable else home
            night = every[part][day] if graders else eligible.get(part, {}).get(day)
            if night is None:
                continue
            task, run = night["tasks"].get(case, (None, night["run"]))
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
                "started": run.get("started"),
                "finished": run.get("finished"),
                "build": run.get("build_id"),
                "job": run.get("job") or nightly.DEFAULT_NIGHTLY_JOB,
                "run_url": nightly.build_url(run),
                # An absent case has no transcript: the build recorded nothing.
                "transcript_url": None if state == STATE_ABSENT else nightly.transcript_url(run, case),
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


def first_graded_after_kept(verdicts: list[dict], news=lambda v: True) -> dict | None:
    """The first graded night after the case's last kept one that ``news``
    accepts (the caller's "after the issue opened"), when its newest
    eligible night graded; None otherwise."""
    if not verdicts or verdicts[-1]["state"] != STATE_GRADED:
        return None
    last = max((i for i, v in enumerate(verdicts) if v["state"] in KEPT_STATES), default=None)
    return None if last is None else next((v for v in verdicts[last + 1:] if news(v)), None)
