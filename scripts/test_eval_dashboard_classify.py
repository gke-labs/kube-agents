"""classify.py answers "is this red mine?" the way the eval crew did by hand.

Two layers. The rule tests build small data.json documents and pin each
threshold from both sides. The fixture tests run the real week:
testdata_classify/incidents.json.gz holds the published data.json's runs
(trimmed to the fields classify.py reads) for two windows --

    2026-09-04 18:00Z .. 09-05 06:00Z   PR #913's last runs: a storm-hit red,
                                        an abort, then green
    2026-09-07 06:00Z .. 09-08 19:00Z   the crashloop outage (#1269, #1278):
                                        the trio redding #1275, #1246, #1238,
                                        #1150, #1226, #1267; PR #608's
                                        15-case red inside it

-- and assert the classification against what those incidents were.
"""

import gzip
import json
import pathlib
import unittest
from datetime import datetime, timedelta, timezone

from eval_dashboard import classify

FIXTURE = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_classify" / "incidents.json.gz"
UTC = timezone.utc
T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

CRASHLOOP_TRIO = [
    "cluster-agent-crashloop-debug",
    "cluster-agent-crashloop-evidence-chain",
    "cluster-agent-crashloop-misleading-symptom",
]
# The roster in force on 2026-09-08 (hack/ci-eval-pr.sh at this checkout).
ADMITTED = frozenset(
    CRASHLOOP_TRIO
    + [
        "reliability-pdb-probe",
        "security-overgrant-probe",
        "upgrades-lagging-master-probe",
        "consistency-authorized-networks-probe",
        "cost-idle-pool-probe",
        "obtainability-remediation-proposal",
        "agent-kanban-smoke",
    ]
)
HOLD_OUT = "compliance-rbac-overgrant"
GRADED_FAIL = "VerificationCorrectness=0.0 (floor 1.0) -- rca-names-the-oom: required phrases absent from the report: ['OOMKilled']"
NEVER_RAN = "the record shows no agent ever ran: the trajectory is empty and tokens.total is 0"
OUTAGE = {"state": "OUTAGE", "condition": "shared_break", "failing_cases": CRASHLOOP_TRIO}
STORM = {"state": "DEGRADED", "condition": "storm", "failing_cases": []}
SETUP = {"state": "DEGRADED", "condition": "setup_deaths", "failing_cases": []}


def rep(letter):
    return {
        "p": {"result": "pass", "reason": None},
        "f": {"result": "fail", "reason": GRADED_FAIL},
        "i": {"result": "infra", "reason": NEVER_RAN},
        "e": {"result": "fail", "reason": NEVER_RAN},
    }[letter]


def task(name, letters):
    reps = [dict(rep(letter), n=i + 1) for i, letter in enumerate(letters)]
    result = "pass" if all(x == "p" for x in letters) else "infra" if all(x == "i" for x in letters) else "fail"
    return {"name": name, "result": result, "reps": reps}


def run(build, pr, finished, minutes=120, result=None, tasks=None):
    tasks = tasks or []
    if result is None:
        result = "FAILURE" if any(t["result"] == "fail" for t in tasks) else "SUCCESS"
    started = finished - timedelta(minutes=minutes)
    return {
        "build_id": str(build), "pr": pr, "project": f"kube-agents-evals-{build % 7}",
        "started": started.isoformat(), "finished": finished.isoformat(),
        "result": result, "duration_s": minutes * 60, "tasks": tasks,
    }


def gate_tasks(failing=(), letters="fff"):
    return [task(name, letters if name in failing else "ppp") for name in sorted(ADMITTED)] + [task(HOLD_OUT, "fff")]


def classify_run(target, runs, health_at=None):
    return classify.classify_run(target, runs, health_at=health_at, admitted=ADMITTED)


def case(verdict, name):
    return next(c for c in verdict["cases"] if c["case"] == name)


class RepAndOutcomeTest(unittest.TestCase):
    def test_storm_reps_are_the_harness_never_ran_phrasings_or_an_infra_verdict(self):
        self.assertEqual(classify.rep_kind({"result": "infra"}), "storm")
        self.assertEqual(classify.rep_kind({"result": "fail", "reason": NEVER_RAN}), "storm")
        self.assertEqual(classify.rep_kind({"result": "fail", "reason": "... (KUBE_AGENTS_INFRA_FAILURE): ..."}), "storm")
        self.assertEqual(classify.rep_kind({"result": "fail", "reason": GRADED_FAIL}), "fail")
        self.assertEqual(classify.rep_kind({"result": "pass"}), "pass")

    def test_outcomes(self):
        self.assertEqual(classify.outcome_of(classify.rep_counts(task("a", "ppp"))), "passed")
        self.assertEqual(classify.outcome_of(classify.rep_counts(task("a", "pfp"))), "partial")
        self.assertEqual(classify.outcome_of(classify.rep_counts(task("a", "ffi"))), "failed")
        self.assertEqual(classify.outcome_of(classify.rep_counts(task("a", "iii"))), "infra")
        self.assertEqual(classify.outcome_of(classify.rep_counts({"name": "a", "result": "fail"})), "failed", "no reps: the single result stands in")
        self.assertIsNone(classify.outcome_of(classify.rep_counts({"name": "a"})))

    def test_the_reason_loses_its_score_prefix(self):
        self.assertEqual(classify.clean_reason(GRADED_FAIL), "rca-names-the-oom: required phrases absent from the report: ['OOMKilled']")
        self.assertEqual(classify.clean_reason("plain text"), "plain text")

    def test_excerpts_are_only_ever_read_from_the_data(self):
        self.assertIsNone(classify.excerpt_of(task("a", "fff")))
        with_excerpt = dict(task("a", "fff"), excerpt="  payments-api is Pending  ")
        self.assertEqual(classify.excerpt_of(with_excerpt), "payments-api is Pending")

    def test_the_roster_is_read_from_the_ci_script(self):
        roster = classify.admitted_cases()
        self.assertIsNotNone(roster)
        for name in CRASHLOOP_TRIO:
            self.assertIn(name, roster)
        self.assertIsNone(classify.admitted_cases(pathlib.Path("/nonexistent/ci-eval-pr.sh")))


class SharedRuleTest(unittest.TestCase):
    def others(self, prs, finished_offsets_min, failing=("cluster-agent-crashloop-debug",)):
        return [run(100 + i, pr, T0 - timedelta(minutes=off), tasks=gate_tasks(failing)) for i, (pr, off) in enumerate(zip(prs, finished_offsets_min))]

    def test_two_other_prs_inside_six_hours_make_a_case_shared(self):
        target = run(1, 1275, T0, tasks=gate_tasks(["cluster-agent-crashloop-debug"]))
        runs = [target] + self.others([1246, 1238], [60, 300])
        verdict = classify_run(target, runs)
        c = case(verdict, "cluster-agent-crashloop-debug")
        self.assertEqual((c["cls"], c["also_failing_prs"]), ("shared", 2))
        self.assertEqual(verdict["verdict"], "infra")
        self.assertEqual(verdict["headline"], "1 of 10 gate cases failed. None of them look like your PR.")
        self.assertFalse(verdict["matches_incident"], "no verdict was given, so nothing to match")

    def test_one_other_pr_is_not_shared_and_the_same_pr_twice_does_not_count(self):
        target = run(1, 1275, T0, tasks=gate_tasks(["cluster-agent-crashloop-debug"]))
        runs = [target] + self.others([1246, 1246, 1275], [60, 120, 180])
        c = case(classify_run(target, runs), "cluster-agent-crashloop-debug")
        self.assertEqual(c["also_failing_prs"], 1)
        self.assertIsNone(c["cls"])

    def test_the_window_is_six_hours_before_the_start_to_the_finish(self):
        target = run(1, 1275, T0, minutes=120, tasks=gate_tasks(["cluster-agent-crashloop-debug"]))

        def also(offsets):
            return case(classify_run(target, [target] + self.others([1246, 1238, 1150], offsets)), "cluster-agent-crashloop-debug")["also_failing_prs"]

        self.assertEqual(also([120 + 6 * 60, 60, 0]), 3, "exactly 6h before the start, and exactly at the finish, are inside")
        self.assertEqual(also([120 + 6 * 60 + 1, 60, 0]), 2, "one minute earlier is outside")
        self.assertEqual(also([120 + 6 * 60, 60, -1]), 2, "one minute after the finish is outside")

    def test_the_verdict_naming_the_case_makes_it_shared_without_other_runs(self):
        target = run(1, 1275, T0, tasks=gate_tasks(["cluster-agent-crashloop-debug"]))
        verdict = classify_run(target, [target], health_at=OUTAGE)
        self.assertEqual(case(verdict, "cluster-agent-crashloop-debug")["cls"], "shared")
        self.assertTrue(verdict["matches_incident"])
        self.assertIn("matches the outage", verdict["lede"])

    def test_a_hold_out_is_reported_but_never_blamed(self):
        target = run(1, 1275, T0, result="SUCCESS", tasks=gate_tasks())
        verdict = classify_run(target, [target] + self.others([1, 2], [10, 20], failing=(HOLD_OUT,)))
        self.assertEqual(verdict["verdict"], "green")
        self.assertEqual(verdict["headline"], "All 10 gate cases passed.")
        self.assertIn("1 held-out case also failed", verdict["lede"])
        self.assertEqual(case(verdict, HOLD_OUT)["do"], classify.DO_HELD_OUT)
        self.assertFalse(case(verdict, HOLD_OUT)["admitted"])


class OnlyThisPrRuleTest(unittest.TestCase):
    def passing_others(self, count, step_min=60, prs=None):
        return [run(200 + i, (prs or [500 + i])[0] if prs else 500 + i, T0 - timedelta(minutes=step_min * (i + 1)), tasks=gate_tasks()) for i in range(count)]

    def test_three_recent_other_pr_passes_make_the_failure_this_prs(self):
        target = run(1, 913, T0, tasks=gate_tasks(["security-overgrant-probe"]))
        verdict = classify_run(target, [target] + self.passing_others(3))
        c = case(verdict, "security-overgrant-probe")
        self.assertEqual(c["cls"], "only-this-pr")
        self.assertEqual(c["do"], classify.DO_ONLY_THIS_PR)
        self.assertEqual(verdict["verdict"], "red")
        self.assertEqual(verdict["headline"], "1 of 10 gate cases failed, and it looks like your PR.")

    def test_two_passes_are_not_enough_and_a_partial_elsewhere_breaks_the_streak(self):
        target = run(1, 913, T0, tasks=gate_tasks(["security-overgrant-probe"]))
        self.assertIsNone(case(classify_run(target, [target] + self.passing_others(2)), "security-overgrant-probe")["cls"])
        others = self.passing_others(3)
        others[0]["tasks"] = [task(n, "pfp" if n == "security-overgrant-probe" else "ppp") for n in sorted(ADMITTED)]
        self.assertIsNone(case(classify_run(target, [target] + others), "security-overgrant-probe")["cls"])

    def test_only_the_last_24_hours_count(self):
        target = run(1, 913, T0, tasks=gate_tasks(["security-overgrant-probe"]))
        old = self.passing_others(3, step_min=25 * 60)
        self.assertIsNone(case(classify_run(target, [target] + old), "security-overgrant-probe")["cls"])

    def test_mixed_headline_counts_both_sides(self):
        target = run(1, 913, T0, tasks=gate_tasks(["security-overgrant-probe", "cluster-agent-crashloop-debug"]))
        others = self.passing_others(3)
        for other in others:
            other["tasks"] = [task(n, "fff" if n == "cluster-agent-crashloop-debug" else "ppp") for n in sorted(ADMITTED)]
        verdict = classify_run(target, [target] + others)
        self.assertEqual(verdict["headline"], "1 of 2 failures match failures on other PRs; 1 is only on your PR.")
        self.assertEqual(verdict["verdict"], "red")
        verdict = classify_run(target, [target] + others, health_at=OUTAGE)
        self.assertEqual(verdict["headline"], "1 of 2 failures match the outage; 1 is only on your PR.")

    def test_an_unexplained_failure_is_said_to_be_unexplained(self):
        target = run(1, 913, T0, tasks=gate_tasks(["security-overgrant-probe"]))
        verdict = classify_run(target, [target])
        self.assertIsNone(case(verdict, "security-overgrant-probe")["cls"])
        self.assertEqual(verdict["headline"], "1 of 10 gate cases failed. We can't tell yet whether it is your PR.")
        self.assertEqual(case(verdict, "security-overgrant-probe")["do"], classify.DO_UNCLEAR)


class StormAndSetupTest(unittest.TestCase):
    def test_five_storm_reps_in_the_run_make_its_failures_storm(self):
        target = run(1, 1, T0, tasks=gate_tasks(["agent-kanban-smoke", "cost-idle-pool-probe"], letters="ffi") + [task("x", "iii")])
        verdict = classify_run(target, [target])
        self.assertEqual(verdict["storm_reps"], 5)
        self.assertEqual(case(verdict, "agent-kanban-smoke")["cls"], "storm")
        self.assertEqual(case(verdict, "x")["cls"], "storm", "an ungraded case inside the storm is the storm's")
        self.assertEqual(verdict["verdict"], "infra")

    def test_four_storm_reps_are_noise_unless_the_verdict_says_storm(self):
        target = run(1, 1, T0, tasks=gate_tasks(["agent-kanban-smoke"], letters="ffi") + [task("x", "iii")])
        self.assertIsNone(case(classify_run(target, [target]), "agent-kanban-smoke")["cls"])
        verdict = classify_run(target, [target], health_at=STORM)
        self.assertEqual(case(verdict, "agent-kanban-smoke")["cls"], "storm")
        self.assertFalse(verdict["matches_incident"], "matching a storm needs the run's own signature")

    def test_nothing_graded_reads_as_the_storms_shape(self):
        target = run(1, 1, T0, tasks=[task(n, "iii") for n in sorted(ADMITTED)], result="FAILURE")
        verdict = classify_run(target, [target], health_at=STORM)
        self.assertTrue(verdict["headline"].startswith("Nothing was graded"))
        self.assertEqual(verdict["verdict"], "infra")
        self.assertTrue(verdict["matches_incident"])

    def test_a_setup_death_is_run_level(self):
        target = run(1, 1, T0, minutes=3, result="FAILURE")
        verdict = classify_run(target, [target], health_at=SETUP)
        self.assertEqual((verdict["verdict"], verdict["cls"], verdict["cases"]), ("infra", "setup", []))
        self.assertTrue(verdict["setup_death"])
        self.assertTrue(verdict["matches_incident"])
        self.assertEqual(verdict["do"], classify.DO_SETUP)
        self.assertFalse(classify_run(target, [target])["matches_incident"])

    def test_a_lost_pod_is_run_level_and_never_a_setup_death(self):
        # The build node went away (#1478): zero tasks, FAILURE, no build
        # log, NodeNotReady -- at any duration, including under five minutes.
        for minutes in (2, 128):
            raw = {"build_id": "1", "pr": 1118, "result": "failure", "started": "2026-09-11T12:11:04+00:00", "finished": "2026-09-11T14:19:16+00:00", "duration_s": minutes * 60, "tasks": [], "has_build_log": False, "pod_phase": "Failed", "pod_node": "gke-kube-agents-prow-default-pool-eb220b2a-sgnk", "pod_last_event": "NodeNotReady"}
            self.assertTrue(classify.is_lost_pod(raw))
            self.assertFalse(classify.is_setup_death(raw))
            verdict = classify.classify_run(raw, [raw], None, admitted=frozenset())
            self.assertEqual(verdict["headline"], f"The build node running this job went away {minutes} minutes in.")
            self.assertIn("lost gke-kube-agents-prow-default-pool-eb220b2a-sgnk mid-run", verdict["lede"])
            self.assertEqual((verdict["verdict"], verdict["setup_death"], verdict["matches_incident"]), ("infra", False, False))
            self.assertEqual(verdict["do"], classify.DO_LOST_POD)
        lost = {"state": "DEGRADED", "condition": "lost_pods", "failing_cases": []}
        verdict = classify.classify_run(raw, [raw], lost, admitted=frozenset())
        self.assertTrue(verdict["matches_incident"])
        self.assertIn("Other PRs lost their runs the same way right now.", verdict["lede"])
        # Missing log alone is enough; an old document with neither field
        # is unknown and falls through to the setup-death rule.
        self.assertTrue(classify.is_lost_pod(dict(raw, pod_last_event=None, pod_node=None)))
        self.assertFalse(classify.is_lost_pod(dict(raw, has_build_log=True, pod_last_event="Started")))
        legacy = {k: v for k, v in raw.items() if k not in ("has_build_log", "pod_phase", "pod_node", "pod_last_event")}
        self.assertFalse(classify.is_lost_pod(legacy))
        self.assertTrue(classify.is_setup_death(dict(legacy, duration_s=120)))

    def test_a_zero_task_green_is_a_revalidated_push(self):
        # hack/ci-eval-pr.sh step 0: inert paths changed, the earlier green stands.
        verdict = classify_run(run(1, 913, T0, minutes=4, result="SUCCESS"), [])
        self.assertEqual((verdict["verdict"], verdict["headline"]), ("green", "Green without running the cases."))

    def test_a_red_run_without_a_collapsed_gate_case_is_still_red(self):
        target = run(1, 913, T0, result="FAILURE", tasks=gate_tasks())
        target["tasks"] = [task(n, "ppp") for n in sorted(ADMITTED)]
        verdict = classify_run(target, [target])
        self.assertEqual(verdict["verdict"], "red")
        self.assertTrue(verdict["headline"].startswith("The run is red, but no gate case failed outright."))

    def test_a_slow_zero_task_failure_is_the_branchs_own(self):
        target = run(1, 913, T0, minutes=25, result="FAILURE")
        verdict = classify_run(target, [target])
        self.assertEqual(verdict["verdict"], "red")
        self.assertEqual(verdict["headline"], "The run failed before any case ran 25 minutes in.")

    def test_an_aborted_run_is_not_a_verdict(self):
        verdict = classify_run(run(1, 913, T0, minutes=8, result="ABORTED"), [])
        self.assertEqual((verdict["verdict"], verdict["headline"]), ("infra", "Aborted before it finished."))

    def test_prows_green_wins_over_collapsed_cases(self):
        target = run(1, 1, T0, result="SUCCESS", tasks=gate_tasks(["agent-kanban-smoke"], letters="ffi"))
        verdict = classify_run(target, [target])
        self.assertEqual(verdict["verdict"], "green")
        self.assertTrue(verdict["headline"].startswith("Prow passed this run"))


class PassRateTest(unittest.TestCase):
    def test_thirty_days_before_now_and_run_level_events_excluded(self):
        good = [run(300 + i, 1, T0 - timedelta(days=i), tasks=gate_tasks()) for i in range(3)]
        bad = run(310, 2, T0 - timedelta(days=1), tasks=gate_tasks(sorted(ADMITTED)))  # every gate case failed: a run-level event
        old = run(311, 3, T0 - timedelta(days=31), tasks=gate_tasks(["agent-kanban-smoke"]))
        rates = classify.case_pass_rates(good + [bad, old], T0)
        self.assertEqual(rates["agent-kanban-smoke"], 1.0)
        self.assertEqual(rates[HOLD_OUT], 0.0)
        target = run(1, 9, T0, tasks=gate_tasks(["agent-kanban-smoke"]))
        c = case(classify.classify_run(target, good + [bad, old, target], admitted=ADMITTED, now=T0), "agent-kanban-smoke")
        self.assertAlmostEqual(c["pass_rate_30d"], 9 / 12)

    def test_a_reused_list_id_does_not_return_another_lists_rates(self):
        # The memo is keyed by id(runs): a freed list's id can go to a new
        # list of the same length. Rather than coax the allocator into the
        # reuse, put the first list's entry under the second list's key,
        # which is the state the reuse leaves behind, and ask for the second.
        first = [run(400, 1, T0 - timedelta(days=1), tasks=gate_tasks())]
        second = [run(401, 2, T0 - timedelta(days=1), tasks=gate_tasks(["agent-kanban-smoke"]))]
        self.assertEqual(classify.case_pass_rates(first, T0)["agent-kanban-smoke"], 1.0)
        first_key, second_key = (id(first), len(first), T0), (id(second), len(second), T0)
        self.assertIn(first_key, classify._RATE_CACHE)
        classify._RATE_CACHE[second_key] = classify._RATE_CACHE[first_key]
        self.assertEqual(classify.case_pass_rates(second, T0)["agent-kanban-smoke"], 0.0, "the hit must be verified against the list, not only its id")
        self.assertIs(classify._RATE_CACHE[second_key][0], second, "the memo holds the list so its id stays pinned")


class RealWeekTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
            cls.data = json.load(fh)
        cls.runs = cls.data["runs"]
        cls.by_build = {r["build_id"]: r for r in cls.runs}

    def verdict(self, build, health_at=OUTAGE):
        return classify.classify_run(self.by_build[build], self.runs, health_at=health_at, admitted=ADMITTED)

    def test_the_fixture_is_real_and_trimmed(self):
        self.assertGreater(len(self.runs), 100)
        self.assertIn("trimmed", self.data)
        prs = {r["pr"] for r in self.runs}
        for pr in (1275, 1246, 1238, 1150, 1226, 1267, 913, 608):
            self.assertIn(pr, prs)

    def test_the_crashloop_trio_is_shared_on_every_outage_pr(self):
        # #1278: the trio collapsed on every PR from 2026-09-08 morning.
        for build, pr in (("2097282860221206528", 1275), ("2097160163919138816", 1246), ("2097197449759166464", 1238), ("2097213176524312576", 1150)):
            verdict = self.verdict(build)
            self.assertEqual(verdict["pr"], pr)
            for name in CRASHLOOP_TRIO:
                self.assertEqual(case(verdict, name)["cls"], "shared", (pr, name))
            self.assertEqual(verdict["verdict"], "infra", pr)
            self.assertTrue(verdict["matches_incident"], pr)
            self.assertEqual(verdict["headline"], "3 of 10 gate cases failed. None of them look like your PR.", pr)

    def test_1275_is_shared_by_the_runs_alone_without_a_verdict(self):
        # Four other PRs collapsed the trio inside the window; no health.json needed.
        verdict = self.verdict("2097282860221206528", health_at=None)
        for name in CRASHLOOP_TRIO:
            c = case(verdict, name)
            self.assertEqual(c["cls"], "shared")
            self.assertGreaterEqual(c["also_failing_prs"], 2)
        self.assertFalse(verdict["matches_incident"])
        self.assertIn("match failures on other PRs", verdict["lede"])

    def test_1226_carries_one_failure_the_outage_does_not_explain(self):
        verdict = self.verdict("2097253644305960960")
        self.assertIsNone(case(verdict, "upgrades-lagging-master-probe")["cls"])
        self.assertEqual(verdict["headline"], "3 of 4 failures match the outage; 1 is unexplained so far.")
        self.assertEqual(verdict["verdict"], "red")
        self.assertTrue(verdict["matches_incident"])

    def test_1267_passed_one_trio_case_on_a_retry(self):
        verdict = self.verdict("2097289472310775808")
        self.assertEqual(case(verdict, "cluster-agent-crashloop-debug")["outcome"], "partial")
        self.assertEqual(verdict["headline"], "2 of 10 gate cases failed. None of them look like your PR.")

    def test_608s_fifteen_failures_are_three_shared_and_the_rest_retries_or_hold_outs(self):
        # The collector marks 15 of this run's tasks `fail` (UNSTABLE partials
        # included); 7 of them collapsed on every graded repetition.
        run_608 = self.by_build["2097197276001734656"]
        self.assertEqual(sum(1 for t in run_608["tasks"] if t["result"] == "fail"), 15)
        verdict = self.verdict("2097197276001734656")
        failed = [c for c in verdict["cases"] if c["outcome"] == "failed"]
        self.assertEqual(len(failed), 7)
        gate_failed = [c for c in failed if c["admitted"]]
        self.assertEqual(sorted(c["case"] for c in gate_failed), CRASHLOOP_TRIO)
        self.assertEqual({c["cls"] for c in gate_failed}, {"shared"})
        self.assertEqual(len([c for c in verdict["cases"] if c["admitted"] and c["outcome"] == "partial"]), 4)
        self.assertEqual(verdict["verdict"], "infra")
        self.assertIn("4 held-out cases also failed", verdict["lede"])

    def test_913s_last_runs(self):
        # 09-04 13:44Z: a red inside the storm; 20:29Z aborted (superseded); 09-05 01:29Z green.
        stormy = self.verdict("2095870560067129344", health_at=None)
        self.assertGreaterEqual(stormy["storm_reps"], classify.STORM_RUN_SIGNATURE_REPS)
        self.assertEqual(case(stormy, "obtainability-remediation-proposal")["cls"], "storm")
        self.assertEqual(stormy["verdict"], "infra")
        self.assertEqual(self.verdict("2095972484435152896", health_at=None)["headline"], "Aborted before it finished.")
        green = self.verdict("2096047888260927488", health_at=None)
        self.assertEqual((green["verdict"], green["headline"]), ("green", "All 10 gate cases passed."))

    def test_a_setup_death_in_the_outage_window(self):
        verdict = self.verdict("2096985236955992064", health_at=SETUP)
        self.assertTrue(verdict["setup_death"])
        self.assertEqual(verdict["cls"], "setup")
        self.assertTrue(verdict["matches_incident"])

    def test_the_contract_keys_are_present_on_every_run(self):
        for r in self.runs:
            verdict = classify.classify_run(r, self.runs, health_at=OUTAGE, admitted=ADMITTED)
            self.assertEqual(set(verdict) >= {"build", "pr", "headline", "verdict", "cases", "matches_incident"}, True)
            self.assertIn(verdict["verdict"], ("red", "green", "infra"))
            for c in verdict["cases"]:
                self.assertEqual(set(c) >= {"case", "outcome", "cls", "also_failing_prs", "pass_rate_30d", "reason", "excerpt", "do"}, True)
                self.assertIn(c["outcome"], ("failed", "partial", "passed", "infra"))
                self.assertIn(c["cls"], ("shared", "only-this-pr", "storm", "setup", None))


if __name__ == "__main__":
    unittest.main()
