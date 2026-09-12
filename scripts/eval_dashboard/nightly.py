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
instead of reporting the counts as if the whole matrix had run.

Only stdlib, like ``tiers.py``: ``post_health.py`` imports it and must stay
free of third-party dependencies.
"""

from __future__ import annotations

import datetime

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
# Where Prow's Spyglass shows a periodic's build and its artifacts. A
# presubmit's build lives under pr-logs/pull/<org_repo>/<pr>/; a periodic's
# under logs/<job>/, with no pull request in the path.
SPYGLASS_LOGS_ROOT = "https://oss.gprow.dev/view/gs/kube-agents-prow/logs"
DEFAULT_NIGHTLY_JOB = "ci-kube-agents-eval-nightly"
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
    job = run.get("job") if isinstance(run.get("job"), str) and run.get("job") else DEFAULT_NIGHTLY_JOB
    return f"{SPYGLASS_LOGS_ROOT}/{job}/{build}"


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


def night_document(run: dict, data: dict, previous: dict | None) -> dict:
    """One night as the page and the digest read it (SCHEMA.md,
    "brief.json": ``nightly.nights[]``). ``previous`` is the night before
    it on record, for "newly failing" and "fixed"; ``None`` on the first
    night."""
    domain_of = domains(data)
    expected = expected_cases(data)
    cases = night_cases(run, domain_of)
    recorded = {c["case"] for c in cases}
    now_states = states_of(cases)
    before = states_of(night_cases(previous, domain_of)) if previous else {}
    failing = sorted(name for name, state in now_states.items() if state == STATE_FAIL)
    newly_failing = [name for name in failing if before.get(name) != STATE_FAIL] if previous else []
    fixed = sorted(name for name, state in before.items() if state == STATE_FAIL and now_states.get(name) == STATE_PASS)
    result = str(run.get("result") or "").upper() or None
    missing = [name for name in expected if name not in recorded]
    truncated = night_truncated(run, result)
    started = parse_iso(run.get("started"))
    finished = parse_iso(run.get("finished"))
    duration = run.get("duration_s")
    if not isinstance(duration, (int, float)) and started and finished:
        duration = int((finished - started).total_seconds())
    return {
        "build": run.get("build_id") if isinstance(run.get("build_id"), str) else None,
        "job": run.get("job") if isinstance(run.get("job"), str) else None,
        "head_sha": run.get("head_sha") if isinstance(run.get("head_sha"), str) else None,
        "project": run.get("project") if isinstance(run.get("project"), str) else None,
        "started": started.isoformat() if started else None,
        "finished": finished.isoformat() if finished else None,
        "duration_s": duration if isinstance(duration, (int, float)) else None,
        "result": result,
        "log_url": build_url(run),
        "truncated": truncated,
        "complete": not truncated and not missing,
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
        "previous_build": previous.get("build_id") if previous and isinstance(previous.get("build_id"), str) else None,
        "cases": cases,
    }


def sorted_nightly_runs(data: dict) -> list[dict]:
    """The nightly runs oldest first, by start time; the collector's order
    stands in when a run lacks one."""
    runs = tiers.nightly_runs([r for r in data.get("runs") or [] if isinstance(r, dict)])
    if runs and all(isinstance(r.get("started"), str) for r in runs):
        runs.sort(key=lambda r: r["started"])
    return runs


def night_reports(data: dict, limit: int = NIGHTS_ON_RECORD) -> list[dict]:
    """The last ``limit`` nights, **newest first**, each compared with the
    night before it on record (the one older than the window included, so
    the oldest listed night still has its "newly failing")."""
    runs = sorted_nightly_runs(data)
    out = []
    for index in range(len(runs) - 1, max(-1, len(runs) - 1 - limit), -1):
        previous = runs[index - 1] if index > 0 else None
        out.append(night_document(runs[index], data, previous))
    return out


def nightly_job(data: dict) -> str:
    """The periodic's name as the newest nightly run carries it, else the default."""
    return next((r.get("job") for r in reversed(sorted_nightly_runs(data)) if isinstance(r.get("job"), str)), DEFAULT_NIGHTLY_JOB)


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
        out.append({"build": build, "first_seen": seen.isoformat(), "log_url": build_url({"build_id": build, "job": job})})
    out.sort(key=lambda e: int(e["build"]))
    return out


def nightly_document(data: dict) -> dict:
    """The ``nightly`` block of brief.json. ``running`` is judged against
    ``generated_at``, the render's time axis, so two renders of one
    data.json agree."""
    return {
        "job": nightly_job(data),
        "nights": night_reports(data),
        "running": running_nights(data, parse_iso(data.get("generated_at"))),
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
    nights = night_reports(data, limit=1)
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


def digest_line(data: dict | None, now: datetime.datetime, clock=None) -> str:
    """One line for the 9 AM digest. ``clock`` renders a datetime the way
    the rest of the digest does (post_health.clock); without one the ISO
    form is used. A truncated or missing night says so instead of numbers."""
    when = clock or (lambda value: value.isoformat(timespec="minutes"))
    if not data:
        return f"{DIGEST_GLYPH} Nightly: no data.json to read a night from"
    night, newest = last_night(data, now)
    if night is None:
        running = running_nights(data, now)
        if running:
            # Still in flight at digest time -- a late start, or a night at
            # its budget -- so there are no numbers yet, and the report
            # carries them once the collector records the build.
            seen = parse_iso(running[-1]["first_seen"])
            return f"{DIGEST_GLYPH} Nightly: still running (first seen {when(seen)}){SEP}the report follows when it finishes"
        if newest is None:
            return f"{DIGEST_GLYPH} Nightly: no run on record yet"
        started = parse_iso(newest.get("started"))
        return f"{DIGEST_GLYPH} Nightly: no run last night (the newest on record started {when(started) if started else 'at an unknown time'})"
    counts = night["counts"]
    took = duration_text(night["duration_s"]) if night.get("duration_s") is not None else "unknown wall clock"
    recorded = f"{counts['recorded']} of {counts['expected']} cases recorded" if counts["expected"] else f"{counts['recorded']} cases recorded"
    if night["truncated"]:
        return f"{DIGEST_GLYPH} Nightly: truncated after {took}{SEP}{recorded}{SEP}the night's numbers are not comparable"
    parts = [f"{counts['recorded']} cases", f"{counts['passed']} passed all reps", f"{counts['partial']} partial", f"{counts['failed']} failed"]
    if counts["infra"]:
        parts.append(f"{counts['infra']} infra")
    if night["newly_failing"]:
        parts.append(f"newly failing: {name_cases(night['newly_failing'])}")
    elif night.get("previous_build") is None:
        parts.append("first night on record")
    else:
        parts.append("nothing newly failing")
    if not night["complete"]:
        parts.append(f"incomplete: {recorded}")
    parts.append(took)
    return f"{DIGEST_GLYPH} Nightly: {SEP.join(parts)}"
