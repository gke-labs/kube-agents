"""The roster loading and the nightly tier switch are model-free shell, so they are testable here.

`hack/ci-eval-pr.sh` builds its task matrix from three files under hack/eval/
(#1546): presubmit-cases.txt into TASKS, nightly-cases.txt into
NIGHTLY_TASKS, blocking-roster.txt into BOOTSTRAP_ADMITTED's default. Then
EVAL_TIER selects the matrix: unset or "presubmit" runs exactly TASKS,
"nightly" appends NIGHTLY_TASKS, and anything else stops the job before it
spends a cluster. The properties below are exercised against the REAL text
lifted out of the script, run over the REAL files, in the same style as
test_ci_eval_fanout.py:

  - the shell and scripts/eval_rosters.py read the files to the same arrays,
    so the lints and the dashboard cannot disagree with the job about what
    runs and what blocks;
  - dormancy: with EVAL_TIER unset the matrix is byte-for-byte the presubmit
    file, so every existing job is untouched by the tier existing;
  - the superset: EVAL_TIER=nightly is the presubmit file then the nightly
    file, in order -- presubmit cases run in the nightly identically, and the
    appended tail is what the nightly adds, nothing reordered, nothing
    dropped;
  - a typo'd tier fails loudly rather than silently running the wrong matrix;
  - a missing file, a malformed line, a path with no case behind it, a
    nightly entry that is also a presubmit one, a roster name outside the
    presubmit, and an empty presubmit file each stop the job with a message
    naming the file, rather than running a wrong matrix and reporting green;
  - the arrays are disjoint and every entry resolves to a real case
    directory -- an entry in both would run one task's repetitions twice per
    nightly (double-counted by the gate and racing its own task lock across
    two queue slots), and a path typo would grade MISSING every night while
    looking registered to the lint.

scripts/test_task_registration.py owns the registration half (a nightly entry
counts as registered; the retired commented-out state is a finding);
scripts/test_eval_rosters.py pins the arrays to the sets the script carried
before the split.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import eval_rosters

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
HACK_DIR = REPO_ROOT / "hack"
BENCH_DIR = REPO_ROOT / "bench"
TASKS_DIR = BENCH_DIR / "tasks"


def lifted_block(pattern: str) -> str:
    """A block of the script as written, lifted by regex."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(pattern, src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a reshape should say so loudly
        raise AssertionError(f"pattern {pattern!r} not found in {SCRIPT}")
    return match.group(0)


def roster_constants() -> str:
    return lifted_block(r"^readonly EVAL_PRESUBMIT_CASES_FILE=[^\n]*\n(?:readonly EVAL_[A-Z_]+_FILE=[^\n]*\n)*")


def matrix_section() -> str:
    """Section 6 through the tier switch: the reader functions, both case
    arrays, the presubmit name list and EVAL_TIER's case statement."""
    return lifted_block(r"^# 6\. Task Matrix Execution Loop\n.*?^esac$")


def roster_block() -> str:
    """The blocking roster's read, subset check and export."""
    return lifted_block(r"^BLOCKING_ROSTER_ENTRIES=.*?^export BOOTSTRAP_ADMITTED=[^\n]*")


def run_bash(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


PRINT_ARRAYS = (
    'printf "TASK %s\\n" "${TASKS[@]}"\n'
    'printf "NIGHTLY %s\\n" "${NIGHTLY_TASKS[@]}"\n'
    'printf "ROSTER %s\\n" "${BOOTSTRAP_ADMITTED}"\n'
)


def load_matrix(env: dict | None = None, hack_dir: pathlib.Path = HACK_DIR) -> subprocess.CompletedProcess:
    """The real loading code over the files under `hack_dir`/eval, BENCH_DIR
    pinned to the real bench/ so entries resolve against real cases."""
    section = matrix_section().replace('BENCH_DIR="${SCRIPT_DIR}/../bench"', f'BENCH_DIR="{BENCH_DIR}"')
    body = "\n".join([f'SCRIPT_DIR="{hack_dir}"', roster_constants(), section, roster_block(), PRINT_ARRAYS])
    return run_bash(body, env)


def lines_tagged(result: subprocess.CompletedProcess, tag: str) -> list[str]:
    return [line[len(tag) + 1 :] for line in result.stdout.splitlines() if line.startswith(tag + " ")]


def presubmit_entries() -> list[str]:
    return [f"./tasks/{name}/task.yaml" for name in eval_rosters.presubmit_cases()]


def nightly_entries() -> list[str]:
    return [f"./tasks/{name}/task.yaml" for name in eval_rosters.nightly_cases()]


class ShellAndPythonAgreeTest(unittest.TestCase):
    def test_the_shell_builds_the_arrays_the_python_parse_reads(self):
        result = load_matrix({"EVAL_TIER": "presubmit"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())
        self.assertEqual(lines_tagged(result, "NIGHTLY"), nightly_entries())
        self.assertEqual(lines_tagged(result, "ROSTER"), [",".join(eval_rosters.blocking_roster())])

    def test_the_env_override_of_the_roster_still_wins(self):
        result = load_matrix({"BOOTSTRAP_ADMITTED": "x-probe,y-probe"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "ROSTER"), ["x-probe,y-probe"])


class TierSwitchTest(unittest.TestCase):
    def test_unset_tier_is_exactly_the_presubmit_matrix(self):
        result = load_matrix()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            lines_tagged(result, "TASK"),
            presubmit_entries(),
            "with EVAL_TIER unset the matrix must be the presubmit file -- every existing job runs this configuration",
        )

    def test_presubmit_tier_is_exactly_the_presubmit_matrix(self):
        result = load_matrix({"EVAL_TIER": "presubmit"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "TASK"), presubmit_entries())

    def test_nightly_tier_appends_the_nightly_tail_in_order(self):
        result = load_matrix({"EVAL_TIER": "nightly"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            lines_tagged(result, "TASK"),
            presubmit_entries() + nightly_entries(),
            "nightly must be presubmit-then-nightly, in file order: the gate's reporting order is the array's order",
        )
        self.assertIn(f"EVAL_TIER=nightly: {len(nightly_entries())} nightly-only task(s) join the matrix", result.stdout)

    def test_an_unknown_tier_fails_loudly(self):
        result = load_matrix({"EVAL_TIER": "nigthly"})
        self.assertNotEqual(result.returncode, 0, "a typo'd EVAL_TIER must stop the job, not silently run the presubmit matrix under a nightly's name")
        self.assertIn("EVAL_TIER must be", result.stderr)
        self.assertIn("nigthly", result.stderr)

    def test_the_nightly_file_is_nonempty(self):
        # A tier that appends nothing is a job that costs a Boskos lease to
        # rerun the presubmit at midnight; if every nightly case graduates
        # or is retired, delete the tier rather than leaving it vacuous.
        self.assertTrue(nightly_entries(), "hack/eval/nightly-cases.txt parsed to no entries")


class RefusalTest(unittest.TestCase):
    """Each way a roster file can be wrong stops the job, naming the file."""

    def scratch(self, mutate) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            hack = pathlib.Path(tmp) / "hack"
            shutil.copytree(HACK_DIR / "eval", hack / "eval")
            mutate(hack / "eval")
            return load_matrix({"EVAL_TIER": "nightly"}, hack_dir=hack)

    def assert_refused(self, result, *needles):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("TASK ", result.stdout, "the matrix must not be built past a bad file")
        for needle in needles:
            self.assertIn(needle, result.stderr)

    def test_a_missing_file_stops_the_job(self):
        result = self.scratch(lambda d: (d / "nightly-cases.txt").unlink())
        self.assert_refused(result, "nightly-cases.txt is missing", "#1546")

    def test_a_line_that_is_not_a_case_path_stops_the_job(self):
        result = self.scratch(lambda d: (d / "nightly-cases.txt").write_text("agent-kanban-smoke\n"))
        self.assert_refused(result, "nightly-cases.txt", "is not a ./tasks/<id>/task.yaml path")

    def test_a_path_with_no_case_behind_it_stops_the_job(self):
        result = self.scratch(lambda d: (d / "presubmit-cases.txt").write_text("./tasks/no-such-case/task.yaml\n"))
        self.assert_refused(result, "presubmit-cases.txt", "no-such-case", "names no case under bench/tasks/")

    def test_a_nightly_entry_that_is_also_presubmit_stops_the_job(self):
        result = self.scratch(lambda d: (d / "nightly-cases.txt").write_text("./tasks/agent-kanban-smoke/task.yaml\n"))
        self.assert_refused(result, "agent-kanban-smoke", "would run it twice")

    def test_a_roster_name_outside_the_presubmit_stops_the_job(self):
        result = self.scratch(lambda d: (d / "blocking-roster.txt").write_text("obtainability-planted-pdb\n"))
        self.assert_refused(result, "blocking-roster.txt", "obtainability-planted-pdb", "subset of the presubmit")

    def test_an_empty_presubmit_file_stops_the_job(self):
        result = self.scratch(lambda d: (d / "presubmit-cases.txt").write_text("# every line a comment\n\n"))
        self.assert_refused(result, "presubmit-cases.txt names no case")

    def test_an_empty_blocking_roster_stops_the_job(self):
        # An empty roster under EVAL_ADMISSION_MODE=roster disarms rung 4 for
        # every pull request while the job reports green; the explicit
        # BOOTSTRAP_ADMITTED override is the way to mean it.
        result = self.scratch(lambda d: (d / "blocking-roster.txt").write_text("# nobody blocks\n"))
        self.assert_refused(result, "blocking-roster.txt names no case", "BOOTSTRAP_ADMITTED")

    def test_comments_and_whitespace_are_ignored(self):
        def mutate(d):
            text = (d / "nightly-cases.txt").read_text()
            (d / "nightly-cases.txt").write_text("  # leading comment\n\n" + text.replace(
                "./tasks/obtainability-planted-pdb/task.yaml",
                "   ./tasks/obtainability-planted-pdb/task.yaml   # trailing note",
            ))

        result = self.scratch(mutate)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(lines_tagged(result, "NIGHTLY"), nightly_entries())


class ArrayHygieneTest(unittest.TestCase):
    def test_the_files_are_disjoint(self):
        # An entry in both runs twice per nightly: six repetitions graded as
        # two three-repetition cases of the same name, with the second's
        # per-task lock directory colliding with the first's.
        overlap = sorted(set(presubmit_entries()) & set(nightly_entries()))
        self.assertEqual(overlap, [], "\n\nThese cases are in both roster files, so a nightly would run them twice:\n  " + "\n  ".join(overlap))

    def test_every_entry_resolves_to_a_case_directory(self):
        # Registration keeps the lint green; only existence keeps the run
        # green -- a moved directory would grade MISSING every night.
        missing = [
            name
            for name in eval_rosters.presubmit_cases() + eval_rosters.nightly_cases()
            if not (TASKS_DIR / name / "task.yaml").is_file()
        ]
        self.assertEqual(missing, [], "\n\nThese roster entries name no bench/tasks/ directory:\n  " + "\n  ".join(missing))

    def test_no_entry_repeats_within_a_file(self):
        for label, names in (("presubmit", eval_rosters.presubmit_cases()), ("nightly", eval_rosters.nightly_cases())):
            with self.subTest(file=label):
                self.assertEqual(len(names), len(set(names)), f"duplicate entries in the {label} file")

    def test_every_audit_shaped_nightly_case_has_a_cost_hint_in_the_audit_band(self):
        # Not every one: the audit-shaped ones. unit_cost_hint's default 200
        # fits the probe-shaped cases; what must not happen is a 600-1300s
        # audit priced at 200, parking it at the queue's tail where a deadline
        # kill eats all three of its repetitions every night.
        hint_fn = lifted_block(r"^unit_cost_hint\(\) \{.*?^\}$")
        for audit in (
            "obtainability-planted-pdb",
            "stockout-pinned-pool",
            "upgrade-readiness-lagging-cluster",
            "consistency-drift-outlier",
            "fleet-cost-idle-pool",
        ):
            with self.subTest(audit=audit):
                result = run_bash(f"{hint_fn}\nunit_cost_hint {audit}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertGreaterEqual(
                    int(result.stdout.strip()),
                    600,
                    f"{audit} is audit-shaped and must carry an explicit unit_cost_hint in the 600-1300s band, not the 200s default",
                )


if __name__ == "__main__":
    unittest.main()
