"""The nightly tier switch is model-free shell, so it is testable here.

`hack/ci-eval-pr.sh` selects its task matrix with EVAL_TIER: unset or
"presubmit" runs exactly the TASKS array, "nightly" appends NIGHTLY_TASKS to
it, and anything else stops the job before it spends a cluster. Four
properties carry that contract, each exercised against the REAL text lifted
out of the script, in the same style as test_ci_eval_fanout.py:

  - dormancy: with EVAL_TIER unset the matrix is byte-for-byte the presubmit
    one, so every existing job is untouched by the tier existing;
  - the superset: EVAL_TIER=nightly is TASKS then NIGHTLY_TASKS, in order --
    presubmit cases run in the nightly identically, and the appended tail is
    what the nightly adds, nothing reordered and nothing dropped;
  - a typo'd tier fails loudly rather than silently running the wrong matrix;
  - the arrays are disjoint and every nightly entry resolves to a real case
    directory -- an entry in both would run one task's repetitions twice per
    nightly (double-counted by the gate and racing its own task lock's
    protections across two queue slots), and a path typo would grade MISSING
    every night while looking registered to the lint.

scripts/test_task_registration.py owns the registration half (a NIGHTLY_TASKS
entry counts as registered; the array is declared exactly once).
"""

import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
TASKS_DIR = REPO_ROOT / "bench" / "tasks"


def lifted_block(pattern: str) -> str:
    """A block of the script as written, lifted by regex."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(pattern, src, re.S | re.M)
    if match is None:  # pragma: no cover - a reshape should say so loudly
        raise AssertionError(f"pattern {pattern!r} not found in {SCRIPT}")
    return match.group(0)


def nightly_array() -> str:
    return lifted_block(r"^NIGHTLY_TASKS=\(\n.*?^\)$")


def tier_switch() -> str:
    return lifted_block(r'^EVAL_TIER="\$\{EVAL_TIER:-presubmit\}"\n.*?^esac$')


def run_bash(body: str, env_line: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -uo pipefail\n" + env_line + "\n" + body],
        capture_output=True,
        text=True,
        check=False,
    )


def matrix_for(env_line: str) -> subprocess.CompletedProcess:
    """TASKS after the real array text and the real tier switch run."""
    body = "\n".join(
        [
            'TASKS=("./tasks/presubmit-a/task.yaml" "./tasks/presubmit-b/task.yaml")',
            nightly_array(),
            tier_switch(),
            'printf "%s\\n" "${TASKS[@]}"',
        ]
    )
    return run_bash(body, env_line)


def matrix_lines(result: subprocess.CompletedProcess) -> list[str]:
    """The task entries out of stdout, past the switch's own status echo."""
    return [line for line in result.stdout.splitlines() if line.startswith("./tasks/")]


class TierSwitchTest(unittest.TestCase):
    def nightly_entries(self) -> list[str]:
        return re.findall(r'^\s*"(\./tasks/[A-Za-z0-9_-]+/task\.yaml)"', nightly_array(), re.M)

    def test_unset_tier_is_exactly_the_presubmit_matrix(self):
        result = matrix_for("unset EVAL_TIER")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            matrix_lines(result),
            ["./tasks/presubmit-a/task.yaml", "./tasks/presubmit-b/task.yaml"],
            "with EVAL_TIER unset the matrix must be untouched -- every "
            "existing job runs this configuration",
        )

    def test_presubmit_tier_is_exactly_the_presubmit_matrix(self):
        result = matrix_for("EVAL_TIER=presubmit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            matrix_lines(result),
            ["./tasks/presubmit-a/task.yaml", "./tasks/presubmit-b/task.yaml"],
        )

    def test_nightly_tier_appends_the_nightly_tail_in_order(self):
        result = matrix_for("EVAL_TIER=nightly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            matrix_lines(result),
            ["./tasks/presubmit-a/task.yaml", "./tasks/presubmit-b/task.yaml"]
            + self.nightly_entries(),
            "nightly must be presubmit-then-nightly, in declaration order: "
            "the gate's reporting order is the array's order",
        )

    def test_an_unknown_tier_fails_loudly(self):
        result = matrix_for("EVAL_TIER=nigthly")
        self.assertNotEqual(
            result.returncode,
            0,
            "a typo'd EVAL_TIER must stop the job, not silently run the "
            "presubmit matrix under a nightly's name",
        )
        self.assertIn("EVAL_TIER must be", result.stderr)
        self.assertIn("nigthly", result.stderr)

    def test_the_nightly_array_is_nonempty_active_entries(self):
        # A tier that appends nothing is a job that costs a Boskos lease to
        # rerun the presubmit at 08:00 UTC; if every nightly case graduates
        # or is demoted, delete the tier rather than leaving it vacuous.
        self.assertTrue(
            self.nightly_entries(),
            "NIGHTLY_TASKS parsed to no active entries",
        )


class ArrayHygieneTest(unittest.TestCase):
    def tasks_active_entries(self) -> list[str]:
        block = lifted_block(r"^TASKS=\(\n.*?^\)$")
        return re.findall(r'^\s*"(\./tasks/[A-Za-z0-9_-]+/task\.yaml)"', block, re.M)

    def test_the_arrays_are_disjoint(self):
        # An entry in both runs twice per nightly: six repetitions graded as
        # two three-repetition cases of the same name, with the second's
        # per-task lock directory colliding with the first's.
        nightly = re.findall(
            r'^\s*"(\./tasks/[A-Za-z0-9_-]+/task\.yaml)"', nightly_array(), re.M
        )
        overlap = sorted(set(self.tasks_active_entries()) & set(nightly))
        self.assertEqual(
            overlap,
            [],
            "\n\nThese cases are active in TASKS and listed in NIGHTLY_TASKS, "
            "so a nightly would run them twice:\n  " + "\n  ".join(overlap),
        )

    def test_every_nightly_entry_resolves_to_a_case_directory(self):
        # Registration keeps the lint green; only existence keeps the run
        # green -- a moved directory would grade MISSING every night.
        missing = [
            entry
            for entry in re.findall(
                r'^\s*"\./tasks/([A-Za-z0-9_-]+)/task\.yaml"', nightly_array(), re.M
            )
            if not (TASKS_DIR / entry / "task.yaml").is_file()
        ]
        self.assertEqual(
            missing,
            [],
            "\n\nThese NIGHTLY_TASKS entries name no bench/tasks/ directory:"
            "\n  " + "\n  ".join(missing),
        )

    def test_every_nightly_case_has_a_cost_hint_in_the_audit_band(self):
        # Not every one: only the audit-shaped four. unit_cost_hint's default
        # 200 fits the probe-shaped fifth; what must not happen is a 600-1300s
        # audit priced at 200, parking it at the queue's tail where a deadline
        # kill eats all three of its repetitions every night.
        src = SCRIPT.read_text(encoding="utf-8")
        hint_fn = re.search(r"^unit_cost_hint\(\) \{.*?^\}$", src, re.S | re.M)
        self.assertIsNotNone(hint_fn, "unit_cost_hint() not found")
        for audit in (
            "obtainability-planted-pdb",
            "stockout-pinned-pool",
            "upgrade-readiness-lagging-cluster",
            "consistency-drift-outlier",
        ):
            with self.subTest(audit=audit):
                self.assertIn(
                    audit,
                    hint_fn.group(0),
                    f"{audit} is audit-shaped (600-1300s measured) and must "
                    "carry an explicit unit_cost_hint entry, not the 200s "
                    "default",
                )


if __name__ == "__main__":
    unittest.main()
