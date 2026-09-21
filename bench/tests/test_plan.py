"""``bench-run plan``, ``compare`` and ``--until-decided``, offline and
against the scripted ``devops-bench``."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kube_agents_bench import execution, plan, report, runner, selection, stats
from kube_agents_bench import priors as priors_mod

TESTS = Path(__file__).parent
FAKE = f"{sys.executable} {TESTS / 'fake_devops_bench.py'}"
ROOT = selection.repo_root(TESTS)
BENCH = ROOT / "bench"

FREE_CASE = "agent-kanban-smoke"
FLEET_CASE = "cost-idle-pool-probe"
STACK_CASE = "gpu-stress-test-diagnosis"
LEDGER_CASE = "compliance-rbac-overgrant"

RUNNER_MODULE = [sys.executable, "-m", "kube_agents_bench.runner"]


def _prior(case_id: str, passes: int, n: int, med: float = 100.0) -> priors_mod.Prior:
    return priors_mod.Prior(case_id, passes, n, med, "test")


class PlanTest(unittest.TestCase):
    def test_rows_carry_reps_and_skip_reasons(self):
        cases = selection.select_cases([FREE_CASE, FLEET_CASE, STACK_CASE, LEDGER_CASE], root=ROOT)
        priors = {FREE_CASE: _prior(FREE_CASE, 783, 900, 300.0), FLEET_CASE: _prior(FLEET_CASE, 573, 807)}
        rows = {
            r["case_id"]: r
            for r in plan.plan_rows(
                cases, priors, effect=0.3, alpha=0.05, power=0.8, parallel=4,
                env={"BENCH_FLEET_KUBECONFIG_DIR": "/tmp/k"}, include_infra=False,
            )
        }
        self.assertIsNone(rows[FREE_CASE]["skip"])
        self.assertEqual(rows[FREE_CASE]["reps_to_show_rise"], stats.reps_to_detect(1.0, 783, 900))
        self.assertIsNone(rows[FLEET_CASE]["skip"])
        self.assertIn("--include-infra", rows[STACK_CASE]["skip"])
        self.assertIn("BENCH_GITHUB_TOKEN", rows[LEDGER_CASE]["skip"])
        self.assertIsNone(rows[STACK_CASE]["reps_to_show_rise"])
        self.assertEqual(rows[STACK_CASE]["baseline_n"], 0)

    def test_render_sorts_by_the_asked_direction_and_totals_the_runnable(self):
        cases = selection.select_cases([FREE_CASE, FLEET_CASE], root=ROOT)
        priors = {FREE_CASE: _prior(FREE_CASE, 783, 900), FLEET_CASE: _prior(FLEET_CASE, 100, 800)}
        rows = plan.plan_rows(
            cases, priors, effect=0.3, alpha=0.05, power=0.8, parallel=4, env={}, include_infra=False
        )
        text = plan.render_plan(rows, direction=plan.DIRECTION_UP, effect=0.3, reps=3, parallel=4)
        self.assertLess(text.index(FLEET_CASE), text.index(FREE_CASE))
        self.assertIn("1 of 2 selected cases can run here", text)

    def test_estimate_serialises_repetitions_unless_asked_to_overlap(self):
        case = selection.select_cases([FREE_CASE], root=ROOT)[0]
        prior = _prior(FREE_CASE, 1, 2, 60.0)
        serial = plan._estimate_minutes(3, case, prior, parallel=4, overlap_reps=False)
        overlapped = plan._estimate_minutes(3, case, prior, parallel=4, overlap_reps=True)
        self.assertEqual(serial, 3.0)
        self.assertEqual(overlapped, 1.0)


class UndecidableTest(unittest.TestCase):
    def test_a_runnable_case_without_a_prior_is_named(self):
        cases = selection.select_cases([FREE_CASE, FLEET_CASE], root=ROOT)
        self.assertEqual(plan.undecidable(cases, {}, {}, False), [FREE_CASE])
        self.assertEqual(plan.undecidable(cases, {FREE_CASE: _prior(FREE_CASE, 1, 2)}, {}, False), [])


class UntilDecidedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "runset"
        self.env = {**os.environ, execution.RUN_COMMAND_ENV: FAKE}
        self.cases = selection.select_cases([FREE_CASE], root=ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, outcomes: dict, prior: priors_mod.Prior, *, max_reps: int, reps: int = 3):
        env = {**self.env, "FAKE_OUTCOMES": json.dumps(outcomes)}
        options = execution.RunOptions(reps=reps, parallel=4, stagger_s=0.0, out_dir=self.out, include_infra=False)
        priors = {FREE_CASE: prior}
        return execution.run_cases(
            self.cases, priors, options, env=env, bench=BENCH, log=lambda _: None,
            continue_case=plan.adaptive_continue(priors, max_reps=max_reps, alpha=0.05),
        )

    def test_stops_at_significance(self):
        # A case at 0/692 on record: after three passes the difference is
        # significant, so no fourth run is launched even with max_reps 12.
        runs = self._run({FREE_CASE: ["pass"]}, _prior(FREE_CASE, 0, 692), max_reps=12)
        self.assertEqual(len(runs[0].units), 3)

    def test_stops_on_futility(self):
        # 1 pass, 2 fails against 0.55 with a ceiling of 5: two more runs
        # cannot reach 0.05 either way, so the run stops at three.
        prior = _prior(FREE_CASE, 459, 834)
        self.assertFalse(stats.can_still_decide(1, 3, 5, prior.passes, prior.n))
        runs = self._run({FREE_CASE: ["pass", "fail", "fail"]}, prior, max_reps=5)
        self.assertEqual(len(runs[0].units), 3)

    def test_counts_the_ceiling_from_planned_units(self):
        # 2 passes and 1 harness death against 0.87 with a ceiling of 5: two
        # more units remain under the ceiling, and even 4/4 cannot clear
        # alpha, so nothing more is launched. Counting the budget from scored
        # units alone would have credited the dead unit as a run to come.
        prior = _prior(FREE_CASE, 783, 900)
        self.assertFalse(stats.can_still_decide(2, 2, 4, prior.passes, prior.n))
        runs = self._run({FREE_CASE: ["pass", "missing", "pass"]}, prior, max_reps=5)
        self.assertEqual(len(runs[0].units), 3)

    def test_keeps_going_while_a_decision_is_reachable(self):
        prior = _prior(FREE_CASE, 459, 834)
        runs = self._run({FREE_CASE: ["pass"]}, prior, max_reps=12)
        n = len(runs[0].units)
        self.assertGreater(n, 3)
        self.assertLess(n, 12)
        self.assertEqual(report.decide(n, 0, prior, 0.05).verdict, report.DECISION_BETTER)
        self.assertNotEqual(report.decide(n - 1, 0, prior, 0.05).verdict, report.DECISION_BETTER)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, execution.RUN_COMMAND_ENV: FAKE}

    def tearDown(self):
        self.tmp.cleanup()

    def _cli(self, *args: str, env=None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*RUNNER_MODULE, *args], capture_output=True, text=True,
            env={**self.env, **(env or {})}, cwd=str(BENCH), check=False,
        )

    def test_plan_runs_offline(self):
        proc = self._cli("plan", FREE_CASE, "--baseline", "none")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(FREE_CASE, proc.stdout)
        as_json = self._cli("plan", FREE_CASE, "--baseline", "none", "--json")
        self.assertEqual(json.loads(as_json.stdout)[0]["case_id"], FREE_CASE)

    def test_a_bad_selector_exits_usage(self):
        self.assertEqual(self._cli("plan", "nope-*", "--baseline", "none").returncode, runner.EXIT_USAGE)

    def test_until_decided_refuses_to_start_without_a_baseline(self):
        proc = self._cli("run", FREE_CASE, "--baseline", "none", "--until-decided", "--out", self.tmp.name)
        self.assertEqual(proc.returncode, runner.EXIT_USAGE)
        self.assertIn("--until-decided needs a baseline", proc.stderr)

    def test_until_decided_runs_and_compare_reads_two_run_sets(self):
        # A baseline run set of 0/3, then an adaptive run against it that
        # passes: three passes against 0/3 are not yet significant, six are.
        base = Path(self.tmp.name) / "base"
        env = {"FAKE_OUTCOMES": json.dumps({FREE_CASE: ["fail"]})}
        proc = self._cli("run", FREE_CASE, "--baseline", "none", "--out", str(base), "--stagger", "0", env=env)
        self.assertEqual(proc.returncode, runner.EXIT_OK, proc.stderr)
        cand = Path(self.tmp.name) / "cand"
        env = {"FAKE_OUTCOMES": json.dumps({FREE_CASE: ["pass"]})}
        proc = self._cli(
            "run", FREE_CASE, "--baseline", str(base), "--until-decided", "--max-reps", "12",
            "--out", str(cand), "--stagger", "0", env=env,
        )
        self.assertEqual(proc.returncode, runner.EXIT_OK, proc.stderr)
        summary = json.loads((cand / report.SUMMARY_JSON).read_text())
        self.assertGreater(summary["cases"][0]["passes"], 3)
        self.assertEqual(summary["cases"][0]["decision"], report.DECISION_BETTER)
        compare = self._cli("compare", str(cand), str(base))
        self.assertEqual(compare.returncode, 0, compare.stderr)
        self.assertIn(report.DECISION_BETTER, compare.stdout)
        self.assertEqual(self._cli("compare", str(cand), str(Path(self.tmp.name) / "nope")).returncode, runner.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
