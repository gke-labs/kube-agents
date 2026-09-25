#!/usr/bin/env python3
"""Adjudicate the presubmit gate's health from the eval dashboard's data.json.

Every gate incident in the week of 2026-09-01 was diagnosed by hand with the
same mechanical procedure: open the dashboard, find the reds of the last few
hours, ask whether the same cases failed on unrelated pull requests (a shared
fixture broke -- #1278), whether the repetitions were lost to 429s and empty
records rather than graded (a quota storm -- #1225, #1097), whether runs
died before any task ran (setup failures), whether the build cluster lost
the node the pod was on (lost pods -- #1478), or whether Prow killed the
runs at the job deadline with nothing graded (deadline kills -- #1894).
Nobody derives that from a heatmap at 8am, so this turns the procedure into
a job.

It is a pure function: data.json (schema v1, SCHEMA.md) plus the previously
written health.json in -- and, when the hourly seeded-fleet scan has
published one, fixture-state.json (scripts/eval_dashboard/fixture_state.py)
-- health.json out::

    {state, since, cause, failing_cases, evidence, advice, slow, pool, metrics, generated_at}

`state` is GREEN, DEGRADED or OUTAGE. The rules are the module-level
constants below -- each names the incident it was tuned on -- and the state
machine that applies hysteresis to them is `transition`. The previous state
is an input (`--prev`) rather than something remembered, so the scheduled
job that runs this is stateless between ticks. `slow` and `pool` are the
findings that are not states: green runs taking far longer than usual
(rule 7, #1586), and runs waiting in a queue before they start (rule 8,
#1607, read from the pool-pressure periodic's artifact rather than measured
here). The poster sends each once and the Brief shows them, while the state
stays what the rules above say.

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
import math
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from . import fixture_state, tiers
except ImportError:  # run as a script: python3 scripts/eval_dashboard/health.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import fixture_state
    import tiers
try:
    import eval_rosters
except ImportError:  # run as a script: scripts/ is not on sys.path yet
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import eval_rosters

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
LOST_PODS = "lost_pods"
FIXTURE_DRIFT = "fixture_drift"
# A ceiling wave: enough repetitions across enough pull requests ended at the
# harness's delegation wait with the worker still running (rule 2b, #1874).
DELEGATION_CEILING = "delegation_ceiling"
# Rule 3d (#1894): runs Prow killed at the job deadline with no verdict.
DEADLINE_KILL = "deadline_kill"
# The conditions whose evidence is a count rather than a full run's
# signature -- runs that never became one, runs Prow killed, or the fleet
# scan's verdict; rule 6's entry check takes the count itself as currency.
COUNTED_CONDITIONS = (SETUP_DEATHS, LOST_PODS, FIXTURE_DRIFT, DEADLINE_KILL)

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
# The fourth kind: a repetition the harness stopped watching with its worker
# still running -- the delegation wait (AGENT_DELEGATION_TIMEOUT) ran out
# before anything was delivered. The scorer grades it `infra` under a reason
# that leads with this marker (bench/kube_agents_bench/scoring.py, #1874).
# Not a storm rep: the agent ran and nothing was lost to 429s, so it stays out
# of rule 2's count and of every pass-rate denominator; rule 2b below counts
# it on its own. test_eval_dashboard_health.py reads the literal out of the
# scorer so the two cannot drift.
REP_CEILING = "ceiling"
DELEGATION_CEILING_MARKER = "KUBE_AGENTS_DELEGATION_CEILING"

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

# --- Rule 2b: delegation ceiling -> DEGRADED (#1874, #1879) -------------------
# Incident: the platform agent's kanban dispatcher stalls under load ("ready
# queue non-empty ... 0 workers spawned", 23 warnings on the 2026-09-21
# nightly, #1879), or workers run past the harness's wait. Every repetition
# that reaches AGENT_DELEGATION_TIMEOUT with nothing delivered is a `ceiling`
# rep (REP_CEILING above): not graded and not a storm rep, so on its own it
# is neither a collapse for rule 1 nor a count for rule 2, and the suite
# reports the run NOT EVALUATED. Without this rule a fleet-wide stall read
# GREEN here while every pull request was told to rerun: visible on each
# run, named nowhere.
#
# The rule is rule 2's shape over ceiling reps, same thresholds and window:
# CEILING_MIN_REPS across CEILING_MIN_PRS distinct pull requests among the
# runs that finished inside CEILING_WINDOW. Three PRs because one project's
# worker can be slow on one PR's task without saying anything about the
# fleet; fifteen reps because one or two per run is a slow worker on a slow
# day, while five per run three times over is the 2026-09-19 nightly's shape
# (13 of 116) spread across presubmits. Ranked below the storm on purpose: a
# 429-starved worker hits the same wait, so when both fire the quota is the
# cause and the ceiling wave is evidence beside it.
CEILING_WINDOW = STORM_WINDOW
CEILING_MIN_REPS = STORM_MIN_REPS
CEILING_MIN_PRS = STORM_MIN_PRS
# A finished run carrying at least this many ceiling repetitions is still
# inside the wave for recovery purposes (rule 6), as STORM_RUN_SIGNATURE_REPS
# is for a storm.
CEILING_RUN_SIGNATURE_REPS = STORM_RUN_SIGNATURE_REPS

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
# conflict, which is that branch's problem and not the gate's. A run the
# collector recorded as a conflicted merge is excluded outright (#1608),
# which is the same judgement without needing a second pull request.
SETUP_DEATH_MAX_DURATION = timedelta(minutes=5)
SETUP_DEATH_WINDOW = timedelta(hours=2)
SETUP_DEATH_MIN = 3
SETUP_DEATH_MIN_PRS = 2

# --- Rule 3b: lost pods -> DEGRADED (#1478) ----------------------------------
# Incident: on 2026-09-11 14:03-14:17Z five nodes of the Prow build cluster
# went NotReady and twelve runs on twelve pull requests died mid-run, some of
# them two hours in. Each left finished.json (`failure`), a podinfo.json
# whose last event is NodeNotReady, and no build-log.txt; rule 3 saw the one
# that was under five minutes old and blamed the pool projects.
#
# The rule: a run is a lost pod when it concluded FAILURE with no tasks and
# either its pod's last event was NodeNotReady or it has no build log
# (SCHEMA.md: runs[].pod_last_event, runs[].has_build_log; a run collected
# before those fields existed is unknown and never one). LOST_POD_MIN of them
# finishing within LOST_POD_SPAN of each other, among the runs that finished
# inside LOST_POD_WINDOW, is the condition; LOST_POD_EVENT_MIN of them is a
# build-cluster event and is announced as one. No distinct-PR floor: the pod
# record already says it was the node, not the branch. A lost pod is never
# also a setup death, whatever its duration -- each zero-task run is counted
# in exactly one class. Ranked above the storm because its evidence is the
# most mechanical and it has an owner to page.
LOST_POD_WINDOW = timedelta(hours=2)
LOST_POD_SPAN = timedelta(minutes=30)
LOST_POD_MIN = 3
LOST_POD_EVENT_MIN = 8
POD_EVENT_NODE_NOT_READY = "NodeNotReady"

# --- Rule 3d: deadline kills -> OUTAGE (#1894) -------------------------------
# Incident: from the evening of 2026-09-22 (UTC) the Hermes bump wedged the
# workers (#1880) and presubmit runs were killed at Prow's 360m deadline with
# every unit at the delegation ceiling and nothing graded -- 33 kills against
# 10 verdicts in the fixture's 32 hours -- and the bot stayed GREEN: a
# deadline death after a successful deploy matched no rule.
#
# The rule: a run is a deadline kill when it concluded FAILURE with no eval
# verdict, is neither a lost pod nor a conflicted merge, and lasted at least
# PROW_JOB_TIMEOUT minus DEADLINE_KILL_MARGIN. Not "no tasks": since #1875 the
# harness records cases as they finish, so a killed run may carry graded
# tasks and still no verdict. DEADLINE_KILL_MIN of them finishing inside
# DEADLINE_KILL_WINDOW on DEADLINE_KILL_MIN_PRS distinct pull requests is an
# OUTAGE -- the gate can pass nobody -- ranked below a shared break, which
# names cases, and above every DEGRADED condition. Recovery is
# RECOVERY_GREEN_RUNS runs WITH A VERDICT, green or red, on distinct pull
# requests after the last kill: a red that graded proves the gate grades.
#
# The timeout is the job's decoration_config.timeout in oss-test-infra
# (prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml); data.json
# does not carry it. The margin covers the 362-365 minutes Prow records for
# a killed run without reaching a long green.
PROW_JOB_TIMEOUT = timedelta(minutes=360)
DEADLINE_KILL_MARGIN = timedelta(minutes=15)
DEADLINE_KILL_WINDOW = timedelta(hours=2)
DEADLINE_KILL_MIN = 3
DEADLINE_KILL_MIN_PRS = 2
# The lost-pod advice names the loss on the reader's clock, the way
# post_health.py writes every time a person reads (docs/ci-health.md, Times);
# zone and label are fixed together there and mirrored here rather than
# imported, because this module is the pure function the replay and the
# tests run standalone and imports nothing that talks to Chat or GitHub.
READER_TZ = ZoneInfo("America/Toronto")
READER_TZ_LABEL = "ET"
NOON = 12

# --- Rule 3c: fixture drift -> DEGRADED (#1550, #1544) -----------------------
# Incident: #1278 again, seen from the fleet's side. The presence probes kept
# passing while every slot-a fixture sat Pending; the designed-state check
# (hack/fleet-fixture-state.py) is what would have said so, and the CI health
# workflow now runs it against every pool project once an hour and publishes
# fixture-state.json beside health.json (scripts/eval_dashboard/
# fixture_state.py). Nothing in the presubmit runs this check and nothing
# acts on a drift (decision 2026-09-14: evals v1 detects, does not act), so a drifted role
# reds the cases that depend on it on the runs that lease those projects;
# the run-based rules above see that red as it happens, and this rule names
# the cause and its owner (the fleet's reconcile). DEGRADED, not an OUTAGE:
# it is a few cases on some projects, with the fix in someone's hands.
#
# The rule: the same role drifted on the same project in
# FIXTURE_DRIFT_CONSECUTIVE_SCANS consecutive scans, or on
# FIXTURE_DRIFT_MIN_PROJECTS projects in one scan. One scan on one project is
# not enough: in #1278's retest sweep the crashloop fixture lagged the node
# repair by about 40 minutes on one project (it needs its first restart
# before OOMKilled evidence exists), and one hourly scan can land inside that
# window; the second scan is an hour later. Three projects at once is the
# fleet-wide shape (#1278 was all 30) and waits for nothing. A scan older than
# FIXTURE_STATE_MAX_AGE (three missed hourly scans) is the scan job being
# broken, not the fleet, and is ignored with a note. A scan that could check
# no project at all (the bot's grant missing) is `unknown`: said once by the
# poster, never a drift. Ranked below every run-based condition, because
# those red a run and this one does not.
FIXTURE_DRIFT_MIN_PROJECTS = 3
FIXTURE_DRIFT_CONSECUTIVE_SCANS = 2
FIXTURE_STATE_MAX_AGE = timedelta(hours=3)
# How many projects an evidence line names before "and N more".
EVIDENCE_MAX_PROJECTS = 4

# --- Rule 5: what GREEN reports ----------------------------------------------
METRICS_WINDOW = timedelta(hours=24)
WALL_CLOCK_PERCENTILES = (50, 90)
# How many pull requests an evidence line names before "and N more".
EVIDENCE_MAX_PRS = 6

# --- Rule 6: hysteresis ------------------------------------------------------
# Entering OUTAGE or DEGRADED needs the condition to be current, not merely
# inside the window: one of the last TRANSITION_MIN_RUNS completed full runs
# has to carry the condition's signature (the rules themselves already need
# three runs' worth of evidence). COUNTED_CONDITIONS are exempt -- setup
# deaths, lost pods, deadline kills and fixture drift have no signature on a
# full run, so the count is the currency. Leaving for GREEN needs
# RECOVERY_GREEN_RUNS consecutive green runs on distinct pull requests, all
# finished after the incident began and none carrying the signature of the
# condition being left -- a single lucky green does not declare victory, and
# the runs that made the incident cannot end it (#1213 is the false-green
# risk in the other direction). A deadline-kill OUTAGE is left on the same
# count of runs WITH A VERDICT, green or red (`recovered`).
TRANSITION_MIN_RUNS = 3
RECOVERY_GREEN_RUNS = 3

# --- Rule 7: slow gate -> a note beside the state, never a state (#1586) ------
# Incident: on 2026-09-14 Vertex latency (per call p90 21 s, p99 84 s in a
# sampled run; every 429 retried successfully) stretched full runs from a
# typical 150 minutes to 175-215 while all of them stayed green. Rules 1-3b
# see nothing: no repetition was lost, no case was shared, and the 23 runs in
# flight never reach this module, which reads finished runs only. So the
# slowness is a note in health.json (`slow`), posted once per episode and
# shown on the Brief, and the state stays whatever the rules say: nothing is
# broken and /retest does not help.
#
# The rule: the median wall clock of the newest SLOW_RUNS full runs -- a
# concluded run of at least SLOW_MIN_TASKS cases (one fewer than the
# presubmit file lists, read from hack/eval/presubmit-cases.txt: 11 for the
# twelve cases the presubmit runs since 2026-09-22, when it became the
# blocking roster only, #1023, having run 18-19 before; a run Prow cut short
# at its ceiling recorded fewer and is not one; the floor was a literal 15
# until the roster shrank under it, which would have made every run since a
# partial one and the rule silent, so it follows the file and sits one
# demotion below the roster on purpose, an admission or a demotion moving
# it without an edit here) -- all of
# them finished inside SLOW_WINDOW, is at least SLOW_FACTOR times the median
# of the full runs of the trailing SLOW_BASELINE before them, given at least
# SLOW_BASELINE_MIN_RUNS of those. Medians rather than the p90 the issue
# proposed: replayed over the published data.json, "three consecutive runs
# above the seven-day p90" never fired on 2026-09-14 (the p90 stood at 198
# minutes because 09-08 to 09-11 had been slow too, and 3 of the day's 12
# finished runs cleared it), while the 1.2x median fired from 18:00Z and
# stayed quiet over 09-06 to 09-09 and the 09-12/13 weekend. Once slow, the
# note holds until the median is back under SLOW_CLEAR_FACTOR, so a ratio
# hovering at the bar is one episode rather than a note every tick.
SLOW_RUNS = 5
SLOW_WINDOW = timedelta(hours=6)


def _slow_min_tasks(default: int = 11) -> int:
    """One fewer than the presubmit file's case count; `default` when the
    file cannot be read (a checkout without hack/eval/, an old era)."""
    try:
        cases = eval_rosters.presubmit_cases()
    except (OSError, ValueError):
        return default
    return max(1, len(cases) - 1) if cases else default


SLOW_MIN_TASKS = _slow_min_tasks()
SLOW_BASELINE = timedelta(days=7)
SLOW_BASELINE_MIN_RUNS = 20
SLOW_FACTOR = 1.2
SLOW_CLEAR_FACTOR = 1.1

# --- Rule 8: a backed-up pool -> a note beside the state, never a state (#1607)
# Runs are timed from their start, so nothing else here sees a run that sat in
# the queue first. The pool-pressure periodic (#1069) measures that hourly and
# already grades itself against the runbook's thresholds; this rule reads its
# verdict rather than deriving a second one.
#
# A note, not a state: the runs still pass, they just start late, and DEGRADED
# would tell people to retest, which lengthens the queue being reported.
# Unlike rule 7 it is not held back outside GREEN -- a different job reading
# different data cannot be the incident's own symptom, and when leases are the
# incident the wait is the explanation.
#
# POOL_STALE_AFTER is the dead-man's switch: latest-build.txt keeps resolving
# after the periodic dies, so a stopped job reads as an unchanging healthy
# artifact. Two missed hourly runs plus the job's 30m timeout is 2.5h, rounded
# up so a run that starts late is not a stale reading.
POOL_STALE_AFTER = timedelta(hours=3)
POOL_BREACH = "BREACH"
POOL_UNMEASURED = "UNMEASURED"
POOL_STALE = "STALE"
SECONDS_PER_MINUTE = 60
# Longer than a queued run can really have waited: the sweep only looks back a
# week. Past it the figure is corrupt, and dating a backlog from it puts the
# start outside the range a datetime can hold.
POOL_MAX_WAIT = timedelta(days=30)
# pool_pressure.py buckets waits by UTC calendar day and emits no row for a day
# with no runs, so the newest row is not always the newest day. "Last 24h" from
# window_end spans two such days; a row older than that is a different day's
# number under the digest's heading.
POOL_DIGEST_DAYS = 2
POOL_DAY_FORMAT = "%Y-%m-%d"

# --- Roster ------------------------------------------------------------------
# The admitted roster is the source of truth for what can red a pull request
# (AGENTS.md, "The behavioural presubmit gate"). Read from the checkout's
# hack/eval/blocking-roster.txt by default; a replay over history passes
# --roster-history because the roster moved four times in the week the
# fixture covers. The roster lived in hack/ci-eval-pr.sh's BOOTSTRAP_ADMITTED
# line until 2026-09-15 (#1546); Roster.from_script_text reads that shape, so
# a `git show <old-commit>:hack/ci-eval-pr.sh` resolves an era before the move.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BLOCKING_ROSTER_FILE = eval_rosters.BLOCKING_ROSTER_FILE

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
CAUSE_LOST_PODS = "lost pods: {count} runs on {prs} PRs died with their build node {start}–{end} UTC"
CAUSE_FIXTURE_DRIFT = "seeded fixture drift: {roles} out of designed state on {projects} pool project(s)"
CAUSE_CEILING = "delegation ceiling: {reps} repetitions on {prs} PRs ended with the worker still running {start}–{end} UTC"
CAUSE_DEADLINE_KILL = "deadline kills: {count} runs on {prs} PRs killed at the {minutes}-minute deadline with no verdict {start}–{end} UTC"
ADVICE_OUTAGE = "Don't retest yet; the failing cases share a cause. Tracking: {tracking}"
ADVICE_OUTAGE_NO_ISSUE = "no issue filed yet — file one with the presubmit-gate label"
ADVICE_STORM = "Retest after {when} UTC; runs started inside the storm lose repetitions to 429s."
ADVICE_SETUP = (
    "Retest once the setup failures stop; check the leased pool projects"
    " (stuck Helm release, image pulls) before spending another run."
)
ADVICE_LOST_PODS = (
    "The Prow build cluster lost node(s) {nodes} at {when}; {count} runs died mid-run."
    " Nothing about your change; /retest when the new jobs are progressing."
    " Cluster owner: check the node events and autorepair."
)
UNKNOWN_NODE = "(name unknown)"
ADVICE_FIXTURE_DRIFT = (
    "A red on a case that depends on {roles} from a run that leased {projects} is the fixture, not your change;"
    " retest once the fleet owner has re-applied bench/tf/fleet there (README, State and reconcile)."
)
ADVICE_CEILING = (
    "Retest once workers are finishing again; those runs read NOT EVALUATED, not red."
    " platform-agent-gateway.log in a run's artifacts says whether the dispatcher stalled (#1879) or 429s starved the workers."
)
ADVICE_DEADLINE_KILL = (
    "Don't retest; runs are being killed at the {minutes}-minute deadline before anything is graded,"
    " so nothing can pass. Tracking: {tracking}"
)
ADVICE_RECOVERING = (
    "The condition has cleared; a retest is reasonable. GREEN is reported"
    " after {count} consecutive green runs on distinct PRs."
)
# Leaving a deadline-kill outage: a verdict either way proves the gate grades
# (`recovered`); the evidence line names the bar with recovery_bar().
RECOVERY_BAR_GREEN = "green runs"
RECOVERY_BAR_VERDICT = "runs with a verdict"
ADVICE_RECOVERING_VERDICT = (
    "The condition has cleared; a retest is reasonable. GREEN is reported once"
    " the newest {count} runs with a verdict, green or red, are on distinct PRs"
    " and all finished after the last kill."
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
# The optional run fields (SCHEMA.md) the trimmer carries when the source has
# them; absent stays absent: how the build ended, and the eval's own verdict,
# which is what tells a deadline kill from a long red.
ENDED_FIELDS = ("has_build_log", "pod_phase", "pod_node", "pod_last_event", "merge_conflict", "eval_verdict")

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


def reader_clock(value: datetime | None) -> str:
    """"10:05 AM ET": the time on the reader's clock, as post_health.py's
    `clock` writes it; "?" for none."""
    if not value:
        return "?"
    local = value.astimezone(READER_TZ)
    return f"{local.hour % NOON or NOON}:{local.minute:02d} {'AM' if local.hour < NOON else 'PM'} {READER_TZ_LABEL}"


def rep_kind(rep: dict) -> str:
    """pass | fail | storm for one repetition.

    `storm` is what the harness could not grade: an `infra` verdict, or a
    `fail` whose reason is one of the never-ran phrasings (graded `fail`
    before #1184, classified `infra` after it -- the text is the same).
    """
    result = rep.get("result")
    if result == REP_RESULT_PASS:
        return REP_PASS
    if DELEGATION_CEILING_MARKER in (rep.get("reason") or ""):
        return REP_CEILING
    if result == REP_RESULT_INFRA:
        return REP_STORM
    if STORM_REASON_RE.search(rep.get("reason") or ""):
        return REP_STORM
    return REP_FAIL


class Task:
    __slots__ = ("ceilings", "fails", "name", "passes", "storms")

    def __init__(self, task: dict):
        self.name = task.get("name") or ""
        self.passes = self.fails = self.storms = self.ceilings = 0
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
            elif kind == REP_CEILING:
                self.ceilings += 1
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
    __slots__ = ("build_id", "duration", "eval_verdict", "eval_verdict_recorded", "finished", "has_build_log", "merge_conflict", "pod_last_event", "pod_node", "pr", "result", "started", "tasks")

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
        # How the build ended (SCHEMA.md, optional run fields). Absent means
        # unknown -- a document written before the collector recorded it --
        # and unknown never makes a lost pod.
        self.has_build_log = run.get("has_build_log") if isinstance(run.get("has_build_log"), bool) else None
        self.pod_node = run.get("pod_node") if isinstance(run.get("pod_node"), str) else None
        self.pod_last_event = run.get("pod_last_event") if isinstance(run.get("pod_last_event"), str) else None
        # True when clonerefs could not merge the pull request into its base.
        # Unknown stays a setup death: a document written before the collector
        # recorded the field must keep reading the way it did.
        self.merge_conflict = run.get("merge_conflict") if isinstance(run.get("merge_conflict"), bool) else None
        # The eval loop's own verdict (SCHEMA.md, optional): None when the
        # run never reached one, which a deadline kill never does. A document
        # without the key is unknown, not "no verdict": it never makes a kill.
        self.eval_verdict = run.get("eval_verdict") if isinstance(run.get("eval_verdict"), str) else None
        self.eval_verdict_recorded = "eval_verdict" in run

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
    def ceiling_reps(self) -> int:
        return sum(task.ceilings for task in self.tasks)

    @property
    def total_reps(self) -> int:
        return sum(task.passes + task.fails + task.storms + task.ceilings for task in self.tasks)

    @property
    def lost_pod(self) -> bool:
        """Rule 3b's unit: died with its build node, whatever its duration."""
        return (
            not self.tasks
            and self.result == RUN_FAILURE
            and (self.pod_last_event == POD_EVENT_NODE_NOT_READY or self.has_build_log is False)
        )

    @property
    def setup_death(self) -> bool:
        """Rule 3's unit. A conflicted merge leaves the same shape and is not
        one: the fix is the author's rebase, so it is neither an outage nor a
        reason to retest (#1608)."""
        return (
            not self.tasks
            and self.result == RUN_FAILURE
            and not self.lost_pod
            and self.merge_conflict is not True
            and self.duration is not None
            and self.duration < SETUP_DEATH_MAX_DURATION
        )

    @property
    def deadline_kill(self) -> bool:
        """Rule 3d's unit: Prow's deadline ended it, not the eval."""
        return (
            self.result == RUN_FAILURE
            and self.eval_verdict_recorded
            and self.eval_verdict is None
            and not self.lost_pod
            and self.merge_conflict is not True
            and self.duration is not None
            and self.duration >= PROW_JOB_TIMEOUT - DEADLINE_KILL_MARGIN
        )

    @property
    def has_verdict(self) -> bool:
        """Reached a verdict, which is what proves the gate grades: the
        eval's own, or -- only for a document written before `eval_verdict`
        existed, which is never a kill -- a concluded run. Either way at
        least one repetition has to have been graded: NOT EVALUATED records
        as RED (SCHEMA.md; the collector reads the `Failed` word), and a red
        whose every repetition was lost to infrastructure graded nothing. A
        recorded null is no verdict whatever the run carries: the harness
        died after some cases and before its line."""
        graded = any(task.graded for task in self.tasks)
        if self.eval_verdict is not None:
            return graded
        return not self.eval_verdict_recorded and self.result in (RUN_SUCCESS, RUN_FAILURE) and graded

    def collapsed_cases(self) -> set[str]:
        return {task.name for task in self.tasks if task.collapsed}

    def passing_cases(self) -> set[str]:
        return {task.name for task in self.tasks if task.passes > 0}


def load_runs(data: dict) -> list[Run]:
    """Every presubmit run with a finish time, oldest finish first.

    The nightly periodic's runs (SCHEMA.md: runs[].tier) never reach a rule
    or a metric here: the gate is the presubmit, a nightly has no pull
    request to count towards a distinct-PR floor, and a nightly collapsing
    is a case's record, not a gate incident.
    """
    runs = [Run(run) for run in tiers.presubmit_runs(data.get("runs"))]
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
    def from_file(cls, path: pathlib.Path = BLOCKING_ROSTER_FILE) -> Roster:
        """The roster in hack/eval/blocking-roster.txt (one id per line)."""
        admitted = eval_rosters.parse_blocking_roster(path.read_text())
        if not admitted:
            raise SystemExit(f"ERROR: no blocking roster found in {path}")
        return cls.fixed(admitted)

    @classmethod
    def from_script_text(cls, text: str) -> Roster:
        """The roster of a pre-2026-09-15 hack/ci-eval-pr.sh, for an era in
        roster history: hand it `git show <commit>:hack/ci-eval-pr.sh`."""
        try:
            return cls.fixed(eval_rosters.parse_script_roster(text))
        except ValueError as exc:
            raise SystemExit(f"ERROR: {exc}") from exc

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


def delegation_ceiling(full_runs, now: datetime) -> dict:
    """Rule 2b. Returns {fires, reps, prs, start, end, evidence, signature_runs, runs}."""
    window = _in_window(full_runs, now, CEILING_WINDOW)
    hit = [run for run in window if run.ceiling_reps > 0]
    reps = sum(run.ceiling_reps for run in hit)
    prs = _prs(hit)
    fires = reps >= CEILING_MIN_REPS and len(prs) >= CEILING_MIN_PRS
    start = min((run.finished for run in hit), default=None)
    end = max((run.finished for run in hit), default=None)
    evidence = []
    if hit:
        evidence.append(
            f"delegation ceiling: {reps} reps across {len(prs)} PRs ended with the worker still running"
            f" in runs finishing {hhmm(start)}–{hhmm(end)} UTC; nothing graded, nothing counted against a case"
        )
    return {
        "fires": fires,
        "reps": reps,
        "prs": prs,
        "start": start,
        "end": end,
        "evidence": evidence if fires else [],
        "signature_runs": {run.build_id for run in hit if run.ceiling_reps >= CEILING_RUN_SIGNATURE_REPS},
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


def deadline_kills(runs, now: datetime) -> dict:
    """Rule 3d. Returns {fires, killed, prs, start, end, evidence}."""
    killed = [run for run in _in_window(runs, now, DEADLINE_KILL_WINDOW) if run.deadline_kill]
    prs = _prs(killed)
    start = min((run.finished for run in killed), default=None)
    end = max((run.finished for run in killed), default=None)
    evidence = []
    if killed:
        evidence.append(_deadline_cause(killed, prs, start, end) + f" ({_pr_list(prs)})")
    return {
        "fires": len(killed) >= DEADLINE_KILL_MIN and len(prs) >= DEADLINE_KILL_MIN_PRS,
        "killed": killed,
        "prs": prs,
        "start": start,
        "end": end,
        "evidence": evidence,
    }


def _deadline_cause(killed, prs, start, end) -> str:
    return CAUSE_DEADLINE_KILL.format(
        count=len(killed), prs=len(prs), minutes=int(PROW_JOB_TIMEOUT.total_seconds() // 60), start=hhmm(start), end=hhmm(end)
    )


def node_counts(runs) -> dict[str, int]:
    """{node: lost pods on it}, by name; runs with no recorded node skipped."""
    counts: dict[str, int] = {}
    for run in runs:
        if run.pod_node:
            counts[run.pod_node] = counts.get(run.pod_node, 0) + 1
    return dict(sorted(counts.items()))


def node_list(nodes: dict[str, int]) -> str:
    """"…-er33 ×3, …-pe72" for the evidence and the advice."""
    if not nodes:
        return UNKNOWN_NODE
    return ", ".join(f"{name} ×{count}" if count > 1 else name for name, count in nodes.items())


def lost_pods(runs, now: datetime) -> dict:
    """Rule 3b. Returns {fires, event, lost, prs, nodes, start, end, evidence}.

    `lost` is the densest LOST_POD_SPAN of lost pods among those that
    finished inside LOST_POD_WINDOW, anchored at one of them; the newest such
    span wins a tie, so the numbers follow the latest losses. Lost pods in
    the window but outside that span are counted in the evidence only.
    """
    recent = [run for run in _in_window(runs, now, LOST_POD_WINDOW) if run.lost_pod]
    lost: list = []
    for anchor in recent:
        span = [run for run in recent if anchor.finished <= run.finished <= anchor.finished + LOST_POD_SPAN]
        if len(span) >= len(lost):
            lost = span
    prs = _prs(lost)
    nodes = node_counts(lost)
    start = min((run.finished for run in lost), default=None)
    end = max((run.finished for run in lost), default=None)
    evidence = []
    if lost:
        line = (
            f"lost pods: {len(lost)} runs on {len(prs)} PRs died with their build node"
            f" {hhmm(start)}–{hhmm(end)} UTC (nodes {node_list(nodes)}; {_pr_list(prs)})"
        )
        more = len(recent) - len(lost)
        if more > 0:
            line += f"; {more} more earlier in the last {int(LOST_POD_WINDOW.total_seconds() // 3600)}h"
        evidence.append(line)
    return {
        "fires": len(lost) >= LOST_POD_MIN,
        "event": len(lost) >= LOST_POD_EVENT_MIN,
        "lost": lost,
        "prs": prs,
        "nodes": nodes,
        "start": start,
        "end": end,
        "evidence": evidence,
    }


def _project_list(projects) -> str:
    shown = ", ".join(projects[:EVIDENCE_MAX_PROJECTS])
    extra = len(projects) - EVIDENCE_MAX_PROJECTS
    return f"{shown} and {extra} more" if extra > 0 else shown


def fixture_drift(state_doc: dict | None, now: datetime) -> dict:
    """Rule 3c over fixture-state.json. Returns {fires, known, stale, unknown,
    scanned_at, roles, projects, drift, current, checked, total, reason,
    evidence}.

    `current` is every project's drifted roles this scan, firing or not;
    `roles` and `projects` are the ones that fire; `drift` is
    {project: {role: [what the scan observed]}} for those; `read` is
    {project: [roles the scan could read there]}, what rule 6's exit checks.
    """
    out = {
        "fires": False,
        "known": False,
        "stale": False,
        "unknown": False,
        "scanned_at": None,
        "roles": [],
        "projects": [],
        "drift": {},
        "current": {},
        "read": {},
        "checked": 0,
        "total": 0,
        "reason": None,
        "evidence": [],
    }
    if not isinstance(state_doc, dict):
        return out
    out["known"] = True
    scanned_at = parse_iso(state_doc.get(fixture_state.KEY_SCANNED_AT))
    out["scanned_at"] = scanned_at
    projects = state_doc.get(fixture_state.KEY_PROJECTS)
    out["total"] = len(projects) if isinstance(projects, dict) else 0
    out["checked"] = fixture_state.checked_projects(state_doc)
    if scanned_at is None or now - scanned_at > FIXTURE_STATE_MAX_AGE:
        out["stale"] = True
        age = f"{int((now - scanned_at).total_seconds() // 3600)}h" if scanned_at else "of unknown age"
        out["evidence"].append(f"fixture-state scan is {age} old (last {iso(scanned_at) or 'never'}); ignored, check the scan job")
        return out
    if out["total"] and out["checked"] == 0:
        out["unknown"] = True
        out["reason"] = fixture_state.not_checked_reason(state_doc)
        out["evidence"].append(
            f"fixture-state scan at {hhmm(scanned_at)} UTC could check none of {out['total']} pool projects"
            + (f" ({out['reason']})" if out["reason"] else "")
        )
        return out
    current = fixture_state.drift_map(state_doc)
    previous = fixture_state.previous_drift_map(state_doc)
    out["current"] = current
    out["read"] = fixture_state.read_map(state_doc)
    role_projects: dict[str, list[str]] = {}
    for project, roles in current.items():
        for role in roles:
            role_projects.setdefault(role, []).append(project)
    firing: dict[str, list[str]] = {}
    for role, projs in sorted(role_projects.items()):
        repeated = [project for project in projs if role in previous.get(project, [])]
        line = f"{role} drifted on {len(projs)} pool project(s) ({_project_list(projs)}) at the {hhmm(scanned_at)} UTC scan"
        if repeated:
            line += f", the {FIXTURE_DRIFT_CONSECUTIVE_SCANS}nd consecutive scan on {_project_list(repeated)}"
        fires = len(projs) >= FIXTURE_DRIFT_MIN_PROJECTS or bool(repeated)
        if fires:
            firing[role] = projs
        else:
            line += "; not yet repeated or widespread"
        out["evidence"].append(line)
    if firing:
        out["fires"] = True
        out["roles"] = sorted(firing)
        out["projects"] = sorted({project for projs in firing.values() for project in projs})
        out["drift"] = {
            project: {role: fixture_state.drift_detail(state_doc, project, role) for role in out["roles"] if project in firing[role]}
            for project in out["projects"]
        }
    return out


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
        # A killed run that recorded cases (#1875) stays in the population --
        # its collapse still makes another PR's red shared -- but is never the
        # PR's own: the kill is the gate's, whatever those cases did.
        if run.result != RUN_FAILURE or run.deadline_kill:
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
    lost = sum(1 for run in window if run.lost_pod)
    kills = [run for run in window if run.deadline_kill]
    # A killed run that recorded cases (#1875) is already among the full
    # reds (and never among the PR's own, see pr_caused_reds).
    kills_not_counted = sum(1 for run in kills if not run.full)
    out = {
        "window_hours": int(METRICS_WINDOW.total_seconds() // 3600),
        "full_runs": len(full),
        "prs": len(_prs(full)),
        "green_runs": len(green),
        "red_runs": reds,
        # The digest's split of the reds: the pull request's own, and
        # everything else (shared breaks, storms, empty records, setup
        # deaths, lost pods, deadline kills) as "infra".
        "pr_caused_reds": own,
        "infra_reds": reds - own + deaths + lost + kills_not_counted,
        "green_rate": round(len(green) / len(concluded), 3) if concluded else None,
        "aborted_runs": sum(1 for run in window if run.result not in (RUN_SUCCESS, RUN_FAILURE)),
        "setup_deaths": deaths,
        "lost_pods": lost,
        "deadline_kills": len(kills),
        "infra_rep_rate": round(storm_reps / reps, 3) if reps else None,
        "infra_reps": storm_reps,
        # Apart from the storm's count: repetitions the harness stopped
        # watching at its delegation ceiling with the worker still running.
        "ceiling_reps": sum(run.ceiling_reps for run in full),
    }
    for pct in WALL_CLOCK_PERCENTILES:
        value = percentile(walls, pct)
        out[f"wall_clock_p{pct}_s"] = int(value) if value is not None else None
    if fixtures is not None:
        out["fixtures"] = fixtures
    return out


def slow_gate(full_runs, now: datetime, prev: dict | None) -> dict | None:
    """Rule 7: the note's numbers while the gate is slow, else None.

    `prev` is the previous tick's note (health.json's `slow`); an episode in
    progress keeps its `since` and clears at SLOW_CLEAR_FACTOR rather than
    SLOW_FACTOR. Wall clock is the run's finish minus its start, the same
    measure as the digest's p50/p90.
    """
    concluded = [
        run
        for run in full_runs
        if run.result in (RUN_SUCCESS, RUN_FAILURE) and len(run.tasks) >= SLOW_MIN_TASKS and run.wall_clock
    ]
    recent = concluded[-SLOW_RUNS:]
    if len(recent) < SLOW_RUNS or recent[0].finished <= now - SLOW_WINDOW:
        return None
    baseline = [run.wall_clock.total_seconds() for run in concluded[:-SLOW_RUNS] if run.finished > now - SLOW_BASELINE]
    if len(baseline) < SLOW_BASELINE_MIN_RUNS:
        return None
    typical = percentile(baseline, 50)
    walls = [run.wall_clock.total_seconds() for run in recent]
    median = percentile(walls, 50)
    if median < (SLOW_CLEAR_FACTOR if prev else SLOW_FACTOR) * typical:
        return None
    return {
        "since": (prev or {}).get("since") or iso(now),
        "runs": len(recent),
        "min_s": int(min(walls)),
        "median_s": int(median),
        "max_s": int(max(walls)),
        "baseline_days": SLOW_BASELINE.days,
        "baseline_runs": len(baseline),
        "baseline_p50_s": int(typical),
        "baseline_p90_s": int(percentile(baseline, 90)),
        "infra_reps": sum(run.storm_reps for run in recent),
    }


def slow_evidence(slow: dict) -> str:
    lost = f"{slow['infra_reps']} reps lost to infra" if slow["infra_reps"] else "no reps lost"
    return (
        f"slow gate: last {slow['runs']} full runs {slow['min_s'] // 60}–{slow['max_s'] // 60} min"
        f" (median {slow['median_s'] // 60}) against a {slow['baseline_days']}-day typical of"
        f" {slow['baseline_p50_s'] // 60} min (p90 {slow['baseline_p90_s'] // 60}); {lost}"
    )


def _as_seconds(minutes) -> int | None:
    """pool-pressure.json reports minutes; health.json stores seconds.

    The product is what gets checked, not the input: json.loads returns both
    `1e308`, finite until multiplied, and integers no float can hold. Bools
    are a data error rather than 1 and 0, as render.is_number has it.
    """
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
        return None
    try:
        seconds = float(minutes) * SECONDS_PER_MINUTE
    except OverflowError:  # an int too large to be a float
        return None
    return int(seconds) if math.isfinite(seconds) else None


def _section(artifact: dict, key: str) -> dict:
    """A named object out of the artifact, `{}` when it is anything else.

    pool-pressure.json is another job's output and nothing validates its shape
    on the way in. A wrong type raises inside adjudicate, and the Actions step
    calling it has no continue-on-error, so one malformed artifact would stop
    the dashboard and the chat bot every 15 minutes until someone noticed.
    """
    value = artifact.get(key)
    return value if isinstance(value, dict) else {}


def _rows(section: dict, key: str = "days") -> list:
    """An array field, `[]` when the artifact put something else there."""
    rows = section.get(key)
    return rows if isinstance(rows, list) else []


def wait_text(seconds) -> str:
    """A measured wait: "24s" below a minute, "22 min" above. Whole minutes
    would print an ordinary 24-second wait as "0 min". post_health imports it
    so the chat message and the evidence line agree."""
    if seconds is None:
        return "?"
    return f"{int(seconds)}s" if seconds < SECONDS_PER_MINUTE else f"{seconds // SECONDS_PER_MINUTE} min"


def minutes_text(seconds) -> str:
    """A threshold, which the runbook always states in whole minutes.

    post_health imports it, as it does wait_text, so the chat message and the
    evidence line cannot drift.
    """
    return "?" if seconds is None else str(int(seconds // SECONDS_PER_MINUTE))


def _longest_wait_s(queue: dict) -> int | None:
    """The longest current wait for a project, or None if Deck was not read.

    None and zero say different things -- "we cannot tell" against "nothing is
    waiting" -- and the message gate treats them differently. A figure over
    POOL_MAX_WAIT is dropped like an unparseable one: it costs its own run,
    not the tick that would otherwise date a backlog from it and raise.
    """
    if not queue.get("read"):
        return None
    cap = int(POOL_MAX_WAIT.total_seconds())
    waits = [_as_seconds(run.get("minutes")) for run in _rows(queue, "waiting_runs") if isinstance(run, dict)]
    return max([w for w in waits if w is not None and w <= cap], default=0)


def _over_limit(row: dict, thresholds: dict) -> bool:
    """Whether a stretch's own percentiles are over either limit.

    `days[]` rows carry the periodic's `breached` flag; the recent block does
    not, because it is evidence and never a verdict. Strict `>` and either
    half, as `DayRow.breached` compares. False when a limit is missing: the
    question cannot be answered, and the periodic's own flagged day can.
    """
    limits = (_as_seconds(thresholds.get("p50_minutes")), _as_seconds(thresholds.get("p95_minutes")))
    return any(
        limit is not None and (_as_seconds(row.get(key)) or 0) > limit
        for key, limit in zip(("p50_minutes", "p95_minutes"), limits)
    )


def _worst_breached_day(trend: dict, thresholds: dict) -> dict | None:
    """The `days[]` row that breached hardest, or None if no day did.

    Hardest is the largest excess over either limit, so a day that went over on
    p95 alone is still picked ahead of a quiet one. A row's own `breached` flag
    is what `trend.breached_days` is built from, sample minimum included.
    """
    days = [d for d in _rows(trend) if isinstance(d, dict) and d.get("breached")]
    if not days:
        return None
    # Through _as_seconds on both sides: the ratio is the same in either unit,
    # and it is the file's existing filter for a figure that is not a number.
    limits = (_as_seconds(thresholds.get("p50_minutes")), _as_seconds(thresholds.get("p95_minutes")))

    def excess(row: dict) -> float:
        return max(
            (_as_seconds(row.get(key)) or 0) / limit if limit else 0
            for key, limit in zip(("p50_minutes", "p95_minutes"), limits)
        )

    return max(days, key=excess)


def pool_note(artifact: dict | None, now: datetime, prev: dict | None) -> dict | None:
    """Rule 8: the note's numbers while the pool is backed up, else None.

    `artifact` is the pool-pressure periodic's pool-pressure.json, None when
    the job has published none. `prev` is the previous tick's note and carries
    the episode's `since`, as rule 7's does. The verdict is the periodic's and
    is not re-derived; staleness is the exception, because an artifact cannot
    report that it has stopped being written.
    """
    if not isinstance(artifact, dict):
        return None
    measured = parse_iso(artifact.get("window_end"))
    verdict = artifact.get("verdict")
    if measured is None or now - measured > POOL_STALE_AFTER:
        # Stale outranks the verdict it carries: the numbers may be hours old.
        verdict = POOL_STALE
    elif verdict not in (POOL_BREACH, POOL_UNMEASURED):
        return None
    # `since` opens on the first note and carries through the episode, but a
    # BREACH does not inherit one from a stretch that only ever said the queue
    # could not be read: "runs are waiting since Monday" over two days nobody
    # measured claims more than the readings do. `breach_seen` is what makes
    # the two directions differ -- a breach that goes STALE and comes back
    # keeps its real start, because that start was measured.
    before = prev or {}
    breach_seen = bool(before.get("breach_seen")) or verdict == POOL_BREACH
    carried = before.get("since") if verdict != POOL_BREACH or before.get("breach_seen") else None
    note = {
        "since": carried or iso(now),
        "verdict": verdict,
        "breach_seen": breach_seen,
        "measured_at": iso(measured) if measured else None,
    }
    if verdict == POOL_STALE:
        # No numbers at all. A reading from hours ago is not evidence about
        # now, and a field that is present will eventually be rendered as
        # though it were current.
        return note
    trend = _section(artifact, "trend")
    pool = _section(artifact, "pool")
    thresholds = _section(artifact, "thresholds")
    # The numbers that justify the verdict, not the window's. The periodic
    # breaches on a single day's row or on a run waiting past p95 right now
    # (pool_pressure.py's `breached_days or live_breach`) and never on the
    # seven-day aggregate, which after one bad day sits back inside its own
    # limit -- printing it under "onboard a project" contradicts the alert.
    #
    # The recent stretch comes first when it has the runs to be judged and is
    # itself over a limit, because a verdict that lasts a week outlives the day
    # that earned it: a Thursday incident is otherwise evidenced by Monday. It
    # has to breach on its own to be quoted at all -- a stretch inside both
    # limits is the same contradiction, only newer. Below the sample floor, or
    # back inside them, the worst breached day is what is left to show.
    recent = _section(artifact, "recent")
    quotable = bool(recent.get("judged")) and _over_limit(recent, thresholds)
    day = {} if quotable else (_worst_breached_day(trend, thresholds) or {})
    source = recent if quotable else day
    longest = _longest_wait_s(_section(artifact, "queue"))
    limit_p50 = _as_seconds(thresholds.get("p50_minutes"))
    backlog = None if longest is None or limit_p50 is None else longest > limit_p50
    return note | {
        "day": day.get("day"),
        "window_hours": recent.get("hours") if quotable else None,
        "p50_s": _as_seconds(source.get("p50_minutes")),
        "p95_s": _as_seconds(source.get("p95_minutes")),
        # The longest current wait, and whether it is a backlog. Judged once
        # here: the alert, the digest and the Brief all ask, and a verdict
        # lasting a week outlives the queue that earned it. Against the p50
        # limit, the periodic's own bar for a bad day -- `over_threshold`, the
        # subset already past p95, misses a pool full all afternoon at half an
        # hour a run. None when nothing can answer: Deck unread, or no limit.
        "waiting_longest_s": longest,
        "waiting_now": backlog,
        # When this backlog began, not when the episode did: the verdict spans
        # a week, so "waiting since Monday" can stand over a Thursday jam. The
        # oldest queued run dates it, and only while there is one.
        "waiting_since": iso(measured - timedelta(seconds=longest)) if backlog else None,
        "over_threshold": _section(artifact, "queue").get("over_threshold") or 0,
        "threshold_p50_s": limit_p50,
        "threshold_p95_s": _as_seconds(thresholds.get("p95_minutes")),
        "free": pool.get("free"),
        "total": pool.get("total"),
        "cause": artifact.get("cause"),
        # The cap the newest run in the window ran under. Only the
        # CONCURRENCY_CAP message quotes it, and without it that message
        # cannot say what to raise the cap from.
        "max_concurrency": artifact.get("max_concurrency"),
    }


def pool_wait_p50_s(artifact: dict | None, now: datetime) -> int | None:
    """The digest's `typical wait`: the median of the artifact's newest day.

    `trend.p50_minutes` is the seven-day figure and belongs to the note; the
    digest headline says "last 24h", so it reads the newest judged `days[]`
    row. None when there is no artifact, it is too old, or every row inside the
    heading went unjudged -- a quiet weekend would otherwise print Friday's
    median as today's.
    """
    if not isinstance(artifact, dict):
        return None
    measured = parse_iso(artifact.get("window_end"))
    if measured is None or now - measured > POOL_STALE_AFTER:
        return None
    # The newest judged row, not the newest row. The producer withholds a
    # verdict below its sample floor, and at 13:00 UTC today's row holds only
    # the overnight runs -- one slow run would otherwise be the morning's
    # typical wait. Withholding the figure instead costs the headline on every
    # quiet morning, and a night with no runs at all already falls back, since
    # the producer writes no row for an empty day. Rows are oldest first, so
    # the first one outside POOL_DIGEST_DAYS ends the walk.
    for row in reversed(_rows(_section(artifact, "trend"))):
        if not isinstance(row, dict):
            continue
        try:
            day = datetime.strptime(row["day"], POOL_DAY_FORMAT).date()
        except (KeyError, TypeError, ValueError):
            continue
        if (measured.date() - day).days >= POOL_DIGEST_DAYS:
            break
        if row.get("judged"):
            return _as_seconds(row.get("p50_minutes"))
    return None


def pool_evidence(pool: dict) -> str:
    if pool["verdict"] == POOL_STALE:
        return (
            f"queue wait unmeasured: last reading {pool['measured_at'] or 'never'};"
            " the hourly pool-pressure job has missed the last few"
        )
    if pool["verdict"] == POOL_UNMEASURED:
        return "pool pressure: the hourly check ran but could not read how long recent runs waited"
    # UNKNOWN *is* the pool read failing, so the counts are absent on exactly
    # the breach that most wants explaining. Say the wait and stop.
    counted = pool["free"] is not None and pool["total"] is not None
    projects = f"; {pool['free']} of {pool['total']} projects free" if counted else ""
    return f"backed-up pool: {pool_measurement(pool)}{projects}"


def pool_span(pool: dict) -> str | None:
    """Which stretch the note's numbers cover, or None when it carries none.

    `pool_note` sets `window_hours` when the periodic could judge the recent
    stretch and `day` when it fell back to the worst breached day, so the three
    readers that quote numbers do not each decide which it was. A label with no
    numbers under it is worse than no label: a breach carried by the live queue
    alone has neither, and would otherwise read "median wait ?".
    """
    if pool.get("p50_s") is None and pool.get("p95_s") is None:
        return None
    hours = pool.get("window_hours")
    if hours:
        return f"last {hours}h"
    return f"worst day {pool['day']}" if pool.get("day") else None


def pool_measurement(pool: dict) -> str:
    """Where the breach is, in the periodic's own terms: the stretch it
    measured, the live queue, or both. post_health and the page read the same
    fields."""
    parts = []
    span = pool_span(pool)
    if span:
        parts.append(
            f"{span} median {wait_text(pool['p50_s'])} against"
            f" {minutes_text(pool['threshold_p50_s'])} min,"
            f" p95 {wait_text(pool['p95_s'])} against"
            f" {minutes_text(pool['threshold_p95_s'])}"
        )
    if pool.get("over_threshold"):
        parts.append(
            f"{pool['over_threshold']} run{'' if pool['over_threshold'] == 1 else 's'}"
            f" waiting now past {minutes_text(pool['threshold_p95_s'])} min"
        )
    # A BREACH always has one or the other; this is what a contract violation
    # reads as, rather than a sentence with a blank in it.
    return "; ".join(parts) or "the periodic reported a breach with no measured stretch and no live queue"


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


def issue_tag(issue) -> str | None:
    """"#1278" for the {number, url} the poster filed or adopted; None otherwise."""
    number = issue.get("number") if isinstance(issue, dict) else None
    return f"#{number}" if number else None


def issue_for(issue, condition: str | None) -> dict | None:
    """The poster's tracking issue when it belongs to this condition. The
    bot files one for an outage and one for a build-cluster event, and
    records which (`condition`); an outage's issue is not the lost pods'
    tracking nor the reverse. One with no `condition` predates the key, and
    only outages filed issues then, so it is an outage's."""
    if not issue_tag(issue):
        return None
    filed_for = issue.get("condition") or SHARED_BREAK
    return issue if filed_for == condition else None


def all_tracking(cases: list[str], notes: dict, issue) -> list[str]:
    """case-notes.yaml's issues for these cases, plus the bot's own if it is
    not already among them."""
    issues = tracking_issues(cases, notes)
    tag = issue_tag(issue)
    if tag and tag not in issues:
        issues.append(tag)
    return issues


def recovery_bar(condition: str | None) -> str:
    """What `recovered` counts on the way out of `condition`, in words."""
    return RECOVERY_BAR_VERDICT if condition == DEADLINE_KILL else RECOVERY_BAR_GREEN


def advice_for(
    state: str,
    condition: str | None,
    cases: list[str],
    storm_end: datetime | None,
    notes: dict,
    recovering: bool = False,
    issue: dict | None = None,
    incident: dict | None = None,
) -> str:
    """What the reader should do. Keyed on the condition, not on whether it
    is still firing: a storm being left is still a storm, not a setup
    failure. `incident` is the numbers behind the cause (assess), read for
    the lost-pod advice's nodes, time and count."""
    if state == GREEN:
        return ADVICE_GREEN
    if recovering and condition == DEADLINE_KILL:
        return ADVICE_RECOVERING_VERDICT.format(count=RECOVERY_GREEN_RUNS)
    if recovering:
        return ADVICE_RECOVERING.format(count=RECOVERY_GREEN_RUNS)
    if condition == SHARED_BREAK:
        issues = all_tracking(cases, notes, issue)
        return ADVICE_OUTAGE.format(tracking=", ".join(issues) if issues else ADVICE_OUTAGE_NO_ISSUE)
    if condition == DEADLINE_KILL:
        issues = all_tracking(cases, notes, issue)
        return ADVICE_DEADLINE_KILL.format(
            minutes=int(PROW_JOB_TIMEOUT.total_seconds() // 60),
            tracking=", ".join(issues) if issues else ADVICE_OUTAGE_NO_ISSUE,
        )
    if condition == STORM:
        when = hhmm(storm_end + STORM_COOLDOWN) if storm_end else "the storm ends"
        return ADVICE_STORM.format(when=when)
    if condition == DELEGATION_CEILING:
        return ADVICE_CEILING
    if condition == LOST_PODS:
        incident = incident or {}
        return ADVICE_LOST_PODS.format(
            nodes=node_list(incident.get("nodes") or {}),
            when=reader_clock(parse_iso(incident.get("window_start"))),
            count=incident.get("runs", 0),
        )
    if condition == FIXTURE_DRIFT:
        incident = incident or {}
        return ADVICE_FIXTURE_DRIFT.format(
            roles=", ".join(incident.get("roles") or []) or "the drifted fixture roles",
            projects=_project_list(list(incident.get("projects") or [])) or "the drifted projects",
        )
    return ADVICE_SETUP


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #


def assess(runs, now: datetime, roster: Roster, fixture_state_doc: dict | None = None) -> dict:
    """Apply rules 1-4 to the runs visible at `now`, and rule 3c to the
    fleet scan when one was given; no hysteresis yet."""
    visible = [run for run in runs if run.finished <= now]
    full_runs = [run for run in visible if run.full]
    r1 = shared_break(full_runs, now, roster)
    r2 = storm(full_runs, now)
    r2b = delegation_ceiling(full_runs, now)
    r3 = setup_deaths(visible, now)
    r3b = lost_pods(visible, now)
    r3c = fixture_drift(fixture_state_doc, now)
    r3d = deadline_kills(visible, now)

    if r1["fires"]:
        state, condition = OUTAGE, SHARED_BREAK
        cause = CAUSE_SHARED_BREAK.format(cases=", ".join(r1["cases"]))
    elif r3d["fires"]:
        state, condition = OUTAGE, DEADLINE_KILL
        cause = _deadline_cause(r3d["killed"], r3d["prs"], r3d["start"], r3d["end"])
    elif r3b["fires"]:
        state, condition = DEGRADED, LOST_PODS
        cause = CAUSE_LOST_PODS.format(count=len(r3b["lost"]), prs=len(r3b["prs"]), start=hhmm(r3b["start"]), end=hhmm(r3b["end"]))
    elif r2["fires"]:
        state, condition = DEGRADED, STORM
        cause = CAUSE_STORM.format(start=hhmm(r2["start"]), end=hhmm(r2["end"]))
    elif r2b["fires"]:
        state, condition = DEGRADED, DELEGATION_CEILING
        cause = CAUSE_CEILING.format(reps=r2b["reps"], prs=len(r2b["prs"]), start=hhmm(r2b["start"]), end=hhmm(r2b["end"]))
    elif r3["fires"]:
        state, condition = DEGRADED, SETUP_DEATHS
        cause = CAUSE_SETUP.format(count=len(r3["deaths"]), prs=_pr_list(r3["prs"]))
    elif r3c["fires"]:
        state, condition = DEGRADED, FIXTURE_DRIFT
        cause = CAUSE_FIXTURE_DRIFT.format(roles=", ".join(r3c["roles"]), projects=len(r3c["projects"]))
    else:
        state, condition, cause = GREEN, None, ""

    evidence = r1["evidence"] + r3d["evidence"] + r3b["evidence"] + r2["evidence"] + r2b["evidence"] + r3["evidence"] + r3c["evidence"] + r1["pr_caused"]
    if r1["fires"] and r2["fires"]:
        # Both true at once on 2026-09-02: the break is the state, the
        # storm is context the reader still needs.
        evidence.append(CAUSE_STORM.format(start=hhmm(r2["start"]), end=hhmm(r2["end"])) + " overlaps the break")

    # Currency for rule 6's entry check: whether one of the newest full runs
    # carries the firing condition's signature. Setup deaths, lost pods and
    # deadline kills are counted, not signed; their count is their currency.
    recent = full_runs[-TRANSITION_MIN_RUNS:]
    if condition == SHARED_BREAK:
        signature = r1["signature_runs"]
    elif condition == STORM:
        signature = {run.build_id for run in full_runs if run.storm_reps > 0}
    elif condition == DELEGATION_CEILING:
        signature = {run.build_id for run in full_runs if run.ceiling_reps > 0}
    else:
        signature = set()
    current = condition in COUNTED_CONDITIONS or any(run.build_id in signature for run in recent)
    # The numbers behind the cause, for the one-sentence message: which
    # pull requests the firing condition touched, how many runs, and the
    # storm's window -- or, for lost pods, the span of the losses, the
    # nodes with a count each, and whether it is a build-cluster event.
    if condition == SHARED_BREAK:
        incident = {"prs": r1["prs"], "runs": r1["runs"], "window_start": None, "window_end": None}
    elif condition == STORM:
        incident = {"prs": r2["prs"], "runs": r2["runs"], "window_start": iso(r2["start"]), "window_end": iso(r2["end"])}
    elif condition == DELEGATION_CEILING:
        # `reps` beside the storm's keys: the message says how many
        # repetitions ended at the wait, not only how many runs held one.
        incident = {"prs": r2b["prs"], "runs": r2b["runs"], "reps": r2b["reps"], "window_start": iso(r2b["start"]), "window_end": iso(r2b["end"])}
    elif condition == SETUP_DEATHS:
        incident = {"prs": r3["prs"], "runs": len(r3["deaths"]), "window_start": None, "window_end": None}
    elif condition == DEADLINE_KILL:
        incident = {"prs": r3d["prs"], "runs": len(r3d["killed"]), "window_start": iso(r3d["start"]), "window_end": iso(r3d["end"])}
    elif condition == LOST_PODS:
        incident = {
            "prs": r3b["prs"],
            "runs": len(r3b["lost"]),
            "window_start": iso(r3b["start"]),
            "window_end": iso(r3b["end"]),
            "nodes": r3b["nodes"],
            "event": r3b["event"],
        }
    elif condition == FIXTURE_DRIFT:
        # No pull requests and no runs: the scan is the evidence. The
        # window opens at the scan that fired; the roles, the projects and
        # what was observed on each are what the message and the issue name.
        incident = {
            "prs": [],
            "runs": 0,
            "window_start": iso(r3c["scanned_at"]),
            "window_end": None,
            "roles": r3c["roles"],
            "projects": r3c["projects"],
            "drift": r3c["drift"],
        }
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
        "last_lost_pod": max((run.finished for run in visible if run.lost_pod), default=None),
        "last_deadline_kill": max((run.finished for run in visible if run.deadline_kill), default=None),
        "roster": roster,
        "fixture": r3c,
    }


def recovered(full_runs, prev: dict, since: datetime, last_setup_death: datetime | None, roster: Roster, last_lost_pod: datetime | None = None, last_deadline_kill: datetime | None = None) -> bool:
    """Rule 6's exit: the last RECOVERY_GREEN_RUNS full runs are green, on
    distinct pull requests, all finished after the incident began, and none
    carries the signature of the condition being left -- a collapse of one
    of its cases for a shared break, STORM_RUN_SIGNATURE_REPS storm
    repetitions for a storm, CEILING_RUN_SIGNATURE_REPS ceiling repetitions
    for a delegation-ceiling wave, a setup death after it for setup deaths,
    a lost pod after it for lost pods. A deadline-kill outage is the one
    exception: the same count of runs with a verdict, green or red, after the
    last kill. Judged from the runs themselves rather than
    from the rule's window, so the runs that constituted the incident never
    count as its recovery once the window has rolled past them."""
    if prev.get("condition") == DEADLINE_KILL:
        # A verdict either way is the proof: the gate is grading again. Green
        # alone would hold the outage through a stretch of honest reds.
        recent = [run for run in full_runs if run.has_verdict][-RECOVERY_GREEN_RUNS:]
        if len(recent) < RECOVERY_GREEN_RUNS:
            return False
        if any(run.finished <= since or (last_deadline_kill is not None and run.finished <= last_deadline_kill) for run in recent):
            return False
        return len(_prs(recent)) >= RECOVERY_GREEN_RUNS
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
        DELEGATION_CEILING: lambda run: run.ceiling_reps >= CEILING_RUN_SIGNATURE_REPS,
        SETUP_DEATHS: lambda run: last_setup_death is not None and run.finished <= last_setup_death,
        LOST_PODS: lambda run: last_lost_pod is not None and run.finished <= last_lost_pod,
    }.get(condition, lambda run: False)
    return not any(carries(run) for run in recent)


def fixture_drift_hold(fixture: dict, incident: dict | None) -> str | None:
    """Rule 6's exit for fixture_drift: why this tick's scan cannot end the
    incident, or None when it can. Entering took a scan that saw the drift;
    leaving takes a scan that could see the same roles on the same projects
    and no longer shows it. A scan that is missing, stale, blind, or that
    recorded one of the incident's projects as not checked (its runner timed
    out, the grant went away) shows nothing about the fixture, so the
    incident holds rather than posting a recovery nothing observed."""
    if not fixture.get("known"):
        return "no fixture-state scan was read this tick"
    if fixture.get("stale"):
        return "the fixture-state scan is stale"
    if fixture.get("unknown"):
        return "the fixture-state scan could check no project"
    roles = set((incident or {}).get("roles") or [])
    read = fixture.get("read") or {}
    unread = sorted(project for project in (incident or {}).get("projects") or [] if not roles <= set(read.get(project, [])))
    if unread:
        return f"the fixture-state scan could not read {_project_list(unread)}"
    return None


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
    if prev_condition == FIXTURE_DRIFT:
        # The scan is the evidence both ways: the hourly scan that could read
        # the incident's roles on its projects and no longer shows the
        # repeated or widespread drift is the recovery (three green runs
        # could all have leased healthy projects and say nothing about the
        # fixture), and a scan that could not see them is not.
        if fixture_drift_hold(assessed["fixture"], prev.get("incident")) is None:
            return _keep(GREEN, None, "", [], now, recovering=False)
        return _keep(prev_state, prev_condition, prev.get("cause") or "", [], since, recovering=False)
    if recovered(assessed["full_runs"], prev, since, assessed["last_setup_death"], assessed["roster"], assessed["last_lost_pod"], assessed.get("last_deadline_kill")):
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
    posted: dict | None = None,
    pool_pressure: dict | None = None,
    fixture_state_doc: dict | None = None,
) -> dict:
    """data.json + previous health.json -> health.json (as a dict).

    `now` is the data's horizon, the instant every window is measured from.
    `wall_clock`, when given, is compared against it for staleness; a replay
    or a pinned `--now` passes None and is never stale. `posted` is the
    poster's state file (post_health.py), read for the tracking issue the
    bot filed: it rides in `issue` and the advice while the state is not
    GREEN and the condition is the one it was filed for (issue_for), and is
    dropped on recovery; it also holds the pool episode's start across a tick
    that read no artifact. `pool_pressure` is the pool-pressure periodic's
    artifact (rule 8); absent, the note and the digest's wait are None.
    `fixture_state_doc` is the hourly fleet scan's fixture-state.json when one
    is published; rule 3c reads it and the `fixture_state` block below
    summarises it for the poster.
    """
    if runs is None:
        runs = load_runs(data)
    assessed = assess(runs, now, roster, fixture_state_doc)
    decided = transition(prev, assessed, now)
    issue = None
    if decided["state"] != GREEN:
        # The poster keeps the current condition's issue in `issue` and
        # every issue of the episode in `issues`; the one for the decided
        # condition is cited, wherever it sits.
        candidates = [(posted or {}).get("issue"), *((posted or {}).get("issues") or []), (prev or {}).get("issue")]
        issue = next((match for match in (issue_for(candidate, decided["condition"]) for candidate in candidates) if match), None)
    evidence = list(assessed["evidence"])
    if decided["recovering"]:
        evidence.append(
            f"condition cleared; waiting for {RECOVERY_GREEN_RUNS} consecutive {recovery_bar(decided['condition'])}"
            " on distinct PRs before reporting GREEN"
        )
    elif decided["state"] != assessed["state"] and SEVERITY[assessed["state"]] > SEVERITY[decided["state"]]:
        evidence.append(f"{assessed['state']} condition seen but not yet current; holding {decided['state']}")
    elif decided["condition"] == FIXTURE_DRIFT and not assessed["fixture"]["fires"]:
        evidence.append(f"fixture drift held: {fixture_drift_hold(assessed['fixture'], (prev or {}).get('incident'))}; a scan that reads those projects clean ends it")

    # A held state (recovering, or a worse condition not yet current) keeps
    # the previous tick's numbers: the assessment's incident describes the
    # raw state, not the one being reported.
    incident = assessed["incident"] if decided["state"] == assessed["state"] else (prev or {}).get("incident")
    if decided["condition"] == DEADLINE_KILL and decided["state"] != GREEN and isinstance(incident, dict):
        # The rule's window slides, so `window_start` is the oldest kill still
        # inside it; the outage's first kill is kept across ticks for the
        # surfaces that date the whole episode (the comment, the issue).
        previous = (prev or {}).get("incident") if (prev or {}).get("condition") == DEADLINE_KILL and (prev or {}).get("state") != GREEN else None
        starts = [s for s in ((previous or {}).get("first_kill"), (previous or {}).get("window_start"), incident.get("window_start")) if s]
        incident = dict(incident, first_kill=min(starts) if starts else None)
    advice = advice_for(
        decided["state"], decided["condition"], decided["failing_cases"], assessed["storm_end"], notes or {}, decided["recovering"], issue, incident
    )
    # Rule 7 rides beside the state, and only a GREEN one: inside a storm
    # or an outage the long runs are the incident's symptom (429 retries
    # stretch a run), and a note saying "not a break, /retest won't help"
    # beside advice to retest after the storm would contradict it. The
    # previous note is the only memory it needs; a health.json from before
    # the field, or from a non-GREEN tick, has none, so an episode that
    # outlasts an incident starts afresh when GREEN returns.
    slow = slow_gate(assessed["full_runs"], now, (prev or {}).get("slow") or None) if decided["state"] == GREEN else None
    if slow:
        evidence.append(slow_evidence(slow))
    # Rule 8 rides beside the state in every state, unlike rule 7: the wait is
    # a different job's measurement of different data, so it cannot be this
    # incident's own symptom, and when leases are the incident it is the
    # explanation.
    # Aged against the wall clock: `now` is data.json's horizon, and one Prow
    # stall freezes it and the artifact together, so the switch never fires.
    pool_clock = wall_clock or now
    # A tick that read no artifact writes no note, so the next one has no
    # `prev` note to take the episode start from. health.json carries it
    # across those ticks itself: it is written every tick, muted or not,
    # where the poster's state file freezes on a mute or a crash and would
    # hand a later episode an older episode's start.
    prior = ((prev or {}).get("pool") or {})
    before_metrics = (prev or {}).get("metrics") or {}
    held = prior.get("since") or before_metrics.get("pool_since")
    # Whether the open episode has ever measured a breach, carried the same way
    # and for the same reason as its start: pool_note refuses to date a breach
    # from a monitoring-only stretch, and a blind tick must not answer that
    # question by forgetting.
    seen = prior.get("breach_seen") if prior else before_metrics.get("pool_breach_seen")
    pool = pool_note(pool_pressure, pool_clock, {"since": held, "breach_seen": bool(seen)})
    if pool:
        evidence.append(pool_evidence(pool))
    stale_after = DEFAULT_STALE_AFTER
    if isinstance(data.get("stale_after_s"), (int, float)):
        stale_after = timedelta(seconds=data["stale_after_s"])
    age = wall_clock - now if wall_clock is not None else None
    stale = age is not None and age > stale_after
    if stale:
        note = ADVICE_STALE.format(generated_at=iso(now), age=f"{int(age.total_seconds() // 3600)}h")
        evidence.append(note)
        advice = f"{note} {advice}".strip()
    out = {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "state": decided["state"],
        "condition": decided["condition"],
        "since": iso(decided["since"]),
        "cause": decided["cause"],
        "failing_cases": decided["failing_cases"],
        "tracking_issues": all_tracking(decided["failing_cases"], notes or {}, issue),
        "issue": issue,
        "incident": incident,
        "evidence": evidence,
        "advice": advice,
        "recovering": decided["recovering"],
        "stale": stale,
        "fixture_state": fixture_state_block(assessed["fixture"]),
        "slow": slow,
        "pool": pool,
        "metrics": metrics([run for run in runs if run.finished <= now], now, fixtures, roster),
        "dashboard_url": DASHBOARD_URL,
        "generated_at": iso(now),
    }
    # Beside the digest's other 24h numbers, but not derived from the runs
    # `metrics` reads -- the wait comes from the periodic, not from data.json.
    out["metrics"]["queue_wait_p50_s"] = pool_wait_p50_s(pool_pressure, pool_clock)
    # Whether the artifact was there at all, which no other field answers: the
    # note is None both for a healthy pool and for a fetch that failed, and
    # queue_wait_p50_s is None on a quiet day too. The poster needs the
    # difference -- going blind must not read as the episode ending.
    out["metrics"]["queue_wait_read"] = isinstance(pool_pressure, dict)
    # The open episode's start, kept only across blind ticks: a tick that read
    # the artifact and wrote no note ended the episode, so the next breach is
    # a new one and starts from its own clock.
    out["metrics"]["pool_since"] = pool["since"] if pool else (None if out["metrics"]["queue_wait_read"] else held)
    out["metrics"]["pool_breach_seen"] = pool["breach_seen"] if pool else (False if out["metrics"]["queue_wait_read"] else bool(seen))
    if age is not None:
        out["metrics"]["data_age_s"] = int(age.total_seconds())
    return out


def fixture_state_block(fixture: dict) -> dict | None:
    """health.json's `fixture_state`: the scan's time, how many projects it
    could read, every project's drifted roles this scan, and whether the
    scan was stale or saw nothing (`unknown`, with the commonest reason).
    None when no fixture-state.json was given."""
    if not fixture.get("known"):
        return None
    return {
        "scanned_at": iso(fixture["scanned_at"]),
        "projects": fixture["total"],
        "checked": fixture["checked"],
        "drifted": fixture["current"],
        "unknown": fixture["unknown"],
        "stale": fixture["stale"],
        "reason": fixture["reason"],
    }


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
    """The state changes in a replay: [{at, state, cause, failing_cases, slow}].

    A change is a new state, a new condition, or a new set of failing
    cases within an OUTAGE -- the things the poster reacts to -- and the
    slow note (rule 7) appearing or clearing, since the poster reacts to
    that too. A storm window's bounds move every tick and are detail, not
    a change. `every` keeps all ticks.
    """
    out = []
    last = None
    for now, health in ticks:
        slow = health.get("slow") or None
        key = (health["state"], health["condition"], tuple(health["failing_cases"]), bool(slow))
        if every or key != last:
            out.append(
                {
                    "at": iso(now),
                    "state": health["state"],
                    "condition": health["condition"],
                    "cause": health["cause"],
                    "failing_cases": health["failing_cases"],
                    "recovering": health["recovering"],
                    "slow": {"since": slow["since"], "median_s": slow["median_s"], "baseline_p50_s": slow["baseline_p50_s"]} if slow else None,
                }
            )
            last = key
    return out


def format_timeline(entries: list[dict]) -> str:
    lines = []
    for entry in entries:
        flag = " (recovering)" if entry["recovering"] else ""
        slow = entry.get("slow")
        if slow:
            flag += f" (slow since {slow['since']}: median {slow['median_s'] // 60} min against {slow['baseline_p50_s'] // 60})"
        lines.append(f"{entry['at']}  {entry['state']:<8}  {entry['cause']}{flag}")
    return "\n".join(lines)


def trim(data: dict, start: datetime, end: datetime, source: str) -> dict:
    """A data.json reduced to the fields this module reads, for a fixture.

    Runs that finished in [start, end); per run build_id, pr, started,
    finished, result, duration_s and tasks, plus how the build ended
    (has_build_log, the pod_* trio, merge_conflict) and eval_verdict when the
    source recorded them; per task
    name, result and reps; per rep result and the first TRIM_REASON_CHARS of
    the reason (null for passing reps, as the collector writes them).
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
        entry = {
            "build_id": run.get("build_id"),
            "pr": run.get("pr"),
            "started": run.get("started"),
            "finished": run.get("finished"),
            "result": run.get("result"),
            "duration_s": run.get("duration_s"),
            "tasks": tasks,
        }
        if tiers.TIER_KEY in run:
            # Kept as written so a fixture cut from a two-tier data.json
            # replays the same filter the live tick applies.
            entry[tiers.TIER_KEY] = run[tiers.TIER_KEY]
        for key in ENDED_FIELDS:
            if key in run:
                entry[key] = run[key]
        runs.append(entry)
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
    return Roster.from_file(args.blocking_roster)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=pathlib.Path, required=True, help="data.json (schema v1)")
    parser.add_argument("--prev", type=pathlib.Path, help="the previous health.json (missing is fine)")
    parser.add_argument("--posted-state", type=pathlib.Path, help="post_health.py's state file, for the tracking issue it filed (missing is fine)")
    parser.add_argument("--out", type=pathlib.Path, help="where to write health.json (default: stdout)")
    parser.add_argument(
        "--now",
        type=parse_when,
        help="evaluate as of this time and skip the staleness check (default: data.json's generated_at, aged against the wall clock)",
    )
    parser.add_argument("--fixture-status", type=pathlib.Path, help="optional fixtures.json to surface in metrics")
    parser.add_argument("--pool-pressure", type=pathlib.Path, help="the pool-pressure periodic's pool-pressure.json, for rule 8 (missing is fine)")
    parser.add_argument("--fixture-state", type=pathlib.Path, help="the hourly fleet scan's fixture-state.json, for the fixture_drift condition (missing is fine)")
    parser.add_argument("--case-notes", type=pathlib.Path, default=DEFAULT_CASE_NOTES, help="case-notes.yaml for tracking issues")
    roster = parser.add_mutually_exclusive_group()
    roster.add_argument("--admitted", help="comma-separated admitted roster (default: hack/eval/blocking-roster.txt)")
    roster.add_argument("--roster-history", help="JSON [{since, admitted[]}] of roster eras, for replay over history")
    parser.add_argument("--blocking-roster", "--ci-eval-script", dest="blocking_roster", type=pathlib.Path, default=BLOCKING_ROSTER_FILE, help=argparse.SUPPRESS)
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
            posted=load_json(args.posted_state),
            pool_pressure=load_json(args.pool_pressure),
            fixture_state_doc=load_json(args.fixture_state),
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
