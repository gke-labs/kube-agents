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

Since the per-case grading moved into the fan-out (#1491: a deadline-cut
night must still record every case that finished), a fourth property joins
them: the unit that brings a case's repetition count to EVAL_REPETITIONS --
and only that unit -- grades and records the case, and a repetition that gave
up on its lock leaves the case for the loop after the fan-out.
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


class DelegationCeilingTest(unittest.TestCase):
    """The full-audit units wait longer for their delegated worker (#1683).

    The nightly of 2026-09-16 cut three audits at the global 2700s ceiling
    after their ledgers were written. The ceiling is per unit, decided by
    name beside the cost hint, and exported inside the unit's own subshell.
    It stops at 3000s because the ledger read token minted before the bench
    lives one hour and the verifier reads GitHub with it last.
    """

    def ceiling(self, name: str, inherited: str | None) -> str:
        env = "" if inherited is None else f'export AGENT_DELEGATION_TIMEOUT="{inherited}"'
        body = "\n".join([lifted("unit_delegation_timeout"), env, f'unit_delegation_timeout "{name}"'])
        return run_bash(body).stdout.strip()

    def test_the_audit_units_get_3000s_inside_the_tokens_hour(self):
        for name in (
            "compliance-rbac-overgrant",
            "obtainability-planted-pdb",
            "stockout-pinned-pool",
            "upgrade-readiness-lagging-cluster",
            "consistency-drift-outlier",
            "fleet-cost-idle-pool",
        ):
            with self.subTest(unit=name):
                self.assertEqual(self.ceiling(name, "2700"), "3000")
                # The one-hour ledger token bounds it: leave at least 600s of
                # the hour for startup, the opening turn, settle and the read.
                self.assertLessEqual(int(self.ceiling(name, "2700")), 3600 - 600)

    def test_every_other_unit_inherits_the_global_ceiling(self):
        self.assertEqual(self.ceiling("capacity-pinned-pool-probe", "2700"), "2700")
        # And the harness's own default when nothing was exported at all.
        self.assertEqual(self.ceiling("capacity-pinned-pool-probe", None), "1800")

    def test_the_unit_exports_its_own_ceiling_before_launching_the_bench(self):
        unit = lifted("run_one_unit")
        # Assigned, then exported (SC2155: an `export X="$(...)"` masks the
        # command's exit status).
        export = (
            'AGENT_DELEGATION_TIMEOUT="$(unit_delegation_timeout "${name}")"\n'
            "  export AGENT_DELEGATION_TIMEOUT"
        )
        self.assertIn(export, unit)
        # Exported before the bench runs, not after it: the position is the
        # whole point of the line.
        self.assertLess(unit.index(export), unit.index("uv run devops-bench"))

    def test_the_task_lock_wait_outlasts_the_units_ceiling(self):
        # A same-task repetition waits on the task lock for the holder's whole
        # unit. With a 3000s ceiling, a fixed 1800s wait would make it give
        # up while the holder was still legitimately running.
        unit = lifted("run_one_unit")
        wait = (
            'lock_acquire "${STATE_DIR}/lock-task-${name}" \\\n'
            '    "$(($(unit_delegation_timeout "${name}") + 600))"'
        )
        self.assertIn(wait, unit)
        body = "\n".join(
            [
                lifted("unit_delegation_timeout"),
                'export AGENT_DELEGATION_TIMEOUT="2700"',
                'name=compliance-rbac-overgrant; echo "$(($(unit_delegation_timeout "${name}") + 600))"',
                'name=capacity-pinned-pool-probe; echo "$(($(unit_delegation_timeout "${name}") + 600))"',
            ]
        )
        self.assertEqual(run_bash(body).stdout.split(), ["3600", "3300"])


class PerCaseGradingTest(unittest.TestCase):
    """A case is graded by the unit that finishes its last repetition.

    run_one_unit writes its state files while it still holds the task lock
    and counts them there, so exactly one repetition sees the count reach
    EVAL_REPETITIONS; finish_case then grades under one grading lock, prints
    the block in one piece, and marks the case `.graded` only when the
    grading produced its JSON. The loop after the fan-out grades what has no
    sentinel and the record step passes the manifest that keeps a case from
    being appended twice.
    """

    UNIT_STUBS = """
_now_ms() { date +%s000; }
lock_acquire() { mkdir "$1" 2>/dev/null; }
lock_release() { rmdir "$1" 2>/dev/null || true; }
mint_ledger_token() { return 0; }
unit_delegation_timeout() { echo 1800; }
_ts_lines() { cat; }
uv() { echo "ran 1 task(s); results: /tmp/fake/run_${rep}/results.json"; }
finish_case() { echo "FINISH_CASE $2 after rep ${rep}"; }
STATE_DIR="$(mktemp -d)"; ARTIFACT_DIR="$(mktemp -d)"; BENCH_DIR=/tmp
EVAL_REPETITIONS=3; INFRA_LOCK_DEADLINE=1
EVAL_CLUSTER_NAME=c; EVAL_DEFAULT_LOCATION=l; SEEDED_TASK_CLUSTER=; SEEDED_TASK_LOCATION=
"""

    def run_reps(self, extra: str = "") -> subprocess.CompletedProcess:
        body = "\n".join([
            lifted("run_one_unit"),
            self.UNIT_STUBS,
            extra,
            'for rep in 1 2 3; do run_one_unit ./tasks/x/task.yaml case-x "${rep}" "" "" "${rep}"; done',
            'ls "${STATE_DIR}" | sort | tr "\\n" " "; echo',
        ])
        return run_bash(body)

    def test_only_the_repetition_that_completes_the_case_grades_it(self):
        result = self.run_reps()
        self.assertEqual(result.stdout.count("FINISH_CASE"), 1, result.stdout + result.stderr)
        self.assertIn("FINISH_CASE case-x after rep 3", result.stdout)
        self.assertIn("case-x.rep1.end case-x.rep1.start case-x.rep2.dir", result.stdout)
        # Graded after its own `finished` line, so the log keeps its markers timely.
        self.assertLess(result.stdout.index("finished case-x rep 3"), result.stdout.index("FINISH_CASE"))

    def test_a_repetition_that_gave_up_on_its_lock_leaves_the_case_to_the_loop(self):
        # Rep 2 never gets the task lock: no state file, so the count stops at
        # two and the case is graded after the fan-out with rep 2 MISSING.
        lost = 'lock_acquire() { case "$1" in *lock-task-*) [ "${rep}" != 2 ] && mkdir "$1" 2>/dev/null ;; *) mkdir "$1" 2>/dev/null ;; esac; }'
        result = self.run_reps(lost)
        self.assertNotIn("FINISH_CASE", result.stdout)
        self.assertIn("case-x rep 2 gave up on its task lock", result.stderr)
        self.assertNotIn("case-x.rep2.end", result.stdout)

    def test_the_state_files_are_written_under_the_task_lock(self):
        unit = lifted("run_one_unit")
        written = unit.index('> "${STATE_DIR}/${name}.rep${rep}.end"')
        counted = unit.index("finished_reps=$((finished_reps + 1))")
        # The last release: the early ones are the give-up paths.
        released = unit.rindex('lock_release "${STATE_DIR}/lock-task-${name}"')
        self.assertLess(written, counted)
        self.assertLess(counted, released)
        self.assertLess(released, unit.index('finish_case "${task}" "${name}"'))

    FINISH_STUBS = """
lock_acquire() { echo "lock $(basename "$1")"; }
lock_release() { echo "unlock $(basename "$1")"; }
grade_case() { echo "Task $2 Result: [PASSED] passed all 3 repetitions"; echo "  rep 1: pass -- ok"; : > "${ARTIFACT_DIR}/case-$2.json"; return "${GRADE_STATUS:-0}"; }
record_case() { echo "  recorded $1: 3/3 -> store"; }
STATE_DIR="$(mktemp -d)"; ARTIFACT_DIR="$(mktemp -d)"
"""

    def run_finish(self, grade_status: int) -> subprocess.CompletedProcess:
        body = "\n".join([
            lifted("finish_case"),
            self.FINISH_STUBS,
            f"GRADE_STATUS={grade_status}",
            "finish_case ./tasks/x/task.yaml case-x",
            '[ -f "${STATE_DIR}/case-x.graded" ] && echo GRADED || echo NOT_GRADED',
        ])
        return run_bash(body)

    def test_a_graded_case_is_marked_recorded_and_printed_in_one_piece(self):
        out = self.run_finish(0).stdout
        self.assertIn("GRADED", out)
        self.assertLess(out.index("lock lock-grade"), out.index("Task case-x Result:"))
        self.assertLess(out.index("Task case-x Result:"), out.index("  recorded case-x"))
        self.assertLess(out.index("  recorded case-x"), out.index("unlock lock-grade"))

    def test_a_grading_that_failed_is_left_for_the_loop_after_the_fan_out(self):
        out = self.run_finish(2).stdout
        self.assertIn("NOT_GRADED", out)
        self.assertIn("grading case-x inside the fan-out failed (status 2)", out)
        self.assertNotIn("recorded case-x", out)
        self.assertIn("unlock lock-grade", out, "the throttle is released either way")

    def test_the_loop_after_the_fan_out_skips_what_the_fan_out_graded(self):
        src = SCRIPT.read_text(encoding="utf-8")
        loop = src[src.index("# ─── Per-case verdicts, in the order TASKS declares"):src.index('profile_begin "record + final gate"')]
        self.assertIn('if [ -f "${STATE_DIR}/${TASK_NAME}.graded" ] && [ -f "${CASE_JSON}" ]; then', loop)
        self.assertIn('grade_case "${TASK}" "${TASK_NAME}"', loop)
        self.assertIn('CASE_RESULTS+=(--case-result "${CASE_JSON}")', loop)

    def test_both_record_calls_share_the_manifest(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('--recorded-manifest "${EVAL_RECORDED_MANIFEST}"', lifted("record_case"))
        tail = src[src.index('echo ">>> [$(date -u +\'%Y-%m-%dT%H:%M:%SZ\')] Recording baseline evidence from main <<<"'):]
        self.assertIn('--recorded-manifest "${EVAL_RECORDED_MANIFEST}"', tail)
        # The decision the in-lane record reads is taken before the fan-out.
        self.assertLess(src.index('EVAL_IS_MAIN_RUN="true"'), src.index("run_one_unit() {"))
        self.assertLess(src.index("EVAL_RECORDED_MANIFEST="), src.index("run_one_unit() {"))


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
