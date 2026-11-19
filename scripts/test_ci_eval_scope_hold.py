"""The per-case cluster hold in hack/ci-eval-pr.sh, against its real text.

A case that declares `scope_hold:` runs alone, after every other unit, between
a hold on the PlatformAgent and its restore. The shell functions are lifted out
of the script and run with the helper stubbed, as test_ci_eval_scope_exclude.py
does; bench/tests/test_scope_hold.py covers the helper itself.
"""

import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

from test_ci_eval_fanout import lifted, lifted_constant

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "bench" / "kube_agents_bench" / "scope_hold.py"

ENV = textwrap.dedent(
    f"""\
    SCRIPT_DIR="{REPO_ROOT / "hack"}"
    PROJECT_ID=pool-1
    AGENT_CLUSTER_CONTEXT=gke_pool-1_us-central1_platform-agent-host
    TARGET_NAMESPACE=kubeagents-system
    AGENT_SERVICE_NAME=platform-agent
    """
)

FUNCTIONS = ["scope_exclude_target", "scope_hold_apply", "scope_hold_restore"]


def run_bash(body: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", "set -uo pipefail\n" + body], cwd=cwd, capture_output=True, text=True, check=False)


def helper_stub(rc: int) -> str:
    """scope_hold_helper that logs its arguments to ./calls and exits `rc`."""
    return f'scope_hold_helper() {{ printf "%s\\n" "$*" >> calls; return {rc}; }}'


class UnitPhaseTest(unittest.TestCase):
    def phase(self, name: str) -> str:
        body = "\n".join(
            [
                lifted("unit_phase"),
                "SCOPE_EXCLUDE_NAMES=scoped",
                "SCOPE_HOLD_NAMES=held,held-writer",
                "INJECT_LANE_REQUESTING=writer,held-writer",
                "STOPPED_ASK_CASES=",
                "WRITES_ALONE_CASES=",
                f"unit_phase {name}",
            ]
        )
        return run_bash(body, ".").stdout.strip()

    def test_a_holding_case_is_phase_2_even_when_it_writes(self):
        self.assertEqual(self.phase("held"), "2")
        self.assertEqual(self.phase("held-writer"), "2")
        self.assertEqual(self.phase("scoped"), "2")
        self.assertEqual(self.phase("writer"), "1")
        self.assertEqual(self.phase("hel"), "0")


class ApplyRestoreTest(unittest.TestCase):
    def run_functions(self, rc: int, call: str) -> tuple[subprocess.CompletedProcess, list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            body = "\n".join(
                [ENV, lifted_constant("EVAL_SCOPE_HOLD_EXIT_UNSUPPORTED"), *map(lifted, FUNCTIONS), helper_stub(rc), call]
            )
            result = run_bash(body, tmp)
            calls = pathlib.Path(tmp) / "calls"
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_a_hold_in_effect_runs_the_case(self):
        result, calls = self.run_functions(0, "scope_hold_apply case-x '{\"all\":true,\"reason\":\"a b\"}'; echo rc=$?")
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertEqual(calls, ['apply --hold {"all":true,"reason":"a b"}'])

    def test_an_install_without_the_field_runs_the_case_unheld(self):
        result, _ = self.run_functions(3, "scope_hold_apply case-x '{}'; echo rc=$?")
        self.assertIn("rc=0", result.stdout, result.stderr)
        self.assertIn("has no spec.scope.hold", result.stdout)

    def test_a_hold_that_did_not_take_effect_stops_the_case(self):
        result, _ = self.run_functions(1, "scope_hold_apply case-x '{}'; echo rc=$?")
        self.assertIn("rc=1", result.stdout)
        self.assertIn("did not take effect", result.stderr)

    def test_a_context_that_does_not_name_the_project_is_not_held(self):
        result, calls = self.run_functions(0, "AGENT_CLUSTER_CONTEXT=gke_other_us-central1_host; scope_hold_apply case-x '{}'; echo rc=$?")
        self.assertIn("rc=1", result.stdout)
        self.assertEqual(calls, [])

    def test_the_lease_restore_waits_and_the_others_do_not(self):
        _, calls = self.run_functions(0, "scope_hold_restore lease wait; scope_hold_restore exit; scope_hold_restore case-x")
        self.assertEqual(calls, ["restore --wait", "restore", "restore"])

    def test_a_failed_restore_is_reported(self):
        result, _ = self.run_functions(1, "scope_hold_restore lease wait; echo rc=$?")
        self.assertIn("rc=1", result.stdout)
        self.assertIn("next lease's preflight tries again", result.stderr)


class CasesByPathTest(unittest.TestCase):
    def test_the_reader_runs_by_path_in_isolated_mode(self):
        # As the fan-out runs it: by path under -I, from a grader tree that may
        # not have the module.
        with tempfile.TemporaryDirectory() as tmp:
            task = pathlib.Path(tmp) / "held-case" / "task.yaml"
            task.parent.mkdir()
            task.write_text("id: held-case\nscope_hold: {agent_cluster: true, reason: paused now}\n")
            result = subprocess.run(
                [sys.executable, "-I", str(HELPER), "cases", str(task)], cwd=tmp, capture_output=True, text=True, check=False
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'held-case {"agent_cluster":true,"reason":"paused now"}\n')


if __name__ == "__main__":
    unittest.main()
