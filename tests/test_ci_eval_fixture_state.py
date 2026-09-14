"""The fixture-state pass in hack/ci-eval-pr.sh (section 2d) and the fan-out skip.

After the seeded-a heal (2c) the runner asks hack/fleet-fixture-state.py whether
every published fixture role is in its designed state, waiting for a fixture
that has just been rescheduled to converge (#1544). A role still drifted at the
deadline leaves a ``<role>.drift`` file, and the fan-out does not launch the
cases that name it. These tests pin, against the REAL text lifted out of the
script with the state script stubbed:

* the block runs the script with the default wait, and the environment can
  shorten or lengthen it;
* it is skipped when no fleet directory was written, and when disabled;
* a script that exits non-zero warns and never kills the run;
* ``task_fixtures`` reads the block-list and inline forms of ``fixtures:`` and
  prints nothing for an absent key or an empty list;
* ``unit_fixture_drift`` joins the drift files for the roles a task names and
  prints nothing when none drifted or the runner never ran.
"""

import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_CI_EVAL = _REPO / "hack" / "ci-eval-pr.sh"
_STATE_START = "# ─── 2d. Assert the planted fixtures are in their designed state"
_STATE_END = "# 3. Agent & Harness Configuration"
_CRASHLOOP_TASK = _REPO / "bench" / "tasks" / "cluster-agent-crashloop-debug" / "task.yaml"


def _state_block() -> str:
    text = _CI_EVAL.read_text(encoding="utf-8")
    start = text.find(_STATE_START)
    assert start != -1, f"{_STATE_START!r} not found in hack/ci-eval-pr.sh"
    end = text.find(_STATE_END, start)
    assert end != -1, f"{_STATE_END!r} not found after the state pass"
    return text[start:end]


def _lifted(name: str) -> str:
    """A top-level shell function as written, lifted from the script."""
    src = _CI_EVAL.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$", src, re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found in hack/ci-eval-pr.sh"
    return match.group(0)


class StatePassTest(unittest.TestCase):
    def _run(self, *, exit_code=0, env_extra=None, fleet_dir=True):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            calls = tmp / "calls.log"
            stub = tmp / "fleet-fixture-state.py"
            # The block runs the script through python3, so the stub is Python.
            stub.write_text(
                "import sys\n"
                f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                f"sys.exit({exit_code})\n",
                encoding="utf-8",
            )
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            env = {**os.environ, "SCRIPT_DIR": str(tmp), "SECONDS": "0"}
            env.pop("BENCH_FLEET_KUBECONFIG_DIR", None)
            env.pop("FLEET_FIXTURE_STATE_WAIT_SECONDS", None)
            env.pop("FLEET_ASSERT_FIXTURE_STATE", None)
            if fleet_dir:
                env["BENCH_FLEET_KUBECONFIG_DIR"] = str(tmp / "fleet")
            env.update(env_extra or {})
            proc = subprocess.run(
                ["bash", "-c", "set -euo pipefail\nSTEP_START=0\n" + _state_block()],
                env=env, capture_output=True, text=True, check=False,
            )
            log = calls.read_text(encoding="utf-8") if calls.exists() else ""
            return proc, log

    def test_the_pass_runs_with_the_default_wait(self):
        proc, log = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.strip(), "--wait 600")
        self.assertIn("Seeded-fleet fixture state finished", proc.stdout)

    def test_the_wait_is_overridable(self):
        proc, log = self._run(env_extra={"FLEET_FIXTURE_STATE_WAIT_SECONDS": "30"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.strip(), "--wait 30")

    def test_no_fleet_directory_means_no_pass(self):
        proc, log = self._run(fleet_dir=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log, "")

    def test_the_escape_hatch_issues_no_call(self):
        proc, log = self._run(env_extra={"FLEET_ASSERT_FIXTURE_STATE": "0"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log, "")

    def test_a_failed_script_warns_and_never_kills_the_run(self):
        proc, log = self._run(exit_code=1)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(log.strip(), "--wait 600")
        self.assertIn("could not run", proc.stderr)


class TaskFixturesTest(unittest.TestCase):
    def _fixtures(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(body)
            path = fh.name
        try:
            proc = subprocess.run(
                ["bash", "-c", f'set -euo pipefail\n{_lifted("task_fixtures")}\ntask_fixtures "{path}"'],
                capture_output=True, text=True, check=False,
            )
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_a_block_list_is_read(self):
        self.assertEqual(
            self._fixtures("id: x\nfixtures:\n  - crashloop-workload\n  # a comment\n  - idle-nodepool\nowner: me\n"),
            "crashloop-workload idle-nodepool",
        )

    def test_an_inline_list_is_read(self):
        self.assertEqual(self._fixtures("fixtures: [crashloop-workload, 'idle-nodepool']\n"), "crashloop-workload idle-nodepool")

    def test_an_absent_key_or_an_empty_list_prints_nothing(self):
        self.assertEqual(self._fixtures("id: x\nowner: me\n"), "")
        self.assertEqual(self._fixtures("fixtures: []\n"), "")

    def test_a_mention_inside_a_prompt_block_is_not_the_key(self):
        self.assertEqual(self._fixtures("prompt: >-\n  the fixtures:\n  - are not here\n"), "")

    def test_the_real_crashloop_case_names_its_role(self):
        self.assertEqual(self._fixtures(_CRASHLOOP_TASK.read_text(encoding="utf-8")), "crashloop-workload")


class UnitFixtureDriftTest(unittest.TestCase):
    def _drift(self, task_body: str, drift: dict | None, *, fleet_dir=True) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            task = tmp / "task.yaml"
            task.write_text(task_body, encoding="utf-8")
            fleet = tmp / "fleet"
            fleet.mkdir()
            for role, text in (drift or {}).items():
                (fleet / f"{role}.drift").write_text(text, encoding="utf-8")
            env = {**os.environ}
            env.pop("BENCH_FLEET_KUBECONFIG_DIR", None)
            if fleet_dir:
                env["BENCH_FLEET_KUBECONFIG_DIR"] = str(fleet)
            proc = subprocess.run(
                [
                    "bash", "-c",
                    (
                        f'set -euo pipefail\n{_lifted("task_fixtures")}\n{_lifted("unit_fixture_drift")}\n'
                        f'unit_fixture_drift "{task}"'
                    ),
                ],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout

    def test_a_drifted_role_the_task_names_is_reported(self):
        out = self._drift(
            "fixtures:\n  - crashloop-workload\n  - rbac-overgrant\n",
            {"crashloop-workload": "pod?app=payments-api restartCount any_ge 1: observed 0\nsecond line\n"},
        )
        self.assertEqual(out, "crashloop-workload: pod?app=payments-api restartCount any_ge 1: observed 0 second line")

    def test_two_drifted_roles_are_joined(self):
        out = self._drift("fixtures: [crashloop-workload, rbac-overgrant]\n", {"crashloop-workload": "a\n", "rbac-overgrant": "b\n"})
        self.assertEqual(out, "crashloop-workload: a; rbac-overgrant: b")

    def test_a_drifted_role_the_task_does_not_name_is_ignored(self):
        self.assertEqual(self._drift("fixtures:\n  - rbac-overgrant\n", {"crashloop-workload": "a\n"}), "")

    def test_no_drift_and_no_fixtures_print_nothing(self):
        self.assertEqual(self._drift("fixtures:\n  - crashloop-workload\n", None), "")
        self.assertEqual(self._drift("id: x\n", {"crashloop-workload": "a\n"}), "")

    def test_no_fleet_directory_prints_nothing(self):
        self.assertEqual(self._drift("fixtures:\n  - crashloop-workload\n", {"crashloop-workload": "a\n"}, fleet_dir=False), "")


if __name__ == "__main__":
    unittest.main()
