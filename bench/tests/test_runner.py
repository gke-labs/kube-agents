"""``bench-run run`` end to end against a scripted ``devops-bench``.

No agent, no judge, no cluster: ``BENCH_RUN_COMMAND`` points the runner at
``tests/fake_devops_bench.py``, which copies the captured run fixtures into
place, so what is under test is everything the runner adds -- scheduling and
locks, run-directory discovery, in-process grading, the interrupt and abort
paths and the summaries.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from kube_agents_bench import execution, report, runner, selection
from kube_agents_bench import priors as priors_mod

TESTS = Path(__file__).parent
FAKE = f"{sys.executable} {TESTS / 'fake_devops_bench.py'}"
ROOT = selection.repo_root(TESTS)
BENCH = ROOT / "bench"
CI_SCRIPT = ROOT / "hack" / "ci-eval-pr.sh"

# A noop-deployer case with no fixtures and no shared-artifact check: it runs
# anywhere.
FREE_CASE = "agent-kanban-smoke"
# A noop case that reads the seeded fleet.
FLEET_CASE = "cost-idle-pool-probe"
# A case that provisions a tofu stack.
STACK_CASE = "gpu-stress-test-diagnosis"
# A case whose checks read GitHub.
LEDGER_CASE = "compliance-rbac-overgrant"

RUNNER_MODULE = [sys.executable, "-m", "kube_agents_bench.runner"]


def _prior(case_id: str, passes: int, n: int, med: float = 100.0) -> priors_mod.Prior:
    return priors_mod.Prior(case_id, passes, n, med, "test")


def _events(trace: Path) -> list[list[str]]:
    return [line.split() for line in trace.read_text().splitlines()]


class PresubmitParityTest(unittest.TestCase):
    def test_audit_ceiling_list_matches_the_presubmit_script(self):
        # unit_delegation_timeout() in hack/ci-eval-pr.sh names the cases
        # that get 3000s; the runner's AUDIT_CASES must be the same set.
        body = CI_SCRIPT.read_text(encoding="utf-8")
        start = body.index("unit_delegation_timeout() {")
        end = body.index("\n}", start)
        names: set[str] = set()
        for line in body[start:end].splitlines():
            m = re.match(r"\s*([a-z0-9|\s-]+)\)\s*echo\s+(\d+)", line)
            if m and int(m.group(2)) == execution.AUDIT_DELEGATION_TIMEOUT_S:
                names |= {n.strip() for n in m.group(1).split("|")}
        self.assertEqual(names, set(execution.AUDIT_CASES))
        m = re.search(r'^export AGENT_DELEGATION_TIMEOUT="(\d+)"', body, re.M)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), execution.PRESUBMIT_DELEGATION_TIMEOUT_S)

    def test_delegation_timeout_per_unit(self):
        self.assertEqual(execution.delegation_timeout_s(FREE_CASE, {}), "2700")
        self.assertEqual(execution.delegation_timeout_s(LEDGER_CASE, {}), "3000")
        self.assertEqual(execution.delegation_timeout_s(LEDGER_CASE, {"AGENT_DELEGATION_TIMEOUT": "60"}), "60")


class SkipTest(unittest.TestCase):
    def test_reasons_name_what_is_missing(self):
        by_id = {c.case_id: c for c in selection.select_cases([FREE_CASE, FLEET_CASE, STACK_CASE, LEDGER_CASE], root=ROOT)}
        self.assertIsNone(execution.skip_reason(by_id[FREE_CASE], {}, False))
        self.assertIn("BENCH_FLEET_KUBECONFIG_DIR", execution.skip_reason(by_id[FLEET_CASE], {}, False))
        self.assertIsNone(execution.skip_reason(by_id[FLEET_CASE], {"BENCH_FLEET_KUBECONFIG_DIR": "/k"}, False))
        self.assertIn("--include-infra", execution.skip_reason(by_id[STACK_CASE], {}, False))
        self.assertIn("PROJECT_ID", execution.skip_reason(by_id[STACK_CASE], {}, True))
        # The ledger case reads the fleet too; with the fleet reachable, the
        # token is what it still lacks.
        self.assertIn("BENCH_GITHUB_TOKEN", execution.skip_reason(by_id[LEDGER_CASE], {"BENCH_FLEET_KUBECONFIG_DIR": "/k"}, False))


class DecisionTest(unittest.TestCase):
    def test_no_prior_is_no_baseline(self):
        self.assertEqual(report.decide(3, 0, None, 0.05).verdict, report.DECISION_NO_BASELINE)

    def test_three_passes_on_a_silent_case_are_better(self):
        decision = report.decide(3, 0, _prior("x", 0, 692), 0.05)
        self.assertEqual(decision.verdict, report.DECISION_BETTER)
        self.assertLess(decision.p_value, 0.05)

    def test_three_passes_on_a_reliable_case_are_undecided(self):
        self.assertEqual(report.decide(3, 0, _prior("x", 783, 900), 0.05).verdict, report.DECISION_UNDECIDED)

    def test_all_failures_on_a_reliable_case_are_worse(self):
        self.assertEqual(report.decide(0, 3, _prior("x", 890, 900), 0.05).verdict, report.DECISION_WORSE)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "runset"
        self.trace = Path(self.tmp.name) / "trace"
        self.env = {**os.environ, execution.RUN_COMMAND_ENV: FAKE, "FAKE_TRACE": str(self.trace)}
        self.cases = selection.select_cases([FREE_CASE], root=ROOT)
        self.lines: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def _options(self, **overrides) -> execution.RunOptions:
        base = dict(reps=3, parallel=4, stagger_s=0.0, out_dir=self.out, include_infra=False)
        base.update(overrides)
        return execution.RunOptions(**base)

    def _run(self, outcomes: dict, options: execution.RunOptions, priors=None, cases=None, env=None, **kwargs):
        env = {**self.env, **(env or {}), "FAKE_OUTCOMES": json.dumps(outcomes)}
        return execution.run_cases(
            cases or self.cases, priors or {}, options, env=env, bench=BENCH, log=self.lines.append, **kwargs
        )

    def test_three_reps_are_graded_the_way_the_gate_grades(self):
        runs = self._run({FREE_CASE: ["pass", "fail", "missing"]}, self._options())
        run = runs[0]
        self.assertEqual([u.result.outcome for u in run.units], ["pass", "fail", "blocked"])
        self.assertEqual((run.passes, run.fails), (1, 1))
        self.assertTrue((self.out / FREE_CASE / "rep1.log").is_file())
        self.assertTrue(run.units[0].run_dir and (run.units[0].run_dir / "results.json").is_file())
        self.assertIsNone(run.units[2].run_dir)
        self.assertEqual(run.units[1].failed_checks, ["report-states-the-probe-title"])

    def test_units_get_their_own_port_and_the_presubmit_delegation_ceiling(self):
        self._run({}, self._options(parallel=3))
        starts = [e for e in _events(self.trace) if e[0] == "start"]
        self.assertEqual(len({e[4] for e in starts}), 3)
        self.assertTrue(all(e[5] == f"delegation={execution.PRESUBMIT_DELEGATION_TIMEOUT_S}" for e in starts))

    def test_repetitions_of_one_case_run_in_series_by_default(self):
        self._run({}, self._options(parallel=3), env={"FAKE_SLEEP_S": "0.3"})
        events = _events(self.trace)
        starts = sorted(float(e[3]) for e in events if e[0] == "start")
        ends = sorted(float(e[3]) for e in events if e[0] == "end")
        self.assertGreaterEqual(starts[1], ends[0])
        self.assertGreaterEqual(starts[2], ends[1])

    def test_overlap_reps_lets_a_free_case_overlap(self):
        self._run({}, self._options(parallel=3, overlap_reps=True), env={"FAKE_SLEEP_S": "0.4"})
        events = _events(self.trace)
        starts = sorted(float(e[3]) for e in events if e[0] == "start")
        ends = sorted(float(e[3]) for e in events if e[0] == "end")
        self.assertLess(starts[2], ends[0])

    def test_overlap_reps_never_overlaps_an_exclusive_case(self):
        forced = [dataclasses.replace(self.cases[0], exclusive=True)]
        self._run({}, self._options(parallel=3, overlap_reps=True), cases=forced, env={"FAKE_SLEEP_S": "0.3"})
        events = _events(self.trace)
        starts = sorted(float(e[3]) for e in events if e[0] == "start")
        ends = sorted(float(e[3]) for e in events if e[0] == "end")
        self.assertGreaterEqual(starts[1], ends[0])

    def test_stack_cases_hold_the_infra_lock(self):
        free = self.cases[0]
        other = selection.select_cases([FLEET_CASE], root=ROOT)[0]
        stack_a = dataclasses.replace(other, has_stack=True, exclusive=True, reads_fleet=False)
        stack_b = dataclasses.replace(free, has_stack=True, exclusive=True)
        env = {"FAKE_SLEEP_S": "0.3", "PROJECT_ID": "p", "CLUSTER_NAME": "c"}
        self._run({}, self._options(reps=1, parallel=4, include_infra=True), cases=[stack_a, stack_b], env=env)
        by_case: dict[str, dict[str, float]] = {}
        for e in _events(self.trace):
            by_case.setdefault(e[1], {})[e[0]] = float(e[3])
        a, b = by_case[FLEET_CASE], by_case[FREE_CASE]
        self.assertTrue(a["start"] >= b["end"] or b["start"] >= a["end"])

    def test_a_unit_past_its_timeout_is_interrupted_and_graded(self):
        started = time.time()
        runs = self._run({}, self._options(reps=1, unit_timeout_s=0.5), env={"FAKE_SLEEP_S": "30"})
        unit = runs[0].units[0]
        self.assertLess(time.time() - started, execution.INTERRUPT_GRACE_S + 5)
        # The stand-in dies on SIGINT before writing anything: a harness death.
        self.assertEqual(unit.result.outcome, "blocked")
        self.assertIn("sent SIGINT", unit.log.read_text())

    def test_cases_that_cannot_run_here_are_skipped_with_a_reason(self):
        cases = selection.select_cases([FREE_CASE, FLEET_CASE, STACK_CASE], root=ROOT)
        runs = self._run({}, self._options(reps=1), cases=cases)
        by_id = {r.case.case_id: r for r in runs}
        self.assertIsNone(by_id[FREE_CASE].skipped)
        self.assertIn("BENCH_FLEET_KUBECONFIG_DIR", by_id[FLEET_CASE].skipped)
        self.assertIn("--include-infra", by_id[STACK_CASE].skipped)
        self.assertEqual(by_id[FLEET_CASE].units, [])

    def test_continue_case_is_asked_once_the_fixed_count_is_in(self):
        asked: list[int] = []

        def more(run: execution.CaseRun, planned: int) -> bool:
            asked.append(planned)
            return planned < 5

        runs = self._run({}, self._options(reps=2), continue_case=more)
        self.assertEqual(len(runs[0].units), 5)
        self.assertEqual(asked, [2, 3, 4, 5])

    def test_summary_round_trips(self):
        prior = _prior(FREE_CASE, 783, 900)
        runs = self._run({FREE_CASE: ["pass", "fail", "pass"]}, self._options(), priors={FREE_CASE: prior})
        summary = report.summarise(runs, {FREE_CASE: prior}, alpha=0.05, root=ROOT, baseline="test", out_dir=self.out)
        case = summary["cases"][0]
        self.assertEqual((case["passes"], case["fails"]), (2, 1))
        self.assertEqual(case["failed_checks"], {"report-states-the-probe-title": 1})
        self.assertEqual(case["baseline"]["n"], 900)
        text = report.render_summary(summary)
        self.assertIn("2/3", text)
        self.assertIn("run directories", text)
        # The summary is itself a baseline for the next run set.
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / report.SUMMARY_JSON).write_text(json.dumps(summary))
        loaded = priors_mod.load_priors(str(self.out))
        self.assertEqual((loaded[FREE_CASE].passes, loaded[FREE_CASE].n), (2, 3))


class TokenTest(unittest.TestCase):
    def test_kubectl_command_names_the_secret_key_and_context(self):
        cmd = execution.token_command({"AGENT_CLUSTER_CONTEXT": "ctx"})
        self.assertEqual(cmd[:3], ["kubectl", "--context", "ctx"])
        self.assertIn("platform-agent-secrets", cmd)
        self.assertIn("kubeagents-system", cmd)
        self.assertTrue(cmd[-1].startswith("go-template=") and "API_SERVER_KEY" in cmd[-1])
        self.assertNotIn("--context", execution.token_command({}))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {**os.environ, execution.RUN_COMMAND_ENV: FAKE}

    def tearDown(self):
        self.tmp.cleanup()

    def _cli(self, *args: str, env=None, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*RUNNER_MODULE, *args], capture_output=True, text=True,
            env={**self.env, **(env or {})}, cwd=str(BENCH), check=False, **kwargs,
        )

    def test_dry_run_prints_the_units_and_preflight(self):
        dry = self._cli("run", FREE_CASE, "--baseline", "none", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f"{FREE_CASE}: 3 reps", dry.stdout)

    def test_a_bad_selector_exits_usage(self):
        self.assertEqual(self._cli("run", "nope-*", "--baseline", "none", "--dry-run").returncode, runner.EXIT_USAGE)

    def test_run_writes_a_summary_that_summarize_reads_and_the_next_run_compares_against(self):
        out = Path(self.tmp.name) / "a"
        env = {"FAKE_OUTCOMES": json.dumps({FREE_CASE: ["pass", "fail", "pass"]})}
        proc = self._cli("run", FREE_CASE, "--baseline", "none", "--out", str(out), "--stagger", "0", env=env)
        self.assertEqual(proc.returncode, runner.EXIT_OK, proc.stderr)
        self.assertTrue((out / report.SUMMARY_JSON).is_file())
        self.assertIn("2/3", proc.stdout)
        summarize = self._cli("summarize", str(out))
        self.assertEqual(summarize.returncode, 0, summarize.stderr)
        self.assertIn("2/3", summarize.stdout)
        again = self._cli(
            "run", FREE_CASE, "--baseline", str(out), "--out", str(Path(self.tmp.name) / "b"), "--stagger", "0", env=env
        )
        self.assertEqual(again.returncode, runner.EXIT_OK, again.stderr)
        self.assertIn("file", again.stdout)
        self.assertIn(report.DECISION_UNDECIDED, again.stdout)

    def test_a_relative_out_from_another_directory_still_grades(self):
        env = {"FAKE_OUTCOMES": json.dumps({FREE_CASE: ["pass"]})}
        proc = subprocess.run(
            [*RUNNER_MODULE, "run", FREE_CASE, "--baseline", "none", "--reps", "1", "--stagger", "0",
             "--out", "relout"],
            capture_output=True, text=True, env={**self.env, **env}, cwd=self.tmp.name, check=False,
        )
        self.assertEqual(proc.returncode, runner.EXIT_OK, proc.stderr)
        self.assertIn(" 1/1 ", proc.stdout)
        self.assertTrue((Path(self.tmp.name) / "relout" / report.SUMMARY_JSON).is_file())

    def test_summarize_refuses_a_bad_path_cleanly(self):
        proc = self._cli("summarize", str(Path(self.tmp.name) / "nope"))
        self.assertEqual(proc.returncode, runner.EXIT_USAGE)
        self.assertIn("bench-run:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_an_unreadable_baseline_is_recorded_as_none_in_the_summary(self):
        out = Path(self.tmp.name) / "nb"
        env = {"FAKE_OUTCOMES": json.dumps({FREE_CASE: ["pass"]})}
        proc = self._cli(
            "run", FREE_CASE, "--baseline", "/no/such/summary.json", "--reps", "1", "--stagger", "0",
            "--out", str(out), env=env,
        )
        self.assertEqual(proc.returncode, runner.EXIT_OK, proc.stderr)
        summary = json.loads((out / report.SUMMARY_JSON).read_text())
        self.assertTrue(summary["baseline"].startswith(priors_mod.BASELINE_NONE))
        self.assertIn("unreadable", summary["baseline"])

    def test_nothing_runnable_exits_nothing_ran(self):
        proc = self._cli("run", FLEET_CASE, "--baseline", "none", "--out", self.tmp.name)
        self.assertEqual(proc.returncode, runner.EXIT_NOTHING_RAN)
        self.assertIn("skip", proc.stdout)

    def test_interrupt_during_the_launch_stagger_starts_nothing(self):
        # SIGINT arrives while every unit is still sleeping its stagger: no
        # unit may start afterwards, and the runner exits promptly.
        out = Path(self.tmp.name) / "stagger"
        trace = Path(self.tmp.name) / "trace"
        env = {"FAKE_SLEEP_S": "8", "FAKE_TRACE": str(trace), "FAKE_OUTCOMES": "{}"}
        proc = subprocess.Popen(
            [*RUNNER_MODULE, "run", FREE_CASE, "--baseline", "none", "--reps", "2", "--parallel", "2",
             "--overlap-reps", "--stagger", "3", "--out", str(out)],
            env={**self.env, **env}, cwd=str(BENCH), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(1.5)
        started = time.time()
        proc.send_signal(signal.SIGINT)
        stdout, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, runner.EXIT_INTERRUPTED, stdout)
        self.assertLess(time.time() - started, 6, stdout)
        self.assertFalse(trace.is_file() and any(e[0] == "start" for e in _events(trace)), stdout)
        self.assertTrue(json.loads((out / report.SUMMARY_JSON).read_text())["interrupted"])

    def test_a_unit_that_raises_aborts_the_run_with_a_summary(self):
        out = Path(self.tmp.name) / "abort"
        proc = self._cli(
            "run", FREE_CASE, "--baseline", "none", "--reps", "1", "--stagger", "0", "--out", str(out),
            env={execution.RUN_COMMAND_ENV: "/no/such/devops-bench"},
        )
        self.assertEqual(proc.returncode, runner.EXIT_ABORTED, proc.stdout + proc.stderr)
        self.assertIn("a unit raised", proc.stderr)
        summary = json.loads((out / report.SUMMARY_JSON).read_text())
        self.assertIn("FileNotFoundError", summary["aborted"])
        self.assertIn("ABORTED", proc.stdout)

    def test_interrupt_stops_the_queue_and_writes_a_partial_summary(self):
        out = Path(self.tmp.name) / "int"
        trace = Path(self.tmp.name) / "trace"
        env = {"FAKE_SLEEP_S": "2", "FAKE_TRACE": str(trace), "FAKE_OUTCOMES": "{}"}
        proc = subprocess.Popen(
            [*RUNNER_MODULE, "run", FREE_CASE, "--baseline", "none", "--reps", "6", "--parallel", "2",
             "--overlap-reps", "--stagger", "0", "--out", str(out)],
            env={**self.env, **env}, cwd=str(BENCH), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        # Let the first two units start, then interrupt.
        deadline = time.time() + 20
        while time.time() < deadline:
            if trace.is_file() and sum(1 for e in _events(trace) if e[0] == "start") >= 2:
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGINT)
        stdout, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, runner.EXIT_INTERRUPTED, stdout)
        starts = sum(1 for e in _events(trace) if e[0] == "start")
        self.assertLessEqual(starts, 3, stdout)
        summary = json.loads((out / report.SUMMARY_JSON).read_text())
        self.assertTrue(summary["interrupted"])
        self.assertIn("INTERRUPTED", stdout)


if __name__ == "__main__":
    unittest.main()
