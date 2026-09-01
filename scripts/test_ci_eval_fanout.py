"""The eval fan-out's scheduler is model-free shell, so it is testable here.

`hack/ci-eval-pr.sh` launches one background unit per (task, repetition) and
serializes the collisions with two mkdir mutexes. Three properties carry the
correctness of that scheme and each is exercised against the REAL text lifted
out of the script, in the same style as test_ci_eval_trap.py:

  - the lock deadline: a holder that died without releasing must convert into
    a loud per-unit failure, never a silent spin `wait` can outlast;
  - lock mutual exclusion: two contenders never hold one lock at once;
  - queue order: repetition-major, cost-descending within a repetition, so a
    lane is never parked on the task lock of a unit launched seconds earlier
    (the 2026-08-31 pool run paid two of four lanes for twelve minutes that
    way).

The run-directory recovery regex is pinned too: it is what replaced the
directory-set diff that could not tell concurrent siblings apart.
"""

import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"


def lifted(name: str) -> str:
    """A top-level shell function as written, lifted from the script."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$|^{name}\(\) \{{[^\n]*\}}$", src, re.S | re.M)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{name}() not found in {SCRIPT}")
    return match.group(0)


def run_bash(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -uo pipefail\n" + body],
        capture_output=True,
        text=True,
        check=False,
    )


class LockTest(unittest.TestCase):
    def test_a_dead_holders_lock_fails_the_contender_loudly(self):
        # Pre-create the lock dir and never release it: the acquire must give
        # up at its deadline with a diagnosis, not spin forever.
        body = "\n".join(
            [
                lifted("lock_acquire"),
                'd="$(mktemp -d)/lock"',
                'mkdir "$d"',
                'if lock_acquire "$d" 6; then echo ACQUIRED; else echo GAVE_UP; fi',
            ]
        )
        result = run_bash(body)
        self.assertIn("GAVE_UP", result.stdout)
        self.assertIn("holder likely died", result.stderr)

    def test_two_contenders_never_hold_the_lock_at_once(self):
        body = "\n".join(
            [
                lifted("lock_acquire"),
                lifted("lock_release"),
                'd="$(mktemp -d)"',
                "worker() {",
                '  lock_acquire "$d/lock" 60 || return 1',
                '  echo "ENTER $1" >> "$d/events"',
                "  sleep 1",
                '  echo "LEAVE $1" >> "$d/events"',
                '  lock_release "$d/lock"',
                "}",
                "worker a & worker b &",
                "wait",
                'cat "$d/events"',
            ]
        )
        result = run_bash(body)
        events = result.stdout.split()
        # Sequence must be ENTER x, LEAVE x, ENTER y, LEAVE y — never nested.
        self.assertEqual(events[0], "ENTER")
        self.assertEqual(events[2], "LEAVE")
        self.assertEqual(events[1], events[3], "a holder was preempted mid-critical-section")
        self.assertEqual(events[4], "ENTER")
        self.assertNotEqual(events[1], events[5])


class QueueOrderTest(unittest.TestCase):
    def queue(self, reps: int) -> list[tuple[int, int, int]]:
        src = SCRIPT.read_text(encoding="utf-8")
        match = re.search(r'^UNIT_QUEUE="\$\(\n.*?^\)"$', src, re.S | re.M)
        if match is None:  # pragma: no cover
            raise AssertionError(f"UNIT_QUEUE block not found in {SCRIPT}")
        body = "\n".join(
            [
                lifted("unit_cost_hint"),
                f"EVAL_REPETITIONS={reps}",
                # compliance carries a 700 hint, the probe 200: order within a
                # repetition must be cost-descending.
                'TASKS=("t/reliability-pdb-probe/task.yaml" "t/compliance-rbac-overgrant/task.yaml")',
                'TASK_NAMES=(reliability-pdb-probe compliance-rbac-overgrant)',
                match.group(0),
                'printf "%s\\n" "${UNIT_QUEUE}"',
            ]
        )
        result = run_bash(body)
        rows = []
        for line in result.stdout.splitlines():
            if line.strip():
                rep, cost, idx = line.split()
                rows.append((int(rep), int(cost), int(idx)))
        return rows

    def test_rep_major_then_cost_descending(self):
        rows = self.queue(reps=3)
        self.assertEqual(len(rows), 6)
        # All rep-1 units precede any rep-2 unit: a task's second repetition
        # must not launch while its first plausibly still runs.
        self.assertEqual([r for r, _, _ in rows], [1, 1, 2, 2, 3, 3])
        # Within a repetition, the expensive unit launches first.
        for pair in (rows[0:2], rows[2:4], rows[4:6]):
            self.assertGreaterEqual(pair[0][1], pair[1][1])


class RunDirRecoveryTest(unittest.TestCase):
    def test_the_results_line_regex_recovers_the_run_directory(self):
        src = SCRIPT.read_text(encoding="utf-8")
        match = re.search(r'dir="\$\(grep[^\n]*\)"', src)
        if match is None:  # pragma: no cover
            raise AssertionError(f"run-directory recovery pipeline not found in {SCRIPT}")
        body = "\n".join(
            [
                'log="$(mktemp)"',
                # The [TS ...] prefix and the absolute path are what the real
                # log carries; a crashed run carries neither.
                "cat > \"$log\" <<'EOF'",
                "[TS 1788137774.137] some other line",
                "[TS 1788137775.001] ran 1 task(s), 0 failed; results: /abs/bench/results/run_20260831_010203_000001/results.json",
                "EOF",
                match.group(0),
                'echo "DIR=${dir}"',
                ': > "$log"',
                match.group(0),
                'echo "EMPTY=[${dir}]"',
            ]
        )
        result = run_bash(body)
        self.assertIn("DIR=/abs/bench/results/run_20260831_010203_000001", result.stdout)
        self.assertIn("EMPTY=[]", result.stdout)


if __name__ == "__main__":
    unittest.main()
