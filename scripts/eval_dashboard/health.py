#!/usr/bin/env python3
"""Adjudicate the presubmit gate's health from the eval dashboard's data.json.

Every gate incident in the week of 2026-09-01 was diagnosed by hand with the
same mechanical procedure: open the dashboard, find the reds of the last few
hours, ask whether the same cases failed on unrelated pull requests (a shared
fixture broke -- #1278), whether the repetitions were lost to 429s and empty
records rather than graded (a quota storm -- #1225, #1097), or whether runs
died before any task ran (setup failures). Nobody derives that from a heatmap
at 8am, so this turns the procedure into a job.

It is a pure function: data.json (schema v1, SCHEMA.md) plus the previously
written health.json in, health.json out::

    {state, since, cause, failing_cases, evidence, advice, metrics, generated_at}

`state` is GREEN, DEGRADED or OUTAGE. The rules are the module-level
constants below -- each names the incident it was tuned on -- and the state
machine that applies hysteresis to them is `transition`. The previous state
is an input (`--prev`) rather than something remembered, so the scheduled
job that runs this is stateless between ticks.

The credibility test is the replay: `--replay` walks a data.json as if the
job had run every `--step` and prints the state timeline, and
scripts/test_eval_dashboard_health.py asserts that timeline against the
incidents the eval crew filed that week (fixture: testdata_health/, written
by `--trim`, which reduces a data.json to the runs of a date range and the
fields read here).

Time has two clocks. Every window is measured from the data's horizon --
its `generated_at`, the newest moment the collector saw -- because that is
when the evidence stops, not when this runs. The wall clock only asks how
old that horizon is: a data.json the refresh job has stopped updating would
otherwise freeze the state forever, so past `stale_after_s` health.json
says so (`stale`) and the poster tells the space.

Run:  python3 scripts/eval_dashboard/health.py --data data.json --prev health.json --out health.json
      python3 scripts/eval_dashboard/health.py --replay --data data.json --step 30m
Test: cd scripts && python3 -m unittest test_eval_dashboard_health
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

HEALTH_SCHEMA_VERSION = 1

# Severity order. Index is severity: a transition "up" is towards OUTAGE.
STATES = ("GREEN", "DEGRADED", "OUTAGE")
GREEN, DEGRADED, OUTAGE = STATES
SEVERITY = {state: rank for rank, state in enumerate(STATES)}

# Condition kinds, recorded in health.json so the next tick knows which
# signature it is waiting to see clear (rule 6).
SHARED_BREAK = "shared_break"
STORM = "storm"
SETUP_DEATHS = "setup_deaths"

# Prow's job verdicts (SCHEMA.md: runs[].result). ABORTED is a superseded
# push, not a statement about the gate, and is counted nowhere below except
# the 24h tallies.
RUN_SUCCESS = "SUCCESS"
RUN_FAILURE = "FAILURE"

# Repetition verdict tokens as the collector writes them (SCHEMA.md:
# tasks[].reps[].result) and the three kinds this module sorts them into.
# `storm` is a repetition the harness could not grade.
REP_RESULT_PASS = "pass"
REP_RESULT_INFRA = "infra"
REP_PASS = "pass"
REP_FAIL = "fail"
REP_STORM = "storm"

# How old data.json may be before health.json is flagged stale. The
# collector writes `stale_after_s` when it knows its own cadence
# (SCHEMA.md, optional top-level fields); this is the renderer's default for
# when it does not. The live copy sat unrefreshed for four days in the week
# of 2026-09-04, which is what the flag exists to say out loud.
DEFAULT_STALE_AFTER = timedelta(seconds=7200)

# --- Rule 1: shared break -> OUTAGE (#1278) ----------------------------------
# Incident: after the 2026-09-06/07 weekend GKE node upgrade, seeded-a's
# e2-medium node was 100% CPU-requested by system pods, payments-api sat
# Pending on all 30 pool projects, and the crashloop trio
# (cluster-agent-crashloop-debug / -misleading-symptom / -evidence-chain)
# redded every pull request. #1171 and #1189 (2026-09-02) are the same shape
# one week earlier: compliance-rbac-overgrant and rca-remediation-pr
# collapsing on unrelated pull requests.
#
# The rule: within SHARED_BREAK_WINDOW the same admitted case failed all of
# its graded repetitions on at least SHARED_BREAK_MIN_RUNS runs from at least
# SHARED_BREAK_MIN_PRS distinct pull requests, and the set of such cases
# explains at least SHARED_BREAK_MAJORITY of the red runs in the window.
# Admitted matters: only a BOOTSTRAP_ADMITTED case reds a pull request on a
# graded failure, so a hold-out collapsing is a case problem, not a gate
# outage. Three distinct pull requests is what separates "the fixture broke"
# from "one branch broke it" -- rule 4 below is the one-PR complement.
#
# SHARED_BREAK_MIN_RED_SHARE is a tuning from the replay: the reds also have
# to be at least half of the window's concluded runs. Without it the tail of
# the 2026-09-03 storm (2026-09-04 00:00-05:00Z) read as an OUTAGE -- the
# few runs the storm did red shared their collapsed cases, but 12 of the 16
# runs in the window were green, and "don't retest" would have been wrong.
SHARED_BREAK_WINDOW = timedelta(hours=6)
SHARED_BREAK_MIN_RUNS = 3
SHARED_BREAK_MIN_PRS = 3
SHARED_BREAK_MAJORITY = 0.5
SHARED_BREAK_MIN_RED_SHARE = 0.5

# --- Rule 2: quota storm -> DEGRADED (#1225, #1097, #1214) -------------------
# Incident: the Gemini API key's fixed token quota (#1208) saturates under a
# burst of concurrent runs; the worker dies on 429s after three fast retries
# (#1225), repetitions come back as empty records ("no agent ever ran") or
# as KUBE_AGENTS_INFRA_FAILURE, and on 2026-09-03 thirteen repetitions of
# one build were lost that way while it stretched to five hours (#1214).
#
# The rule: STORM_MIN_REPS storm-classified repetitions across STORM_MIN_PRS
# distinct pull requests among the runs that finished inside STORM_WINDOW.
# A repetition is storm-classified when the harness graded it `infra`, or
# when its reason carries one of the harness's own never-ran phrasings
# (bench/kube_agents_bench/scoring.py) -- which before #1184 landed were
# graded `fail`, so the reason text is the only signal that survives in
# older runs. render.py's INFRA_REASON_KEYWORDS is the dashboard's version
# of the same list; the two are kept in step by hand.
STORM_WINDOW = timedelta(hours=2)
STORM_MIN_REPS = 15
STORM_MIN_PRS = 3
STORM_REASON_RE = re.compile(
    r"KUBE_AGENTS_INFRA_FAILURE"
    r"|no agent ever ran"
    r"|trajectory is empty"
    r"|not evidence of a real agent run"
    r"|exhausted its retries without reaching the agent"
    r"|died provisioning, before any agent ran"
    r"|http 429"
    r"|RESOURCE_EXHAUSTED"
    r"|rate.?limit",
    re.IGNORECASE,
)
# "Retest after" is the end of the storm window plus this: a run started the
# minute the last storm-hit run finished still overlaps its tail.
STORM_COOLDOWN = timedelta(minutes=30)
# A finished run carrying at least this many storm repetitions is still
# "inside the storm" for recovery purposes (rule 6): one or two infra reps
# in a run are background noise on any day and must not hold GREEN off.
STORM_RUN_SIGNATURE_REPS = 5

# --- Rule 3: setup deaths -> DEGRADED (#1172, #1176) -------------------------
# Incident: a stuck Helm release record left by ci-teardown poisoned pool
# projects, and the next pull request to lease one died at deploy with
# "UPGRADE FAILED, no deployed releases" before a single task ran.
#
# The rule: runs that recorded zero tasks, concluded FAILURE (an ABORTED
# zero-task run is a superseded push, #1179) and lasted under
# SETUP_DEATH_MAX_DURATION -- SETUP_DEATH_MIN of them inside
# SETUP_DEATH_WINDOW across SETUP_DEATH_MIN_PRS distinct pull requests. The
# distinct-PR floor is a tuning from the replay: on 2026-09-01 08:00Z one
# pull request (#1068) died seven times in an hour on its own merge
# conflict, which is that branch's problem and not the gate's.
SETUP_DEATH_MAX_DURATION = timedelta(minutes=5)
SETUP_DEATH_WINDOW = timedelta(hours=2)
SETUP_DEATH_MIN = 3
SETUP_DEATH_MIN_PRS = 2

# --- Rule 5: what GREEN reports ----------------------------------------------
METRICS_WINDOW = timedelta(hours=24)
WALL_CLOCK_PERCENTILES = (50, 90)
# How many pull requests an evidence line names before "and N more".
EVIDENCE_MAX_PRS = 6

# --- Rule 6: hysteresis ------------------------------------------------------
# Entering OUTAGE or DEGRADED needs the condition to be current, not merely
# inside the window: one of the last TRANSITION_MIN_RUNS completed full runs
# has to carry the condition's signature (the rules themselves already need
# three runs' worth of evidence). Setup deaths are exempt -- they are not
# full runs, so the count is the currency. Leaving for GREEN needs
# RECOVERY_GREEN_RUNS consecutive green runs on distinct pull requests, all
# finished after the incident began and none carrying the signature of the
# condition being left -- a single lucky green does not declare victory, and
# the runs that made the incident cannot end it (#1213 is the false-green
# risk in the other direction).
TRANSITION_MIN_RUNS = 3
RECOVERY_GREEN_RUNS = 3

# --- Roster ------------------------------------------------------------------
# The admitted roster is the source of truth for what can red a pull request
# (AGENTS.md, "The behavioural presubmit gate"). Read from the checkout by
# default; a replay over history passes --roster-history because the roster
# moved four times in the week the fixture covers.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CI_EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
ROSTER_RE = re.compile(r'BOOTSTRAP_ADMITTED="\$\{BOOTSTRAP_ADMITTED:-([^}]*)\}"')

# case-notes.yaml is the dashboard's per-case annotation file; its `issues`
# list is where the tracking issue for a broken case already lives, so the
# OUTAGE advice cites it rather than asking a human to look it up.
DEFAULT_CASE_NOTES = pathlib.Path(__file__).resolve().parent / "case-notes.yaml"

DASHBOARD_URL = "https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html"

# Cause and advice text. Plain language: the reader is whoever is deciding
# whether to type /retest.
CAUSE_SHARED_BREAK = "shared fixture/environment break: {cases}"
CAUSE_STORM = "quota storm window {start}–{end} UTC"
CAUSE_SETUP = "setup/clone failures on {count} runs ({prs})"
ADVICE_OUTAGE = "Don't retest yet; the failing cases share a cause. Tracking: {tracking}"
ADVICE_OUTAGE_NO_ISSUE = "no issue filed yet — file one with the presubmit-gate label"
ADVICE_STORM = "Retest after {when} UTC; runs started inside the storm lose repetitions to 429s."
ADVICE_SETUP = (
    "Retest once the setup failures stop; check the leased pool projects"
    " (stuck Helm release, image pulls) before spending another run."
)
ADVICE_RECOVERING = (
    "The condition has cleared; a retest is reasonable. GREEN is reported"
    " after {count} consecutive green runs on distinct PRs."
)
ADVICE_STALE = "data.json last refreshed {generated_at} ({age} ago); the dashboard refresh is stalled and this state is that old."
ADVICE_GREEN = ""

# Replay defaults.
REPLAY_STEP = timedelta(minutes=30)
STEP_RE = re.compile(r"(\d+)([mh])")
# --data and --out read and write gzip when the name says so: the replay
# fixture is a week of real data.json, ten times smaller compressed.
GZIP_SUFFIX = ".gz"
# How many characters of a repetition's reason the fixture trimmer keeps:
# every phrase STORM_REASON_RE matches sits inside the first 96 characters
# of the harness's phrasings, and the dashboard keeps 300.
TRIM_REASON_CHARS = 96

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Reading data.json
# --------------------------------------------------------------------------- #


def parse_iso(value) -> datetime | None:
    """ISO 8601 to an aware UTC datetime; None for anything unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def hhmm(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%H:%M")


def rep_kind(rep: dict) -> str:
    """pass | fail | storm for one repetition.

    `storm` is what the harness could not grade: an `infra` verdict, or a
    `fail` whose reason is one of the never-ran phrasings (graded `fail`
    before #1184, classified `infra` after it -- the text is the same).
    """
    result = rep.get("result")
    if result == REP_RESULT_PASS:
        return REP_PASS
    if result == REP_RESULT_INFRA:
        return REP_STORM
    if STORM_REASON_RE.search(rep.get("reason") or ""):
        return REP_STORM
    return REP_FAIL


class Task:
    __slots__ = ("fails", "name", "passes", "storms")

    def __init__(self, task: dict):
        self.name = task.get("name") or ""
        self.passes = self.fails = self.storms = 0
        reps = task.get("reps")
        if reps is None:
            # No per-rep detail (SCHEMA.md: absence means unknown); the
            # task's single result stands in for one repetition.
            reps = [{"result": task.get("result"), "reason": None}]
        for rep in reps:
            kind = rep_kind(rep)
            if kind == REP_PASS:
                self.passes += 1
            elif kind == REP_STORM:
                self.storms += 1
            else:
                self.fails += 1

    @property
    def collapsed(self) -> bool:
        """Failed every repetition the harness graded (rung 4's shape)."""
        return self.fails > 0 and self.passes == 0

    @property
    def graded(self) -> int:
        return self.passes + self.fails


class Run:
    __slots__ = ("build_id", "duration", "finished", "pr", "result", "started", "tasks")

    def __init__(self, run: dict):
        self.build_id = str(run.get("build_id") or "")
        self.pr = run.get("pr")
        self.started = parse_iso(run.get("started"))
        self.finished = parse_iso(run.get("finished"))
        # SCHEMA.md promises SUCCESS|FAILURE|ABORTED verbatim; Prow has also
        # written lowercase `failure` (six zero-task runs on 2026-09-05).
        self.result = (run.get("result") or "").upper()
        seconds = run.get("duration_s")
        self.duration = timedelta(seconds=seconds) if isinstance(seconds, (int, float)) else None
        self.tasks = [Task(task) for task in run.get("tasks") or []]

    @property
    def full(self) -> bool:
        return bool(self.tasks)

    @property
    def wall_clock(self) -> timedelta | None:
        if self.started and self.finished and self.finished >= self.started:
            return self.finished - self.started
        return self.duration

    @property
    def storm_reps(self) -> int:
        return sum(task.storms for task in self.tasks)

    @property
    def total_reps(self) -> int:
        return sum(task.passes + task.fails + task.storms for task in self.tasks)

    @property
    def setup_death(self) -> bool:
        return (
            not self.tasks
            and self.result == RUN_FAILURE
            and self.duration is not None
            and self.duration < SETUP_DEATH_MAX_DURATION
        )

    def collapsed_cases(self) -> set[str]:
        return {task.name for task in self.tasks if task.collapsed}

    def passing_cases(self) -> set[str]:
        return {task.name for task in self.tasks if task.passes > 0}


def load_runs(data: dict) -> list[Run]:
    """Every run with a finish time, oldest finish first."""
    runs = [Run(run) for run in data.get("runs") or []]
    return sorted((run for run in runs if run.finished), key=lambda run: run.finished)


# --------------------------------------------------------------------------- #
# The roster
# --------------------------------------------------------------------------- #


class Roster:
    """Which cases were admitted when.

    `eras` is a list of (since, admitted) oldest first; `since` None means
    "from the beginning". `at(t)` returns the roster in force at time t --
    the latest era whose `since` is not after t, or the empty set before the
    first dated era. A run is judged by the roster at its start, because a
    presubmit runs branch code that was merged with main around then.
    """

    def __init__(self, eras: list[tuple[datetime | None, frozenset[str]]]):
        self.eras = sorted(eras, key=lambda era: era[0] or datetime.min.replace(tzinfo=UTC))

    @classmethod
    def fixed(cls, admitted) -> Roster:
        return cls([(None, frozenset(admitted))])

    @classmethod
    def from_history(cls, history: list[dict]) -> Roster:
        eras = []
        for entry in history:
            eras.append((parse_iso(entry.get("since")), frozenset(entry.get("admitted") or [])))
        return cls(eras)

    @classmethod
    def from_script(cls, path: pathlib.Path = CI_EVAL_SCRIPT) -> Roster:
        match = ROSTER_RE.search(path.read_text())
        if not match:
            raise SystemExit(f"ERROR: no BOOTSTRAP_ADMITTED default found in {path}")
        return cls.fixed(name for name in match.group(1).split(",") if name)

    def at(self, when: datetime | None) -> frozenset[str]:
        current: frozenset[str] = frozenset()
        for since, admitted in self.eras:
            if since is None or (when is not None and since <= when):
                current = admitted
            else:
                break
        return current

    @property
    def current(self) -> frozenset[str]:
        return self.eras[-1][1] if self.eras else frozenset()


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #


def _prs(runs) -> list:
    """Distinct pull-request numbers, ascending, None dropped."""
    return sorted({run.pr for run in runs if run.pr is not None})


def _pr_list(prs) -> str:
    shown = ", ".join(f"#{pr}" for pr in prs[:EVIDENCE_MAX_PRS])
    extra = len(prs) - EVIDENCE_MAX_PRS
    return f"{shown} and {extra} more" if extra > 0 else shown


def _in_window(runs, now: datetime, window: timedelta):
    return [run for run in runs if now - window < run.finished <= now]


def shared_break(full_runs, now: datetime, roster: Roster) -> dict:
    """Rule 1. Returns {fires, cases, evidence, pr_caused, signature_runs}."""
    window = _in_window(full_runs, now, SHARED_BREAK_WINDOW)
    collapses: dict[str, list] = {}
    passes: dict[str, set] = {}
    for run in window:
        admitted = roster.at(run.started or run.finished)
        for case in run.collapsed_cases():
            if case in admitted:
                collapses.setdefault(case, []).append(run)
        for case in run.passing_cases():
            passes.setdefault(case, set()).add(run.pr)

    cases = sorted(
        case
        for case, runs in collapses.items()
        if len(runs) >= SHARED_BREAK_MIN_RUNS and len(_prs(runs)) >= SHARED_BREAK_MIN_PRS
    )
    # Rule 4: a case failing on exactly one pull request while passing on
    # others is that pull request's, and carries no state.
    pr_caused = []
    for case, runs in sorted(collapses.items()):
        prs = _prs(runs)
        elsewhere = passes.get(case, set()) - set(prs)
        if len(prs) == 1 and elsewhere:
            pr_caused.append(
                f"PR-caused: {case} failing only on #{prs[0]} (passing on {len(elsewhere)} other PRs)"
            )

    concluded = [run for run in window if run.result in (RUN_SUCCESS, RUN_FAILURE)]
    reds = [run for run in concluded if run.result == RUN_FAILURE]
    covered = [run for run in reds if run.collapsed_cases() & set(cases)]
    fires = (
        bool(cases)
        and bool(reds)
        and len(covered) / len(reds) >= SHARED_BREAK_MAJORITY
        and len(reds) / len(concluded) >= SHARED_BREAK_MIN_RED_SHARE
    )

    evidence = []
    for case in cases:
        runs = collapses[case]
        prs = _prs(runs)
        evidence.append(
            f"{case} failed all graded reps on {len(runs)} runs from {len(prs)} PRs ({_pr_list(prs)})"
        )
    if cases and reds:
        hours = int(SHARED_BREAK_WINDOW.total_seconds() // 3600)
        evidence.append(
            f"these cases explain {len(covered)} of {len(reds)} red runs"
            f" ({len(reds)} of {len(concluded)} concluded runs red) in the last {hours}h"
        )
    return {
        "fires": fires,
        "cases": cases,
        "evidence": evidence,
        "pr_caused": pr_caused,
        "signature_runs": {run.build_id for run in covered},
        "prs": _prs(covered),
        "runs": len(covered),
    }


def storm(full_runs, now: datetime) -> dict:
    """Rule 2. Returns {fires, reps, prs, start, end, evidence, signature_runs}."""
    window = _in_window(full_runs, now, STORM_WINDOW)
    hit = [run for run in window if run.storm_reps > 0]
    reps = sum(run.storm_reps for run in hit)
    prs = _prs(hit)
    fires = reps >= STORM_MIN_REPS and len(prs) >= STORM_MIN_PRS
    start = min((run.finished for run in hit), default=None)
    end = max((run.finished for run in hit), default=None)
    evidence = []
    if hit:
        evidence.append(
            f"quota storm: {reps} infra/empty-record reps across {len(prs)} PRs"
            f" in runs finishing {hhmm(start)}–{hhmm(end)} UTC"
        )
    return {
        "fires": fires,
        "reps": reps,
        "prs": prs,
        "start": start,
        "end": end,
        "evidence": evidence if fires else [],
        "signature_runs": {run.build_id for run in hit if run.storm_reps >= STORM_RUN_SIGNATURE_REPS},
        "runs": len(hit),
    }


def setup_deaths(runs, now: datetime) -> dict:
    """Rule 3. Returns {fires, deaths, prs, evidence}."""
    deaths = [run for run in _in_window(runs, now, SETUP_DEATH_WINDOW) if run.setup_death]
    prs = _prs(deaths)
    fires = len(deaths) >= SETUP_DEATH_MIN and len(prs) >= SETUP_DEATH_MIN_PRS
    evidence = []
    if deaths:
        evidence.append(
            f"setup/clone failures: {len(deaths)} runs under"
            f" {int(SETUP_DEATH_MAX_DURATION.total_seconds() // 60)} min with no tasks"
            f" in the last {int(SETUP_DEATH_WINDOW.total_seconds() // 3600)}h ({_pr_list(prs)})"
        )
    return {"fires": fires, "deaths": deaths, "prs": prs, "evidence": evidence}


def percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((pct / 100) * (len(ordered) - 1))
    return ordered[index]


def pr_caused_reds(full_runs, roster: Roster) -> int:
    """Rule 4 over a set of runs: red runs whose every collapsed admitted case
    collapsed on no other pull request among them. A red with no admitted
    collapse (an absolute rung, an empty record) is not this: it is the
    environment's, and the digest counts it on the infra side."""
    prs_by_case: dict[str, set] = {}
    for run in full_runs:
        admitted = roster.at(run.started or run.finished)
        for case in run.collapsed_cases() & admitted:
            prs_by_case.setdefault(case, set()).add(run.pr)
    count = 0
    for run in full_runs:
        if run.result != RUN_FAILURE:
            continue
        mine = run.collapsed_cases() & roster.at(run.started or run.finished)
        if mine and all(prs_by_case[case] == {run.pr} for case in mine):
            count += 1
    return count


def metrics(runs, now: datetime, fixtures: dict | None, roster: Roster) -> dict:
    """Rule 5: what a GREEN report and the daily digest carry."""
    window = _in_window(runs, now, METRICS_WINDOW)
    full = [run for run in window if run.full]
    concluded = [run for run in full if run.result in (RUN_SUCCESS, RUN_FAILURE)]
    green = [run for run in concluded if run.result == RUN_SUCCESS]
    walls = [run.wall_clock.total_seconds() for run in concluded if run.wall_clock]
    reps = sum(run.total_reps for run in full)
    storm_reps = sum(run.storm_reps for run in full)
    reds = len(concluded) - len(green)
    own = pr_caused_reds(full, roster)
    deaths = sum(1 for run in window if run.setup_death)
    out = {
        "window_hours": int(METRICS_WINDOW.total_seconds() // 3600),
        "full_runs": len(full),
        "prs": len(_prs(full)),
        "green_runs": len(green),
        "red_runs": reds,
        # The digest's split of the reds: the pull request's own, and
        # everything else (shared breaks, storms, empty records, setup
        # deaths) as "infra".
        "pr_caused_reds": own,
        "infra_reds": reds - own + deaths,
        "green_rate": round(len(green) / len(concluded), 3) if concluded else None,
        "aborted_runs": sum(1 for run in window if run.result not in (RUN_SUCCESS, RUN_FAILURE)),
        "setup_deaths": deaths,
        "infra_rep_rate": round(storm_reps / reps, 3) if reps else None,
        "infra_reps": storm_reps,
    }
    for pct in WALL_CLOCK_PERCENTILES:
        value = percentile(walls, pct)
        out[f"wall_clock_p{pct}_s"] = int(value) if value is not None else None
    if fixtures is not None:
        out["fixtures"] = fixtures
    return out


# --------------------------------------------------------------------------- #
# Advice
# --------------------------------------------------------------------------- #


def load_case_notes(path: pathlib.Path | None) -> dict[str, dict]:
    """case-notes.yaml's `notes` map, or {} when absent or unreadable."""
    if path is None or not path.is_file():
        return {}
    try:
        import yaml  # optional flavor, exactly as render.py treats it
    except ImportError:
        print(f"warning: pyyaml is not installed; {path} ignored, no tracking issues will be cited", file=sys.stderr)
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError:
        return {}
    notes = raw.get("notes") if isinstance(raw, dict) else None
    return notes if isinstance(notes, dict) else {}


def tracking_issues(cases: list[str], notes: dict[str, dict]) -> list[str]:
    issues: list[str] = []
    for case in cases:
        entry = notes.get(case) or {}
        for issue in entry.get("issues") or []:
            if issue not in issues:
                issues.append(str(issue))
    return issues


def advice_for(
    state: str,
    condition: str | None,
    cases: list[str],
    storm_end: datetime | None,
    notes: dict,
    recovering: bool = False,
) -> str:
    """What the reader should do. Keyed on the condition, not on whether it
    is still firing: a storm being left is still a storm, not a setup
    failure."""
    if state == GREEN:
        return ADVICE_GREEN
    if recovering:
        return ADVICE_RECOVERING.format(count=RECOVERY_GREEN_RUNS)
    if condition == SHARED_BREAK:
        issues = tracking_issues(cases, notes)
        return ADVICE_OUTAGE.format(tracking=", ".join(issues) if issues else ADVICE_OUTAGE_NO_ISSUE)
    if condition == STORM:
        when = hhmm(storm_end + STORM_COOLDOWN) if storm_end else "the storm ends"
        return ADVICE_STORM.format(when=when)
    return ADVICE_SETUP


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #


def assess(runs, now: datetime, roster: Roster) -> dict:
    """Apply rules 1-4 to the runs visible at `now`; no hysteresis yet."""
    visible = [run for run in runs if run.finished <= now]
    full_runs = [run for run in visible if run.full]
    r1 = shared_break(full_runs, now, roster)
    r2 = storm(full_runs, now)
    r3 = setup_deaths(visible, now)

    if r1["fires"]:
        state, condition = OUTAGE, SHARED_BREAK
        cause = CAUSE_SHARED_BREAK.format(cases=", ".join(r1["cases"]))
    elif r2["fires"]:
        state, condition = DEGRADED, STORM
        cause = CAUSE_STORM.format(start=hhmm(r2["start"]), end=hhmm(r2["end"]))
    elif r3["fires"]:
        state, condition = DEGRADED, SETUP_DEATHS
        cause = CAUSE_SETUP.format(count=len(r3["deaths"]), prs=_pr_list(r3["prs"]))
    else:
        state, condition, cause = GREEN, None, ""

    evidence = r1["evidence"] + r2["evidence"] + r3["evidence"] + r1["pr_caused"]
    if r1["fires"] and r2["fires"]:
        # Both true at once on 2026-09-02: the break is the state, the
        # storm is context the reader still needs.
        evidence.append(CAUSE_STORM.format(start=hhmm(r2["start"]), end=hhmm(r2["end"])) + " overlaps the break")

    # Currency for rule 6's entry check: whether one of the newest full runs
    # carries the firing condition's signature. Setup deaths are not full
    # runs; their count is their currency.
    recent = full_runs[-TRANSITION_MIN_RUNS:]
    if condition == SHARED_BREAK:
        signature = r1["signature_runs"]
    elif condition == STORM:
        signature = {run.build_id for run in full_runs if run.storm_reps > 0}
    else:
        signature = set()
    current = condition == SETUP_DEATHS or any(run.build_id in signature for run in recent)
    # The numbers behind the cause, for the one-sentence message: which
    # pull requests the firing condition touched, how many runs, and the
    # storm's window.
    if condition == SHARED_BREAK:
        incident = {"prs": r1["prs"], "runs": r1["runs"], "window_start": None, "window_end": None}
    elif condition == STORM:
        incident = {"prs": r2["prs"], "runs": r2["runs"], "window_start": iso(r2["start"]), "window_end": iso(r2["end"])}
    elif condition == SETUP_DEATHS:
        incident = {"prs": r3["prs"], "runs": len(r3["deaths"]), "window_start": None, "window_end": None}
    else:
        incident = None
    return {
        "state": state,
        "condition": condition,
        "cause": cause,
        "failing_cases": r1["cases"] if r1["fires"] else [],
        "evidence": evidence,
        "storm_end": r2["end"] if r2["fires"] else None,
        "incident": incident,
        "current": current,
        "full_runs": full_runs,
        "last_setup_death": max((run.finished for run in visible if run.setup_death), default=None),
        "roster": roster,
    }


def recovered(full_runs, prev: dict, since: datetime, last_setup_death: datetime | None, roster: Roster) -> bool:
    """Rule 6's exit: the last RECOVERY_GREEN_RUNS full runs are green, on
    distinct pull requests, all finished after the incident began, and none
    carries the signature of the condition being left -- a collapse of one
    of its cases for a shared break, STORM_RUN_SIGNATURE_REPS storm
    repetitions for a storm, a setup death after it for setup deaths. Judged
    from the runs themselves rather than from the rule's window, so the runs
    that constituted the incident never count as its recovery once the
    window has rolled past them."""
    recent = full_runs[-RECOVERY_GREEN_RUNS:]
    if len(recent) < RECOVERY_GREEN_RUNS:
        return False
    if any(run.result != RUN_SUCCESS or run.finished <= since for run in recent):
        return False
    if len(_prs(recent)) < RECOVERY_GREEN_RUNS:
        return False
    condition = prev.get("condition")
    # The cases the incident named, but only while they are still admitted:
    # demoting the broken case is the documented fix for a rung-4 shared
    # break (hack/ci-eval-pr.sh), and once it is a hold-out its collapses
    # red nobody, so they cannot hold the gate in OUTAGE either.
    cases = set(prev.get("failing_cases") or [])
    carries = {
        SHARED_BREAK: lambda run: bool(run.collapsed_cases() & cases & roster.at(run.started or run.finished)),
        STORM: lambda run: run.storm_reps >= STORM_RUN_SIGNATURE_REPS,
        SETUP_DEATHS: lambda run: last_setup_death is not None and run.finished <= last_setup_death,
    }.get(condition, lambda run: False)
    return not any(carries(run) for run in recent)


def transition(prev: dict | None, assessed: dict, now: datetime) -> dict:
    """Rule 6: reconcile the raw assessment with the previous state.

    Returns {state, condition, cause, failing_cases, since, recovering}.
    """
    raw_state = assessed["state"]
    if not prev or prev.get("state") not in SEVERITY:
        # First tick ever: take the assessment, with the same currency bar
        # a transition up would need.
        if raw_state != GREEN and not assessed["current"]:
            return _keep(GREEN, None, "", [], now, recovering=False)
        return _keep(raw_state, assessed["condition"], assessed["cause"], assessed["failing_cases"], now, recovering=False)

    prev_state = prev["state"]
    since = parse_iso(prev.get("since")) or now
    up = SEVERITY[raw_state] > SEVERITY[prev_state]
    down = SEVERITY[raw_state] < SEVERITY[prev_state]

    if up:
        if assessed["current"]:
            return _keep(raw_state, assessed["condition"], assessed["cause"], assessed["failing_cases"], now, recovering=False)
        return _keep(prev_state, prev.get("condition"), prev.get("cause") or "", prev.get("failing_cases") or [], since, recovering=bool(prev.get("recovering")))

    if not down:
        # Same severity. The cause follows the evidence (a second case
        # joining a break changes what the reader should be told), but a
        # condition that has stopped firing does not reset `since`.
        return _keep(raw_state, assessed["condition"], assessed["cause"], assessed["failing_cases"], since, recovering=False)

    # Down. Leaving OUTAGE for a lesser live condition is immediate: the
    # break cleared and the storm is what is left. Leaving for GREEN waits
    # for the recovery bar.
    if raw_state != GREEN:
        return _keep(raw_state, assessed["condition"], assessed["cause"], assessed["failing_cases"], now, recovering=False)
    prev_condition = prev.get("condition")
    if recovered(assessed["full_runs"], prev, since, assessed["last_setup_death"], assessed["roster"]):
        return _keep(GREEN, None, "", [], now, recovering=False)
    return _keep(prev_state, prev_condition, prev.get("cause") or "", prev.get("failing_cases") or [], since, recovering=True)


def _keep(state, condition, cause, cases, since, recovering):
    return {
        "state": state,
        "condition": condition,
        "cause": cause,
        "failing_cases": list(cases),
        "since": since,
        "recovering": recovering,
    }


def adjudicate(
    data: dict,
    now: datetime,
    prev: dict | None,
    roster: Roster,
    fixtures: dict | None = None,
    notes: dict | None = None,
    runs: list | None = None,
    wall_clock: datetime | None = None,
) -> dict:
    """data.json + previous health.json -> health.json (as a dict).

    `now` is the data's horizon, the instant every window is measured from.
    `wall_clock`, when given, is compared against it for staleness; a replay
    or a pinned `--now` passes None and is never stale.
    """
    if runs is None:
        runs = load_runs(data)
    assessed = assess(runs, now, roster)
    decided = transition(prev, assessed, now)
    evidence = list(assessed["evidence"])
    if decided["recovering"]:
        evidence.append(
            f"condition cleared; waiting for {RECOVERY_GREEN_RUNS} consecutive green runs"
            " on distinct PRs before reporting GREEN"
        )
    elif decided["state"] != assessed["state"] and SEVERITY[assessed["state"]] > SEVERITY[decided["state"]]:
        evidence.append(f"{assessed['state']} condition seen but not yet current; holding {decided['state']}")

    advice = advice_for(
        decided["state"], decided["condition"], decided["failing_cases"], assessed["storm_end"], notes or {}, decided["recovering"]
    )
    stale_after = DEFAULT_STALE_AFTER
    if isinstance(data.get("stale_after_s"), (int, float)):
        stale_after = timedelta(seconds=data["stale_after_s"])
    age = wall_clock - now if wall_clock is not None else None
    stale = age is not None and age > stale_after
    if stale:
        note = ADVICE_STALE.format(generated_at=iso(now), age=f"{int(age.total_seconds() // 3600)}h")
        evidence.append(note)
        advice = f"{note} {advice}".strip()
    # A held state (recovering, or a worse condition not yet current) keeps
    # the previous tick's numbers: the assessment's incident describes the
    # raw state, not the one being reported.
    incident = assessed["incident"] if decided["state"] == assessed["state"] else (prev or {}).get("incident")
    out = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "state": decided["state"],
        "condition": decided["condition"],
        "since": iso(decided["since"]),
        "cause": decided["cause"],
        "failing_cases": decided["failing_cases"],
        "tracking_issues": tracking_issues(decided["failing_cases"], notes or {}),
        "incident": incident,
        "evidence": evidence,
        "advice": advice,
        "recovering": decided["recovering"],
        "stale": stale,
        "metrics": metrics([run for run in runs if run.finished <= now], now, fixtures, roster),
        "dashboard_url": DASHBOARD_URL,
        "generated_at": iso(now),
    }
    if age is not None:
        out["metrics"]["data_age_s"] = int(age.total_seconds())
    return out


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


def replay(data: dict, step: timedelta, roster: Roster, start: datetime | None = None, end: datetime | None = None, notes: dict | None = None):
    """Yield (now, health) for every tick as if the job had run on schedule.

    Ticks are aligned to the step from the first run's finish (or `start`),
    up to the last run's finish (or `end`), inclusive of the first tick at
    or after `end`.
    """
    runs = load_runs(data)
    if not runs:
        return
    first = start or runs[0].finished
    last = end or runs[-1].finished
    now = first
    prev = None
    while True:
        health = adjudicate(data, now, prev, roster, notes=notes, runs=runs)
        yield now, health
        prev = health
        if now >= last:
            break
        now += step


def timeline(ticks, every: bool = False) -> list[dict]:
    """The state changes in a replay: [{at, state, cause, failing_cases}].

    A change is a new state, a new condition, or a new set of failing
    cases within an OUTAGE -- the things the poster reacts to. A storm
    window's bounds move every tick and are detail, not a change. `every`
    keeps all ticks.
    """
    out = []
    last = None
    for now, health in ticks:
        key = (health["state"], health["condition"], tuple(health["failing_cases"]))
        if every or key != last:
            out.append(
                {
                    "at": iso(now),
                    "state": health["state"],
                    "condition": health["condition"],
                    "cause": health["cause"],
                    "failing_cases": health["failing_cases"],
                    "recovering": health["recovering"],
                }
            )
            last = key
    return out


def format_timeline(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        flag = " (recovering)" if entry["recovering"] else ""
        lines.append(f"{entry['at']}  {entry['state']:<8}  {entry['cause']}{flag}")
    return "\n".join(lines)


def trim(data: dict, start: datetime, end: datetime, source: str) -> dict:
    """A data.json reduced to the fields this module reads, for a fixture.

    Runs that finished in [start, end); per run build_id, pr, started,
    finished, result, duration_s and tasks; per task name, result and reps;
    per rep result and the first TRIM_REASON_CHARS of the reason (null for
    passing reps, as the collector writes them).
    """
    runs = []
    for run in data.get("runs") or []:
        finished = parse_iso(run.get("finished"))
        if finished is None or not (start <= finished < end):
            continue
        tasks = []
        for task in run.get("tasks") or []:
            trimmed = {"name": task.get("name"), "result": task.get("result")}
            if task.get("reps") is not None:
                trimmed["reps"] = [
                    {
                        "result": rep.get("result"),
                        "reason": (rep.get("reason") or None) and rep["reason"][:TRIM_REASON_CHARS],
                    }
                    for rep in task["reps"]
                ]
            tasks.append(trimmed)
        runs.append(
            {
                "build_id": run.get("build_id"),
                "pr": run.get("pr"),
                "started": run.get("started"),
                "finished": run.get("finished"),
                "result": run.get("result"),
                "duration_s": run.get("duration_s"),
                "tasks": tasks,
            }
        )
    runs.sort(key=lambda run: run["finished"])
    return {
        "schema_version": data.get("schema_version"),
        "generated_at": data.get("generated_at"),
        "trimmed": {
            "source": source,
            "from": iso(start),
            "to": iso(end),
            "reason_chars": TRIM_REASON_CHARS,
            "fields": "the fields scripts/eval_dashboard/health.py reads; see SCHEMA.md, Fixtures",
        },
        "runs": runs,
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_step(text: str) -> timedelta:
    match = STEP_RE.fullmatch(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(f"step must look like 30m or 1h, got {text!r}")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount)


def parse_when(text: str) -> datetime:
    parsed = parse_iso(text)
    if parsed is None:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}")
    return parsed


def load_json(path: pathlib.Path | None) -> dict | None:
    """A JSON object from `path`, gzip-compressed when the name ends in .gz
    (how the replay fixture is stored); None when missing or unreadable."""
    if path is None or not path.is_file():
        return None
    try:
        if path.suffix == GZIP_SUFFIX:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                loaded = json.load(handle)
        else:
            loaded = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"warning: {path}: {exc}; ignoring", file=sys.stderr)
        return None
    return loaded if isinstance(loaded, dict) else None


def write_text(path: pathlib.Path, text: str) -> None:
    """`text` to `path`, gzip-compressed when the name ends in .gz."""
    if path.suffix == GZIP_SUFFIX:
        with gzip.open(path, "wt", encoding="utf-8") as handle:
            handle.write(text)
    else:
        path.write_text(text)


def build_roster(args) -> Roster:
    if args.roster_history:
        history = json.loads(pathlib.Path(args.roster_history).read_text())
        return Roster.from_history(history)
    if args.admitted is not None:
        return Roster.fixed(name for name in args.admitted.split(",") if name)
    return Roster.from_script(args.ci_eval_script)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=pathlib.Path, required=True, help="data.json (schema v1)")
    parser.add_argument("--prev", type=pathlib.Path, help="the previous health.json (missing is fine)")
    parser.add_argument("--out", type=pathlib.Path, help="where to write health.json (default: stdout)")
    parser.add_argument(
        "--now",
        type=parse_when,
        help="evaluate as of this time and skip the staleness check (default: data.json's generated_at, aged against the wall clock)",
    )
    parser.add_argument("--fixture-status", type=pathlib.Path, help="optional fixtures.json to surface in metrics")
    parser.add_argument("--case-notes", type=pathlib.Path, default=DEFAULT_CASE_NOTES, help="case-notes.yaml for tracking issues")
    roster = parser.add_mutually_exclusive_group()
    roster.add_argument("--admitted", help="comma-separated admitted roster (default: BOOTSTRAP_ADMITTED in hack/ci-eval-pr.sh)")
    roster.add_argument("--roster-history", help="JSON [{since, admitted[]}] of roster eras, for replay over history")
    parser.add_argument("--ci-eval-script", type=pathlib.Path, default=CI_EVAL_SCRIPT, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replay", action="store_true", help="walk the data as if the job had run every --step; print the timeline")
    mode.add_argument("--trim", action="store_true", help="write a fixture: the runs in [--from, --to) reduced to the fields read here")
    parser.add_argument("--step", type=parse_step, default=REPLAY_STEP, help="replay tick (default 30m)")
    parser.add_argument("--from", dest="start", type=parse_when, help="replay/trim start (default: first run)")
    parser.add_argument("--to", dest="end", type=parse_when, help="replay/trim end (default: last run)")
    parser.add_argument("--every", action="store_true", help="replay: print every tick, not only changes")
    parser.add_argument("--json", action="store_true", help="replay: print the timeline as JSON")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data = load_json(args.data)
    if data is None:
        print(f"ERROR: {args.data} is not a readable JSON object", file=sys.stderr)
        return 1
    roster = build_roster(args)
    notes = load_case_notes(args.case_notes)

    if args.trim:
        if not (args.start and args.end):
            print("ERROR: --trim needs --from and --to", file=sys.stderr)
            return 1
        source = f"{args.data.name} generated_at {data.get('generated_at')}"
        text = json.dumps(trim(data, args.start, args.end, source), separators=(",", ":")) + "\n"
    elif args.replay:
        entries = timeline(replay(data, args.step, roster, args.start, args.end, notes), every=args.every)
        text = json.dumps(entries, indent=2) + "\n" if args.json else format_timeline(entries) + "\n"
    else:
        wall_clock = datetime.now(UTC)
        now = args.now or parse_iso(data.get("generated_at")) or wall_clock
        health = adjudicate(
            data,
            now,
            load_json(args.prev),
            roster,
            load_json(args.fixture_status),
            notes,
            wall_clock=None if args.now else wall_clock,
        )
        text = json.dumps(health, indent=2) + "\n"

    if args.out:
        write_text(args.out, text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
