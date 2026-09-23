"""health.py calls the week of 2026-09-01 the way the eval crew did by hand.

Two layers. The rule tests build small synthetic data.json documents and pin
each threshold from both sides. The replay tests walk the REAL week --
testdata_health/data.json.gz is a published data.json trimmed to the runs
that finished in [2026-09-01, 2026-09-09), the last of them on 09-08, and to
the fields the adjudicator reads (SCHEMA.md, Fixtures) -- as if the job had
ticked every 30 minutes, and assert the state timeline against the incidents
filed that week:

    #1171  compliance-rbac-overgrant collapsing on unrelated PRs from 09-02 ~02:00Z
    #1189  rca-remediation-pr, same shape, 09-02 evening
    #1214  the 09-03 token-quota storm (five-hour builds, reps lost to infra)
    #1269  seeded-a saturated after the 09-07 auto-upgrade
    #1278  the crashloop trio redding every PR from 09-08

The roster moved four times in that week (testdata_health/roster-history.json,
from the commits that changed BOOTSTRAP_ADMITTED); the replay judges each
run by the roster at its start, which is what makes the 09-02 outage
visible at all -- both cases were demoted the same day.
"""

import contextlib
import gzip
import io
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from eval_dashboard import health

TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_health"
FIXTURE = TESTDATA / "data.json.gz"
ROSTER_HISTORY = TESTDATA / "roster-history.json"
# 2026-09-11, the build-cluster node loss (#1478), and BOOTSTRAP_ADMITTED as
# hack/ci-eval-pr.sh had it that day.
LOST_FIXTURE = TESTDATA / "lost-pods-2026-09-11.json.gz"
# The week ending 2026-09-14 18:20Z: every run green, every run long (#1586).
SLOW_FIXTURE = TESTDATA / "slow-gate-2026-09-14.json.gz"
ROSTER_0911 = [
    "reliability-pdb-probe",
    "security-overgrant-probe",
    "upgrades-lagging-master-probe",
    "consistency-authorized-networks-probe",
    "cost-idle-pool-probe",
    "obtainability-remediation-proposal",
    "cluster-agent-crashloop-debug",
    "cluster-agent-crashloop-misleading-symptom",
    "cluster-agent-crashloop-evidence-chain",
    "agent-kanban-smoke",
]
CASE_NOTES = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "case-notes.yaml"

UTC = timezone.utc
T0 = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
# Figures json.loads produces that int() cannot take: the `Infinity` and `NaN`
# literals it accepts, a finite float whose product with 60 is not, and an
# integer no float can hold.
INF = float("inf")
NAN = float("nan")
OVERFLOWS = 1e308
HUGE_INT = 10**400

CRASHLOOP_TRIO = [
    "cluster-agent-crashloop-debug",
    "cluster-agent-crashloop-evidence-chain",
    "cluster-agent-crashloop-misleading-symptom",
]
ADMITTED = frozenset(CRASHLOOP_TRIO + ["reliability-pdb-probe", "agent-kanban-smoke"])
HOLD_OUT = "autoops-warning-event-triage"

EMPTY_RECORD = "the record is not evidence of a real agent run: the trajectory is empty: the agent made no tool calls"
NEVER_RAN = "the record shows no agent ever ran: the trajectory is empty and tokens.total is 0"
RETRIES = "the harness exhausted its retries without reaching the agent (KUBE_AGENTS_INFRA_FAILURE): "
GRADED_FAIL = "VerificationCorrectness=0.0 (floor 1.0) -- rca-names-the-oom: required phrases absent"
# The scorer's delegation-ceiling marker, read out of its source: this test
# does not import the bench package, and the literal must not drift.
SCORER = pathlib.Path(__file__).resolve().parent.parent / "bench" / "kube_agents_bench" / "scoring.py"


def _scorer_ceiling_marker() -> str:
    for line in SCORER.read_text(encoding="utf-8").splitlines():
        if line.startswith("DELEGATION_CEILING_MARKER = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise AssertionError(f"DELEGATION_CEILING_MARKER not found in {SCORER}")


SCORER_CEILING_MARKER = _scorer_ceiling_marker()
CEILING = f"{SCORER_CEILING_MARKER}: the harness's delegation wait ran out before any delegated card delivered a result, so the record holds the acknowledgement alone and nothing to grade (delegated tasks did not finish within 2700s: t_2282937f (running))"


# --------------------------------------------------------------------------- #
# Synthetic data.json builders
# --------------------------------------------------------------------------- #

REP_LETTER = {
    "p": {"result": "pass", "reason": None},
    "f": {"result": "fail", "reason": GRADED_FAIL},
    "i": {"result": "infra", "reason": RETRIES},
    "e": {"result": "fail", "reason": EMPTY_RECORD},
    "c": {"result": "infra", "reason": CEILING},
}


def task(name, letters):
    reps = [dict(REP_LETTER[letter], n=i + 1) for i, letter in enumerate(letters)]
    if all(letter == "p" for letter in letters):
        result = "pass"
    elif all(letter == "i" for letter in letters):
        result = "infra"
    else:
        result = "fail"
    return {"name": name, "result": result, "duration_s": None, "outcome_validity": None, "reps": reps}


def run(build_id, pr, finished, minutes=120, result=None, tasks=None):
    """A run finishing at `finished` (datetime) after `minutes` of wall clock."""
    tasks = tasks or []
    if result is None:
        result = "FAILURE" if any(t["result"] == "fail" for t in tasks) else "SUCCESS"
    started = finished - timedelta(minutes=minutes)
    return {
        "build_id": str(build_id),
        "pr": pr,
        "head_sha": "abc1234",
        "project": "kube-agents-evals-1",
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "result": result,
        "duration_s": minutes * 60,
        "tasks": tasks,
    }


def data(*runs):
    return {"schema_version": 1, "generated_at": T0.isoformat(), "source": "logs", "runs": list(runs)}


def green_tasks():
    return [task(name, "ppp") for name in sorted(ADMITTED)] + [task(HOLD_OUT, "fff")]


def full_tasks():
    """Eighteen passing cases: a full run in rule 7's sense (SLOW_MIN_TASKS)."""
    return [task(f"case-{k}", "ppp") for k in range(18)]


def broken_tasks(cases):
    return [task(name, "fff" if name in cases else "ppp") for name in sorted(ADMITTED)]


def assess(doc, now, roster=None):
    return health.assess(health.load_runs(doc), now, roster or health.Roster.fixed(ADMITTED))


def adjudicate(doc, now, prev=None, roster=None):
    return health.adjudicate(doc, now, prev, roster or health.Roster.fixed(ADMITTED))


# --------------------------------------------------------------------------- #
# Repetition and task classification
# --------------------------------------------------------------------------- #


class RepKinds(unittest.TestCase):
    def test_the_harness_phrasings_are_storm_whatever_the_verdict_token(self):
        for reason in (EMPTY_RECORD, NEVER_RAN, RETRIES, "HTTP 429 from the endpoint", "RESOURCE_EXHAUSTED"):
            self.assertEqual(health.rep_kind({"result": "fail", "reason": reason}), "storm", reason)
        self.assertEqual(health.rep_kind({"result": "infra", "reason": None}), "storm")

    def test_a_graded_failure_is_a_fail_and_a_pass_is_a_pass(self):
        self.assertEqual(health.rep_kind({"result": "fail", "reason": GRADED_FAIL}), "fail")
        self.assertEqual(health.rep_kind({"result": "fail", "reason": None}), "fail")
        self.assertEqual(health.rep_kind({"result": "pass", "reason": None}), "pass")

    def test_collapse_ignores_storm_reps_and_needs_a_graded_fail(self):
        self.assertTrue(health.Task(task("x", "fff")).collapsed)
        self.assertTrue(health.Task(task("x", "ffe")).collapsed, "an empty record is not a pass")
        self.assertFalse(health.Task(task("x", "ffp")).collapsed, "one pass out of three is not a collapse")
        self.assertFalse(health.Task(task("x", "eee")).collapsed, "nothing graded, nothing collapsed")
        self.assertFalse(health.Task(task("x", "iii")).collapsed)

    def test_a_task_without_reps_stands_in_for_one_repetition(self):
        self.assertTrue(health.Task({"name": "x", "result": "fail"}).collapsed)
        self.assertEqual(health.Task({"name": "x", "result": "infra"}).storms, 1)
        self.assertFalse(health.Task({"name": "x", "result": "pass"}).collapsed)

    def test_the_ceiling_marker_is_the_scorers(self):
        self.assertEqual(health.DELEGATION_CEILING_MARKER, SCORER_CEILING_MARKER)

    def test_a_ceiling_rep_is_its_own_kind_whatever_the_verdict_token(self):
        self.assertEqual(health.rep_kind({"result": "infra", "reason": CEILING}), "ceiling")
        self.assertEqual(health.rep_kind({"result": "fail", "reason": CEILING}), "ceiling")
        self.assertEqual(health.rep_kind({"result": "infra", "reason": RETRIES}), "storm")

    def test_ceiling_reps_are_neither_graded_nor_storms(self):
        t = health.Task(task("x", "ccc"))
        self.assertEqual((t.passes, t.fails, t.storms, t.ceilings, t.graded), (0, 0, 0, 3, 0))
        self.assertFalse(t.collapsed, "nothing graded, nothing collapsed")
        self.assertTrue(health.Task(task("x", "ffc")).collapsed, "a ceiling rep never softens a collapse")


# --------------------------------------------------------------------------- #
# Rule 1: shared break
# --------------------------------------------------------------------------- #


class SharedBreak(unittest.TestCase):
    def broken_week(self, prs, cases=("cluster-agent-crashloop-debug",), spread_minutes=60):
        runs = []
        for i, pr in enumerate(prs):
            runs.append(run(100 + i, pr, T0 - timedelta(minutes=spread_minutes * (len(prs) - i)), tasks=broken_tasks(set(cases))))
        return data(*runs)

    def test_three_runs_on_three_prs_is_an_outage_naming_the_case(self):
        result = assess(self.broken_week([1, 2, 3]), T0)
        self.assertEqual(result["state"], "OUTAGE")
        self.assertEqual(result["failing_cases"], ["cluster-agent-crashloop-debug"])
        self.assertIn("cluster-agent-crashloop-debug failed all graded reps on 3 runs from 3 PRs (#1, #2, #3)", result["evidence"][0])

    def test_three_runs_on_two_prs_is_not(self):
        self.assertEqual(assess(self.broken_week([1, 2, 2]), T0)["state"], "GREEN")

    def test_a_hold_out_collapsing_everywhere_is_not_an_outage(self):
        doc = data(*(run(100 + i, i, T0 - timedelta(hours=i), tasks=green_tasks()) for i in range(1, 5)))
        self.assertEqual(assess(doc, T0)["state"], "GREEN")

    def test_one_pr_failing_while_others_pass_is_pr_caused(self):
        doc = data(
            run(1, 11, T0 - timedelta(hours=3), tasks=broken_tasks({"agent-kanban-smoke"})),
            run(2, 11, T0 - timedelta(hours=2), tasks=broken_tasks({"agent-kanban-smoke"})),
            run(3, 12, T0 - timedelta(hours=1), tasks=broken_tasks(set())),
            run(4, 13, T0 - timedelta(minutes=30), tasks=broken_tasks(set())),
        )
        result = assess(doc, T0)
        self.assertEqual(result["state"], "GREEN")
        self.assertIn("PR-caused: agent-kanban-smoke failing only on #11 (passing on 2 other PRs)", result["evidence"])

    def test_the_break_must_explain_most_reds_and_most_runs(self):
        # Three PRs share the collapse, but nine other runs are green: the
        # 2026-09-04 storm tail. Not an outage.
        greens = [run(200 + i, 50 + i, T0 - timedelta(minutes=20 * i), tasks=broken_tasks(set())) for i in range(9)]
        doc = self.broken_week([1, 2, 3])
        doc["runs"] += greens
        self.assertEqual(assess(doc, T0)["state"], "GREEN")
        # Three PRs share the collapse and four other reds are unrelated
        # single-PR failures, a different case each: the shared set explains
        # 3 of 7 reds, so not a shared break either.
        singles = ["reliability-pdb-probe", "agent-kanban-smoke", "cluster-agent-crashloop-evidence-chain", "cluster-agent-crashloop-misleading-symptom"]
        others = [run(300 + i, 70 + i, T0 - timedelta(minutes=25 * i), tasks=broken_tasks({case})) for i, case in enumerate(singles)]
        doc = self.broken_week([1, 2, 3])
        doc["runs"] += others
        result = assess(doc, T0)
        self.assertEqual(result["state"], "GREEN")
        self.assertIn("these cases explain 3 of 7 red runs (7 of 7 concluded runs red) in the last 6h", result["evidence"])

    def test_the_window_is_six_hours(self):
        self.assertEqual(assess(self.broken_week([1, 2, 3], spread_minutes=110), T0)["state"], "OUTAGE")
        self.assertEqual(assess(self.broken_week([1, 2, 3], spread_minutes=125), T0)["state"], "GREEN")

    def test_roster_eras_apply_per_run(self):
        roster = health.Roster.from_history(
            [
                {"since": (T0 - timedelta(days=2)).isoformat(), "admitted": ["cluster-agent-crashloop-debug"]},
                {"since": (T0 - timedelta(hours=4, minutes=30)).isoformat(), "admitted": []},
            ]
        )
        doc = self.broken_week([1, 2, 3, 4], spread_minutes=60)
        result = assess(doc, T0, roster)
        # Runs 1 and 2 started before the demotion (finished 4h and 3h ago,
        # 2h long, so started 6h and 5h ago); runs 3 and 4 started after it.
        # Two admitted collapses: no outage.
        self.assertEqual(result["state"], "GREEN")
        self.assertEqual(assess(doc, T0, health.Roster.fixed(ADMITTED))["state"], "OUTAGE", "the same runs under a fixed roster")
        self.assertEqual(roster.at(T0 - timedelta(days=3)), frozenset())
        self.assertEqual(roster.current, frozenset())

    def test_the_roster_is_read_from_the_roster_file_or_the_old_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            roster_file = pathlib.Path(tmp) / "blocking-roster.txt"
            roster_file.write_text("# the roster\na-probe\nb-probe  # admitted 09-01\n\n")
            self.assertEqual(health.Roster.from_file(roster_file).current, frozenset({"a-probe", "b-probe"}))
            # The shape the script carried before 2026-09-15, for an era
            # taken from `git show <old-commit>:hack/ci-eval-pr.sh`, has its
            # own reader; the file reader treats that line as prose.
            old_script = '#!/bin/bash\nexport BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-a-probe,b-probe}"\n'
            self.assertEqual(health.Roster.from_script_text(old_script).current, frozenset({"a-probe", "b-probe"}))
            roster_file.write_text("# a laptop run may export " + old_script.splitlines()[1] + "\nc-probe\n")
            self.assertEqual(health.Roster.from_file(roster_file).current, frozenset({"c-probe"}))
            with self.assertRaises(SystemExit):
                health.Roster.from_script_text("c-probe\n")
            roster_file.write_text("# nothing admitted\n")
            with self.assertRaises(SystemExit):
                health.Roster.from_file(roster_file)
        # And the real one parses to a non-empty roster of case names.
        live = health.Roster.from_file()
        self.assertTrue(live.current)
        self.assertTrue(all("/" not in name and " " not in name for name in live.current))


# --------------------------------------------------------------------------- #
# Rule 2: quota storm
# --------------------------------------------------------------------------- #


class Storm(unittest.TestCase):
    def stormy(self, prs, reps_per_run, letter="e", spread_minutes=20):
        runs = []
        for i, pr in enumerate(prs):
            tasks = [task(f"case-{k}", letter * 3) for k in range(reps_per_run // 3)]
            tasks += [task(name, "ppp") for name in sorted(ADMITTED)]
            runs.append(run(100 + i, pr, T0 - timedelta(minutes=spread_minutes * i), result="SUCCESS", tasks=tasks))
        return data(*runs)

    def test_fifteen_storm_reps_across_three_prs_degrades(self):
        result = assess(self.stormy([1, 2, 3], 6), T0)
        self.assertEqual(result["state"], "DEGRADED")
        self.assertEqual(result["condition"], "storm")
        self.assertRegex(result["cause"], r"quota storm window \d\d:\d\d–\d\d:\d\d UTC")
        self.assertIn("quota storm: 18 infra/empty-record reps across 3 PRs", result["evidence"][0])

    def test_infra_verdicts_count_the_same_as_empty_records(self):
        self.assertEqual(assess(self.stormy([1, 2, 3], 6, letter="i"), T0)["state"], "DEGRADED")

    def test_fourteen_reps_or_two_prs_do_not(self):
        self.assertEqual(assess(self.stormy([1, 2, 3], 3), T0)["state"], "GREEN", "9 reps")
        doc = self.stormy([1, 2, 2], 6)
        self.assertEqual(assess(doc, T0)["state"], "GREEN", "2 PRs")
        doc = self.stormy([1, 2, 3, 4, 5], 3)  # 15 reps on 5 PRs
        self.assertEqual(assess(doc, T0)["state"], "DEGRADED")

    def test_the_window_is_two_hours(self):
        self.assertEqual(assess(self.stormy([1, 2, 3], 6, spread_minutes=50), T0)["state"], "DEGRADED")
        self.assertEqual(assess(self.stormy([1, 2, 3], 6, spread_minutes=65), T0)["state"], "GREEN")

    def test_advice_says_when_to_retest(self):
        # The newest storm-hit run finished at T0 (00:00Z); plus the 30-minute cool-down.
        result = adjudicate(self.stormy([1, 2, 3], 6), T0)
        self.assertEqual(result["advice"], "Retest after 00:30 UTC; runs started inside the storm lose repetitions to 429s.")

    def test_the_storm_runs_themselves_do_not_count_as_its_recovery(self):
        # Three green runs with six storm reps each ARE the storm. Two hours
        # later the window has rolled past them and rule 2 no longer fires;
        # with no new runs the state must still be DEGRADED, recovering.
        doc = self.stormy([1, 2, 3], 6)
        prev = adjudicate(doc, T0)
        self.assertEqual(prev["state"], "DEGRADED")
        later = T0 + timedelta(hours=2, minutes=1)
        held = adjudicate(doc, later, prev)
        self.assertEqual((held["state"], held["recovering"]), ("DEGRADED", True), held)
        self.assertEqual(held["advice"], "The condition has cleared; a retest is reasonable. GREEN is reported after 3 consecutive green runs on distinct PRs.")
        # Three clean greens on new PRs after the storm: GREEN.
        doc["runs"] += [run(200 + i, 20 + i, later - timedelta(minutes=30 - 5 * i), result="SUCCESS", tasks=broken_tasks(set())) for i in range(3)]
        self.assertEqual(adjudicate(doc, later, held)["state"], "GREEN")


# --------------------------------------------------------------------------- #
# Rule 2b: delegation ceiling
# --------------------------------------------------------------------------- #


class DelegationCeiling(unittest.TestCase):
    """Rule 2 shape over ceiling reps (#1874, #1879): a fleet-wide worker
    stall is named here instead of reading GREEN while every PR says
    NOT EVALUATED."""

    def wave(self, prs, reps_per_run, spread_minutes=20, letter="c", nightly=False):
        runs = []
        for i, pr in enumerate(prs):
            tasks = [task(f"case-{k}", letter * 3) for k in range(reps_per_run // 3)]
            tasks += [task(name, "ppp") for name in sorted(ADMITTED)]
            doc = run(100 + i, pr, T0 - timedelta(minutes=spread_minutes * i), result="SUCCESS", tasks=tasks)
            runs.append(dict(doc, tier="nightly", pr=None) if nightly else doc)
        return data(*runs)

    def test_fifteen_ceiling_reps_across_three_prs_degrade_under_their_own_name(self):
        result = assess(self.wave([1, 2, 3], 6), T0)
        self.assertEqual((result["state"], result["condition"]), ("DEGRADED", "delegation_ceiling"))
        self.assertRegex(result["cause"], r"^delegation ceiling: 18 repetitions on 3 PRs ended with the worker still running \d\d:\d\d–\d\d:\d\d UTC$")
        self.assertIn("delegation ceiling: 18 reps across 3 PRs ended with the worker still running", result["evidence"][0])
        self.assertEqual((result["incident"]["reps"], result["incident"]["runs"], sorted(result["incident"]["prs"])), (18, 3, [1, 2, 3]))
        self.assertEqual(result["failing_cases"], [], "a ceiling wave is no collapse")
        self.assertFalse(any("quota storm" in line for line in result["evidence"]), "and no storm")

    def test_the_thresholds_and_window_are_the_storms(self):
        self.assertEqual((health.CEILING_MIN_REPS, health.CEILING_MIN_PRS, health.CEILING_WINDOW), (health.STORM_MIN_REPS, health.STORM_MIN_PRS, health.STORM_WINDOW))
        self.assertEqual(health.CEILING_RUN_SIGNATURE_REPS, health.STORM_RUN_SIGNATURE_REPS)
        self.assertEqual(assess(self.wave([1, 2, 3], 3), T0)["state"], "GREEN", "9 reps")
        self.assertEqual(assess(self.wave([1, 2, 2], 6), T0)["state"], "GREEN", "2 PRs")
        self.assertEqual(assess(self.wave([1, 2, 3, 4, 5], 3), T0)["state"], "DEGRADED", "15 reps on 5 PRs")
        self.assertEqual(assess(self.wave([1, 2, 3], 6, spread_minutes=65), T0)["state"], "GREEN", "outside the 2h window")

    def test_nightly_ceiling_reps_do_not_count(self):
        self.assertEqual(assess(self.wave([None, None, None], 6, nightly=True), T0)["state"], "GREEN")

    def test_the_storm_outranks_it_and_keeps_it_as_evidence(self):
        doc = self.wave([1, 2, 3], 6)
        stormy = [task(f"s-{k}", "eee") for k in range(2)] + [task(name, "ppp") for name in sorted(ADMITTED)]
        doc["runs"] += [run(200 + i, 10 + i, T0 - timedelta(minutes=5 * i), result="SUCCESS", tasks=stormy) for i in range(3)]
        result = assess(doc, T0)
        self.assertEqual(result["condition"], "storm")
        self.assertTrue(any(line.startswith("delegation ceiling: 18 reps") for line in result["evidence"]), result["evidence"])

    def test_advice_points_at_the_gateway_log(self):
        result = adjudicate(self.wave([1, 2, 3], 6), T0)
        self.assertEqual(result["advice"], health.ADVICE_CEILING)
        self.assertIn("#1879", result["advice"])
        self.assertIn("NOT EVALUATED", result["advice"])

    def test_the_wave_runs_themselves_do_not_count_as_its_recovery(self):
        doc = self.wave([1, 2, 3], 6)
        prev = adjudicate(doc, T0)
        self.assertEqual(prev["state"], "DEGRADED")
        later = T0 + timedelta(hours=2, minutes=1)
        held = adjudicate(doc, later, prev)
        self.assertEqual((held["state"], held["condition"], held["recovering"]), ("DEGRADED", "delegation_ceiling", True), held)
        doc["runs"] += [run(200 + i, 20 + i, later - timedelta(minutes=30 - 5 * i), result="SUCCESS", tasks=broken_tasks(set())) for i in range(3)]
        self.assertEqual(adjudicate(doc, later, held)["state"], "GREEN")

    def test_a_green_run_with_five_ceiling_reps_still_carries_the_wave(self):
        doc = self.wave([1, 2, 3], 6)
        prev = adjudicate(doc, T0)
        later = T0 + timedelta(hours=2, minutes=1)
        held = adjudicate(doc, later, prev)
        doc["runs"] += [
            run(200 + i, 20 + i, later - timedelta(minutes=30 - 5 * i), result="SUCCESS",
                tasks=broken_tasks(set()) + ([task("slow", "ccccc")] if i == 1 else []))
            for i in range(3)
        ]
        self.assertEqual((adjudicate(doc, later, held)["state"], adjudicate(doc, later, held)["recovering"]), ("DEGRADED", True))


# --------------------------------------------------------------------------- #
# Rule 3: setup deaths
# --------------------------------------------------------------------------- #


class SetupDeaths(unittest.TestCase):
    def deaths(self, prs, minutes=1, result="FAILURE"):
        return data(*(run(100 + i, pr, T0 - timedelta(minutes=10 * i), minutes=minutes, result=result) for i, pr in enumerate(prs)))

    def test_three_deaths_on_two_prs_degrade(self):
        result = assess(self.deaths([1, 1, 2]), T0)
        self.assertEqual(result["state"], "DEGRADED")
        self.assertEqual(result["condition"], "setup_deaths")
        self.assertEqual(result["cause"], "setup/clone failures on 3 runs (#1, #2)")

    def test_one_pr_dying_repeatedly_is_that_prs_problem(self):
        result = assess(self.deaths([1068, 1068, 1068, 1068]), T0)
        self.assertEqual(result["state"], "GREEN")
        self.assertIn("setup/clone failures: 4 runs under 5 min with no tasks in the last 2h (#1068)", result["evidence"])

    def test_aborted_and_slow_zero_task_runs_are_not_deaths(self):
        self.assertEqual(assess(self.deaths([1, 2, 3], result="ABORTED"), T0)["state"], "GREEN")
        self.assertEqual(assess(self.deaths([1, 2, 3], minutes=6), T0)["state"], "GREEN")
        self.assertEqual(assess(self.deaths([1, 2, 3], minutes=4), T0)["state"], "DEGRADED")

    def test_setup_advice(self):
        self.assertEqual(
            adjudicate(self.deaths([1, 2, 3]), T0)["advice"],
            "Retest once the setup failures stop; check the leased pool projects (stuck Helm release, image pulls) before spending another run.",
        )

    def test_a_conflicted_merge_is_not_a_setup_death(self):
        # 2026-09-15 (#1608): #1569, #1572 and #1575 died in six to twelve
        # seconds because they would not merge into main, and were reported
        # as an infrastructure degradation.
        conflicted = self.deaths([1569, 1572, 1575])
        for entry in conflicted["runs"]:
            entry["merge_conflict"] = True
        self.assertFalse(any(health.Run(entry).setup_death for entry in conflicted["runs"]))
        result = adjudicate(conflicted, T0)
        self.assertEqual(result["state"], "GREEN")
        self.assertEqual(result["metrics"]["setup_deaths"], 0)
        self.assertEqual(result["metrics"]["infra_reds"], 0, "the author's rebase is not the pool's fault")

    def test_a_clone_that_failed_any_other_way_still_is_one(self):
        for flag in (False, None):
            doc = self.deaths([1, 2, 3])
            for entry in doc["runs"]:
                if flag is not None:
                    entry["merge_conflict"] = flag
            self.assertEqual(assess(doc, T0)["condition"], "setup_deaths", f"merge_conflict={flag}")

    def test_greens_that_predate_the_deaths_do_not_recover_it(self):
        doc = self.deaths([1, 2, 3])
        doc["runs"] += [run(200 + i, 20 + i, T0 - timedelta(hours=3) + timedelta(minutes=10 * i), tasks=broken_tasks(set())) for i in range(3)]
        prev = adjudicate(doc, T0)
        self.assertEqual(prev["state"], "DEGRADED")
        later = T0 + timedelta(hours=2, minutes=1)
        held = adjudicate(doc, later, prev)
        self.assertEqual((held["state"], held["recovering"]), ("DEGRADED", True))
        doc["runs"] += [run(300 + i, 30 + i, later - timedelta(minutes=30 - 5 * i), tasks=broken_tasks(set())) for i in range(3)]
        self.assertEqual(adjudicate(doc, later, held)["state"], "GREEN")

    def test_a_lowercase_prow_verdict_still_counts(self):
        # Prow wrote `failure` on six zero-task runs on 2026-09-05.
        self.assertEqual(assess(self.deaths([1, 1, 2], result="failure"), T0)["state"], "DEGRADED")


# --------------------------------------------------------------------------- #
# Rule 3b: lost pods
# --------------------------------------------------------------------------- #


def lost(build_id, pr, finished, minutes=120, node="node-a", **fields):
    """A run whose build node went away, as the collector records it: a
    zero-task FAILURE with no build log and a NodeNotReady pod event."""
    raw = run(build_id, pr, finished, minutes=minutes, result="FAILURE")
    raw.update({"has_build_log": False, "pod_phase": "Failed", "pod_node": node, "pod_last_event": "NodeNotReady"})
    raw.update(fields)
    return raw


class LostPods(unittest.TestCase):
    def lost_doc(self, count, spread_minutes=5, prs=None, nodes=("node-a", "node-b")):
        prs = prs or list(range(1, count + 1))
        return data(*(lost(100 + i, prs[i], T0 - timedelta(minutes=spread_minutes * i), node=nodes[i % len(nodes)]) for i in range(count)))

    def test_the_predicate_reads_the_pod_record_not_the_clock(self):
        long_run, short_run = health.Run(lost(1, 1, T0, minutes=128)), health.Run(lost(2, 2, T0, minutes=2))
        self.assertEqual((long_run.lost_pod, long_run.setup_death), (True, False))
        self.assertEqual((short_run.lost_pod, short_run.setup_death), (True, False), "under five minutes is still a lost pod, never a setup death")
        self.assertTrue(health.Run(lost(3, 3, T0, pod_phase=None, pod_node=None, pod_last_event=None)).lost_pod, "a missing log alone is enough")
        self.assertTrue(health.Run(lost(4, 4, T0, has_build_log=True)).lost_pod, "NodeNotReady alone is enough")
        self.assertTrue(health.Run(lost(5, 5, T0, result="failure")).lost_pod, "Prow's lowercase verdict")
        clone_failed = health.Run(lost(6, 6, T0, minutes=0, has_build_log=True, pod_last_event="Started"))
        self.assertEqual((clone_failed.lost_pod, clone_failed.setup_death), (False, True))
        legacy = run(7, 7, T0, minutes=2, result="FAILURE")
        self.assertEqual((health.Run(legacy).lost_pod, health.Run(legacy).setup_death), (False, True), "a document without the fields is unknown")
        self.assertEqual((health.Run(dict(legacy, duration_s=3600)).lost_pod, health.Run(dict(legacy, duration_s=3600)).setup_death), (False, False))
        self.assertFalse(health.Run(lost(8, 8, T0, result="ABORTED")).lost_pod)
        self.assertFalse(health.Run(lost(9, 9, T0, tasks=green_tasks())).lost_pod, "a run with tasks is a full run")

    def test_three_within_thirty_minutes_degrade_and_fewer_or_sparser_do_not(self):
        result = assess(self.lost_doc(3, spread_minutes=10), T0)
        self.assertEqual((result["state"], result["condition"]), ("DEGRADED", "lost_pods"))
        self.assertEqual(result["cause"], "lost pods: 3 runs on 3 PRs died with their build node 23:40–00:00 UTC")
        self.assertEqual(result["evidence"][0], "lost pods: 3 runs on 3 PRs died with their build node 23:40–00:00 UTC (nodes node-a ×2, node-b; #1, #2, #3)")
        self.assertEqual(assess(self.lost_doc(2), T0)["state"], "GREEN")
        self.assertEqual(assess(self.lost_doc(3, spread_minutes=15), T0)["state"], "DEGRADED", "0, 15 and 30 minutes ago fit one span")
        self.assertEqual(assess(self.lost_doc(3, spread_minutes=20), T0)["state"], "GREEN", "0, 20 and 40 minutes ago do not")

    def test_no_distinct_pr_floor_the_pod_record_already_blames_the_node(self):
        self.assertEqual(assess(self.lost_doc(3, prs=[7, 7, 7]), T0)["condition"], "lost_pods")

    def test_eight_is_a_build_cluster_event(self):
        # Eight losses four minutes apart: 28 minutes, one span.
        incident = assess(self.lost_doc(8, spread_minutes=4), T0)["incident"]
        self.assertTrue(incident["event"])
        self.assertEqual(incident["nodes"], {"node-a": 4, "node-b": 4})
        self.assertEqual((incident["runs"], incident["prs"]), (8, list(range(1, 9))))
        self.assertEqual((incident["window_start"], incident["window_end"]), (health.iso(T0 - timedelta(minutes=28)), health.iso(T0)))
        self.assertFalse(assess(self.lost_doc(7, spread_minutes=4), T0)["incident"]["event"])
        # Eight losses five minutes apart span 35 minutes: the densest
        # 30-minute span holds seven, and seven is not an event.
        self.assertEqual(assess(self.lost_doc(8), T0)["incident"]["runs"], 7)

    def test_older_losses_in_the_window_are_evidence_not_the_event(self):
        doc = self.lost_doc(3)
        doc["runs"].append(lost(200, 20, T0 - timedelta(minutes=90)))
        result = assess(doc, T0)
        self.assertEqual(result["incident"]["runs"], 3)
        self.assertTrue(result["evidence"][0].endswith("; 1 more earlier in the last 2h"), result["evidence"])

    def test_lost_pods_outrank_a_storm_and_are_counted_in_exactly_one_class(self):
        doc = self.lost_doc(3)
        for i in range(3):
            stormy = [task(f"s{k}", "eee") for k in range(2)] + broken_tasks(set())
            doc["runs"].append(run(300 + i, 30 + i, T0 - timedelta(minutes=2 * i), result="SUCCESS", tasks=stormy))
        result = adjudicate(doc, T0)
        self.assertEqual(result["condition"], "lost_pods")
        self.assertTrue(any(line.startswith("quota storm:") for line in result["evidence"]), "the storm stays as context")
        self.assertEqual((result["metrics"]["lost_pods"], result["metrics"]["setup_deaths"]), (3, 0))
        self.assertEqual(result["metrics"]["infra_reds"], 3)

    def test_advice_names_the_nodes_on_the_readers_clock(self):
        # The first loss was 20 minutes before T0 (2026-09-08 00:00Z): 7:40 PM EDT on the 7th.
        self.assertEqual(
            adjudicate(self.lost_doc(3, spread_minutes=10), T0)["advice"],
            "The Prow build cluster lost node(s) node-a ×2, node-b at 7:40 PM ET; 3 runs died mid-run."
            " Nothing about your change; /retest when the new jobs are progressing. Cluster owner: check the node events and autorepair.",
        )
        self.assertEqual(health.reader_clock(None), "?")

    def test_recovery_needs_greens_after_the_last_loss(self):
        doc = self.lost_doc(3)
        doc["runs"] += [run(200 + i, 20 + i, T0 - timedelta(hours=3) + timedelta(minutes=10 * i), tasks=broken_tasks(set())) for i in range(3)]
        prev = adjudicate(doc, T0)
        self.assertEqual((prev["state"], prev["condition"]), ("DEGRADED", "lost_pods"))
        later = T0 + timedelta(hours=2, minutes=1)
        held = adjudicate(doc, later, prev)
        self.assertEqual((held["state"], held["condition"], held["recovering"]), ("DEGRADED", "lost_pods", True))
        doc["runs"] += [run(300 + i, 30 + i, later - timedelta(minutes=30 - 5 * i), tasks=broken_tasks(set())) for i in range(3)]
        self.assertEqual(adjudicate(doc, later, held)["state"], "GREEN")

    def test_the_posters_issue_is_cited_only_for_the_condition_it_was_filed_for(self):
        doc = self.lost_doc(3)
        outage_issue = {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300", "condition": "shared_break"}
        self.assertIsNone(health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), posted={"issue": outage_issue})["issue"])
        owner_issue = dict(outage_issue, number=1301, condition="lost_pods")
        cited = health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), posted={"issue": owner_issue})
        self.assertEqual((cited["issue"], cited["tracking_issues"]), (owner_issue, ["#1301"]))
        # An issue from before the key was only ever an outage's: cited for
        # a shared break, never for lost pods.
        untagged = {"number": 1302, "url": "x"}
        self.assertIsNone(health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), posted={"issue": untagged})["issue"])
        outage_doc = data(*(run(100 + i, i, T0 - timedelta(hours=1) + timedelta(minutes=10 * i), tasks=broken_tasks({"agent-kanban-smoke"})) for i in range(4)))
        self.assertEqual(health.adjudicate(outage_doc, T0, None, health.Roster.fixed(ADMITTED), posted={"issue": untagged})["issue"], untagged)

    def test_trim_carries_how_the_build_ended(self):
        doc = data(lost(1, 1, T0), run(2, 2, T0, tasks=[task("x", "ppp")]))
        trimmed = health.trim(doc, T0 - timedelta(days=1), T0 + timedelta(days=1), "test")["runs"]
        self.assertEqual({k: v for k, v in trimmed[0].items() if k in health.ENDED_FIELDS}, {"has_build_log": False, "pod_phase": "Failed", "pod_node": "node-a", "pod_last_event": "NodeNotReady"})
        self.assertFalse(set(health.ENDED_FIELDS) & set(trimmed[1]))

    def test_trim_carries_merge_conflict_so_a_fixture_replays_the_same_verdict(self):
        # #1608: a field trim drops is a field the replay cannot see, and the
        # conflicted merge silently returns as a setup death in the fixture.
        doc = data(*(dict(run(i, i, T0, minutes=0.2, result="FAILURE"), merge_conflict=True) for i in (1569, 1572, 1575)))
        trimmed = health.trim(doc, T0 - timedelta(days=1), T0 + timedelta(days=1), "test")
        self.assertTrue(all(entry["merge_conflict"] is True for entry in trimmed["runs"]))
        self.assertEqual(assess(trimmed, T0)["condition"], None)


# --------------------------------------------------------------------------- #
# Rule 6: hysteresis
# --------------------------------------------------------------------------- #


class Hysteresis(unittest.TestCase):
    def outage_doc(self):
        return data(*(run(100 + i, i, T0 - timedelta(hours=5) + timedelta(minutes=30 * i), tasks=broken_tasks({"agent-kanban-smoke"})) for i in range(4)))

    def test_a_break_whose_last_three_runs_are_clean_does_not_enter_outage(self):
        doc = self.outage_doc()
        # Three clean runs from three more PRs finish after the broken ones.
        # Four reds of seven still clears the red-share bar; what holds the
        # state is currency -- none of the last three runs carries the
        # collapse.
        doc["runs"] += [run(200 + i, 20 + i, T0 - timedelta(minutes=10 * i), tasks=broken_tasks(set())) for i in range(3)]
        result = adjudicate(doc, T0)
        self.assertEqual(result["state"], "GREEN")
        self.assertIn("OUTAGE condition seen but not yet current; holding GREEN", result["evidence"])

    def test_recovery_needs_three_consecutive_greens_on_distinct_prs(self):
        doc = self.outage_doc()
        prev = adjudicate(doc, T0 - timedelta(hours=2))
        self.assertEqual(prev["state"], "OUTAGE")
        later = T0 + timedelta(hours=7)  # the break has rolled out of the window
        # Two greens: still OUTAGE, recovering.
        doc["runs"] += [run(300, 31, later - timedelta(minutes=40), tasks=broken_tasks(set())), run(301, 32, later - timedelta(minutes=20), tasks=broken_tasks(set()))]
        held = adjudicate(doc, later, prev)
        self.assertEqual(held["state"], "OUTAGE")
        self.assertTrue(held["recovering"])
        self.assertEqual(held["since"], prev["since"], "since is the start of the incident, not of the tick")
        self.assertIn("waiting for 3 consecutive green runs", held["evidence"][-1])
        # A third green on a PR already counted: still held.
        doc["runs"].append(run(302, 32, later - timedelta(minutes=10), tasks=broken_tasks(set())))
        self.assertEqual(adjudicate(doc, later, held)["state"], "OUTAGE")
        # Once the last three are green on three distinct PRs: GREEN.
        doc["runs"].append(run(303, 33, later - timedelta(minutes=5), tasks=broken_tasks(set())))
        doc["runs"].append(run(304, 34, later - timedelta(minutes=2), tasks=broken_tasks(set())))
        result = adjudicate(doc, later, held)
        self.assertEqual(result["state"], "GREEN")
        self.assertFalse(result["recovering"])
        self.assertEqual(result["advice"], "")

    def test_a_green_run_that_still_carries_the_break_does_not_count(self):
        doc = self.outage_doc()
        prev = adjudicate(doc, T0 - timedelta(hours=2))
        later = T0 + timedelta(hours=7)
        tasks = broken_tasks({"agent-kanban-smoke"})
        doc["runs"] += [run(300 + i, 30 + i, later - timedelta(minutes=10 * i), result="SUCCESS", tasks=tasks) for i in range(3)]
        self.assertEqual(adjudicate(doc, later, prev)["state"], "OUTAGE")

    def test_demoting_the_broken_case_lets_the_gate_recover(self):
        # The documented fix for a rung-4 shared break is to demote the case
        # (hack/ci-eval-pr.sh). Once it is a hold-out its collapses red
        # nobody, so three greens that still carry it are a recovery.
        doc = self.outage_doc()
        prev = adjudicate(doc, T0)
        self.assertEqual(prev["failing_cases"], ["agent-kanban-smoke"])
        later = T0 + timedelta(hours=7)
        demoted = later - timedelta(hours=1)
        roster = health.Roster.from_history(
            [
                {"since": (T0 - timedelta(days=1)).isoformat(), "admitted": sorted(ADMITTED)},
                {"since": demoted.isoformat(), "admitted": sorted(ADMITTED - {"agent-kanban-smoke"})},
            ]
        )
        still_failing = broken_tasks({"agent-kanban-smoke"})
        doc["runs"] += [run(300 + i, 30 + i, later - timedelta(minutes=10 * i), minutes=30, result="SUCCESS", tasks=still_failing) for i in range(3)]
        self.assertEqual(adjudicate(doc, later, prev)["state"], "OUTAGE", "with the case still admitted the greens carry the break")
        result = adjudicate(doc, later, prev, roster)
        self.assertEqual(result["state"], "GREEN", "with the case demoted before those runs started, they recover it")

    def test_leaving_outage_for_a_live_storm_is_immediate(self):
        doc = self.outage_doc()
        prev = adjudicate(doc, T0 - timedelta(hours=2))
        later = T0 + timedelta(hours=7)
        for i in range(3):
            stormy = [task(f"s{k}", "eee") for k in range(2)] + broken_tasks(set())
            doc["runs"].append(run(300 + i, 30 + i, later - timedelta(minutes=10 * i), result="SUCCESS", tasks=stormy))
        result = adjudicate(doc, later, prev)
        self.assertEqual(result["state"], "DEGRADED")
        self.assertEqual(result["condition"], "storm")

    def test_the_first_tick_ever_takes_the_assessment(self):
        self.assertEqual(adjudicate(self.outage_doc(), T0)["state"], "OUTAGE")
        self.assertEqual(adjudicate(data(), T0)["state"], "GREEN")

    def test_a_previous_state_is_held_while_a_worse_one_is_not_yet_current(self):
        doc = self.outage_doc()
        doc["runs"] += [run(200 + i, 20 + i, T0 - timedelta(minutes=10 * i), tasks=broken_tasks(set())) for i in range(3)]
        prev = {"state": "GREEN", "condition": None, "cause": "", "failing_cases": [], "since": (T0 - timedelta(days=1)).isoformat(), "recovering": False}
        result = adjudicate(doc, T0, prev)
        self.assertEqual(result["state"], "GREEN")
        self.assertEqual(result["since"], prev["since"])
        self.assertIn("OUTAGE condition seen but not yet current; holding GREEN", result["evidence"])

    def test_stale_data_is_flagged_only_against_a_wall_clock(self):
        doc = self.outage_doc()
        fresh = health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), wall_clock=T0 + timedelta(hours=1))
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["metrics"]["data_age_s"], 3600)
        stale = health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), wall_clock=T0 + timedelta(hours=5))
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["state"], "OUTAGE", "the state is still judged at the data's horizon")
        self.assertTrue(stale["advice"].startswith(f"data.json last refreshed {T0.isoformat()} (5h ago); the dashboard refresh is stalled"))
        doc["stale_after_s"] = 6 * 3600
        self.assertFalse(health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), wall_clock=T0 + timedelta(hours=5))["stale"], "the collector's own cadence wins")
        self.assertFalse(adjudicate(doc, T0)["stale"], "no wall clock, never stale")
        self.assertNotIn("data_age_s", adjudicate(doc, T0)["metrics"])


# --------------------------------------------------------------------------- #
# Rule 5 and rule 7: metrics, fixtures, advice
# --------------------------------------------------------------------------- #


class NightlyTier(unittest.TestCase):
    """A nightly run (SCHEMA.md: runs[].tier) is never the gate's evidence:
    not for a rule, not for the recovery bar, not in the digest's numbers."""

    def nightly(self, run_doc):
        return dict(run_doc, tier="nightly", pr=None)

    def test_a_shared_break_made_of_nightly_runs_is_not_an_outage(self):
        broken = broken_tasks({"cluster-agent-crashloop-debug"})
        runs = [run(100 + i, i, T0 - timedelta(minutes=60 * (3 - i)), tasks=broken) for i in range(3)]
        self.assertEqual(assess(data(*runs), T0)["state"], "OUTAGE", "as presubmit runs, the same three fire")
        doc = data(*(self.nightly(r) for r in runs))
        result = assess(doc, T0)
        self.assertEqual(result["state"], "GREEN")
        self.assertEqual(result["evidence"], [])

    def test_nightly_storm_reps_and_setup_deaths_do_not_count(self):
        stormy = [task(f"case-{k}", "eee") for k in range(2)] + [task(n, "ppp") for n in sorted(ADMITTED)]
        storm_runs = [self.nightly(run(100 + i, None, T0 - timedelta(minutes=20 * i), result="SUCCESS", tasks=stormy)) for i in range(3)]
        self.assertEqual(assess(data(*storm_runs), T0)["state"], "GREEN")
        deaths = [self.nightly(run(200 + i, None, T0 - timedelta(minutes=10 * i), minutes=1, result="FAILURE")) for i in range(3)]
        self.assertEqual(assess(data(*deaths), T0)["state"], "GREEN")

    def test_nightly_runs_change_nothing_about_a_presubmit_verdict(self):
        presubmit = [run(100 + i, i, T0 - timedelta(hours=i), tasks=broken_tasks({"agent-kanban-smoke"})) for i in range(1, 5)]
        baseline = adjudicate(data(*presubmit), T0)
        self.assertEqual(baseline["state"], "OUTAGE")
        nightly_green = self.nightly(run(900, None, T0 - timedelta(minutes=5), tasks=broken_tasks(set())))
        nightly_red = self.nightly(run(901, None, T0 - timedelta(minutes=3), tasks=broken_tasks({"reliability-pdb-probe"})))
        with_nightly = adjudicate(data(*presubmit, nightly_green, nightly_red), T0)
        for key in ("state", "condition", "cause", "failing_cases", "evidence", "incident"):
            self.assertEqual(with_nightly[key], baseline[key], key)
        self.assertEqual(with_nightly["metrics"], baseline["metrics"], "the digest's 24h numbers are the presubmit's")

    def test_nightly_greens_do_not_recover_an_incident(self):
        presubmit = [run(100 + i, i, T0 - timedelta(hours=5) + timedelta(minutes=30 * i), tasks=broken_tasks({"agent-kanban-smoke"})) for i in range(4)]
        doc = data(*presubmit)
        prev = adjudicate(doc, T0)
        later = T0 + timedelta(hours=7)
        doc["runs"] += [self.nightly(run(300 + i, None, later - timedelta(minutes=10 * i), tasks=broken_tasks(set()))) for i in range(3)]
        held = adjudicate(doc, later, prev)
        self.assertEqual((held["state"], held["recovering"]), ("OUTAGE", True))

    def test_a_run_without_a_tier_is_the_presubmit(self):
        doc = data(run(1, 1, T0 - timedelta(hours=1), tasks=broken_tasks(set())))
        self.assertEqual(adjudicate(doc, T0)["metrics"]["full_runs"], 1)
        doc["runs"][0]["tier"] = "nightly"
        self.assertEqual(adjudicate(doc, T0)["metrics"]["full_runs"], 0)
        doc["runs"][0]["tier"] = "rc"
        self.assertEqual(adjudicate(doc, T0)["metrics"]["full_runs"], 0, "an unknown tier is never the gate's by default")

    def test_trim_keeps_the_tier_so_a_fixture_replays_the_same_filter(self):
        doc = data(run(1, 1, T0, tasks=[task("x", "ppp")]), self.nightly(run(2, None, T0, tasks=[task("x", "fff")])))
        trimmed = health.trim(doc, T0 - timedelta(days=1), T0 + timedelta(days=1), "test")
        self.assertNotIn("tier", trimmed["runs"][0])
        self.assertEqual(trimmed["runs"][1]["tier"], "nightly")
        self.assertEqual(len(health.load_runs(trimmed)), 1)


class Metrics(unittest.TestCase):
    def test_green_report_metrics(self):
        doc = data(
            run(1, 1, T0 - timedelta(hours=1), minutes=100, tasks=broken_tasks(set())),
            run(2, 2, T0 - timedelta(hours=2), minutes=200, tasks=broken_tasks(set())),
            run(3, 3, T0 - timedelta(hours=3), minutes=300, tasks=broken_tasks({"agent-kanban-smoke"})),
            run(4, 4, T0 - timedelta(hours=4), minutes=50, result="ABORTED"),
            run(5, 5, T0 - timedelta(hours=5), minutes=1, result="FAILURE"),
            run(6, 6, T0 - timedelta(hours=30), minutes=100, tasks=broken_tasks({"agent-kanban-smoke"})),
        )
        fixtures = {"healed": 28, "broken": 2, "projects": 30}
        result = health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), fixtures=fixtures)
        m = result["metrics"]
        self.assertEqual((m["full_runs"], m["prs"], m["green_runs"], m["red_runs"]), (3, 3, 2, 1))
        self.assertEqual(m["green_rate"], 0.667)
        self.assertEqual(m["wall_clock_p50_s"], 200 * 60)
        self.assertEqual(m["wall_clock_p90_s"], 300 * 60)
        self.assertEqual((m["aborted_runs"], m["setup_deaths"]), (1, 1))
        self.assertEqual(m["infra_rep_rate"], 0.0)
        self.assertEqual(m["fixtures"], fixtures)
        self.assertEqual(result["state"], "GREEN")
        self.assertEqual(result["dashboard_url"], health.DASHBOARD_URL)

    def test_infra_rep_rate_counts_storm_and_infra_reps(self):
        tasks = [task("a", "ppp"), task("b", "pie"), task("c", "iii")]
        doc = data(run(1, 1, T0 - timedelta(hours=1), result="SUCCESS", tasks=tasks))
        self.assertEqual(adjudicate(doc, T0)["metrics"]["infra_rep_rate"], round(5 / 9, 3))

    def test_ceiling_reps_are_counted_apart_from_the_storms(self):
        """Fifteen ceiling reps across three PRs are rule 2b's condition, not
        rule 2's: the storm count and rate stay at zero and the digest
        carries them under their own key."""
        runs = [
            run(k, 100 + k, T0 - timedelta(minutes=10 * k), result="SUCCESS",
                tasks=green_tasks() + [task("slow", "ccc"), task("slower", "cc")])
            for k in range(1, 4)
        ]
        result = adjudicate(data(*runs), T0)
        self.assertEqual((result["state"], result["condition"]), ("DEGRADED", "delegation_ceiling"))
        self.assertEqual(result["metrics"]["infra_reps"], 0)
        self.assertEqual(result["metrics"]["ceiling_reps"], 15)
        self.assertEqual(result["metrics"]["infra_rep_rate"], 0.0)


# --------------------------------------------------------------------------- #
# Rule 7: slow gate
# --------------------------------------------------------------------------- #


class SlowGate(unittest.TestCase):
    def week(self, recent, typical=150, baseline=30, recent_end=T0, tasks=None):
        """`baseline` full runs of `typical` minutes, four hours apart, the
        newest finishing seven hours before T0; then the `recent` runs
        (minutes each), ten minutes apart, the last finishing at
        `recent_end`."""
        runs = [run(100 + i, i, T0 - timedelta(hours=7 + 4 * i), minutes=typical, tasks=full_tasks()) for i in range(baseline)]
        runs += [
            run(200 + i, 50 + i, recent_end - timedelta(minutes=10 * (len(recent) - 1 - i)), minutes=m, tasks=tasks or full_tasks())
            for i, m in enumerate(recent)
        ]
        return data(*runs)

    def test_five_runs_whose_median_is_1_2x_the_weeks_typical_are_slow(self):
        result = adjudicate(self.week([180, 200, 170, 185, 190]), T0)
        self.assertEqual(result["state"], "GREEN", "a slow gate is a note, not a state")
        self.assertEqual(
            result["slow"],
            {
                "since": health.iso(T0),
                "runs": 5,
                "min_s": 170 * 60,
                "median_s": 185 * 60,
                "max_s": 200 * 60,
                "baseline_days": 7,
                "baseline_runs": 30,
                "baseline_p50_s": 150 * 60,
                "baseline_p90_s": 150 * 60,
                "infra_reps": 0,
            },
        )
        self.assertIn("slow gate: last 5 full runs 170–200 min (median 185) against a 7-day typical of 150 min (p90 150); no reps lost", result["evidence"])

    def test_the_median_decides_so_one_straggler_does_not(self):
        self.assertIsNone(adjudicate(self.week([150, 150, 150, 150, 400]), T0)["slow"])
        self.assertIsNone(adjudicate(self.week([179] * 5), T0)["slow"])
        self.assertIsNotNone(adjudicate(self.week([180] * 5), T0)["slow"])

    def test_needs_twenty_full_runs_in_the_baseline_and_five_after_them(self):
        self.assertIsNone(adjudicate(self.week([200] * 5, baseline=19), T0)["slow"])
        self.assertIsNotNone(adjudicate(self.week([200] * 5, baseline=20), T0)["slow"])
        self.assertIsNone(adjudicate(self.week([200] * 4, baseline=0), T0)["slow"])

    def test_only_recent_concluded_full_runs_count(self):
        # The same five slow runs, the newest finished six hours ago: nobody
        # is waiting on them.
        self.assertIsNone(adjudicate(self.week([200] * 5, recent_end=T0 - timedelta(hours=6)), T0)["slow"])
        self.assertIsNotNone(adjudicate(self.week([200] * 5, recent_end=T0 - timedelta(hours=5)), T0)["slow"])
        # Ten cases is a run Prow cut short, not a full run, and an aborted
        # run concluded nothing: the newest five full runs are then the
        # baseline's own, at the typical length.
        short = [task(f"case-{k}", "ppp") for k in range(10)]
        self.assertIsNone(adjudicate(self.week([200] * 5, tasks=short), T0)["slow"])
        doc = self.week([200] * 5)
        for aborted in doc["runs"][-5:]:
            aborted["result"] = "ABORTED"
        self.assertIsNone(adjudicate(doc, T0)["slow"])

    def test_the_floor_sits_one_demotion_below_the_live_presubmit(self):
        # Since 2026-09-22 the presubmit runs the blocking roster only (twelve
        # cases, #1023) and the floor is read from the presubmit file rather
        # than pinned: a full-roster run is a full run, one demotion away it
        # still is, and two demotions away (the ten-case run Prow cut short
        # above, today) is not. A literal floor of 15 would never see a full
        # run and the rule would go silent; a literal of any size would need
        # an edit here on every admission or demotion.
        n = len(health.eval_rosters.presubmit_cases())
        self.assertGreaterEqual(n, 3)
        full = [task(f"case-{k}", "ppp") for k in range(n)]
        self.assertIsNotNone(adjudicate(self.week([200] * 5, tasks=full), T0)["slow"])
        self.assertIsNotNone(adjudicate(self.week([200] * 5, tasks=full[: n - 1]), T0)["slow"])
        self.assertIsNone(adjudicate(self.week([200] * 5, tasks=full[: n - 2]), T0)["slow"])
        self.assertEqual(health.SLOW_MIN_TASKS, n - 1, "one demotion below the live presubmit roster, on purpose")
        self.assertEqual(health._slow_min_tasks(), n - 1)

    def test_an_episode_holds_until_the_median_is_under_1_1x_and_keeps_its_start(self):
        first = adjudicate(self.week([180] * 5), T0)
        self.assertIsNotNone(first["slow"])
        later = T0 + timedelta(hours=1)
        # 1.15x would not start an episode; it does not end one either.
        self.assertIsNone(adjudicate(self.week([173] * 5, recent_end=later), later)["slow"])
        held = adjudicate(self.week([173] * 5, recent_end=later), later, first)
        self.assertEqual((held["slow"]["since"], held["slow"]["median_s"]), (first["slow"]["since"], 173 * 60))
        self.assertIsNone(adjudicate(self.week([164] * 5, recent_end=later), later, held)["slow"])
        # A previous health.json from before the field starts fresh.
        legacy = {"state": "GREEN", "condition": None, "cause": "", "failing_cases": [], "since": health.iso(T0), "recovering": False}
        self.assertEqual(adjudicate(self.week([180] * 5), T0, legacy)["slow"]["since"], health.iso(T0))

    def test_a_slow_gate_inside_an_incident_is_not_a_note(self):
        # Three setup deaths on two pull requests in the last half hour make
        # the state DEGRADED (rule 3); the same five slow runs are then the
        # incident's symptom, not a note, and an episode in progress does not
        # hold across the incident: GREEN afterwards starts one afresh.
        earlier = T0 - timedelta(hours=1)
        before = adjudicate(self.week([200] * 5, recent_end=earlier), earlier)
        self.assertIsNotNone(before["slow"])
        doc = self.week([200] * 5)
        doc["runs"] += [run(300 + i, pr, T0 - timedelta(minutes=10 * i), minutes=1, result="FAILURE") for i, pr in enumerate([1, 1, 2])]
        degraded = adjudicate(doc, T0, before)
        self.assertEqual((degraded["state"], degraded["condition"]), ("DEGRADED", "setup_deaths"))
        self.assertIsNone(degraded["slow"])
        self.assertFalse(any(line.startswith("slow gate") for line in degraded["evidence"]), degraded["evidence"])
        later = T0 + timedelta(hours=3)
        again = adjudicate(self.week([200] * 5, recent_end=later), later, degraded)
        self.assertEqual((again["state"], again["slow"]["since"]), ("GREEN", health.iso(later)))

    def test_repetitions_lost_in_the_slow_runs_are_counted(self):
        doc = self.week([200] * 5)
        doc["runs"][-1]["tasks"][-1] = task("case-17", "eee")
        result = adjudicate(doc, T0)
        self.assertEqual(result["slow"]["infra_reps"], 3)
        self.assertTrue(result["evidence"][-1].endswith("; 3 reps lost to infra"), result["evidence"])


# --------------------------------------------------------------------------- #
# Rule 8: pool pressure
# --------------------------------------------------------------------------- #


def pressure(
    verdict="OK",
    cause=None,
    window_end=T0,
    p50=0.4,
    p95=0.5,
    today_p50=None,
    free=25,
    bad_day="2026-09-06",
    bad_p50=24.1,
    bad_p95=157.3,
    over_threshold=0,
    **over,
):
    """pool-pressure.json in the shape the periodic publishes it.

    Three sets of numbers, deliberately far apart. `p50`/`p95` are the
    seven-day window, which the periodic never judges on and which the note
    must therefore not quote. `bad_p50`/`bad_p95` are the breached day's row,
    which is what it judged and what the note owes the reader. `today_p50` is
    the newest row, which only the digest reads. A test that swaps two of them
    fails on the value.
    """
    breach = verdict == "BREACH"
    days = [
        {
            "day": "2026-09-07",
            "runs": 65,
            "p50_minutes": p50 if today_p50 is None else today_p50,
            "p95_minutes": p95,
            "worst_minutes": 2.5,
            "max_concurrency": 30,
            "breached": False,
            "judged": True,
        }
    ]
    if breach and bad_day:
        # Oldest first, as the periodic writes them: the digest reads days[-1].
        days.insert(
            0,
            {
                "day": bad_day,
                "runs": 12,
                "p50_minutes": bad_p50,
                "p95_minutes": bad_p95,
                "worst_minutes": 175.2,
                "max_concurrency": 30,
                "breached": True,
                "judged": True,
            },
        )
    return {
        "job": "pull-kube-agents-smoke-test",
        "window_start": health.iso(window_end - timedelta(days=7)),
        "window_end": health.iso(window_end),
        "max_concurrency": 30,
        "cause": cause,
        "breached": breach,
        "verdict": verdict,
        "thresholds": {"p50_minutes": 15, "p95_minutes": 45},
        "pool": {"busy": 30 - free, "free": free, "total": 30, "in_transition": 0, "stranded": 0},
        "queue": {"read": True, "waiting": over_threshold, "running": 3, "over_threshold": over_threshold},
        "trend": {
            "runs": 586,
            "p50_minutes": p50,
            "p95_minutes": p95,
            "worst_minutes": 175.2,
            "days": days,
            "breached_days": [bad_day] if breach and bad_day else [],
        },
    } | over


def pooled(doc=None, now=T0, prev=None, posted=None, wall_clock=None, **artifact):
    return health.adjudicate(
        doc or data(),
        now,
        prev,
        health.Roster.fixed(ADMITTED),
        posted=posted,
        wall_clock=wall_clock,
        pool_pressure=pressure(**artifact),
    )


class PoolNote(unittest.TestCase):
    def test_a_breach_quotes_the_day_it_breached_on_not_the_window(self):
        # The #1069 incident's shape. The seven-day window keeps its quiet
        # default of 0.4/0.5 min throughout: the periodic breaches on a day's
        # row or on the live queue and never on the aggregate, so a note built
        # from the aggregate would print "24s against a 15 min limit" under
        # "the pool is full, buy another project".
        result = pooled(verdict="BREACH", cause="CAPACITY", free=0)
        self.assertEqual(result["state"], "GREEN", "a backed-up pool is a note, not a state")
        self.assertEqual(
            result["pool"],
            {
                "since": health.iso(T0),
                "verdict": "BREACH",
                "measured_at": health.iso(T0),
                "breach_seen": True,
                "day": "2026-09-06",
                "window_hours": None,
                "p50_s": 1446,
                "p95_s": 9438,
                "waiting_longest_s": 0,
                "waiting_now": False,
                "waiting_since": None,
                "over_threshold": 0,
                "threshold_p50_s": 900,
                "threshold_p95_s": 2700,
                "free": 0,
                "total": 30,
                "cause": "CAPACITY",
                "max_concurrency": 30,
            },
        )
        self.assertIn(
            "backed-up pool: worst day 2026-09-06 median 24 min against 15 min, p95 157 min against 45;"
            " 0 of 30 projects free",
            result["evidence"],
        )

    def test_the_recent_stretch_beats_the_worst_day_when_the_periodic_judged_it(self):
        # The verdict lasts a week, so Monday's row is still the worst day on
        # Thursday. Quoting it dates the evidence to a queue that has drained.
        recent = {"hours": 3, "runs": 31, "judged": True, "p50_minutes": 18.0,
                  "p95_minutes": 52.0, "worst_minutes": 61.0}
        note = pooled(verdict="BREACH", cause="CAPACITY", free=0, recent=recent)["pool"]
        self.assertEqual(note["window_hours"], 3)
        self.assertIsNone(note["day"], "one label, so the messages need no tie-break")
        self.assertEqual(note["p50_s"], 1080)
        self.assertEqual(note["p95_s"], 3120)
        self.assertEqual(
            health.pool_measurement(note),
            "last 3h median 18 min against 15 min, p95 52 min against 45",
        )

    def test_the_worst_day_stands_in_when_the_recent_stretch_is_too_thin_to_judge(self):
        # A quiet Sunday holds fewer runs than the periodic will judge on, and
        # it withholds the percentiles rather than quoting a handful.
        recent = {"hours": 3, "runs": 2, "judged": False, "p50_minutes": None,
                  "p95_minutes": None, "worst_minutes": None}
        note = pooled(verdict="BREACH", cause="CAPACITY", free=0, recent=recent)["pool"]
        self.assertIsNone(note["window_hours"])
        self.assertEqual(note["day"], "2026-09-06")
        self.assertEqual(note["p50_s"], 1446)

    def test_a_recent_stretch_inside_both_limits_is_not_the_breach_evidence(self):
        # Monday's row holds the verdict all week; by Thursday afternoon the
        # last three hours are busy and fine. Quoting them would print "median
        # wait 24s against a 15 min limit" under "queue backed up".
        recent = {"hours": 3, "runs": 31, "judged": True, "p50_minutes": 0.4,
                  "p95_minutes": 0.5, "worst_minutes": 1.0}
        note = pooled(verdict="BREACH", cause="CAPACITY", free=0, recent=recent)["pool"]
        self.assertIsNone(note["window_hours"])
        self.assertEqual(note["day"], "2026-09-06")
        self.assertEqual(note["p50_s"], 1446)

    def test_a_recent_stretch_over_p95_alone_is_still_the_breach_evidence(self):
        # Either half, as a day's row breaches: a compliant median under a p95
        # that is double the limit is a queue, not a quiet stretch.
        recent = {"hours": 3, "runs": 31, "judged": True, "p50_minutes": 2.0,
                  "p95_minutes": 90.0, "worst_minutes": 120.0}
        note = pooled(verdict="BREACH", cause="CAPACITY", free=0, recent=recent)["pool"]
        self.assertEqual(note["window_hours"], 3)
        self.assertEqual(note["p95_s"], 5400)

    def test_a_live_breach_with_nothing_over_a_limit_carries_no_numbers(self):
        # Runs queued past p95 right now are not in the sweep, so a live breach
        # can have no breached day and a fine recent stretch. A span over no
        # numbers reads "median wait ?"; pool_span returns None instead.
        recent = {"hours": 3, "runs": 31, "judged": True, "p50_minutes": 0.4,
                  "p95_minutes": 0.5, "worst_minutes": 1.0}
        note = pooled(verdict="BREACH", cause="CONTROL_PLANE", free=25,
                      over_threshold=4, bad_day=None, recent=recent)["pool"]
        self.assertIsNone(note["window_hours"])
        self.assertIsNone(note["day"])
        self.assertIsNone(health.pool_span(note))
        self.assertEqual(health.pool_measurement(note), "4 runs waiting now past 45 min")

    def test_a_breach_after_a_monitoring_stretch_is_dated_from_the_breach(self):
        # The periodic dies Monday and comes back Wednesday reporting a queue.
        # Carrying Monday's start would put "runs are waiting to start since
        # Monday" on the lede over two days nobody measured.
        dead = T0 - timedelta(hours=4)
        stale = pooled(verdict="BREACH", cause="CAPACITY", window_end=dead)
        self.assertEqual(stale["pool"]["verdict"], "STALE")
        self.assertFalse(stale["pool"]["breach_seen"])
        back = T0 + timedelta(days=2)
        note = pooled(now=back, prev=stale, verdict="BREACH", cause="CAPACITY", window_end=back)["pool"]
        self.assertEqual(note["since"], health.iso(back), "dated from the first reading that saw it")
        self.assertTrue(note["breach_seen"])

    def test_a_breach_that_goes_stale_and_returns_keeps_its_real_start(self):
        # The other direction, and why breach_seen exists rather than a plain
        # verdict comparison: that start was measured.
        first = pooled(verdict="BREACH", cause="CAPACITY")
        blind_at = T0 + timedelta(hours=1)
        stale = pooled(now=blind_at, prev=first, verdict="BREACH", cause="CAPACITY", window_end=blind_at - timedelta(hours=4))
        self.assertEqual(stale["pool"]["verdict"], "STALE")
        back = blind_at + timedelta(hours=1)
        note = pooled(now=back, prev=stale, verdict="BREACH", cause="CAPACITY", window_end=back)["pool"]
        self.assertEqual(note["since"], health.iso(T0), "one episode, and it breached at the start")

    def test_a_blind_tick_does_not_forget_that_the_episode_never_breached(self):
        # The same question across a tick that read no artifact. metrics
        # carries it beside the start, or the breach would date itself from
        # the monitoring stretch again.
        dead = T0 - timedelta(hours=4)
        stale = pooled(verdict="BREACH", cause="CAPACITY", window_end=dead)
        blind_at = T0 + timedelta(minutes=15)
        blind = health.adjudicate(data(), blind_at, stale, health.Roster.fixed(ADMITTED))
        self.assertIsNone(blind["pool"])
        self.assertFalse(blind["metrics"]["pool_breach_seen"])
        back = blind_at + timedelta(minutes=15)
        note = pooled(now=back, prev=blind, verdict="BREACH", cause="CAPACITY", window_end=back)["pool"]
        self.assertEqual(note["since"], health.iso(back))

    def test_the_longest_live_wait_separates_an_unread_queue_from_an_empty_one(self):
        # The gate posts on an unread queue and withholds on an empty one, so
        # None and 0 cannot collapse into each other.
        artifact = pressure(verdict="BREACH", cause="CAPACITY")
        artifact["queue"]["waiting_runs"] = [{"minutes": 3.0, "pull": 1}, {"minutes": 31.5, "pull": 2}]
        self.assertEqual(health.pool_note(artifact, T0, None)["waiting_longest_s"], 1890)
        artifact["queue"]["waiting_runs"] = []
        self.assertEqual(health.pool_note(artifact, T0, None)["waiting_longest_s"], 0)
        artifact["queue"]["read"] = False
        self.assertIsNone(health.pool_note(artifact, T0, None)["waiting_longest_s"])

    def test_whether_the_queue_is_a_backlog_is_judged_once_for_every_reader(self):
        # The alert, the digest line and the Brief sentence all ask it, so the
        # answer is derived here rather than three times. The bar is the p50
        # limit, and it is strict: a wait exactly at the limit is not over it.
        artifact = pressure(verdict="BREACH", cause="CAPACITY")
        limit = artifact["thresholds"]["p50_minutes"]
        for name, minutes, expected in (("over", limit + 0.5, True), ("at", limit, False), ("under", limit - 0.5, False)):
            with self.subTest(name):
                artifact["queue"]["waiting_runs"] = [{"minutes": minutes, "pull": 1}]
                self.assertIs(health.pool_note(artifact, T0, None)["waiting_now"], expected)
        # Two ways to have no answer, and neither may read as "nothing is
        # waiting": the readers say less on None instead of claiming it cleared.
        artifact["queue"]["read"] = False
        self.assertIsNone(health.pool_note(artifact, T0, None)["waiting_now"])
        artifact["queue"]["read"] = True
        artifact["queue"]["waiting_runs"] = [{"minutes": limit + 99, "pull": 1}]
        artifact["thresholds"].pop("p50_minutes")
        self.assertIsNone(health.pool_note(artifact, T0, None)["waiting_now"])

    def test_a_backlog_is_dated_from_the_oldest_run_in_it(self):
        # The episode's `since` spans the verdict, which lasts a week, so under
        # a Thursday jam it can read Monday -- three days the queue was not
        # measured waiting. The oldest queued run dates the jam itself.
        artifact = pressure(verdict="BREACH", cause="CAPACITY")
        artifact["queue"]["waiting_runs"] = [{"minutes": 40, "pull": 1}, {"minutes": 9, "pull": 2}]
        note = health.pool_note(artifact, T0, None)
        measured = health.parse_iso(note["measured_at"])
        self.assertEqual(health.parse_iso(note["waiting_since"]), measured - timedelta(minutes=40))
        # Nothing to date: no backlog, and no reading of the queue at all.
        artifact["queue"]["waiting_runs"] = [{"minutes": 9, "pull": 2}]
        self.assertIsNone(health.pool_note(artifact, T0, None)["waiting_since"])
        artifact["queue"]["read"] = False
        self.assertIsNone(health.pool_note(artifact, T0, None)["waiting_since"])

    def test_a_malformed_waiting_queue_costs_the_figure_not_the_tick(self):
        # Same filter as the rest of the artifact reader: the adjudicate step
        # is not continue-on-error.
        shapes = (
            ("a dict", {"minutes": 9}),
            ("a string in the list", ["9"]),
            ("a figure infinite", [{"minutes": INF}]),
            # Finite, so _as_seconds returns it, and larger than a date can be
            # moved back by: without the cap it dates the backlog and raises.
            ("a figure past any real wait", [{"minutes": 2e9}]),
        )
        for name, runs in shapes:
            with self.subTest(name):
                artifact = pressure(verdict="BREACH", cause="CAPACITY")
                artifact["queue"]["waiting_runs"] = runs
                self.assertEqual(health.pool_note(artifact, T0, None)["waiting_longest_s"], 0)
        # And it costs only its own run: the jam beside it is still measured.
        artifact = pressure(verdict="BREACH", cause="CAPACITY")
        artifact["queue"]["waiting_runs"] = [{"minutes": 2e9}, {"minutes": 40}]
        self.assertEqual(health.pool_note(artifact, T0, None)["waiting_longest_s"], 2400)

    def test_the_digest_wait_is_withheld_on_a_day_the_producer_would_not_judge(self):
        # At 13:00 UTC the newest row holds only the overnight runs. One slow
        # run would otherwise be the morning's "typical wait" and the evidence
        # the queue had cleared.
        artifact = pressure(verdict="OK", today_p50=40.0)
        artifact["trend"]["days"][-1] |= {"runs": 1, "judged": False}
        self.assertIsNone(health.pool_wait_p50_s(artifact, T0))
        artifact["trend"]["days"][-1]["judged"] = True
        self.assertEqual(health.pool_wait_p50_s(artifact, T0), 2400)

    def test_a_quiet_night_falls_back_to_the_last_day_the_producer_judged(self):
        # A night with no runs at all already prints yesterday's median, because
        # the producer emits no row for an empty day. Three overnight runs must
        # not say less than none: the floor above withholds the unjudged row,
        # not the figure, and this is the headline that teaches normal.
        artifact = pressure(verdict="OK")
        artifact["trend"]["days"].append(
            {"day": "2026-09-08", "runs": 3, "p50_minutes": 90.0, "p95_minutes": 95.0, "judged": False})
        self.assertEqual(health.pool_wait_p50_s(artifact, T0), 24)
        # Bounded by the same two days: reaching back past them would print
        # Friday's median under a Monday heading, which is what POOL_DIGEST_DAYS
        # was added to stop.
        artifact["trend"]["days"][0]["day"] = "2026-09-06"
        self.assertIsNone(health.pool_wait_p50_s(artifact, T0))

    def test_the_worst_breached_day_wins_even_when_it_breached_on_p95_alone(self):
        # Excess over either limit, so a day that went over on p95 only is
        # still picked ahead of a quieter day that went over on p50.
        artifact = pressure(verdict="BREACH", cause="CAPACITY", bad_p50=16.0, bad_p95=46.0)
        artifact["trend"]["days"].insert(
            1,
            {"day": "2026-09-06b", "runs": 9, "p50_minutes": 2.0, "p95_minutes": 300.0,
             "worst_minutes": 301.0, "max_concurrency": 30, "breached": True, "judged": True},
        )
        note = health.pool_note(artifact, T0, None)
        self.assertEqual(note["day"], "2026-09-06b")
        self.assertEqual(note["p50_s"], 120, "the day's own median, low though it is")
        self.assertEqual(note["p95_s"], 18000)

    def test_a_breach_with_no_bad_day_quotes_the_runs_queued_right_now(self):
        # pool_pressure.py breaches on `breached_days or live_breach`, so one
        # run stuck past p95 breaches a week that has no bad day in it at all.
        result = pooled(verdict="BREACH", cause="CAPACITY", free=0, bad_day=None, over_threshold=3)
        note = result["pool"]
        self.assertIsNone(note["day"])
        self.assertIsNone(note["p50_s"], "no day breached, so there is no day's median to quote")
        self.assertEqual(note["over_threshold"], 3)
        self.assertIn(
            "backed-up pool: 3 runs waiting now past 45 min; 0 of 30 projects free",
            result["evidence"],
        )

    def test_a_breach_that_could_not_count_the_pool_says_the_wait_and_stops(self):
        # pool_pressure.cause() returns UNKNOWN exactly when the occupancy read
        # failed, and writes free/total as null in the same breath -- so this is
        # every UNKNOWN breach, not a corner of one.
        unread = {"read": False, "error": "boskos: connection refused",
                  "busy": None, "free": None, "total": None, "in_transition": None, "stranded": None}
        result = pooled(verdict="BREACH", cause="UNKNOWN", pool=unread)
        line = next(one for one in result["evidence"] if one.startswith("backed-up pool"))
        self.assertNotIn("None", line)
        self.assertEqual(
            "backed-up pool: worst day 2026-09-06 median 24 min against 15 min, p95 157 min against 45",
            line,
        )

    def test_a_breach_on_both_counts_reports_both(self):
        note = pooled(verdict="BREACH", cause="CAPACITY", over_threshold=1)["pool"]
        self.assertEqual(
            health.pool_measurement(note),
            "worst day 2026-09-06 median 24 min against 15 min, p95 157 min against 45;"
            " 1 run waiting now past 45 min",
        )

    def test_a_fresh_pass_and_a_missing_artifact_are_both_silent(self):
        self.assertIsNone(pooled(verdict="OK")["pool"])
        bare = health.adjudicate(data(), T0, None, health.Roster.fixed(ADMITTED))
        self.assertIsNone(bare["pool"])
        self.assertIsNone(bare["metrics"]["queue_wait_p50_s"])
        self.assertFalse(any(line.startswith("backed-up pool") for line in bare["evidence"]), bare["evidence"])

    def test_the_read_flag_separates_a_healthy_pool_from_a_missing_artifact(self):
        # Both leave `pool` null, and a stale artifact leaves the digest
        # number null too; only this bit says the fetch worked.
        self.assertTrue(pooled(verdict="OK")["metrics"]["queue_wait_read"])
        self.assertTrue(pooled(verdict="OK", window_end=T0 - timedelta(hours=4))["metrics"]["queue_wait_read"])
        bare = health.adjudicate(data(), T0, None, health.Roster.fixed(ADMITTED))
        self.assertFalse(bare["metrics"]["queue_wait_read"])

    def test_the_workflow_sentinel_reads_as_stale_with_nothing_to_quote(self):
        # What ci-health.yml substitutes when the copy or the parse fails. It
        # is a dict, so it counts as a reading, but it has no window_end --
        # the same STALE the dead-man's switch gives, minus a last reading.
        sentinel = {"note": "build 2099957253191766016 published no usable pool-pressure.json"}
        result = health.adjudicate(data(), T0, None, health.Roster.fixed(ADMITTED), pool_pressure=sentinel)
        self.assertEqual(
            result["pool"],
            {"since": health.iso(T0), "verdict": "STALE", "breach_seen": False, "measured_at": None},
        )
        self.assertTrue(result["metrics"]["queue_wait_read"], "the fetch worked; the periodic did not")
        self.assertIsNone(result["metrics"]["queue_wait_p50_s"])

    def test_a_section_of_the_wrong_type_costs_the_figure_not_the_tick(self):
        # Another job writes this file and nothing validates it on the way in.
        # The step that calls adjudicate has no continue-on-error, so a raise
        # here stops the dashboard and the bot every 15 minutes.
        window = health.iso(T0)
        for name, artifact in (
            ("trend a list", {"trend": []}),
            ("days a dict", {"trend": {"days": {"friday": 1}}}),
            ("days a string", {"trend": {"days": "none"}}),
            ("pool a string", {"pool": "full"}),
            ("queue a list", {"queue": []}),
            ("thresholds a list", {"thresholds": []}),
            (
                "a threshold as a string",
                {
                    "thresholds": {"p50_minutes": "15", "p95_minutes": "45"},
                    "trend": {"days": [{"day": "2026-09-06", "breached": True, "p50_minutes": 22.0}]},
                },
            ),
            (
                "a day's minutes as a string",
                {
                    "thresholds": {"p50_minutes": 15, "p95_minutes": 45},
                    "trend": {"days": [{"day": "2026-09-06", "breached": True, "p50_minutes": "22"}]},
                },
            ),
            # Rows dated to T0 and judged so the digest figure is really read:
            # a row two days back leaves before _as_seconds on
            # POOL_DIGEST_DAYS, and an unjudged one before the sample floor.
            *(
                (
                    f"a day's minutes {name}",
                    {
                        "thresholds": {"p50_minutes": 15, "p95_minutes": 45},
                        "trend": {"days": [{"day": "2026-09-08", "breached": True, "judged": True, "p50_minutes": value}]},
                    },
                )
                for name, value in (
                    ("infinite", INF),
                    ("NaN", NAN),
                    ("a bool", True),
                    ("overflowing on the way to seconds", OVERFLOWS),
                    ("an integer too large to be a float", HUGE_INT),
                )
            ),
            (
                "a threshold infinite",
                {
                    "thresholds": {"p50_minutes": INF, "p95_minutes": 45},
                    "trend": {"days": [{"day": "2026-09-06", "breached": True, "p50_minutes": 22.0}]},
                },
            ),
        ):
            with self.subTest(name):
                bad = {"window_end": window, "verdict": "BREACH"} | artifact
                self.assertEqual(health.pool_note(bad, T0, None)["verdict"], "BREACH")
                self.assertIsNone(health.pool_wait_p50_s(bad, T0))

    def test_an_episode_keeps_its_start_and_a_new_one_gets_a_new_start(self):
        first = pooled(verdict="BREACH", cause="CAPACITY")
        later = T0 + timedelta(hours=2)
        held = pooled(now=later, prev=first, verdict="BREACH", cause="CAPACITY", bad_p50=20.0, window_end=later)
        self.assertEqual(held["pool"]["since"], health.iso(T0))
        self.assertEqual(held["pool"]["p50_s"], 1200, "the numbers are this tick's")
        cleared = pooled(now=later, prev=held, verdict="OK", window_end=later)
        self.assertIsNone(cleared["pool"])
        again = T0 + timedelta(hours=5)
        self.assertEqual(
            pooled(now=again, prev=cleared, verdict="BREACH", cause="CAPACITY", window_end=again)["pool"]["since"],
            health.iso(again),
        )

    def test_a_blind_tick_does_not_restart_the_episode(self):
        # The in-flight skip makes a missing artifact an hourly event, so a
        # start carried only through the previous note would reset about every
        # hour. health.json holds it in metrics.pool_since across those ticks.
        first = pooled(verdict="BREACH", cause="CAPACITY")
        blind_at = T0 + timedelta(minutes=15)
        blind = health.adjudicate(data(), blind_at, first, health.Roster.fixed(ADMITTED))
        self.assertIsNone(blind["pool"], "no artifact is no note")
        self.assertEqual(blind["metrics"]["pool_since"], health.iso(T0), "the start outlives the note")
        back = blind_at + timedelta(minutes=15)
        carried = pooled(now=back, prev=blind, verdict="BREACH", cause="CAPACITY", window_end=back)
        self.assertEqual(carried["pool"]["since"], health.iso(T0), "one episode, not two")

    def test_a_read_tick_with_no_note_ends_the_episode_for_good(self):
        # The counterpart: an episode that really ended must not come back,
        # however many blind ticks follow it.
        first = pooled(verdict="BREACH", cause="CAPACITY")
        over_at = T0 + timedelta(hours=1)
        over = pooled(now=over_at, prev=first, verdict="OK", window_end=over_at)
        self.assertIsNone(over["pool"])
        self.assertIsNone(over["metrics"]["pool_since"], "a reading with no note is the episode over")
        blind_at = over_at + timedelta(minutes=15)
        blind = health.adjudicate(data(), blind_at, over, health.Roster.fixed(ADMITTED))
        self.assertIsNone(blind["metrics"]["pool_since"], "a blind tick holds nothing when nothing is open")
        again = T0 + timedelta(days=2)
        fresh = pooled(now=again, prev=blind, verdict="BREACH", cause="CAPACITY", window_end=again)
        self.assertEqual(fresh["pool"]["since"], health.iso(again), "a new episode, dated from itself")

    def test_a_previous_document_with_no_metrics_does_not_stop_the_tick(self):
        # load_json checks that health.json is a dict and nothing more, and
        # the adjudicate step is not continue-on-error: a null field here
        # stops the dashboard and the bot every 15 minutes.
        note = pooled(prev={"metrics": None}, verdict="BREACH", cause="CAPACITY")["pool"]
        self.assertEqual(note["since"], health.iso(T0))

    def test_an_artifact_that_stopped_moving_carries_no_numbers(self):
        # latest-build.txt keeps resolving after the periodic dies, so a stale
        # window_end is the only signal that the numbers stopped.
        measured = T0 - timedelta(hours=4)
        note = pooled(verdict="BREACH", cause="CAPACITY", window_end=measured)["pool"]
        self.assertEqual(note, {"since": health.iso(T0), "verdict": "STALE", "breach_seen": False, "measured_at": health.iso(measured)})
        self.assertIn(
            f"queue wait unmeasured: last reading {health.iso(measured)};"
            " the hourly pool-pressure job has missed the last few",
            pooled(verdict="BREACH", cause="CAPACITY", window_end=measured)["evidence"],
        )
        # Two missed hourly runs plus the job's own timeout: still fresh at 3h.
        self.assertEqual(pooled(verdict="BREACH", cause="CAPACITY", window_end=T0 - timedelta(hours=3))["pool"]["verdict"], "BREACH")

    def test_the_artifact_ages_against_the_wall_clock_not_the_data_horizon(self):
        # The branch every production tick takes: main() passes a wall clock
        # unless --now. One Prow stall freezes data.json's horizon and the
        # artifact together, so ageing against `now` never fires the switch.
        stalled = pooled(verdict="BREACH", cause="CAPACITY", window_end=T0, wall_clock=T0 + timedelta(hours=4))
        self.assertEqual(stalled["pool"]["verdict"], "STALE")
        self.assertIsNone(stalled["metrics"]["queue_wait_p50_s"])

    def test_a_stale_artifact_also_drops_the_digest_number(self):
        self.assertIsNone(pooled(verdict="OK", window_end=T0 - timedelta(hours=4))["metrics"]["queue_wait_p50_s"])

    def test_unmeasured_is_a_note_even_though_nothing_breached(self):
        result = pooled(verdict="UNMEASURED")
        self.assertEqual(result["pool"]["verdict"], "UNMEASURED")
        self.assertIn("pool pressure: the hourly check ran but could not read how long recent runs waited", result["evidence"])

    def test_the_digest_wait_is_todays_median_not_the_seven_day_one(self):
        # "last 24h" in the headline: the seven-day median under it would be a
        # different window's number wearing the same label.
        result = pooled(verdict="OK", p50=9.0, today_p50=0.4)
        self.assertEqual(result["metrics"]["queue_wait_p50_s"], 24)

    def test_the_note_rides_beside_an_incident_unlike_the_slow_one(self):
        # Three setup deaths on two pull requests make the state DEGRADED
        # (rule 3). Rule 7 is suppressed there; rule 8 is not -- a different
        # job measuring different data cannot be this incident's own symptom.
        doc = data(*(run(300 + i, pr, T0 - timedelta(minutes=10 * i), minutes=1, result="FAILURE") for i, pr in enumerate([1, 1, 2])))
        result = pooled(doc=doc, verdict="BREACH", cause="CAPACITY", p50=24.1)
        self.assertEqual(result["state"], "DEGRADED")
        self.assertIsNotNone(result["pool"])

    def test_a_sub_minute_wait_reads_in_seconds(self):
        self.assertEqual(health.wait_text(24), "24s")
        self.assertEqual(health.wait_text(1320), "22 min")
        self.assertEqual(health.wait_text(None), "?")


class Advice(unittest.TestCase):
    def test_outage_advice_cites_the_tracking_issue_from_case_notes(self):
        notes = {"compliance-rbac-overgrant": {"issues": ["#998", "#1171"]}}
        text = health.advice_for("OUTAGE", "shared_break", ["compliance-rbac-overgrant"], None, notes)
        self.assertEqual(text, "Don't retest yet; the failing cases share a cause. Tracking: #998, #1171")
        text = health.advice_for("OUTAGE", "shared_break", ["unknown-case"], None, notes)
        self.assertTrue(text.endswith("Tracking: no issue filed yet — file one with the presubmit-gate label"))

    def test_the_posters_tracking_issue_rides_in_health_json_until_green(self):
        doc = data(*(run(100 + i, i, T0 - timedelta(hours=1) + timedelta(minutes=10 * i), tasks=broken_tasks({"agent-kanban-smoke"})) for i in range(4)))
        posted = {"state": "OUTAGE", "issue": {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300"}}
        first = health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), posted=posted)
        self.assertEqual(first["issue"], posted["issue"])
        self.assertEqual(first["tracking_issues"], ["#1300"])
        self.assertEqual(first["advice"], "Don't retest yet; the failing cases share a cause. Tracking: #1300")
        # Carried from the previous health.json when the poster's state has none.
        second = health.adjudicate(doc, T0 + timedelta(minutes=15), first, health.Roster.fixed(ADMITTED), posted={"state": "OUTAGE"})
        self.assertEqual(second["issue"], posted["issue"])
        # Gone on GREEN, and never a non-issue.
        self.assertIsNone(health.adjudicate(data(), T0, None, health.Roster.fixed(ADMITTED), posted=posted)["issue"])
        self.assertIsNone(health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED), posted={"issue": None})["issue"])
        self.assertEqual(health.adjudicate(doc, T0, None, health.Roster.fixed(ADMITTED))["tracking_issues"], [])

    def test_the_repo_case_notes_load(self):
        notes = health.load_case_notes(CASE_NOTES)
        self.assertIn("#1171", notes["compliance-rbac-overgrant"]["issues"])
        self.assertEqual(health.load_case_notes(pathlib.Path("/nonexistent.yaml")), {})


# --------------------------------------------------------------------------- #
# The replay over the real week
# --------------------------------------------------------------------------- #


def at(timeline, when):
    """The timeline entry in force at `when` (an aware datetime)."""
    current = None
    for entry in timeline:
        if health.parse_iso(entry["at"]) <= when:
            current = entry
        else:
            break
    return current


def between(timeline, start, end):
    """Entries whose `at` falls in [start, end)."""
    return [e for e in timeline if start <= health.parse_iso(e["at"]) < end]


def day(month_day, hour=0, minute=0):
    month, dom = month_day.split("-")
    return datetime(2026, int(month), int(dom), hour, minute, tzinfo=UTC)


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = health.load_json(FIXTURE)
        cls.roster = health.Roster.from_history(json.loads(ROSTER_HISTORY.read_text()))
        cls.notes = health.load_case_notes(CASE_NOTES)
        ticks = list(health.replay(cls.data, timedelta(minutes=30), cls.roster, start=day("09-01"), notes=cls.notes))
        cls.every = ticks
        cls.timeline = health.timeline(ticks)

    def test_the_fixture_is_what_trim_produces(self):
        trimmed = self.data["trimmed"]
        again = health.trim(self.data, health.parse_iso(trimmed["from"]), health.parse_iso(trimmed["to"]), trimmed["source"])
        self.assertEqual(again["runs"], self.data["runs"], "trim is idempotent over its own output")
        self.assertEqual(trimmed["reason_chars"], health.TRIM_REASON_CHARS)
        self.assertGreater(len(self.data["runs"]), 500)
        self.assertLessEqual(max(len(rep.get("reason") or "") for r in self.data["runs"] for t in r["tasks"] for rep in t.get("reps") or []), health.TRIM_REASON_CHARS)

    def test_09_01_afternoon_is_a_storm(self):
        # 2026-09-01 14:00-16:00Z: 108 empty-record reps on three PRs (#1082,
        # #1105, #1107 -- every case collapsed with "trajectory is empty")
        # plus five setup deaths on four PRs. Evening in IST, morning in PDT.
        entry = at(self.timeline, day("09-01", 15, 30))
        self.assertEqual((entry["state"], entry["condition"]), ("DEGRADED", "storm"), entry)
        # Nothing recovers that evening -- one green run in twenty -- so the
        # storm call is still the state at 22:00Z.
        entry = at(self.timeline, day("09-01", 22, 0))
        self.assertEqual((entry["state"], entry["condition"]), ("DEGRADED", "storm"), entry)
        self.assertNotIn("OUTAGE", {e["state"] for e in between(self.timeline, day("09-01"), day("09-02"))})

    def test_09_02_morning_outage_names_compliance_then_rca(self):
        # #1171: compliance-rbac-overgrant collapsing on unrelated PRs from
        # ~02:00Z; the fourth distinct PR lands at 10:42Z. #1189: rca joins
        # in the afternoon. Both were admitted at the time (roster era 2).
        entry = at(self.timeline, day("09-02", 11, 0))
        self.assertEqual(entry["state"], "OUTAGE", entry)
        self.assertEqual(entry["failing_cases"], ["compliance-rbac-overgrant"])
        first_outage = next(e for e in self.timeline if e["state"] == "OUTAGE")
        self.assertGreaterEqual(health.parse_iso(first_outage["at"]), day("09-02", 9, 0))
        self.assertLessEqual(health.parse_iso(first_outage["at"]), day("09-02", 12, 0))
        entry = at(self.timeline, day("09-02", 18, 0))
        self.assertEqual(entry["state"], "OUTAGE")
        self.assertIn("compliance-rbac-overgrant", entry["failing_cases"])
        self.assertIn("rca-remediation-pr", entry["failing_cases"])

    def test_09_03_morning_recovers_and_evening_is_a_storm(self):
        # #1214: the token-quota storm. Builds started ~13:30Z ran five hours
        # and finished 18:00-19:40Z with 13+ reps each lost to infra.
        self.assertEqual(at(self.timeline, day("09-03", 12, 0))["state"], "GREEN")
        entry = at(self.timeline, day("09-03", 19, 0))
        self.assertEqual((entry["state"], entry["condition"]), ("DEGRADED", "storm"), entry)

    def test_09_04_calm_windows_are_green(self):
        self.assertEqual(at(self.timeline, day("09-04", 6, 0))["state"], "GREEN")
        self.assertEqual(at(self.timeline, day("09-04", 12, 0))["state"], "GREEN")

    def test_the_quiet_weekend_never_reads_as_an_outage(self):
        # Saturday 09-05 afternoon through Monday 09-07 morning: a setup-death
        # cluster on 09-05 12:27-12:52Z (#965, #1121, #1186, #1199 died at
        # 0 min), then greens. Nothing shared broke until the 09-07
        # auto-upgrade (#1269, filed 16:52Z; the first four collapsed runs
        # finished 11:30-13:59Z).
        weekend = between(self.timeline, day("09-05", 13, 0), day("09-07", 11, 0))
        self.assertNotIn("OUTAGE", {e["state"] for e in weekend}, weekend)
        self.assertEqual(at(self.timeline, day("09-06", 12, 0))["state"], "GREEN")
        self.assertEqual(at(self.timeline, day("09-07", 10, 0))["state"], "GREEN")
        entry = at(self.timeline, day("09-07", 16, 0))
        self.assertEqual((entry["state"], entry["failing_cases"]), ("OUTAGE", CRASHLOOP_TRIO), "#1269, the same break as #1278 a day earlier")

    def test_09_08_is_an_outage_naming_the_crashloop_trio(self):
        # #1269 / #1278: seeded-a saturated after the weekend node upgrade,
        # payments-api Pending everywhere, the crashloop trio reds every PR.
        entry = at(self.timeline, day("09-08", 12, 0))
        self.assertEqual(entry["state"], "OUTAGE", entry)
        for case in CRASHLOOP_TRIO:
            self.assertIn(case, entry["failing_cases"])
        first = next(e for e in self.timeline if health.parse_iso(e["at"]) >= day("09-08") and e["state"] == "OUTAGE")
        self.assertGreaterEqual(health.parse_iso(first["at"]), day("09-08", 3, 0))
        _, last_health = self.every[-1]
        self.assertEqual(last_health["state"], "OUTAGE")
        self.assertTrue(last_health["advice"].startswith("Don't retest yet; the failing cases share a cause. Tracking:"))

    def test_the_timeline_is_compact_enough_to_read(self):
        # A change is a state, a condition or a case-set change: the storm
        # window's moving bounds do not count. Bound the week's entries so
        # the poster's silence is real, not a coincidence of the fixture.
        self.assertLess(len(self.timeline), 60, [e["at"] for e in self.timeline])


class LostPodsReplay(unittest.TestCase):
    """2026-09-11 (#1478): the published data.json's runs of that day, with
    the twenty zero-task reds re-read by the collector so they carry
    has_build_log and the pod record. Twelve of them are lost pods -- five
    nodes went NotReady 14:03-14:17Z under runs on twelve pull requests --
    and eight are clone failures (setup deaths), five of them in the morning.
    health.py of the day counted three of the twelve as setup deaths and
    advised checking the pool projects."""

    @classmethod
    def setUpClass(cls):
        cls.data = health.load_json(LOST_FIXTURE)
        cls.roster = health.Roster.fixed(ROSTER_0911)
        cls.every = list(health.replay(cls.data, timedelta(minutes=30), cls.roster, start=day("09-11")))
        cls.timeline = health.timeline(cls.every)

    def tick(self, when):
        return next(h for now, h in self.every if now == when)

    def test_the_fixture_is_what_trim_produces(self):
        trimmed = self.data["trimmed"]
        again = health.trim(self.data, health.parse_iso(trimmed["from"]), health.parse_iso(trimmed["to"]), trimmed["source"])
        self.assertEqual(again["runs"], self.data["runs"])
        runs = [health.Run(r) for r in self.data["runs"]]
        self.assertEqual(sum(1 for r in runs if r.lost_pod), 12)
        self.assertEqual(sum(1 for r in runs if r.setup_death), 8)

    def test_the_morning_clone_failures_are_still_setup_deaths(self):
        # 10:50-11:16Z: four clone failures on three pull requests (#1195
        # twice, #1456, #1468), none of them a lost pod.
        entry = at(self.timeline, day("09-11", 11, 30))
        self.assertEqual((entry["state"], entry["condition"]), ("DEGRADED", "setup_deaths"), entry)
        self.assertNotIn("lost_pods", {e["condition"] for e in between(self.timeline, day("09-11"), day("09-11", 14, 0))})

    def test_the_build_cluster_event_is_lost_pods_on_five_nodes(self):
        entry = at(self.timeline, day("09-11", 14, 30))
        self.assertEqual((entry["state"], entry["condition"]), ("DEGRADED", "lost_pods"), entry)
        self.assertEqual(entry["cause"], "lost pods: 12 runs on 12 PRs died with their build node 14:05–14:19 UTC")
        tick = self.tick(day("09-11", 15, 0))
        self.assertEqual(
            tick["incident"],
            {
                "prs": [926, 1118, 1246, 1258, 1319, 1351, 1362, 1439, 1451, 1456, 1460, 1471],
                "runs": 12,
                "window_start": "2026-09-11T14:05:52+00:00",
                "window_end": "2026-09-11T14:19:16+00:00",
                "nodes": {
                    "gke-kube-agents-prow-default-pool-eb220b2a-6uhg": 1,
                    "gke-kube-agents-prow-default-pool-eb220b2a-93sl": 2,
                    "gke-kube-agents-prow-default-pool-eb220b2a-er33": 3,
                    "gke-kube-agents-prow-default-pool-eb220b2a-pe72": 3,
                    "gke-kube-agents-prow-default-pool-eb220b2a-sgnk": 3,
                },
                "event": True,
            },
        )
        self.assertTrue(
            tick["advice"].startswith(
                "The Prow build cluster lost node(s) gke-kube-agents-prow-default-pool-eb220b2a-6uhg, gke-kube-agents-prow-default-pool-eb220b2a-93sl ×2,"
                " gke-kube-agents-prow-default-pool-eb220b2a-er33 ×3, gke-kube-agents-prow-default-pool-eb220b2a-pe72 ×3, gke-kube-agents-prow-default-pool-eb220b2a-sgnk ×3"
                " at 10:05 AM ET; 12 runs died mid-run."
            ),
            tick["advice"],
        )

    def test_setup_deaths_no_longer_claim_the_lost_pods(self):
        # At 15:00Z the setup-death window holds three clone failures (#1471
        # 13:14Z, #1446 14:29Z, #1319 14:49Z); #1351's 297-second lost pod,
        # which the old rule counted, is not among them.
        tick = self.tick(day("09-11", 15, 0))
        setup = [line for line in tick["evidence"] if line.startswith("setup/clone failures:")]
        self.assertEqual(setup, ["setup/clone failures: 3 runs under 5 min with no tasks in the last 2h (#1319, #1446, #1471)"])
        self.assertEqual((tick["metrics"]["lost_pods"], tick["metrics"]["setup_deaths"]), (12, 8))


class SlowGateReplay(unittest.TestCase):
    """2026-09-14 (#1586): the published data.json's runs of the week ending
    18:20Z that day -- the seven days rule 7's baseline needs. Every run of
    the afternoon was green and three hours long (Vertex latency; the 429s
    were all retried), so rules 1-3b see nothing. The note appears at 18:00Z,
    about one run's length after the slowdown began, and never over the quiet
    09-12/13 weekend before it."""

    @classmethod
    def setUpClass(cls):
        cls.data = health.load_json(SLOW_FIXTURE)
        # The roster enters rules 1 and 4 only; rule 7 reads the wall clock.
        cls.every = list(health.replay(cls.data, timedelta(minutes=30), health.Roster.fixed(ROSTER_0911), start=day("09-07", 18, 0)))

    def tick(self, when):
        return next(h for now, h in self.every if now == when)

    def test_the_fixture_is_what_trim_produces(self):
        trimmed = self.data["trimmed"]
        again = health.trim(self.data, health.parse_iso(trimmed["from"]), health.parse_iso(trimmed["to"]), trimmed["source"])
        self.assertEqual(again["runs"], self.data["runs"])

    def test_the_quiet_weekend_is_not_slow(self):
        self.assertEqual([now for now, h in self.every if day("09-12") <= now < day("09-14", 18, 0) and h["slow"]], [])

    def test_09_14_reads_slow_from_18_00z_with_the_days_numbers(self):
        tick = self.tick(day("09-14", 18, 0))
        self.assertEqual(tick["state"], "GREEN")
        self.assertEqual(
            tick["slow"],
            {
                "since": "2026-09-14T18:00:00+00:00",
                "runs": 5,
                "min_s": 9161,
                "median_s": 10984,
                "max_s": 12836,
                "baseline_days": 7,
                "baseline_runs": 264,
                "baseline_p50_s": 9085,
                "baseline_p90_s": 11919,
                "infra_reps": 2,
            },
        )
        self.assertIn("slow gate: last 5 full runs 152–213 min (median 183) against a 7-day typical of 151 min (p90 198); 2 reps lost to infra", tick["evidence"])
        self.assertEqual(self.tick(day("09-14", 18, 30))["slow"]["since"], "2026-09-14T18:00:00+00:00", "the episode keeps its start")

    def test_the_replay_timeline_shows_the_note_beside_the_state(self):
        # `--replay` is how a threshold is re-checked on the next incident,
        # so the note's edges are entries and the text form names them.
        entries = health.timeline(self.every)
        edge = next(e for e in entries if e["slow"] and health.parse_iso(e["at"]) >= day("09-14"))
        self.assertEqual((edge["at"], edge["state"], edge["slow"]), ("2026-09-14T18:00:00+00:00", "GREEN", {"since": "2026-09-14T18:00:00+00:00", "median_s": 10984, "baseline_p50_s": 9085}))
        self.assertIn("2026-09-14T18:00:00+00:00  GREEN      (slow since 2026-09-14T18:00:00+00:00: median 183 min against 151)", health.format_timeline([edge]))
        self.assertTrue(all(e["slow"] is None for e in entries if day("09-12") <= health.parse_iso(e["at"]) < day("09-14", 18, 0)))

    def test_three_runs_above_the_seven_day_p90_would_not_have_fired(self):
        # The rule the issue proposed, checked at the tick the note appears:
        # the newest three full runs took 207, 152 and 167 minutes against a
        # p90 of 198, because 09-08 to 09-11 had been slow days too.
        now = day("09-14", 18, 0)
        full = [r for r in health.load_runs(self.data) if r.finished <= now and r.result in ("SUCCESS", "FAILURE") and len(r.tasks) >= health.SLOW_MIN_TASKS]
        p90 = health.percentile([r.wall_clock.total_seconds() for r in full[:-3] if r.finished > now - health.SLOW_BASELINE], 90)
        self.assertEqual([int(r.wall_clock.total_seconds() // 60) for r in full[-3:]], [207, 152, 167])
        self.assertEqual(int(p90 // 60), 198)
        self.assertFalse(all(r.wall_clock.total_seconds() > p90 for r in full[-3:]))


class CommandLine(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = health.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_replay_json_on_the_fixture(self):
        rc, out, _ = self.run_main(["--replay", "--json", "--data", str(FIXTURE), "--roster-history", str(ROSTER_HISTORY), "--from", "2026-09-08T00:00:00Z"])
        self.assertEqual(rc, 0)
        entries = json.loads(out)
        self.assertEqual(entries[-1]["state"], "OUTAGE")
        rc, out, _ = self.run_main(["--replay", "--data", str(FIXTURE), "--roster-history", str(ROSTER_HISTORY), "--from", "2026-09-08T00:00:00Z", "--step", "1h"])
        self.assertIn("OUTAGE    shared fixture/environment break: cluster-agent-crashloop", out)

    def test_one_tick_round_trips_prev_through_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            doc = tmp / "data.json"
            doc.write_text(json.dumps(data(*(run(100 + i, i, T0 - timedelta(hours=1) + timedelta(minutes=10 * i), tasks=broken_tasks({"agent-kanban-smoke"})) for i in range(4)))))
            out = tmp / "health.json"
            rc, _, _ = self.run_main(["--data", str(doc), "--out", str(out), "--now", T0.isoformat(), "--admitted", ",".join(sorted(ADMITTED))])
            self.assertEqual(rc, 0)
            first = json.loads(out.read_text())
            self.assertEqual(first["state"], "OUTAGE")
            later = (T0 + timedelta(hours=8)).isoformat()
            rc, _, _ = self.run_main(["--data", str(doc), "--prev", str(out), "--out", str(out), "--now", later, "--admitted", ",".join(sorted(ADMITTED))])
            second = json.loads(out.read_text())
            self.assertEqual(second["state"], "OUTAGE", "no greens yet: the outage holds")
            self.assertTrue(second["recovering"])
            self.assertEqual(second["since"], first["since"])

    def test_fixture_status_is_surfaced_and_missing_prev_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            doc = tmp / "data.json"
            doc.write_text(json.dumps(data()))
            fixtures = tmp / "fixtures.json"
            fixtures.write_text(json.dumps({"healed": 30, "broken": 0}))
            rc, out, _ = self.run_main(["--data", str(doc), "--prev", str(tmp / "missing.json"), "--fixture-status", str(fixtures), "--admitted", "a"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["metrics"]["fixtures"], {"healed": 30, "broken": 0})

    def test_trim_writes_gzip_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            doc = tmp / "data.json"
            doc.write_text(json.dumps(data(run(1, 1, T0, tasks=[task("x", "pfe")]))))
            out = tmp / "fixture.json.gz"
            rc, _, _ = self.run_main(["--trim", "--data", str(doc), "--from", (T0 - timedelta(days=1)).isoformat(), "--to", (T0 + timedelta(days=1)).isoformat(), "--out", str(out), "--admitted", "x"])
            self.assertEqual(rc, 0)
            with gzip.open(out, "rt") as handle:
                trimmed = json.load(handle)
            self.assertEqual(trimmed["runs"][0]["tasks"][0]["reps"][2]["reason"], EMPTY_RECORD[: health.TRIM_REASON_CHARS])
            self.assertNotIn("n", trimmed["runs"][0]["tasks"][0]["reps"][0])
            self.assertEqual(health.load_json(out)["runs"], trimmed["runs"])


# --------------------------------------------------------------------------- #
# Rule 3c: fixture drift (the hourly seeded-fleet scan)
# --------------------------------------------------------------------------- #

DRIFT_ROLE = "crashloop-workload"
OTHER_ROLE = "no-pdb-workload"
DRIFT_DETAIL = "pod?app=payments-api status.containerStatuses[*].restartCount any_ge 1: observed 0 (why: the crashloop cases read an OOMKilled termination)"
BLIND_REASON = "cannot mint a token as seeded-fleet-reader@kube-agents-evals-1.iam.gserviceaccount.com (is roles/iam.serviceAccountTokenCreator granted to the bot?): ERROR: PERMISSION_DENIED"


def project(i):
    return f"kube-agents-evals-{i}"


def scan(drifted=None, previous=None, at=None, projects=30, checked=None):
    """A fixture-state.json as scripts/eval_dashboard/fixture_state.py writes
    it: `drifted` {project: [roles]} this scan, `previous` the same for the
    scan before, every other role healthy -- or not checked on every project
    outside `checked` when it is given."""
    drifted = drifted or {}
    at = at or T0 - timedelta(minutes=5)
    entries = {}
    for i in range(1, projects + 1):
        name = project(i)
        readable = checked is None or name in checked
        roles = {}
        for role in (DRIFT_ROLE, OTHER_ROLE):
            if not readable:
                roles[role] = {"state": "not_checked", "detail": [BLIND_REASON]}
            elif role in drifted.get(name, []):
                roles[role] = {"state": "drifted", "detail": [DRIFT_DETAIL]}
            else:
                roles[role] = {"state": "healthy", "detail": []}
        entries[name] = {"roles": roles}
    return {
        "schema_version": 1,
        "scanned_at": health.iso(at),
        "projects": entries,
        "previous": {"scanned_at": health.iso(at - timedelta(hours=1)), "drifted": previous or {}},
    }


class FixtureDrift(unittest.TestCase):
    def judge(self, scan_doc, prev=None, now=T0, doc=None, posted=None):
        doc = doc or data(*(run(100 + i, 10 + i, T0 - timedelta(minutes=30 * i), tasks=broken_tasks(set())) for i in range(3)))
        return health.adjudicate(doc, now, prev, health.Roster.fixed(ADMITTED), posted=posted, fixture_state_doc=scan_doc)

    def test_no_scan_means_no_condition_and_no_block(self):
        result = self.judge(None)
        self.assertEqual((result["state"], result["fixture_state"]), ("GREEN", None))
        self.assertFalse(any("fixture" in line for line in result["evidence"]))

    def test_one_project_once_is_evidence_not_a_condition(self):
        result = self.judge(scan(drifted={project(1): [DRIFT_ROLE]}))
        self.assertEqual(result["state"], "GREEN")
        self.assertIn(f"{DRIFT_ROLE} drifted on 1 pool project(s) ({project(1)}) at the 23:55 UTC scan; not yet repeated or widespread", result["evidence"])
        block = result["fixture_state"]
        self.assertEqual((block["projects"], block["checked"], block["drifted"], block["unknown"], block["stale"]), (30, 30, {project(1): [DRIFT_ROLE]}, False, False))
        self.assertEqual(block["scanned_at"], health.iso(T0 - timedelta(minutes=5)))

    def test_the_same_role_on_the_same_project_two_scans_running_degrades(self):
        result = self.judge(scan(drifted={project(1): [DRIFT_ROLE]}, previous={project(1): [DRIFT_ROLE]}))
        self.assertEqual((result["state"], result["condition"]), ("DEGRADED", "fixture_drift"))
        self.assertEqual(result["cause"], f"seeded fixture drift: {DRIFT_ROLE} out of designed state on 1 pool project(s)")
        self.assertEqual(result["since"], health.iso(T0), "the first tick enters directly: the scan is the currency")
        incident = result["incident"]
        self.assertEqual((incident["prs"], incident["runs"], incident["window_end"]), ([], 0, None))
        self.assertEqual(incident["window_start"], health.iso(T0 - timedelta(minutes=5)))
        self.assertEqual((incident["roles"], incident["projects"]), ([DRIFT_ROLE], [project(1)]))
        self.assertEqual(incident["drift"], {project(1): {DRIFT_ROLE: [DRIFT_DETAIL]}})
        self.assertIn(f"{DRIFT_ROLE} drifted on 1 pool project(s) ({project(1)}) at the 23:55 UTC scan, the 2nd consecutive scan on {project(1)}", result["evidence"])
        self.assertEqual(
            result["advice"],
            f"A red on a case that depends on {DRIFT_ROLE} from a run that leased {project(1)} is the fixture, not your change;"
            " retest once the fleet owner has re-applied bench/tf/fleet there (README, State and reconcile).",
        )
        self.assertEqual(result["failing_cases"], [])

    def test_previous_drift_on_another_project_or_role_does_not_count(self):
        self.assertEqual(self.judge(scan(drifted={project(1): [DRIFT_ROLE]}, previous={project(2): [DRIFT_ROLE]}))["state"], "GREEN")
        self.assertEqual(self.judge(scan(drifted={project(1): [DRIFT_ROLE]}, previous={project(1): [OTHER_ROLE]}))["state"], "GREEN")

    def test_three_projects_in_one_scan_degrade_and_two_do_not(self):
        three = {project(i): [DRIFT_ROLE] for i in (1, 2, 3)}
        result = self.judge(scan(drifted=three))
        self.assertEqual((result["state"], result["condition"]), ("DEGRADED", "fixture_drift"))
        self.assertEqual(result["incident"]["projects"], [project(1), project(2), project(3)])
        self.assertEqual(result["cause"], f"seeded fixture drift: {DRIFT_ROLE} out of designed state on 3 pool project(s)")
        self.assertEqual(self.judge(scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2)}))["state"], "GREEN")
        # Two roles on two projects each is still two projects per role.
        self.assertEqual(self.judge(scan(drifted={project(1): [DRIFT_ROLE, OTHER_ROLE], project(2): [DRIFT_ROLE, OTHER_ROLE]}))["state"], "GREEN")

    def test_only_the_firing_roles_make_the_incident(self):
        drifted = {project(i): [DRIFT_ROLE] for i in (1, 2, 3)}
        drifted[project(4)] = [OTHER_ROLE]
        result = self.judge(scan(drifted=drifted))
        self.assertEqual((result["incident"]["roles"], result["incident"]["projects"]), ([DRIFT_ROLE], [project(1), project(2), project(3)]))
        self.assertEqual(result["fixture_state"]["drifted"], drifted, "the block carries every drift, firing or not")
        self.assertTrue(any(line.startswith(f"{OTHER_ROLE} drifted on 1 pool project(s)") and line.endswith("not yet repeated or widespread") for line in result["evidence"]))

    def test_evidence_names_four_projects_then_counts(self):
        drifted = {project(i): [DRIFT_ROLE] for i in range(1, 8)}
        result = self.judge(scan(drifted=drifted))
        self.assertIn(f"{DRIFT_ROLE} drifted on 7 pool project(s) ({project(1)}, {project(2)}, {project(3)}, {project(4)} and 3 more) at the 23:55 UTC scan", result["evidence"])

    def test_ranked_below_every_run_based_condition(self):
        lost_doc = data(*(lost(100 + i, i, T0 - timedelta(minutes=5 * i)) for i in range(3)))
        result = self.judge(scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2, 3)}), doc=lost_doc)
        self.assertEqual(result["condition"], "lost_pods")
        self.assertTrue(any(line.startswith(f"{DRIFT_ROLE} drifted on 3 pool project(s)") for line in result["evidence"]), "the drift stays as context")

    def test_a_stale_scan_is_ignored_with_a_note(self):
        result = self.judge(scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2, 3)}, at=T0 - timedelta(hours=4)))
        self.assertEqual(result["state"], "GREEN")
        self.assertTrue(result["fixture_state"]["stale"])
        self.assertIn(f"fixture-state scan is 4h old (last {health.iso(T0 - timedelta(hours=4))}); ignored, check the scan job", result["evidence"])
        fresh = self.judge(scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2, 3)}, at=T0 - timedelta(hours=2, minutes=59)))
        self.assertEqual(fresh["state"], "DEGRADED", "inside three hours the scan counts")

    def test_a_scan_that_saw_nothing_is_unknown_never_a_drift(self):
        result = self.judge(scan(checked=set()))
        self.assertEqual(result["state"], "GREEN")
        block = result["fixture_state"]
        self.assertEqual((block["unknown"], block["checked"], block["reason"]), (True, 0, BLIND_REASON))
        self.assertIn(f"fixture-state scan at 23:55 UTC could check none of 30 pool projects ({BLIND_REASON})", result["evidence"])
        # One readable project is a scan, not a blind one.
        partial = self.judge(scan(checked={project(1)}))
        self.assertEqual((partial["fixture_state"]["unknown"], partial["fixture_state"]["checked"]), (False, 1))

    def test_it_ends_the_hour_the_scan_clears_and_holds_while_it_does_not(self):
        firing = scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2, 3)})
        prev = self.judge(firing)
        self.assertEqual(prev["condition"], "fixture_drift")
        later = T0 + timedelta(hours=1)
        held = self.judge(scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2, 3)}, at=later - timedelta(minutes=5)), prev=prev, now=later)
        self.assertEqual((held["state"], held["condition"], held["since"]), ("DEGRADED", "fixture_drift", health.iso(T0)))
        cleared = self.judge(scan(at=later - timedelta(minutes=5)), prev=held, now=later)
        self.assertEqual((cleared["state"], cleared["condition"], cleared["recovering"]), ("GREEN", None, False), "no three-green-runs bar: the scan is the recovery")
        # One project still drifted, not repeated: the condition is over.
        one = self.judge(scan(drifted={project(1): [DRIFT_ROLE]}, at=later - timedelta(minutes=5)), prev=held, now=later)
        self.assertEqual(one["state"], "GREEN")

    def test_a_scan_that_could_not_see_the_incident_holds_it(self):
        firing = scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2, 3)})
        prev = self.judge(firing)
        later = T0 + timedelta(hours=1)
        at = later - timedelta(minutes=5)
        cases = {
            "absent": (None, "no fixture-state scan was read this tick"),
            "stale": (scan(at=later - timedelta(hours=4)), "the fixture-state scan is stale"),
            "blind": (scan(at=at, checked=set()), "the fixture-state scan could check no project"),
            "one project not checked": (scan(at=at, checked={project(i) for i in range(1, 31)} - {project(2)}), f"the fixture-state scan could not read {project(2)}"),
        }
        for name, (doc, why) in cases.items():
            with self.subTest(name):
                held = self.judge(doc, prev=prev, now=later)
                self.assertEqual((held["state"], held["condition"], held["since"], held["recovering"]), ("DEGRADED", "fixture_drift", health.iso(T0), False))
                self.assertEqual(held["incident"], prev["incident"], "the held tick keeps the incident it entered with")
                self.assertIn(f"fixture drift held: {why}; a scan that reads those projects clean ends it", held["evidence"])
        # A project the incident did not name being unreadable is not a hold.
        other = self.judge(scan(at=at, checked={project(i) for i in range(1, 31)} - {project(9)}), prev=prev, now=later)
        self.assertEqual((other["state"], other["condition"]), ("GREEN", None))
        # The hold is not "recovering": no three-green-runs line is added.
        self.assertFalse(any("consecutive green runs" in line for line in self.judge(None, prev=prev, now=later)["evidence"]))

    def test_the_posters_issue_is_cited_for_the_condition_it_was_filed_for(self):
        firing = scan(drifted={project(i): [DRIFT_ROLE] for i in (1, 2, 3)})
        owner = {"number": 1400, "url": "https://github.com/gke-labs/kube-agents/issues/1400", "condition": "fixture_drift"}
        cited = self.judge(firing, posted={"issue": owner})
        self.assertEqual((cited["issue"], cited["tracking_issues"]), (owner, ["#1400"]))
        other = dict(owner, condition="lost_pods")
        self.assertIsNone(self.judge(firing, posted={"issue": other})["issue"])


if __name__ == "__main__":
    unittest.main()
