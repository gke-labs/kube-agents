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


# --------------------------------------------------------------------------- #
# Synthetic data.json builders
# --------------------------------------------------------------------------- #

REP_LETTER = {
    "p": {"result": "pass", "reason": None},
    "f": {"result": "fail", "reason": GRADED_FAIL},
    "i": {"result": "infra", "reason": RETRIES},
    "e": {"result": "fail", "reason": EMPTY_RECORD},
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

    def test_the_roster_is_read_from_the_ci_script_default_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = pathlib.Path(tmp) / "ci-eval-pr.sh"
            script.write_text('#!/bin/bash\nexport BOOTSTRAP_ADMITTED="${BOOTSTRAP_ADMITTED:-a-probe,b-probe}"\n')
            self.assertEqual(health.Roster.from_script(script).current, frozenset({"a-probe", "b-probe"}))
            script.write_text("#!/bin/bash\n")
            with self.assertRaises(SystemExit):
                health.Roster.from_script(script)
        # And the real one parses to a non-empty roster of case names.
        live = health.Roster.from_script()
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
        self.assertEqual({k: trimmed[0][k] for k in health.ENDED_FIELDS}, {"has_build_log": False, "pod_phase": "Failed", "pod_node": "node-a", "pod_last_event": "NodeNotReady"})
        self.assertFalse(set(health.ENDED_FIELDS) & set(trimmed[1]))


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


if __name__ == "__main__":
    unittest.main()
