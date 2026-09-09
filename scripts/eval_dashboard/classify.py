#!/usr/bin/env python3
"""Classify one smoke-test run: which of its failures are the gate's and
which are the pull request's.

The gate comment on a pull request, the dashboard's PR view
(``run.html?build=<id>``) and the incident brief all answer the same
question about a red run -- "is this mine?" -- and they must answer it the
same way, so the rules live here once. ``classify_run`` is the whole
interface::

    classify_run(run, runs, health_at=None, now=None, admitted=None) -> {
        "build": str, "pr": int|None, "headline": str, "lede": str,
        "verdict": "red" | "green" | "infra",
        "cases": [{"case", "outcome", "cls", "also_failing_prs",
                   "pass_rate_30d", "reason", "excerpt", "do",
                   "admitted", "reps"}],
        "matches_incident": bool,
        # run-level detail: "setup_death", "storm_reps", "cls", "do"
    }

The first line of keys is the contract other callers rely on; the rest is
additive detail the pages show.

``run`` and ``runs`` are data.json shapes (SCHEMA.md); ``health_at`` is the
health.json document in force when the run finished (state, condition,
failing_cases are read; anything else is ignored); ``now`` anchors the
30-day pass rate and defaults to the run's finish. Every rule below is a
module constant with the incident it was tuned on; the vocabulary
(``shared_break`` / ``storm`` / ``setup_deaths``, storm-classified
repetitions, collapsed cases) is the CI health adjudicator's, restated here
so this module has no dependency beyond the standard library.

Per failed admitted case, in priority order:

* ``shared`` -- the same case failed every graded repetition on runs of at
  least SHARED_MIN_OTHER_PRS other pull requests that finished inside
  [start - SHARED_WINDOW_BEFORE, finish], or the health verdict names it.
  The 2026-09-07/08 crashloop outage (#1269, #1278) is the shape.
* ``storm`` -- the run lost at least STORM_RUN_SIGNATURE_REPS repetitions
  to 429s or empty records, or the verdict says storm (#1225, #1214).
* ``only-this-pr`` -- the case passed on the last ONLY_PR_MIN_OTHER_RUNS
  runs of other pull requests inside ONLY_PR_WINDOW and failed here.
* ``None`` -- nothing above fits; the page says it cannot tell.

``setup`` is a run-level class: no tasks, a FAILURE verdict, under
SETUP_DEATH_MAX_DURATION (#1172). Such a run has no cases to classify.

Only stdlib.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timedelta, timezone

# --- Shared break (#1269, #1278; #1171 a week earlier) -----------------------
# Other pull requests' runs are looked at when they finished inside this
# long before the run started, up to the run's own finish: the same six
# hours the adjudicator's shared-break rule uses.
SHARED_WINDOW_BEFORE = timedelta(hours=6)
# A case collapsing on this many *other* distinct pull requests is shared.
# Two others plus this run is the adjudicator's three-PR floor.
SHARED_MIN_OTHER_PRS = 2

# --- Only this PR (#913's image-tag failures; the adjudicator's PR-caused rule) --
# The case must have passed on this many of the most recent other-PR runs
# that graded it, all inside ONLY_PR_WINDOW before this run's finish.
ONLY_PR_MIN_OTHER_RUNS = 3
ONLY_PR_WINDOW = timedelta(hours=24)

# --- Storm (#1225, #1097, #1214) -----------------------------------------------
# A run carrying at least this many storm-classified repetitions is inside
# the storm (the adjudicator's STORM_RUN_SIGNATURE_REPS); fewer are
# background noise on any day.
STORM_RUN_SIGNATURE_REPS = 5
# The harness's own never-ran phrasings (bench/kube_agents_bench/scoring.py),
# the same list the adjudicator matches; before #1184 these were graded `fail`.
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

# --- Setup death (#1172, #1176) -----------------------------------------------
# Zero tasks, concluded FAILURE, and over inside this long: the run died at
# clone or deploy before any case ran (the adjudicator's setup-death rule).
SETUP_DEATH_MAX_DURATION = timedelta(minutes=5)

# --- Pass rate ---------------------------------------------------------------
# The per-case pass rate the PR view quotes is over runs started inside
# this many days before `now`, run-level events excluded as on the legacy
# page (a broken run's failures are the run's, not the cases').
PASS_RATE_DAYS = 30
RUN_EVENT_FAIL_FRACTION = 0.8
# Repetition results the collector writes (SCHEMA.md); anything else is
# "not measured".
REP_RESULTS = ("pass", "fail", "infra")
# Memo bounds for the per-run derivations and the 30-day pass rates.
RUN_CACHE_MAX = 4096
RATE_CACHE_MAX = 8

# --- Vocabulary shared with health.json ---------------------------------------
STATE_GREEN = "GREEN"
CONDITION_SHARED_BREAK = "shared_break"
CONDITION_STORM = "storm"
CONDITION_SETUP_DEATHS = "setup_deaths"
RUN_SUCCESS = "SUCCESS"
RUN_FAILURE = "FAILURE"
RUN_ABORTED = "ABORTED"

OUTCOME_PASSED = "passed"
OUTCOME_PARTIAL = "partial"
OUTCOME_FAILED = "failed"
OUTCOME_INFRA = "infra"
CLS_SHARED = "shared"
CLS_ONLY_THIS_PR = "only-this-pr"
CLS_STORM = "storm"
CLS_SETUP = "setup"
VERDICT_RED = "red"
VERDICT_GREEN = "green"
VERDICT_INFRA = "infra"

# --- Roster -------------------------------------------------------------------
# Only an admitted case reds a pull request (AGENTS.md, "The behavioural
# presubmit gate"); a hold-out failing is reported but never blamed. Read
# from the checkout; when the script is missing every case counts as
# admitted, which over-reports rather than hides.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CI_EVAL_SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
ROSTER_RE = re.compile(r'BOOTSTRAP_ADMITTED="\$\{BOOTSTRAP_ADMITTED:-([^}]*)\}"')

# The grader prefixes every reason with its score; the reader wants the
# check name and what was missing.
REASON_SCORE_PREFIX_RE = re.compile(r"^VerificationCorrectness=\S+ \(floor [^)]*\) -- ")

# --- What to do, per class. Plain words; the reader is deciding whether to
# type /retest. ---------------------------------------------------------------
DO_SHARED = "Nothing. This failure is the gate's; retest once the brief says it is healthy again."
DO_STORM = "Retest after the storm clears; a run started inside it loses repetitions to 429s."
DO_ONLY_THIS_PR = "Fix the PR. Read the transcript first; it usually names the problem."
DO_SETUP = "Retest. If it dies the same way again, the leased project is the suspect, not your change."
DO_UNCLEAR = "Read the transcript. Nothing else on the gate matches this failure yet, so it may be yours."
DO_HELD_OUT = "Nothing for the gate; this case is held out and does not block."
DO_PASSED = ""

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# data.json access (tolerant of absent optional fields)
# --------------------------------------------------------------------------- #


def parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def run_tasks(run: dict) -> list[dict]:
    return [t for t in run.get("tasks") or [] if isinstance(t, dict)]


def task_reps(task: dict) -> list[dict]:
    """The task's repetitions, or one synthetic rep from its single result
    (SCHEMA.md: an absent ``reps`` means unknown, not empty)."""
    reps = [r for r in task.get("reps") or [] if isinstance(r, dict)] if isinstance(task.get("reps"), list) else []
    if reps:
        return reps
    result = str(task.get("result") or "").lower()
    if result in REP_RESULTS:
        return [{"result": result, "reason": None}]
    return []


def rep_kind(rep: dict) -> str:
    """'pass' | 'fail' | 'storm' -- the adjudicator's three kinds. A storm rep is
    one the harness could not grade: an infra verdict, or a fail whose
    reason is a never-ran phrasing."""
    result = str(rep.get("result") or "").lower()
    if result == "pass":
        return "pass"
    if result == "infra":
        return "storm"
    if STORM_REASON_RE.search(rep.get("reason") or ""):
        return "storm"
    return "fail"


def rep_counts(task: dict) -> dict:
    counts = {"pass": 0, "fail": 0, "infra": 0}
    for rep in task_reps(task):
        kind = rep_kind(rep)
        counts["infra" if kind == "storm" else kind] += 1
    return counts


def outcome_of(counts: dict) -> str | None:
    """passed (every graded rep passed), partial, failed (every graded rep
    failed), infra (nothing graded), None (no reps at all)."""
    if counts["pass"] and counts["fail"]:
        return OUTCOME_PARTIAL
    if counts["fail"]:
        return OUTCOME_FAILED
    if counts["pass"]:
        return OUTCOME_PASSED
    if counts["infra"]:
        return OUTCOME_INFRA
    return None


# Per-run derivations are asked for once per (run, other run) pair when a
# whole data.json is classified; memoized by object identity, bounded.
_RUN_CACHE: dict[int, tuple[dict, dict]] = {}


def _run_facts(run: dict) -> dict:
    """{outcomes: {case: outcome}, collapsed: set, storm: int} for a run."""
    key = id(run)
    hit = _RUN_CACHE.get(key)
    if hit is not None and hit[0] is run:
        return hit[1]
    outcomes = {}
    counts_by_case = {}
    storm = 0
    for task in run_tasks(run):
        counts = rep_counts(task)
        name = str(task.get("name"))
        counts_by_case[name] = counts
        outcomes[name] = outcome_of(counts)
        storm += counts["infra"]
    started = parse_iso(run.get("started"))
    facts = {
        "outcomes": outcomes,
        "counts": counts_by_case,
        "collapsed": {c for c, o in outcomes.items() if o == OUTCOME_FAILED},
        "storm": storm,
        "started": started,
        "finished": parse_iso(run.get("finished")) or started,
    }
    if len(_RUN_CACHE) >= RUN_CACHE_MAX:
        _RUN_CACHE.clear()
    _RUN_CACHE[key] = (run, facts)
    return facts


def collapsed_cases(run: dict) -> set[str]:
    """Cases that failed every graded repetition (the adjudicator's collapse)."""
    return _run_facts(run)["collapsed"]


def storm_reps(run: dict) -> int:
    return _run_facts(run)["storm"]


def run_length(run: dict) -> timedelta | None:
    """duration_s, else finish - start (the collector falls back the same
    way when the log has no verdict line)."""
    seconds = run.get("duration_s")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return timedelta(seconds=seconds)
    start, finish = parse_iso(run.get("started")), parse_iso(run.get("finished"))
    if start and finish and finish >= start:
        return finish - start
    return None


def is_setup_death(run: dict) -> bool:
    length = run_length(run)
    return (
        not run_tasks(run)
        and str(run.get("result") or "").upper() == RUN_FAILURE
        and length is not None
        and length < SETUP_DEATH_MAX_DURATION
    )


def is_run_event(run: dict) -> bool:
    graded = [o for o in _run_facts(run)["outcomes"].values() if o in (OUTCOME_PASSED, OUTCOME_PARTIAL, OUTCOME_FAILED)]
    return bool(graded) and graded.count(OUTCOME_FAILED) / len(graded) >= RUN_EVENT_FAIL_FRACTION


def run_start(run: dict) -> datetime | None:
    return _run_facts(run)["started"]


def run_finish(run: dict) -> datetime | None:
    return _run_facts(run)["finished"]


def clean_reason(reason: str | None) -> str:
    return REASON_SCORE_PREFIX_RE.sub("", reason or "").strip()


def first_reason(task: dict) -> str:
    """The first graded failure's reason, else the first storm rep's."""
    graded = [r for r in task_reps(task) if rep_kind(r) == "fail" and r.get("reason")]
    other = [r for r in task_reps(task) if r.get("reason")]
    for rep in graded + other:
        return clean_reason(rep.get("reason"))
    return ""


def excerpt_of(task: dict) -> str | None:
    """A report excerpt, when a collector ever records one (additive,
    optional: ``tasks[].excerpt`` or ``reps[].excerpt``). Never invented."""
    if isinstance(task.get("excerpt"), str) and task["excerpt"].strip():
        return task["excerpt"].strip()
    for rep in task_reps(task):
        if isinstance(rep.get("excerpt"), str) and rep["excerpt"].strip():
            return rep["excerpt"].strip()
    return None


# --------------------------------------------------------------------------- #
# Roster
# --------------------------------------------------------------------------- #

_ROSTER_CACHE: dict[str, frozenset | None] = {}


def admitted_cases(script: pathlib.Path = CI_EVAL_SCRIPT) -> frozenset | None:
    """BOOTSTRAP_ADMITTED from hack/ci-eval-pr.sh; None when unreadable."""
    key = str(script)
    if key not in _ROSTER_CACHE:
        roster = None
        try:
            match = ROSTER_RE.search(script.read_text())
            if match:
                roster = frozenset(name for name in match.group(1).split(",") if name)
        except OSError:
            roster = None
        _ROSTER_CACHE[key] = roster
    return _ROSTER_CACHE[key]


# --------------------------------------------------------------------------- #
# Pass rates
# --------------------------------------------------------------------------- #

_RATE_CACHE: dict[tuple, dict] = {}


def case_pass_rates(runs: list[dict], now: datetime) -> dict[str, float | None]:
    """{case: pass / (pass + fail)} over runs started inside PASS_RATE_DAYS
    before `now`, run-level events excluded; None when nothing graded."""
    key = (id(runs), len(runs), now)
    if key in _RATE_CACHE:
        return _RATE_CACHE[key]
    since = now - timedelta(days=PASS_RATE_DAYS)
    tally: dict[str, list[int]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        started = run_start(run)
        if started is None or not (since <= started <= now) or is_run_event(run):
            continue
        for name, counts in _run_facts(run)["counts"].items():
            bucket = tally.setdefault(name, [0, 0])
            bucket[0] += counts["pass"]
            bucket[1] += counts["fail"]
    rates = {case: (p / (p + f) if p + f else None) for case, (p, f) in tally.items()}
    if len(_RATE_CACHE) >= RATE_CACHE_MAX:
        _RATE_CACHE.clear()
    _RATE_CACHE[key] = rates
    return rates


# --------------------------------------------------------------------------- #
# The rules
# --------------------------------------------------------------------------- #


def _other_pr_runs(run: dict, runs: list[dict]) -> list[dict]:
    pr = run.get("pr")
    return [
        r
        for r in runs
        if isinstance(r, dict) and r is not run and r.get("build_id") != run.get("build_id") and (pr is None or r.get("pr") != pr) and run_tasks(r)
    ]


def shared_prs(case: str, run: dict, others: list[dict]) -> set:
    """Other pull requests on whose runs `case` collapsed inside the shared
    window: finished in [start - SHARED_WINDOW_BEFORE, finish]."""
    start, finish = run_start(run), run_finish(run)
    if start is None or finish is None:
        return set()
    low = start - SHARED_WINDOW_BEFORE
    prs = set()
    for other in others:
        when = run_finish(other)
        if when is None or not (low <= when <= finish):
            continue
        if case in collapsed_cases(other):
            prs.add(other.get("pr"))
    return prs


def passed_elsewhere(case: str, run: dict, others: list[dict]) -> bool:
    """The last ONLY_PR_MIN_OTHER_RUNS other-PR runs inside ONLY_PR_WINDOW
    that graded `case` all passed it outright."""
    finish = run_finish(run)
    if finish is None:
        return False
    low = finish - ONLY_PR_WINDOW
    graded = []
    for other in others:
        when = run_finish(other)
        if when is None or not (low <= when <= finish):
            continue
        outcome = _run_facts(other)["outcomes"].get(case)
        if outcome in (OUTCOME_PASSED, OUTCOME_PARTIAL, OUTCOME_FAILED):
            graded.append((when, outcome))
    graded.sort()
    recent = graded[-ONLY_PR_MIN_OTHER_RUNS:]
    return len(recent) >= ONLY_PR_MIN_OTHER_RUNS and all(o == OUTCOME_PASSED for _, o in recent)


def _health_fields(health_at: dict | None) -> tuple[str | None, str | None, set]:
    if not isinstance(health_at, dict):
        return None, None, set()
    state = str(health_at.get("state") or "").upper() or None
    condition = health_at.get("condition") if isinstance(health_at.get("condition"), str) else None
    cases = health_at.get("failing_cases")
    named = {str(c) for c in cases} if isinstance(cases, list) else set()
    if state == STATE_GREEN or state is None:
        return state, None, set()
    return state, condition, named


def classify_case(task: dict, run: dict, others: list[dict], admitted: frozenset | None, health_at: dict | None, rates: dict, run_storm: bool) -> dict:
    name = str(task.get("name"))
    counts = rep_counts(task)
    outcome = outcome_of(counts)
    is_admitted = admitted is None or name in admitted
    _, condition, named = _health_fields(health_at)
    cls = None
    also = 0
    do = DO_PASSED
    if outcome == OUTCOME_FAILED:
        prs = shared_prs(name, run, others)
        also = len(prs)
        if name in named or also >= SHARED_MIN_OTHER_PRS:
            cls = CLS_SHARED
        elif run_storm or condition == CONDITION_STORM:
            cls = CLS_STORM
        elif passed_elsewhere(name, run, others):
            cls = CLS_ONLY_THIS_PR
        do = {CLS_SHARED: DO_SHARED, CLS_STORM: DO_STORM, CLS_ONLY_THIS_PR: DO_ONLY_THIS_PR}.get(cls, DO_UNCLEAR)
        if not is_admitted:
            do = DO_HELD_OUT
    elif outcome == OUTCOME_INFRA:
        if run_storm or condition == CONDITION_STORM:
            cls = CLS_STORM
            do = DO_STORM
    return {
        "case": name,
        "outcome": outcome,
        "cls": cls,
        "also_failing_prs": also,
        "pass_rate_30d": rates.get(name),
        "reason": first_reason(task) if outcome in (OUTCOME_FAILED, OUTCOME_PARTIAL, OUTCOME_INFRA) else "",
        "excerpt": excerpt_of(task),
        "do": do,
        # Additive detail the pages show; the keys above are the contract.
        "admitted": is_admitted,
        "reps": counts,
    }


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def headline_for(cases: list[dict], run: dict, incident: bool, has_incident: bool) -> tuple[str, str, str]:
    """(headline, lede, verdict) for a run that recorded tasks."""
    gate = [c for c in cases if c["admitted"] and c["outcome"] in (OUTCOME_PASSED, OUTCOME_PARTIAL, OUTCOME_FAILED)]
    held = [c for c in cases if not c["admitted"]]
    held_failed = [c for c in held if c["outcome"] == OUTCOME_FAILED]
    failed = [c for c in gate if c["outcome"] == OUTCOME_FAILED]
    n = len(gate)
    held_note = (
        f" {_plural(len(held_failed), 'held-out case')} also failed; held-out cases do not block."
        if held_failed
        else ""
    )
    if not gate:
        if any(c["outcome"] == OUTCOME_INFRA for c in cases):
            return (
                "Nothing was graded: every repetition was lost before the agent ran.",
                "That is the quota storm's shape, not a verdict on your change." + held_note,
                VERDICT_INFRA,
            )
        return ("No gate case ran in this build.", held_note.strip() or "Check the build log.", VERDICT_INFRA)
    if failed and str(run.get("result") or "").upper() == RUN_SUCCESS:
        # Prow's verdict is the gate's: a green run with collapsed cases is
        # one the gate excused (infra-excluded repetitions, a roster older
        # than this checkout's). Report it green and say what failed.
        return (
            f"Prow passed this run; {_plural(len(failed), 'gate case')} failed every graded repetition.",
            "The gate counted it green, so nothing here blocks the PR. The failures are listed for the record." + held_note,
            VERDICT_GREEN,
        )
    if not failed and str(run.get("result") or "").upper() == RUN_FAILURE:
        # Red without a collapsed gate case: an absolute rule tripped (a
        # forbidden mutation, a verifier error, an inconsistent record) or
        # the log was cut short. The build log has it; the cases do not.
        return (
            "The run is red, but no gate case failed outright.",
            "An absolute rule tripped or the log was cut short; the build log has the reason, the case list does not." + held_note,
            VERDICT_RED,
        )
    if not failed:
        partial = [c for c in gate if c["outcome"] == OUTCOME_PARTIAL]
        lede = (
            f"{_plural(len(partial), 'case')} passed on a retry (some repetitions failed); the gate counts that as a pass."
            if partial
            else "Every gate case passed on every repetition."
        )
        return (f"All {n} gate cases passed.", lede + held_note, VERDICT_GREEN)
    theirs = [c for c in failed if c["cls"] in (CLS_SHARED, CLS_STORM)]
    yours = [c for c in failed if c["cls"] == CLS_ONLY_THIS_PR]
    unclear = [c for c in failed if c["cls"] is None]
    f = len(failed)
    what = "the outage" if (incident and has_incident) else "failures on other PRs"
    if len(theirs) == f:
        head = f"{f} of {n} gate cases failed. None of them look like your PR."
        lede = f"{'All ' if f > 1 else ''}{_plural(f, 'failure')} match{'' if f > 1 else 'es'} {what}. Your other {n - f} gate cases passed."
        return head, lede + held_note, VERDICT_INFRA
    if len(yours) == f:
        head = (
            f"1 of {n} gate cases failed, and it looks like your PR."
            if f == 1
            else f"{f} of {n} gate cases failed, and they look like your PR."
        )
        lede = f"{'This case passes' if f == 1 else 'These cases pass'} on other PRs' recent runs and failed here."
        return head, lede + held_note, VERDICT_RED
    if not yours:
        head = f"{f} of {n} gate cases failed. We can't tell yet whether {'it is' if f == 1 else 'they are'} your PR."
        if theirs:
            head = f"{len(theirs)} of {f} failures match {what}; {len(unclear)} {'is' if len(unclear) == 1 else 'are'} unexplained so far."
        lede = "Nothing on other PRs matches the unexplained failure yet; read its transcript."
        return head, lede + held_note, VERDICT_RED
    yours_text = f"{len(yours)} {'is' if len(yours) == 1 else 'are'} only on your PR"
    if not theirs:
        head = f"{len(yours)} of {f} failures {'is' if len(yours) == 1 else 'are'} only on your PR; {len(unclear)} {'is' if len(unclear) == 1 else 'are'} unexplained."
    elif unclear:
        head = f"{len(theirs)} of {f} failures match {what}; {yours_text} and {len(unclear)} unexplained."
    else:
        head = f"{len(theirs)} of {f} failures match {what}; {yours_text}."
    lede = "Fix the ones marked only your PR; the rest clear with the gate."
    return head, lede + held_note, VERDICT_RED


def classify_run(run: dict, runs: list[dict], health_at: dict | None = None, now: datetime | None = None, admitted: frozenset | None = None) -> dict:
    """See the module docstring. `admitted` overrides the roster read from
    the checkout (tests, and a replay over history when the roster moved)."""
    if admitted is None:
        admitted = admitted_cases()
    finish = run_finish(run)
    anchor = now or finish or datetime.now(UTC)
    state, condition, named = _health_fields(health_at)
    has_incident = state is not None and state != STATE_GREEN
    build = str(run.get("build_id") or "")
    base = {"build": build, "pr": run.get("pr"), "cases": [], "matches_incident": False}

    tasks = run_tasks(run)
    if not tasks:
        result = str(run.get("result") or "").upper()
        length = run_length(run)
        minutes = int(length.total_seconds() // 60) if length is not None else None
        if is_setup_death(run):
            return dict(
                base,
                headline="The run died during setup, before any case ran.",
                lede="A clone or deploy failure on the leased project; the agent was never started." + ("" if condition != CONDITION_SETUP_DEATHS else " Other PRs are dying the same way right now."),
                verdict=VERDICT_INFRA,
                setup_death=True,
                cls=CLS_SETUP,
                do=DO_SETUP,
                matches_incident=condition == CONDITION_SETUP_DEATHS,
            )
        if result == RUN_ABORTED:
            return dict(base, headline="Aborted before it finished.", lede="Usually a newer push superseded this run; the next one carries the verdict.", verdict=VERDICT_INFRA, setup_death=False, cls=None, do="")
        if result == RUN_SUCCESS:
            # hack/ci-eval-pr.sh step 0: an inert push is revalidated against
            # the branch's earlier green and exits before the eval matrix.
            return dict(base, headline="Green without running the cases.", lede="Only inert paths changed since this branch's last green run, so the gate revalidated that run instead of spending another.", verdict=VERDICT_GREEN, setup_death=False, cls=None, do="")
        when = f" {minutes} minutes in" if minutes is not None else ""
        return dict(
            base,
            headline=f"The run failed before any case ran{when}.",
            lede="Check the build log: a broken image build or deploy on this branch looks like this.",
            verdict=VERDICT_RED,
            setup_death=False,
            cls=None,
            do="Read the build log; the failure is before the eval loop.",
        )

    others = _other_pr_runs(run, runs)
    rates = case_pass_rates(runs, anchor)
    run_storm = storm_reps(run) >= STORM_RUN_SIGNATURE_REPS
    cases = [classify_case(t, run, others, admitted, health_at, rates, run_storm) for t in tasks]
    cases = [c for c in cases if c["outcome"] is not None]

    failed_names = {c["case"] for c in cases if c["outcome"] == OUTCOME_FAILED and c["admitted"]}
    if condition == CONDITION_SHARED_BREAK:
        matches = bool(failed_names & named)
    elif condition == CONDITION_STORM:
        matches = run_storm
    elif condition == CONDITION_SETUP_DEATHS:
        matches = False
    else:
        matches = False
    headline, lede, verdict = headline_for(cases, run, matches, has_incident)
    return dict(
        base,
        headline=headline,
        lede=lede,
        verdict=verdict,
        cases=cases,
        matches_incident=matches,
        setup_death=False,
        cls=None,
        do="",
        storm_reps=storm_reps(run),
    )
