"""One night of the nightly tier as a report, and the digest line about it.

The nightly periodic (``ci-kube-agents-eval-nightly``, ``EVAL_TIER=nightly``
in ``hack/ci-eval-pr.sh``) runs every case against ``main`` once a night, and
the collector records each build as a ``tier: "nightly"`` run (SCHEMA.md).
This module turns those runs into what a reader wants the next morning: for
each night, every case it recorded with its state, its repetitions, the
grader's reason and a transcript link; which cases fail tonight that did not
fail the night before; the wall clock; and whether the night finished or was
cut short. ``render.py`` puts the result into ``brief.json`` for
``nightly.html`` and the Brief, ``post_health.py`` writes one line of it into
the 9 AM digest, so the two say the same thing by construction. A night
still in flight -- a nightly build on the collector's ``pending_builds`` --
is reported as running rather than missing (``running_nights``).

States follow the vocabulary the Cases page's strip uses: ``pass`` (every
graded repetition passed), ``partial`` (some passed, some failed), ``fail``
(every graded repetition failed), ``infra`` (nothing graded -- quota or
setup losses, which never count against a case).

A night is **truncated** when Prow ended the job before the eval loop's
verdict -- ``result: ABORTED`` (an interrupt), or any other non-SUCCESS
result whose log has no verdict line (``eval_verdict: null``): the
periodic's deadline arrives as SIGTERM and Prow records FAILURE, so ABORTED
alone would miss it -- and **incomplete** when it concluded but recorded
fewer cases than the nightly matrix on this checkout expects
(``cases[].nightly_active``). Either way the page and the digest say so
instead of reporting the counts as if the whole matrix had run. Since
2026-09-22 ``hack/ci-eval-pr.sh`` grades each case inside its fan-out, the
moment the case's last repetition finishes, so a truncated night still
carries every case graded before the deadline and its counts are those
cases, flagged truncated (the collector's fixture 2102186223282950144);
before that a deadline-cut night recorded nothing at all (#1491).

One night can be two builds. The matrix can run split across two
periodics on two pool projects, both started at 00:00 UTC: the main part
(``ci-kube-agents-eval-nightly``, which is the whole matrix when it runs
alone) and the writers part (``ci-kube-agents-eval-nightly-writers``, the
cases that request a pull request: each resets and watches the project's
one GitOps repo, so on a shared project they run one at a time after
everything else). A build's part is read from its job name, and the builds
of one date are one night (``filed_nights``; a build is dated by its UTC
start plus NIGHT_START_GRACE, so one that starts a moment before midnight
joins the night it was meant for): its cases
are the union of theirs, "incomplete" is judged against the whole matrix,
and each part says for itself whether it was cut short (``parts[]``). The
night is truncated when its main part is; a writers part at its deadline
does not hide or relabel the main part's results. A night with one part
says which part is missing, or still running; a same-day re-run of the
main job reports its date's writers part as its own (``lent_writers``),
while every reader that counts runs counts that build once. One job alone
is one build per night, exactly as before.

Only stdlib, like ``tiers.py``: ``post_health.py`` imports it and must stay
free of third-party dependencies.
"""

from __future__ import annotations

import datetime
import re

try:
    from eval_dashboard import tiers
except ImportError:  # run as a script from scripts/eval_dashboard/
    import tiers  # type: ignore[no-redef]

# The report page render.py writes and the digest links to.
NIGHTLY_PAGE = "nightly.html"
# How many nights brief.json carries: two weeks, the same depth as the
# Brief's runs (render.RUN_VIEW_DAYS) and the collector's cold sweep.
NIGHTS_ON_RECORD = 14
# Case states, the strip vocabulary (render.STRIP_*).
STATE_PASS = "pass"
STATE_PARTIAL = "partial"
STATE_FAIL = "fail"
STATE_INFRA = "infra"
STATES = (STATE_PASS, STATE_PARTIAL, STATE_FAIL, STATE_INFRA)
# Rep results as the collector writes them (SCHEMA.md: runs[].tasks[].reps).
REP_RESULTS = ("pass", "fail", "infra")
# Prow's verdict on the job (SCHEMA.md: runs[].result) and the eval loop's
# own (runs[].eval_verdict; None when the log has no verdict line). ABORTED
# is an interrupt. The periodic's deadline is not one: it arrives as SIGTERM
# and Prow records FAILURE (collect.py's fixture 2092688354838581248), so a
# non-SUCCESS run with no verdict line is the truncated night this module
# exists to name. A record from before the collector wrote eval_verdict has
# no key at all: unknown, not truncated.
RESULT_ABORTED = "ABORTED"
RESULT_SUCCESS = "SUCCESS"
EVAL_VERDICT_KEY = "eval_verdict"
# A nightly build on the collector's pending_builds (listed, no finished.json
# yet) is a night still running only while its first sighting is this
# recent: the periodic's budget is 8 hours (oss-test-infra: timeout 480m)
# and Prow needs a little longer to write finished.json after ending the
# job. Past that the build is a pod that died without uploading, which the
# collector keeps on pending_builds for two days; it is not a running night.
RUNNING_MAX_AGE = datetime.timedelta(hours=9)
# A night whose start is older than this when the digest goes out is not
# "last night": the 8 PM ET run ends by 4 AM under its 8-hour budget, so at
# 9 AM the newest night is at most 13 hours old; 36 hours tolerates one
# late or re-run night without reading the night before as last night.
LAST_NIGHT_MAX_AGE = datetime.timedelta(hours=36)
# A nightly build is dated by its start plus this. Both periodics start at
# 00:00 UTC, so a slow clock or a cron a few minutes early must not move a
# build into the night before. Kept short: only a build started in the
# last 15 minutes before midnight UTC moves to the next date, and a main
# re-run started then is a night of its own there; it does not take the
# cron night's writers part, only reports it (filed_nights, lent_writers).
NIGHT_START_GRACE = datetime.timedelta(minutes=15)
# Where Prow's Spyglass shows a periodic's build and its artifacts. The
# collector records it per nightly run (``runs[].log_url``, SCHEMA.md) from
# the build directory it listed, so the link follows whichever bucket the
# nightly logs to. A nightly record without the field predates it and was
# read from the bucket the nightly used before 2026-09-15, the cluster
# default gs://kube-agents-prow; a periodic's build lives under logs/<job>/
# there, with no pull request in the path.
LEGACY_SPYGLASS_LOGS_ROOT = "https://oss.gprow.dev/view/gs/kube-agents-prow/logs"
DEFAULT_NIGHTLY_JOB = "ci-kube-agents-eval-nightly"
# The second periodic of a split night (module docstring; #2467) and the
# two parts' names. A build of NIGHTLY_WRITERS_JOB is the writers part; any
# other nightly build is the main part, which is also what every build was
# before the split. PART_WORDS is how the digest and the page name a part.
NIGHTLY_WRITERS_JOB = "ci-kube-agents-eval-nightly-writers"
PART_MAIN = "main"
PART_WRITERS = "writers"
PARTS = (PART_MAIN, PART_WRITERS)
PART_WORDS = {PART_MAIN: "main part", PART_WRITERS: "writers part"}
# The job segment of a Spyglass build link (.../logs/<job>/<build>), which
# is how a pending build's part is read: its entry carries no job.
JOB_IN_LOG_URL = re.compile(r"/(?P<job>[^/]+)/\d+/?$")
# The per-case transcript hack/ci-eval-pr.sh archives, first repetition
# (the same object pages.js links for a presubmit run).
TRANSCRIPT_ARTIFACT = "artifacts/eval_{case}_rep1.log"
# The grader's reason as the report carries it; the collector already caps
# a rep's reason at 300 characters, this is the report's own bound.
REASON_MAX_CHARS = 300
DOMAIN_UNKNOWN = "unknown"
# The digest line's glyph and the separator the other digest lines use.
DIGEST_GLYPH = "🌙"
SEP = " · "
# How many newly failing cases the digest names before counting the rest.
DIGEST_NAMED_CASES = 3

UTC = datetime.timezone.utc


# --------------------------------------------------------------------------
# reading the collector's records


def parse_iso(value) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def task_reps(task: dict) -> list[str]:
    """The task's rep results; without ``reps`` its single result stands in
    for one rep (SCHEMA.md, "Optional run and task fields")."""
    reps = task.get("reps")
    if isinstance(reps, list):
        out = [r.get("result") for r in reps if isinstance(r, dict)]
        out = [r for r in out if r in REP_RESULTS]
        if out:
            return out
    result = task.get("result")
    return [result] if result in REP_RESULTS else []


def rep_counts(results: list[str]) -> dict[str, int]:
    return {name: results.count(name) for name in REP_RESULTS}


def case_state(counts: dict[str, int]) -> str | None:
    """The strip state for the rep counts, ``None`` when the task recorded
    nothing at all (an unmeasured row is not on the report)."""
    graded = counts["pass"] + counts["fail"]
    if graded == 0:
        return STATE_INFRA if counts["infra"] else None
    if counts["fail"] == 0:
        return STATE_PASS
    if counts["pass"] == 0:
        return STATE_FAIL
    return STATE_PARTIAL


def first_reason(task: dict) -> str | None:
    """The first failing rep's reason -- the grader's own words -- or the
    first infra rep's when nothing was graded."""
    reps = task.get("reps") if isinstance(task.get("reps"), list) else []
    for wanted in ("fail", "infra"):
        for rep in reps:
            if isinstance(rep, dict) and rep.get("result") == wanted and isinstance(rep.get("reason"), str) and rep["reason"]:
                return rep["reason"][:REASON_MAX_CHARS]
    return None


def domains(data: dict) -> dict[str, str]:
    out = {}
    for case in data.get("cases") or []:
        if isinstance(case, dict) and isinstance(case.get("name"), str):
            domain = case.get("domain")
            out[case["name"]] = domain if isinstance(domain, str) and domain else DOMAIN_UNKNOWN
    return out


def expected_cases(data: dict) -> list[str]:
    """The nightly matrix on this checkout: every case ``nightly_active``
    (SCHEMA.md: cases[]). A document written before the field existed has
    only ``active``, the presubmit's matrix, which the nightly contains."""
    out = []
    for case in data.get("cases") or []:
        if not isinstance(case, dict) or not isinstance(case.get("name"), str):
            continue
        flag = case.get("nightly_active")
        if flag is None:
            flag = case.get("active")
        if flag:
            out.append(case["name"])
    return sorted(out)


def build_url(run: dict) -> str | None:
    build = run.get("build_id")
    if not isinstance(build, str) or not build.isdigit():
        return None
    recorded = run.get("log_url")
    if isinstance(recorded, str) and recorded.startswith("https://"):
        return recorded
    job = run.get("job") if isinstance(run.get("job"), str) and run.get("job") else DEFAULT_NIGHTLY_JOB
    return f"{LEGACY_SPYGLASS_LOGS_ROOT}/{job}/{build}"


def transcript_url(run: dict, case: str) -> str | None:
    base = build_url(run)
    return f"{base}/{TRANSCRIPT_ARTIFACT.format(case=case)}" if base else None


# --------------------------------------------------------------------------
# one night


def night_cases(run: dict, domain_of: dict[str, str]) -> list[dict]:
    """The cases a night recorded, one entry per task row that measured
    something, sorted by domain then name."""
    out = []
    for task in run.get("tasks") or []:
        if not isinstance(task, dict) or not isinstance(task.get("name"), str):
            continue
        counts = rep_counts(task_reps(task))
        state = case_state(counts)
        if state is None:
            continue
        name = task["name"]
        out.append({
            "case": name,
            "domain": domain_of.get(name, DOMAIN_UNKNOWN),
            "state": state,
            "reps": counts,
            "reason": first_reason(task) if state != STATE_PASS else None,
            "transcript_url": transcript_url(run, name),
        })
    out.sort(key=lambda c: (c["domain"], c["case"]))
    return out


def states_of(cases: list[dict]) -> dict[str, str]:
    return {c["case"]: c["state"] for c in cases}


def night_truncated(run: dict, result: str | None) -> bool:
    """Whether Prow ended the job before its verdict (module docstring):
    ABORTED, or any other non-SUCCESS result whose record says the log has
    no verdict line. A record without ``eval_verdict`` is unknown."""
    if result == RESULT_ABORTED:
        return True
    if result == RESULT_SUCCESS or EVAL_VERDICT_KEY not in run:
        return False
    return run.get(EVAL_VERDICT_KEY) is None


def night_part(run: dict) -> str:
    """The part of a split night a nightly build ran, read from its job."""
    return PART_WRITERS if run.get("job") == NIGHTLY_WRITERS_JOB else PART_MAIN


def pending_part(entry: dict) -> str:
    """The part of a nightly build still in flight, read from the job in its
    link; an entry without one predates the split and is the main part."""
    match = JOB_IN_LOG_URL.search(entry.get("log_url") or "") if isinstance(entry.get("log_url"), str) else None
    return PART_WRITERS if match and match.group("job") == NIGHTLY_WRITERS_JOB else PART_MAIN


def night_day(moment: datetime.datetime | None) -> datetime.date | None:
    """The night a moment belongs to: its UTC date after NIGHT_START_GRACE."""
    return (moment + NIGHT_START_GRACE).date() if moment else None


def night_date(run: dict) -> datetime.date | None:
    return night_day(parse_iso(run.get("started")))


def filed_nights(runs: list[dict]) -> list[tuple[dict[str, dict], list[dict]]]:
    """``runs`` (nightly, oldest first) as nights, oldest first, each
    ``({part: run}, every run filed under the night)``. A main build joins
    the oldest night of its date (``night_date``) that has no main part yet;
    failing that, it opens a night of its own, so one job alone is one
    build per night as before and a same-day re-run of the main job is a
    night of its own. A writers build joins, among the nights of its date
    that have no writers part yet, the one whose main part started closest
    to it (the newer on a tie), so a main re-run started late the evening
    before does not take the cron night's writers part; a writers re-run
    joins one only if ``replaces`` says it beats the date's writers part
    already filed. Failing that, it
    takes the writers part of the date's newest night when ``replaces``
    says so: the writers part is the short one, the one re-run after a
    flake, and its newest build stands for the night unless that build
    graded less (one that died in setup or lost every case to quota does
    not wipe the night's verdicts). Either way the build left out of the
    night stays filed under it, so a reader keyed by build still finds it
    there. A run with no start time stands alone. A night left without a
    writers part beside a main re-run borrows one for its report only
    (``lent_writers``); it is filed here once."""
    nights: list[tuple[dict[str, dict], list[dict]]] = []
    by_date: dict[datetime.date, list[tuple[dict[str, dict], list[dict]]]] = {}
    for run in runs:
        part, day = night_part(run), night_date(run)
        dated = by_date.get(day, []) if day else []
        open_nights = [n for n in dated if part not in n[0]]
        if part == PART_WRITERS and open_nights:
            # A re-run that would not take the date's writers part from the
            # build that has it does not become a re-run night's own part
            # either: it stays filed under that build's night, and the open
            # night borrows the better part for its report.
            holders = [n for n in dated if PART_WRITERS in n[0]]
            if holders and not replaces(run, holders[-1][0][PART_WRITERS]):
                holders[-1][1].append(run)
                continue
            started = parse_iso(run.get("started"))
            filed = min(reversed(open_nights), key=lambda n: abs(parse_iso(n[0][PART_MAIN].get("started")) - started))
        else:
            filed = open_nights[0] if open_nights else None
        if filed is None and part == PART_WRITERS and dated:
            filed = dated[-1]
            if not replaces(run, filed[0][part]):
                filed[1].append(run)
                continue
        if filed is None:
            filed = ({}, [])
            nights.append(filed)
            if day:
                by_date.setdefault(day, []).append(filed)
        filed[0][part] = run
        filed[1].append(run)
    return nights


def graded_cases(run: dict) -> int:
    """How many cases the run graded: state pass, partial or fail."""
    return sum(1 for c in night_cases(run, {}) if c["state"] != STATE_INFRA)


def run_truncated(run: dict) -> bool:
    return night_truncated(run, str(run.get("result") or "").upper() or None)


def replaces(newcomer: dict, incumbent: dict) -> bool:
    """Whether a writers re-run takes the night's writers part from the
    build that has it: it graded more cases (an infra-only or unrecorded
    case is no verdict), or as many and was not cut short where the
    incumbent finished."""
    new, old = graded_cases(newcomer), graded_cases(incumbent)
    return new > old or (new == old and (run_truncated(incumbent) or not run_truncated(newcomer)))


def group_nights(runs: list[dict]) -> list[dict[str, dict]]:
    """``runs`` (nightly, oldest first) as nights, oldest first, each
    ``{part: run}`` (``filed_nights``)."""
    return [night for night, _ in filed_nights(runs)]


def lent_writers(nights: list[dict[str, dict]]) -> dict[int, int]:
    """``{index: lender index}`` over ``group_nights``: a night with a main
    part and no writers part -- a same-day re-run of the main job -- reports
    the writers part of the newest other night of its date that has one,
    the writers build filed for that date. Only the report borrows it: the
    build stays filed under its own night (``filed_nights``), so a reader
    that counts runs counts it once."""
    out = {}
    for index, night in enumerate(nights):
        day = night_date(night[PART_MAIN]) if PART_MAIN in night and PART_WRITERS not in night else None
        lenders = [i for i, other in enumerate(nights) if day and PART_WRITERS in other and night_date(other[PART_WRITERS]) == day]
        if lenders:
            out[index] = lenders[-1]
    return out


def night_parts(night: dict[str, dict]) -> list[dict]:
    """The night's runs in part order: main first."""
    return [night[part] for part in PARTS if part in night]


def joined_night_runs(runs: list[dict]) -> dict[int, dict]:
    """``{id(run): the run's night as one run}``: every task row of the
    night's parts under ``tasks``. For a rule that judges a whole night,
    such as the pages' run-level event (most graded cases failed), which a
    split night must not judge on its small writers part alone."""
    out = {}
    for night, filed in filed_nights(by_start(runs)):
        joined = {"tasks": [task for run in night_parts(night) for task in run.get("tasks") or []]}
        for run in filed:
            out[id(run)] = joined
    return out


def night_builds(data: dict) -> dict[str, str]:
    """``{build id: the build its night is reported under}`` for every
    nightly run on record: a night's builds share its main part's id (the
    night document's ``build``), so a reader keyed by build -- the Trend
    page's nights -- joins a split night the way the report does."""
    out = {}
    for night, filed in filed_nights(sorted_nightly_runs(data)):
        anchor = night_parts(night)[0].get("build_id")
        for run in filed:
            if isinstance(run.get("build_id"), str) and isinstance(anchor, str):
                out[run["build_id"]] = anchor
    return out


def parts_by_date(data: dict) -> dict[datetime.date, set[str]]:
    """``{night date: the parts of the nightly runs dated to it}``
    (``night_date``), across every night of that date."""
    out: dict[datetime.date, set[str]] = {}
    for run in sorted_nightly_runs(data):
        day = night_date(run)
        if day:
            out.setdefault(day, set()).add(night_part(run))
    return out


def writers_cases(data: dict, day: datetime.date | None) -> set[str]:
    """The cases a writers build dated on or before ``day`` recorded."""
    out: set[str] = set()
    for run in sorted_nightly_runs(data):
        dated = night_date(run)
        if night_part(run) == PART_WRITERS and day and dated and dated <= day:
            out.update(c["case"] for c in night_cases(run, {}))
    return out


def run_duration(run: dict) -> int | float | None:
    duration = run.get("duration_s")
    if isinstance(duration, (int, float)):
        return duration
    started, finished = parse_iso(run.get("started")), parse_iso(run.get("finished"))
    return int((finished - started).total_seconds()) if started and finished else None


def part_document(part: str, run: dict, cases: list[dict], from_night: str | None = None) -> dict:
    result = str(run.get("result") or "").upper() or None
    started, finished = parse_iso(run.get("started")), parse_iso(run.get("finished"))
    return {
        "part": part,
        "build": run.get("build_id") if isinstance(run.get("build_id"), str) else None,
        "job": run.get("job") if isinstance(run.get("job"), str) else None,
        "result": result,
        "truncated": night_truncated(run, result),
        "started": started.isoformat() if started else None,
        "finished": finished.isoformat() if finished else None,
        "duration_s": run_duration(run),
        "log_url": build_url(run),
        "recorded": len(cases),
        "from_night": from_night,
    }


def absent_parts(night: dict[str, dict], missing_cases: list[str], writers: set[str], running: list[dict], dated: set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """``(missing parts, running parts)`` of a night: the parts it should
    have and has not, each either still in flight -- a running build of
    that part first seen on the night's date (``night_day``) -- or missing.
    ``dated`` is ``parts_by_date`` for the night's date: a part that ran
    that date in another night (beside a same-day re-run of the main job)
    is not missing.

    The main part is expected beside any writers part: it is the job that
    always runs. The writers part is expected only when a case the night is
    missing is one a writers build recorded on or before the night's date
    (``writers``, ``writers_cases``). So every night before the split, and
    every night while the main job runs alone, looks exactly as it did; and
    once the main job runs the whole matrix again (the split undone), a
    night short of a main case is incomplete without naming the writers
    part, while one short of a former writers case still names it."""
    present = night_parts(night)
    day = night_date(present[0])
    in_flight = {pending_part(e) for e in running if day and night_day(parse_iso(e.get("first_seen"))) == day}
    expected = []
    if PART_MAIN not in night and PART_MAIN not in dated:
        expected.append(PART_MAIN)
    elif PART_WRITERS not in night and PART_WRITERS not in dated and writers.intersection(missing_cases):
        expected.append(PART_WRITERS)
    missing = [p for p in expected if p not in in_flight]
    still = [p for p in expected if p in in_flight]
    return missing, still


def night_document(run: dict, data: dict, previous: dict | None) -> dict:
    """One build as a night (SCHEMA.md, "brief.json": ``nightly.nights[]``).
    ``previous`` is the build of the night before it on record, for "newly
    failing" and "fixed"; ``None`` on the first night. ``night_reports``
    joins a split night's builds through ``joined_night_document``."""
    return joined_night_document({night_part(run): run}, data, {night_part(previous): previous} if previous else None)


def joined_night_document(night: dict[str, dict], data: dict, previous: dict[str, dict] | None, running: list[dict] = (), borrowed: dict[str, str] | None = None) -> dict:
    """One night, ``{part: run}``, as the page and the digest read it
    (SCHEMA.md, "brief.json": ``nightly.nights[]``). ``previous`` is the
    night before it on record, ``{part: run}``, ``None`` on the first
    night (``night_reports`` builds it per part); a case more than one of
    its runs recorded reads as the first of them, in dict order, did.
    ``running`` is ``running_nights``, which says whether a part the night
    lacks is still in flight. ``borrowed`` is ``{part: build of the night
    it is filed under}`` for a part ``night`` borrows (``lent_writers``):
    it counts as the night's own, but the night's wall clock is its own
    builds'. The night-wide fields that name one build (``build``,
    ``job``, ``result``, ``log_url`` ...) are the main part's, or the
    writers part's when the main one is absent; ``parts[]`` has each."""
    borrowed = borrowed or {}
    domain_of = domains(data)
    expected = expected_cases(data)
    runs = night_parts(night)
    cases: list[dict] = []
    parts = []
    for part in PARTS:
        if part not in night:
            continue
        # A case recorded by both parts (a misconfigured split) counts
        # once, as the main part recorded it.
        seen = {c["case"] for c in cases}
        mine = [c for c in night_cases(night[part], domain_of) if c["case"] not in seen]
        cases.extend(mine)
        parts.append(part_document(part, night[part], mine, borrowed.get(part)))
    cases.sort(key=lambda c: (c["domain"], c["case"]))
    recorded = {c["case"] for c in cases}
    now_states = states_of(cases)
    # The night before, each case as the first run of ``previous`` that
    # recorded it.
    before: dict[str, str] = {}
    for run in (previous or {}).values():
        for c in night_cases(run, domain_of):
            before.setdefault(c["case"], c["state"])
    failing = sorted(name for name, state in now_states.items() if state == STATE_FAIL)
    newly_failing = [name for name in failing if before.get(name) != STATE_FAIL] if previous else []
    fixed = sorted(name for name, state in before.items() if state == STATE_FAIL and now_states.get(name) == STATE_PASS)
    missing = [name for name in expected if name not in recorded]
    day = night_date(runs[0])
    missing_parts, running_parts = absent_parts(night, missing, writers_cases(data, day), list(running), parts_by_date(data).get(day, set()) if day else set())
    # The night is cut short as a whole when its main part was (the part
    # that holds nearly every case), or every part when it has no main one.
    # A writers part at its deadline alone is that part's ``truncated``: the
    # main part's results stand, and the night is not complete.
    truncated = parts[0]["truncated"] if PART_MAIN in night else all(p["truncated"] for p in parts)
    own = [p for p in parts if p["part"] not in borrowed]
    starts = [parse_iso(p["started"]) for p in own if p["started"]]
    finishes = [parse_iso(p["finished"]) for p in own if p["finished"]]
    durations = [p["duration_s"] for p in own if p["duration_s"] is not None]
    primary = runs[0]
    previous_primary = (previous.get(PART_MAIN) or next(iter(previous.values()))) if previous else None
    return {
        "build": primary.get("build_id") if isinstance(primary.get("build_id"), str) else None,
        "job": primary.get("job") if isinstance(primary.get("job"), str) else None,
        "head_sha": primary.get("head_sha") if isinstance(primary.get("head_sha"), str) else None,
        "project": primary.get("project") if isinstance(primary.get("project"), str) else None,
        "started": min(starts).isoformat() if starts else None,
        "finished": max(finishes).isoformat() if finishes else None,
        # The parts run side by side, so the night took as long as its
        # longest part.
        "duration_s": max(durations) if durations else None,
        "result": parts[0]["result"],
        "log_url": parts[0]["log_url"],
        "truncated": truncated,
        "complete": not any(p["truncated"] for p in parts) and not missing and not missing_parts and not running_parts,
        "counts": {
            "expected": len(expected),
            "recorded": len(cases),
            "passed": sum(1 for c in cases if c["state"] == STATE_PASS),
            "partial": sum(1 for c in cases if c["state"] == STATE_PARTIAL),
            "failed": len(failing),
            "infra": sum(1 for c in cases if c["state"] == STATE_INFRA),
            "missing": len(missing),
        },
        "missing": missing,
        "newly_failing": newly_failing,
        "fixed": fixed,
        "previous_build": previous_primary.get("build_id") if previous_primary and isinstance(previous_primary.get("build_id"), str) else None,
        "parts": parts,
        "missing_parts": missing_parts,
        "running_parts": running_parts,
        "cases": cases,
    }


def sorted_nightly_runs(data: dict) -> list[dict]:
    """The nightly runs oldest first, by start time; the collector's order
    stands in when a run lacks one."""
    return by_start(tiers.nightly_runs([r for r in data.get("runs") or [] if isinstance(r, dict)]))


def by_start(runs: list[dict]) -> list[dict]:
    """``runs`` oldest first by start time, kept in the given order when a
    run lacks one."""
    runs = list(runs)
    if runs and all(isinstance(r.get("started"), str) for r in runs):
        runs.sort(key=lambda r: r["started"])
    return runs


def night_reports(data: dict, limit: int = NIGHTS_ON_RECORD, running: list[dict] = ()) -> list[dict]:
    """The last ``limit`` nights, **newest first**, each compared with the
    night before it on record (the one older than the window included, so
    the oldest listed night still has its "newly failing"). "The night
    before" is per part: each part's run from the newest earlier night that
    has that part, so a night that lacks one part does not make every case
    of that part failing tonight read as newly failing. A case both runs
    recorded reads as the newer night recorded it, as the main part did
    when they are one night. ``running`` is ``running_nights``, for a night
    with a part still in flight. A main re-run's night reports its date's
    writers part (``lent_writers``), compared with the writers part before
    that one."""
    nights = group_nights(sorted_nightly_runs(data))
    lent = lent_writers(nights)
    out = []
    for index in range(len(nights) - 1, max(-1, len(nights) - 1 - limit), -1):
        night, borrowed = nights[index], {}
        if index in lent:
            lender = nights[lent[index]]
            night = {**night, PART_WRITERS: lender[PART_WRITERS]}
            borrowed = {PART_WRITERS: night_parts(lender)[0].get("build_id")}
        sources = []
        for part in PARTS:
            at = next((i for i in range(index - 1, -1, -1) if part in nights[i] and nights[i][part] is not night.get(part)), None)
            if at is not None:
                sources.append((at, part))
        # Newest night first; the sort is stable, so main first on a tie.
        sources.sort(key=lambda source: -source[0])
        previous = {part: nights[at][part] for at, part in sources}
        out.append(joined_night_document(night, data, previous or None, running, borrowed))
    return out


def nightly_job(data: dict) -> str:
    """The periodic's name as the newest main-part run carries it, else the default."""
    return next((r.get("job") for r in reversed(sorted_nightly_runs(data)) if isinstance(r.get("job"), str) and night_part(r) == PART_MAIN), DEFAULT_NIGHTLY_JOB)


def running_nights(data: dict, now: datetime.datetime | None) -> list[dict]:
    """The nightly builds still in flight: the ``tier: "nightly"`` entries of
    the collector's ``pending_builds`` (SCHEMA.md) first seen inside
    RUNNING_MAX_AGE of ``now``, oldest first, each ``{build, first_seen,
    log_url}``. Without a ``now`` the age is not judged. A malformed entry
    is skipped; a value that is not a list is no entries."""
    raw = data.get("pending_builds")
    if not isinstance(raw, list):
        return []
    job = nightly_job(data)
    out = []
    for entry in raw:
        if not isinstance(entry, dict) or not tiers.is_nightly(entry):
            continue
        build = entry.get("build_id")
        seen = parse_iso(entry.get("first_seen"))
        if not isinstance(build, str) or not build.isdigit() or seen is None:
            continue
        if now is not None and now - seen > RUNNING_MAX_AGE:
            continue
        out.append({"build": build, "first_seen": seen.isoformat(), "log_url": build_url({"build_id": build, "job": job, "log_url": entry.get("log_url")})})
    out.sort(key=lambda e: int(e["build"]))
    return out


def nightly_document(data: dict) -> dict:
    """The ``nightly`` block of brief.json. ``running`` is judged against
    ``generated_at``, the render's time axis, so two renders of one
    data.json agree."""
    running = running_nights(data, parse_iso(data.get("generated_at")))
    return {
        "job": nightly_job(data),
        "nights": night_reports(data, running=running),
        "running": running,
    }


# --------------------------------------------------------------------------
# the digest line


def duration_text(seconds) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def last_night(data: dict, now: datetime.datetime) -> tuple[dict | None, dict | None]:
    """``(last night, newest night on record)``: the newest night is last
    night when it started inside LAST_NIGHT_MAX_AGE of ``now``; otherwise
    last night is missing and the newest is returned for the message to
    date."""
    nights = night_reports(data, limit=1, running=running_nights(data, now))
    if not nights:
        return None, None
    newest = nights[0]
    started = parse_iso(newest.get("started"))
    if started is None or now - started > LAST_NIGHT_MAX_AGE:
        return None, newest
    return newest, newest


def name_cases(names: list[str]) -> str:
    if len(names) <= DIGEST_NAMED_CASES:
        return ", ".join(names)
    return f"{', '.join(names[:DIGEST_NAMED_CASES])} and {len(names) - DIGEST_NAMED_CASES} more"


def part_notes(night: dict, cut: bool = True) -> list[str]:
    """What the digest says about a split night's parts: each part cut
    short (when ``cut``) with its own wall clock, each part missing, each
    part still running. Nothing for a night of one part that expects no
    other."""
    notes = []
    for part in (night.get("parts") or []) if cut else []:
        if part.get("truncated"):
            took = duration_text(part["duration_s"]) if part.get("duration_s") is not None else "an unknown wall clock"
            notes.append(f"{PART_WORDS.get(part.get('part'), part.get('part'))} truncated after {took}")
    notes += [f"{PART_WORDS.get(p, p)} missing" for p in night.get("missing_parts") or []]
    notes += [f"{PART_WORDS.get(p, p)} still running" for p in night.get("running_parts") or []]
    return notes


def digest_line(data: dict | None, now: datetime.datetime, clock=None) -> str:
    """One line for the 9 AM digest. ``clock`` renders a datetime the way
    the rest of the digest does (post_health.clock); without one the ISO
    form is used. A truncated or missing night says so instead of numbers,
    and so does a split night whose main part is still running; a split
    night with one part cut short, missing or still running otherwise has
    its numbers and says which part, without a newly-failing verdict when
    the main part is missing."""
    when = clock or (lambda value: value.isoformat(timespec="minutes"))
    if not data:
        return f"{DIGEST_GLYPH} Nightly: no data.json to read a night from"
    night, newest = last_night(data, now)

    def still_running(entry: dict) -> str:
        return f"{DIGEST_GLYPH} Nightly: still running (first seen {when(parse_iso(entry['first_seen']))}){SEP}the report follows when it finishes"

    if night is None:
        running = running_nights(data, now)
        if running:
            # Still in flight at digest time -- a late start, or a night at
            # its budget -- so there are no numbers yet, and the report
            # carries them once the collector records the build.
            return still_running(running[-1])
        if newest is None:
            return f"{DIGEST_GLYPH} Nightly: no run on record yet"
        started = parse_iso(newest.get("started"))
        return f"{DIGEST_GLYPH} Nightly: no run last night (the newest on record started {when(started) if started else 'at an unknown time'})"
    if PART_MAIN in night.get("running_parts", []):
        # Only the writers part has finished: its few cases are not the
        # night's numbers, which follow with the main part.
        return still_running([e for e in running_nights(data, now) if pending_part(e) == PART_MAIN][-1])
    counts = night["counts"]
    took = duration_text(night["duration_s"]) if night.get("duration_s") is not None else "unknown wall clock"
    recorded = f"{counts['recorded']} of {counts['expected']} cases recorded" if counts["expected"] else f"{counts['recorded']} cases recorded"
    if night["truncated"]:
        # A night is cut short by its main part when it has one: that
        # part's wall clock, not the longest part's.
        main = next((p for p in night.get("parts") or [] if p.get("part") == PART_MAIN), None)
        if main is not None:
            took = duration_text(main["duration_s"]) if main.get("duration_s") is not None else "unknown wall clock"
        notes = "".join(f"{SEP}{note}" for note in part_notes(night, cut=False))
        return f"{DIGEST_GLYPH} Nightly: truncated after {took}{SEP}{recorded}{notes}{SEP}the night's numbers are not comparable"
    parts = [f"{counts['recorded']} cases", f"{counts['passed']} passed all reps", f"{counts['partial']} partial", f"{counts['failed']} failed"]
    if counts["infra"]:
        parts.append(f"{counts['infra']} infra")
    # Without its main part only the writers part's cases are on record, so
    # no verdict on what is newly failing; the part notes say the main part
    # is missing.
    if PART_MAIN in night.get("missing_parts", []):
        pass
    elif night["newly_failing"]:
        parts.append(f"newly failing: {name_cases(night['newly_failing'])}")
    elif night.get("previous_build") is None:
        parts.append("first night on record")
    else:
        parts.append("nothing newly failing")
    parts += part_notes(night)
    if not night["complete"]:
        # Every case recorded: the part notes above say what is short.
        parts.append(f"incomplete: {recorded}" if counts["missing"] else "incomplete")
    parts.append(took)
    return f"{DIGEST_GLYPH} Nightly: {SEP.join(parts)}"
