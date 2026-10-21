"""The eval fan-out's scheduler is model-free shell, so it is testable here.

`hack/ci-eval-pr.sh` launches one background unit per (task, repetition) and
serializes the collisions with four mkdir mutexes (task, state, stream, infra). Three properties carry the
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

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"


def lifted(name: str) -> str:
    """A top-level shell function as written, lifted from the script."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{.*?^\}}$|^{name}\(\) \{{[^\n]*\}}$", src, re.DOTALL | re.MULTILINE)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{name}() not found in {SCRIPT}")
    return match.group(0)


def lifted_constant(name: str) -> str:
    """A `readonly NAME=...` line as written, lifted from the script: the
    function under test reads it, and a stub would hide a rename."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^readonly {name}=.*$", src, re.MULTILINE)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"readonly {name} not found in {SCRIPT}")
    return match.group(0)


def run_bash(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -uo pipefail\n" + body],
        capture_output=True,
        text=True,
        check=False,
    )


class EvalLogDirTest(unittest.TestCase):
    """EVAL_LOG_DIR is absolute and exists once the script's top has run: the
    write runs in a subshell that cds into BENCH_DIR, the reads do not."""

    def setup_lines(self) -> str:
        src = SCRIPT.read_text(encoding="utf-8")
        match = re.search(r'^EVAL_LOG_DIR="\$\{EVAL_LOG_DIR:-/tmp\}"\n.*?^readonly EVAL_LOG_DIR$', src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(match, "the EVAL_LOG_DIR setup block moved or changed shape")
        return match.group(0)

    def test_a_relative_directory_is_created_and_made_absolute(self):
        with tempfile.TemporaryDirectory() as cwd:
            out = subprocess.run(
                ["bash", "-c", "set -euo pipefail\n" + self.setup_lines() + '\necho "${EVAL_LOG_DIR}"'],
                cwd=cwd, env={**os.environ, "EVAL_LOG_DIR": "logs", "CDPATH": ""},
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertEqual(os.path.realpath(os.path.join(cwd, "logs")), os.path.realpath(out))
            self.assertTrue(os.path.isabs(out))
            self.assertTrue(os.path.isdir(out))

    def test_cdpath_does_not_redirect_a_relative_directory(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as elsewhere:
            os.mkdir(os.path.join(elsewhere, "logs"))
            out = subprocess.run(
                ["bash", "-c", "set -euo pipefail\n" + self.setup_lines() + '\necho "${EVAL_LOG_DIR}"'],
                cwd=cwd, env={**os.environ, "EVAL_LOG_DIR": "logs", "CDPATH": elsewhere},
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertEqual(1, len(out.splitlines()), out)
            self.assertEqual(os.path.realpath(os.path.join(cwd, "logs")), os.path.realpath(out.strip()))

    def test_unset_keeps_tmp(self):
        env = {k: v for k, v in os.environ.items() if k != "EVAL_LOG_DIR"}
        out = subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + self.setup_lines() + '\necho "${EVAL_LOG_DIR}"'],
            env=env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(os.path.realpath("/tmp"), os.path.realpath(out))


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
        match = re.search(r'^UNIT_QUEUE="\$\(\n.*?^\)"$', src, re.DOTALL | re.MULTILINE)
        if match is None:  # pragma: no cover
            raise AssertionError(f"UNIT_QUEUE block not found in {SCRIPT}")
        body = "\n".join(
            [
                lifted("unit_cost_hint"),
                # The queue leaves out the cases that request a pull request
                # (the inject lane's second phase); with the list unset every
                # task is in this queue, as on the api lane.
                lifted("unit_phase"),
                f"EVAL_REPETITIONS={reps}",
                # compliance carries a 1000 hint, the probe 200: order within a
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
            "upgrades-audit-reports-upgrade-blocked",
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
        # And on a stream another case in the run also writes, the holder
        # waits its turn on the stream lock first, so the wait is that figure
        # times the cases on the stream (stream_case_count), plus the infra
        # queue the stream's stack-bearing cases may hold it through
        # (stream_stack_wait); a case alone on its stream, or writing none,
        # keeps the single-unit figure -- plus one INFRA_LOCK_DEADLINE when it
        # writes none and carries a stack, since its previous rep holds the
        # task lock while queued on lock-infra. The in-flight grace a
        # ledger-writing unit may spend before its run is in the per-case
        # figure too, so a holder that spends it does not push its waiter past
        # the deadline, and so is the wait for a run the install started on
        # the unit's streams (wait_platform_runs).
        unit = lifted("run_one_unit")
        deadline = 'lock_deadline="$(stream_lock_deadline "${name}" "${audit_id}" "${streams}")"'
        ledgerless_stack = (
            'if [ -z "${audit_id}" ] && [ -n "${has_stack}" ]; then\n'
            "    lock_deadline=$(( lock_deadline + INFRA_LOCK_DEADLINE ))\n"
            "  fi"
        )
        self.assertIn(deadline, unit)
        self.assertIn(ledgerless_stack, unit)
        grace = re.search(r"^readonly EVAL_INFLIGHT_GRACE_SECONDS=\d+$", SCRIPT.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(grace)
        allowance = re.search(r"^readonly UNIT_LOCK_ALLOWANCE_SECONDS=\d+$", SCRIPT.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(allowance)
        run_wait = re.search(r"^readonly EVAL_PLATFORM_RUN_WAIT_SECONDS=\d+$", SCRIPT.read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(run_wait)
        self.assertIn('lock_acquire "${STATE_DIR}/lock-task-${name}" "${lock_deadline}"', unit)
        computed = deadline + "; " + ledgerless_stack + '; echo "${lock_deadline}"'
        body = "\n".join(
            [
                lifted("unit_delegation_timeout"),
                lifted("stream_lock_deadline"),
                grace.group(0),
                allowance.group(0),
                run_wait.group(0),
                'export AGENT_DELEGATION_TIMEOUT="2700"',
                "INFRA_LOCK_DEADLINE=900",
                'stream_case_count() { echo "${CASES_ON_STREAM}"; }',
                'stream_stack_wait() { echo "${STACK_WAIT:-0}"; }',
                "CASES_ON_STREAM=1 STACK_WAIT=0 name=compliance-rbac-overgrant audit_id=compliance-audit streams=compliance-audit has_stack=\n" + computed,
                "CASES_ON_STREAM=1 STACK_WAIT=0 name=capacity-pinned-pool-probe audit_id= streams= has_stack=\n" + computed,
                "CASES_ON_STREAM=2 STACK_WAIT=0 name=consistency-drift-outlier audit_id=fleet-consistency-drift streams=fleet-consistency-drift has_stack=\n" + computed,
                "CASES_ON_STREAM=1 STACK_WAIT=900 name=compliance-rbac-overgrant audit_id=compliance-audit streams=compliance-audit has_stack=1\n" + computed,
                "CASES_ON_STREAM=1 STACK_WAIT=0 name=capacity-pinned-pool-probe audit_id= streams= has_stack=1\n" + computed,
            ]
        )
        self.assertEqual(run_bash(body).stdout.split(), ["11100", "3600", "22200", "12000", "4500"])


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
ledger_audit_id_for_task() { echo ""; }
task_streams() { ledger_audit_id_for_task "$1"; }
stream_lock_deadline() { echo 1; }
release_streams() { local s; for s in $1; do lock_release "${STATE_DIR}/lock-stream-${s}"; done; }
stream_case_count() { echo 1; }
stream_stack_wait() { echo 0; }
unit_state_lock() { :; }
release_state_lock() { :; }
infra_lock_exempt() { return 1; }
_ts_lines() { cat; }
uv() { echo "ran 1 task(s); results: /tmp/fake/run_${rep}/results.json"; }
finish_case() { echo "FINISH_CASE $2 after rep ${rep}"; }
STATE_DIR="$(mktemp -d)"; ARTIFACT_DIR="$(mktemp -d)"; BENCH_DIR=/tmp
# A private log directory, so runs that share the machine do not share rep logs.
EVAL_LOG_DIR="$(mktemp -d)"
EVAL_REPETITIONS=3; INFRA_LOCK_DEADLINE=1; EVAL_INFLIGHT_GRACE_SECONDS=300
release_inflight_note() { :; }
EVAL_CLUSTER_NAME=c; EVAL_DEFAULT_LOCATION=l; SEEDED_TASK_CLUSTER=; SEEDED_TASK_LOCATION=
"""

    def run_reps(self, extra: str = "", after: str = "") -> subprocess.CompletedProcess:
        body = "\n".join([
            # The one constant the unit reads from the script's top: the base
            # of its per-unit inject port.
            lifted_constant("EVAL_INJECT_LOCAL_PORT_BASE"),
            # The name the opening turn's timing is copied in under.
            lifted_constant("EVAL_TIMING_FILE"),
            # The unit resolves the task file it hands the bench through
            # this helper (the inject lane's copy, or the file under
            # bench/tasks/); with no lane directory set it is the identity.
            lifted("unit_task_path"),
            # The count of finished repetitions the unit reads under its
            # task lock, and the unwinding its skip paths share.
            lifted("finished_rep_count"),
            lifted("skip_unit"),
            # What the unit does when the install is gone (#3129): the code
            # wait_platform_runs returns for it and the marker that latches it.
            lifted_constant("EVAL_GATEWAY_GONE_RC"),
            lifted_constant("EVAL_INSTALL_GONE_MARKER"),
            lifted("run_one_unit"),
            self.UNIT_STUBS,
            extra,
            'for rep in 1 2 3; do run_one_unit ./tasks/x/task.yaml case-x "${rep}" "" "" "${rep}"; done',
            'ls "${STATE_DIR}" | sort | tr "\\n" " "; echo',
            after,
        ])
        return run_bash(body)

    def test_only_the_repetition_that_completes_the_case_grades_it(self):
        result = self.run_reps()
        self.assertEqual(result.stdout.count("FINISH_CASE"), 1, result.stdout + result.stderr)
        self.assertIn("FINISH_CASE case-x after rep 3", result.stdout)
        self.assertIn("case-x.rep1.end case-x.rep1.start case-x.rep2.dir", result.stdout)
        # Graded after its own `finished` line, so the log keeps its markers timely.
        self.assertLess(result.stdout.index("finished case-x rep 3"), result.stdout.index("FINISH_CASE"))

    def test_the_opening_turns_timing_lands_beside_each_repetitions_results(self):
        """Run, not read: the bench stub writes the file the unit names in
        KUBE_AGENTS_TIMING_FILE for reps 1 and 3 only, and rep 2's state
        directory holds a stale file from an earlier lease, which must not
        reach rep 2's run directory."""
        runs = 'RUNS="$(mktemp -d)"; : > "${STATE_DIR}/case-x.rep2.timing.json"\n'
        bench = (
            'uv() { mkdir -p "${RUNS}/run_${rep}"; '
            '[ "${rep}" = 2 ] || echo "{\\"turn1_s\\": ${rep}.5}" > "${KUBE_AGENTS_TIMING_FILE}"; '
            'echo "ran 1 task(s); results: ${RUNS}/run_${rep}/results.json"; }\n'
        )
        check = 'for r in 1 2 3; do echo "rep ${r}: $(cat "${RUNS}/run_${r}/timing.json" 2>/dev/null || echo none)"; done'
        result = self.run_reps(runs + bench, check)
        self.assertIn('rep 1: {"turn1_s": 1.5}', result.stdout, result.stdout + result.stderr)
        self.assertIn("rep 2: none", result.stdout)
        self.assertIn('rep 3: {"turn1_s": 3.5}', result.stdout)

    def test_a_repetition_that_gave_up_on_its_lock_leaves_the_case_to_the_loop(self):
        # Rep 2 never gets the task lock: no state file, so the count stops at
        # two and the case is graded after the fan-out with rep 2 MISSING.
        lost = 'lock_acquire() { case "$1" in *lock-task-*) [ "${rep}" != 2 ] && mkdir "$1" 2>/dev/null ;; *) mkdir "$1" 2>/dev/null ;; esac; }'
        result = self.run_reps(lost)
        self.assertNotIn("FINISH_CASE", result.stdout)
        self.assertIn("case-x rep 2 gave up on its task lock", result.stderr)
        self.assertNotIn("case-x.rep2.end", result.stdout)

    def test_a_gateway_that_is_gone_records_every_repetition_as_infrastructure_and_never_launches(self):
        """#3129: when wait_platform_runs reports the gateway Deployment gone,
        the unit records its repetition as infrastructure instead of running
        the bench against a missing install, and latches the state for the
        run, so later units, streams or none, skip the exec and record the
        same way; the last one grades the case."""
        gone = "\n".join(
            [
                lifted_constant("EVAL_INFRA_FAILURE_MARKER"),
                lifted_constant("EVAL_NOT_RUN_DIR"),
                lifted_constant("EVAL_NOT_RUN_STATUS"),
                lifted("record_unit_not_run"),
                'wait_platform_runs() { echo "WAIT ${rep}"; return "${EVAL_GATEWAY_GONE_RC}"; }',
            ]
        )
        result = self.run_reps(gone)
        out = result.stdout + result.stderr
        self.assertEqual(result.stdout.count("WAIT"), 1, out)
        self.assertEqual(result.stderr.count("recorded as infrastructure"), 3, out)
        self.assertNotIn("ran 1 task(s)", result.stdout, "the bench must not launch against a missing install")
        self.assertIn("FINISH_CASE case-x after rep 3", result.stdout, out)
        self.assertIn("install-gone", result.stdout, out)

    def test_a_gone_gateway_leaves_a_state_lock_another_unit_holds(self):
        """The gone-gateway exit comes before this unit takes its state lock,
        so it must not release that lock: another unit sharing it may hold it
        at that moment, and skip_unit's release would take it from under it."""
        gone = "\n".join(
            [
                lifted_constant("EVAL_INFRA_FAILURE_MARKER"),
                lifted_constant("EVAL_NOT_RUN_DIR"),
                lifted_constant("EVAL_NOT_RUN_STATUS"),
                lifted("record_unit_not_run"),
                lifted("release_state_lock"),
                'unit_state_lock() { echo shared-state; }',
                'state_lock_deadline() { echo 1; }',
                'mkdir "${STATE_DIR}/lock-state-shared-state"',
                'wait_platform_runs() { return "${EVAL_GATEWAY_GONE_RC}"; }',
            ]
        )
        after = '[ -d "${STATE_DIR}/lock-state-shared-state" ] && echo "STATE LOCK KEPT" || echo "STATE LOCK RELEASED"'
        result = self.run_reps(gone, after)
        out = result.stdout + result.stderr
        self.assertEqual(result.stderr.count("recorded as infrastructure"), 3, out)
        self.assertIn("STATE LOCK KEPT", result.stdout, out)

    def test_every_repetition_gets_its_own_tunnels(self):
        """The harness owns one port-forward per process and its atexit
        teardown drops a shared listener under every sibling, so each unit
        exports its own local port for the agent API and, from the script's
        named base, for the inject door. Run, not read: the ports the bench
        stub sees are the ones the harness would bind."""
        # The unit sends the bench's stdout to its own log file, so the stub
        # records what it saw in a file of this test's.
        with tempfile.TemporaryDirectory() as tmp:
            record = pathlib.Path(tmp) / "ports"
            ports = (
                f'uv() {{ echo "PORTS rep=${{rep}} api=${{AGENT_LOCAL_PORT}} inject=${{AGENT_INJECT_LOCAL_PORT}}" >> "{record}"; '
                'echo "ran 1 task(s); results: /tmp/fake/run_${rep}/results.json"; }'
            )
            result = self.run_reps(ports)
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = record.read_text(encoding="utf-8") if record.exists() else ""
        seen = re.findall(r"^PORTS rep=(\d) api=(\d+) inject=(\d+)$", recorded, re.MULTILINE)
        self.assertEqual([rep for rep, _, _ in seen], ["1", "2", "3"], recorded)
        base = int(lifted_constant("EVAL_INJECT_LOCAL_PORT_BASE").split("=")[1])
        for rep, api, inject in seen:
            self.assertEqual(int(api), 28642 + int(rep))
            self.assertEqual(int(inject), base + int(rep))
        self.assertEqual(len({inject for _, _, inject in seen}), 3)
        self.assertFalse({api for _, api, _ in seen} & {inject for _, _, inject in seen})

    def stamps(self, audit_id: str, gitops_override: str = "") -> list[str]:
        """EVAL_STREAM_STARTED_AT, EVAL_AUDIT_STREAM and EVAL_STREAM_REPO as each of case-y's
        reps, then case-x's, sees them (`-` for unset).
        `date` ticks per call here, so a stamp taken per unit would differ."""
        with tempfile.TemporaryDirectory() as tmp:
            record = pathlib.Path(tmp) / "stamps"
            extra = "\n".join([
                'tick=1000; date() { if [ "$*" = "-u +%s" ]; then tick=$((tick + 1)); echo "${tick}"; '
                'else command date "$@"; fi; }',
                f'ledger_audit_id_for_task() {{ echo "{audit_id}"; }}',
                'reset_audit_ledgers() { :; }',
                'EVAL_LEDGER_REPO=gke-agentic/kube-agents-evals-4-infra',
                f'EVAL_GITOPS_REPO="{gitops_override}"',
                f'uv() {{ echo "STAMP ${{EVAL_CASE_ID}} ${{EVAL_STREAM_STARTED_AT:--}} ${{EVAL_AUDIT_STREAM:--}} ${{EVAL_STREAM_REPO:--}}" >> "{record}"; '
                'echo "ran 1 task(s); results: /tmp/fake/run_${rep}/results.json"; }',
                'for rep in 1 2 3; do run_one_unit ./tasks/y/task.yaml case-y "${rep}" "" "" "$((rep + 3))"; done',
            ])
            result = self.run_reps(extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = record.read_text(encoding="utf-8") if record.exists() else ""
        return re.findall(r"^STAMP (\S+ \S+ \S+ \S+)$", recorded, re.MULTILINE)

    def test_every_unit_on_a_stream_sees_when_the_stream_began(self):
        """pull_request_opened's accepts_stream_pull_request measures from
        EVAL_STREAM_STARTED_AT: the pull request case-y's rep 1 opened is the
        one case-x's reps find open, so all six must see case-y's rep 1 start."""
        self.assertEqual(
            self.stamps("obtainability-audit"),
            ["case-y 1001 obtainability-audit gke-agentic/kube-agents-evals-4-infra"] * 3
            + ["case-x 1001 obtainability-audit gke-agentic/kube-agents-evals-4-infra"] * 3,
        )

    def test_a_developers_gitops_repo_is_the_streams_repository(self):
        """The deploy told the agent to write to EVAL_GITOPS_REPO, so its pull
        requests are there; `none` is the opt-out, not a repository."""
        self.assertEqual(
            {s.split()[3] for s in self.stamps("obtainability-audit", "me/infra")},
            {"me/infra"},
        )
        self.assertEqual(
            {s.split()[3] for s in self.stamps("obtainability-audit", "none")},
            {"gke-agentic/kube-agents-evals-4-infra"},
        )

    def test_a_case_with_no_stream_gets_no_stream_window(self):
        """Nothing ties a streamless case's earlier pull request to it, so the
        verifier gets no stamp and measures from the run."""
        self.assertEqual(self.stamps(""), ["case-y - - -"] * 3 + ["case-x - - -"] * 3)

    def test_the_state_files_are_written_under_the_task_lock(self):
        unit = lifted("run_one_unit")
        written = unit.index('> "${STATE_DIR}/${name}.rep${rep}.end"')
        counted = unit.index('finished_reps="$(finished_rep_count "${name}")"')
        # The last release: the early ones are the give-up paths.
        released = unit.rindex('lock_release "${STATE_DIR}/lock-task-${name}"')
        self.assertLess(written, counted)
        self.assertLess(counted, released)
        # The last finish_case: the main path's (the gone-gateway exit grades
        # earlier, under the same rule).
        self.assertLess(released, unit.rindex('finish_case "${task}" "${name}"'))
        # A repetition the launcher could not start writes its record and is
        # counted the same way, under the lock, before it lets go.
        skip = lifted("skip_unit")
        self.assertLess(skip.index('record_unit_not_run "${name}"'), skip.index("finished_rep_count"))
        self.assertLess(skip.index("finished_rep_count"), skip.index('lock_release "${STATE_DIR}/lock-task-${name}"'))
        self.assertLess(skip.index('lock_release "${STATE_DIR}/lock-task-${name}"'), skip.index('finish_case "${task}" "${name}"'))

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

    def test_the_run_marks_its_suite_step_reached_before_writing_its_own_verdict(self):
        """The one line that keeps the EXIT trap's cut-off table from
        overwriting a finished run's verdict is `EVAL_SUITE_REACHED=1`, set
        in the main body after the fan-out's `wait` and before the run's own
        `bench-gate suite`; report_partial_verdict returns on it. Dropped,
        moved below the suite call, or moved into the `(cd ...)` subshell,
        every run that reached its verdict would have the trap rewrite
        eval-verdict.md / .json with the PARTIAL banner, and the suite would
        stay green. test_ci_eval_trap.py proves the function honours the
        variable; this pins that the script sets it, where it must.
        """
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(src.count("\nwait\n"), 1, "one fan-out wait in the main body")
        self.assertEqual(src.count("\nEVAL_SUITE_REACHED=1\n"), 1, "set once, at top level")
        own_suite = '(cd "${BENCH_DIR}" && uv run bench-gate suite \\\n  "${CASE_RESULTS[@]}"'
        self.assertLess(src.index("\nwait\n"), src.index("\nEVAL_SUITE_REACHED=1\n"))
        self.assertLess(src.index("\nEVAL_SUITE_REACHED=1\n"), src.index(own_suite))
        self.assertIn('[ -z "${EVAL_SUITE_REACHED:-}" ] || return 0', lifted("report_partial_verdict"))


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


TASKS_DIR = REPO_ROOT / "bench" / "tasks"
TF_ROOT = REPO_ROOT / "bench" / "tf"
# The onboarding markers a stack under the onboarding state lock rewrites. The
# stacks that touch the Planning Agent's `oobe` job all write one of them too.
ONBOARDING_STATE = re.compile(r"bootstrap_scan_filed|bootstrap_handoff_filed|oobe_audits_fired")


def task_stack(case: str) -> str:
    """The stack the script resolves for a case, through its own task_stack."""
    out = run_bash(lifted("task_stack") + f' && task_stack "{TASKS_DIR / case / "task.yaml"}"').stdout
    return out.strip()


def stack_text(stack: str) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((TF_ROOT / stack).rglob("*")) if p.is_file() and p.suffix in {".tf", ".py", ".sh"})


def shell_answer(function: str, call: str) -> subprocess.CompletedProcess:
    return run_bash(lifted(function) + "\n" + call)


class StateLockTest(unittest.TestCase):
    """A stack-bearing case can run off the infra lock (infra_lock_exempt).

    The infra lock kept every stack apart, the onboarding cases included, so
    an exempt case needs two things in its place: a private kubeconfig, since
    devops-bench runs get-credentials for the stack's output on the shared one,
    and a state lock with the cases whose stacks rewrite the same install state.
    """

    def exempt(self, case: str) -> bool:
        return shell_answer("infra_lock_exempt", f"infra_lock_exempt {case}").returncode == 0

    def state_lock(self, case: str) -> str:
        return shell_answer("unit_state_lock", f"unit_state_lock {case}").stdout.strip()

    def test_only_oobe_first_run_audits_is_exempt(self):
        self.assertTrue(self.exempt("oobe-first-run-audits"))
        for case in ("bootstrap-discovery-fanout", "gpu-stress-test-diagnosis", "capacity-pinned-pool-probe"):
            with self.subTest(case=case):
                self.assertFalse(self.exempt(case))

    def test_an_exempt_case_is_no_infra_contender(self):
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('if [ -n "${TASK_STACK}" ] && ! infra_lock_exempt "${TASK_NAME}"; then', src)
        # Defined before the loop that calls it, which runs at top level.
        self.assertLess(src.index("infra_lock_exempt() {"), src.index('! infra_lock_exempt "${TASK_NAME}"'))
        self.assertLess(src.index("unit_state_lock() {"), src.index('! infra_lock_exempt "${TASK_NAME}"'))

    def test_every_stack_that_rewrites_onboarding_state_holds_the_onboarding_lock(self):
        # Against the real files: a new stack that touches the markers without
        # the lock would race oobe-first-run-audits, which the infra lock no
        # longer keeps apart from it.
        rewriters = []
        for task in sorted(TASKS_DIR.glob("*/task.yaml")):
            case = task.parent.name
            stack = task_stack(case)
            if not stack:
                continue
            # A stack the script resolves but this test cannot read would pass
            # unchecked, so it is a failure, not a skip.
            self.assertTrue((TF_ROOT / stack).is_dir(), f"{case}: stack {stack!r} is not a directory under bench/tf")
            if ONBOARDING_STATE.search(stack_text(stack)):
                rewriters.append(case)
        self.assertIn("oobe-first-run-audits", rewriters)
        self.assertIn("bootstrap-discovery-fanout", rewriters)
        for case in rewriters:
            with self.subTest(case=case):
                self.assertEqual(self.state_lock(case), "onboarding")
        self.assertEqual(self.state_lock("capacity-pinned-pool-probe"), "")

    def test_an_exempt_stack_reaches_its_cluster_through_its_own_kubeconfig(self):
        for case in ("oobe-first-run-audits",):
            with self.subTest(case=case):
                self.assertTrue(self.exempt(case))
                main = (TF_ROOT / task_stack(case) / "main.tf").read_text(encoding="utf-8")
                own = main.index('KUBECONFIG="$kubeconfig_dir/config"')
                self.assertLess(own, main.index("get-credentials"))
                self.assertLess(own, main.index("kubectl"))

    def test_the_state_lock_is_taken_after_the_stream_waits_or_with_the_infra_lock(self):
        # Off the infra lock, a unit takes it once its stream locks are held and
        # wait_platform_runs is over, so it holds the state only while its stack
        # is in use. A stack-bearing unit takes it only while it holds the infra
        # lock, so it never holds the state lock while queued for infra.
        unit = lifted("run_one_unit")
        task_lock = unit.index('lock_acquire "${STATE_DIR}/lock-task-${name}"')
        stream_lock = unit.index('lock_acquire "${STATE_DIR}/lock-stream-${s}"')
        wait = unit.index('wait_platform_runs "${name} rep ${rep}" "${streams}"')
        blocking = unit.index('lock_acquire "${STATE_DIR}/lock-state-${state_lock}"')
        infra_lock = unit.index('lock_acquire "${STATE_DIR}/lock-infra"')
        one_try = unit.index('mkdir "${STATE_DIR}/lock-state-${state_lock}"')
        self.assertIn('if [ -n "${state_lock}" ] && [ -z "${has_stack}" ]', unit[wait:blocking])
        self.assertLess(task_lock, stream_lock)
        self.assertLess(stream_lock, wait)
        self.assertLess(wait, blocking)
        self.assertLess(blocking, infra_lock)
        self.assertLess(infra_lock, one_try)
        # Given back after the run and in skip_unit, the two places a unit
        # reaches only holding it; no give-up path before them took it.
        self.assertEqual(unit.count('release_state_lock "${name}"'), 1)
        self.assertIn('release_state_lock "${name}"', lifted("skip_unit"))

    def stack_unit(self, hold_state: str, deadline: int) -> subprocess.CompletedProcess:
        body = "\n".join(
            [
                lifted_constant("EVAL_INJECT_LOCAL_PORT_BASE"),
                lifted_constant("EVAL_GATEWAY_GONE_RC"),
                lifted_constant("EVAL_INSTALL_GONE_MARKER"),
                lifted_constant("EVAL_TIMING_FILE"),
                lifted("unit_task_path"),
                lifted("finished_rep_count"),
                lifted("skip_unit"),
                lifted("run_one_unit"),
                PerCaseGradingTest.UNIT_STUBS,
                lifted("release_state_lock"),
                'ACQ_LOG="$(mktemp)"',
                'lock_acquire() { echo "$(basename "$1")" >> "${ACQ_LOG}"; mkdir "$1" 2>/dev/null; }',
                "unit_state_lock() { echo onboarding; }",
                "state_lock_deadline() { echo 0; }",
                f"INFRA_LOCK_DEADLINE={deadline}; STATE_LOCK_RETRY_SECONDS=1",
                'mkdir "${STATE_DIR}/lock-state-onboarding"',
                f'( sleep {hold_state}; rmdir "${{STATE_DIR}}/lock-state-onboarding" ) &' if hold_state else ":",
                'uv() { [ -d "${STATE_DIR}/lock-state-onboarding" ] && [ -d "${STATE_DIR}/lock-infra" ] && echo "BOTH"; echo "ran 1 task(s); results: /tmp/fake/run_1/results.json"; }',
                'run_one_unit ./tasks/x/task.yaml case-x 1 "" true 1 2>&1 | tail -1',
                'cat "${EVAL_LOG_DIR}/eval_case-x_rep1.log" 2>/dev/null',
                'echo "INFRA_TRIES=$(grep -c lock-infra "${ACQ_LOG}")"',
                'ls "${STATE_DIR}" | grep "^lock-" | tr "\\n" " "; echo',
            ]
        )
        return run_bash(body)

    def test_a_busy_state_lock_sends_the_infra_lock_back_to_the_queue(self):
        out = self.stack_unit(hold_state="2.5", deadline=60).stdout
        self.assertIn("BOTH", out)
        self.assertGreaterEqual(int(re.search(r"INFRA_TRIES=(\d+)", out).group(1)), 2)
        self.assertNotIn("lock-", out.splitlines()[-1])

    def test_a_state_lock_that_never_frees_is_a_loud_give_up_that_takes_nothing(self):
        out = self.stack_unit(hold_state="", deadline=3).stdout
        self.assertIn("gave up on the infra lock with the onboarding state lock", out)
        self.assertNotIn("BOTH", out)
        # The other holder's state lock stays; this unit left neither infra
        # nor its task lock behind.
        self.assertEqual(out.splitlines()[-1].split(), ["lock-state-onboarding"])

    def test_the_lock_is_held_and_given_back_by_a_real_unit(self):
        body = "\n".join(
            [
                lifted_constant("EVAL_INJECT_LOCAL_PORT_BASE"),
                lifted_constant("EVAL_GATEWAY_GONE_RC"),
                lifted_constant("EVAL_INSTALL_GONE_MARKER"),
                lifted_constant("EVAL_TIMING_FILE"),
                lifted("unit_task_path"),
                lifted("finished_rep_count"),
                lifted("skip_unit"),
                lifted("run_one_unit"),
                PerCaseGradingTest.UNIT_STUBS,
                lifted("release_state_lock"),
                'unit_state_lock() { echo onboarding; }',
                'state_lock_deadline() { echo 1; }',
                'uv() { [ -d "${STATE_DIR}/lock-state-onboarding" ] && echo "HELD"; echo "ran 1 task(s); results: /tmp/fake/run_1/results.json"; }',
                'run_one_unit ./tasks/x/task.yaml case-x 1 "" "" 1 >/dev/null 2>&1',
                'cat "${EVAL_LOG_DIR}/eval_case-x_rep1.log"',
                '[ -d "${STATE_DIR}/lock-state-onboarding" ] && echo "LEFT" || echo "RELEASED"',
            ]
        )
        out = run_bash(body).stdout
        self.assertIn("HELD", out)
        self.assertIn("RELEASED", out)

    def test_the_hold_covers_the_exempt_stacks_waits(self):
        # STATE_LOCK_HOLD_SECONDS stands for the time an exempt stack's apply
        # can wait: its own_wait plus its chain_wait. The turn and teardown on
        # top fit in the INFRA_LOCK_DEADLINE term state_lock_deadline adds.
        hold = int(lifted_constant("STATE_LOCK_HOLD_SECONDS").split("=")[1])
        exempt = [t.parent.name for t in sorted(TASKS_DIR.glob("*/task.yaml")) if self.exempt(t.parent.name)]
        self.assertIn("oobe-first-run-audits", exempt)
        for case in exempt:
            with self.subTest(case=case):
                main = (TF_ROOT / task_stack(case) / "main.tf").read_text(encoding="utf-8")
                waits = []
                for name in ("own_wait", "chain_wait"):
                    match = re.search(rf"^\s*{name}\s*=\s*(\d+)\s*$", main, re.M)
                    self.assertIsNotNone(match, f"{case}: no {name} local in its stack's main.tf")
                    waits.append(int(match.group(1)))
                self.assertLessEqual(sum(waits), hold)

    def give_up_on_busy_state_lock(self, has_stack: str) -> str:
        body = "\n".join(
            [
                lifted_constant("EVAL_INJECT_LOCAL_PORT_BASE"),
                lifted_constant("EVAL_GATEWAY_GONE_RC"),
                lifted_constant("EVAL_INSTALL_GONE_MARKER"),
                lifted_constant("EVAL_TIMING_FILE"),
                lifted("unit_task_path"),
                lifted("finished_rep_count"),
                lifted("skip_unit"),
                lifted("run_one_unit"),
                PerCaseGradingTest.UNIT_STUBS,
                lifted("release_state_lock"),
                lifted("release_streams"),
                'ACQ_LOG="$(mktemp)"',
                'lock_acquire() { mkdir "$1" 2>/dev/null && echo "$(basename "$1")" >> "${ACQ_LOG}"; }',
                "unit_state_lock() { echo onboarding; }",
                "state_lock_deadline() { echo 0; }",
                "INFRA_LOCK_DEADLINE=3; STATE_LOCK_RETRY_SECONDS=1",
                'mkdir "${STATE_DIR}/lock-state-onboarding"',
                f'run_one_unit ./tasks/x/task.yaml case-x 1 "" "{has_stack}" 1 "s-one s-two" 2>&1 | tail -1',
                'echo "HELD=$(grep -c "^lock-stream-" "${ACQ_LOG}")"',
                'ls "${STATE_DIR}" | grep "^lock-" | tr "\\n" " "; echo',
            ]
        )
        return run_bash(body).stdout

    def test_a_unit_that_gives_up_on_the_state_lock_keeps_no_stream(self):
        # Both give-up paths hand back the streams the unit already holds;
        # without that, every later unit on them waits out its own deadline.
        for has_stack, said in (("true", "gave up on the infra lock with the onboarding state lock"), ("", "gave up on the onboarding state lock")):
            with self.subTest(has_stack=has_stack):
                out = self.give_up_on_busy_state_lock(has_stack)
                self.assertIn(said, out)
                self.assertIn("HELD=2", out)
                self.assertEqual(out.splitlines()[-1].split(), ["lock-state-onboarding"])

    def test_the_state_lock_deadline_outlasts_every_holder(self):
        # Each case on the lock keeps its stack applied while it holds it, and a
        # stack-bearing waiter queues for infra between its tries.
        body = "\n".join(
            [
                lifted("unit_state_lock"),
                lifted("state_lock_deadline"),
                lifted_constant("STATE_LOCK_HOLD_SECONDS"),
                "INFRA_LOCK_DEADLINE=100",
                "TASK_NAMES=(oobe-first-run-audits bootstrap-discovery-fanout capacity-pinned-pool-probe bootstrap-inventory-ranking-delivery)",
                "state_lock_deadline onboarding",
                "state_lock_deadline none",
            ]
        )
        hold = int(lifted_constant("STATE_LOCK_HOLD_SECONDS").split("=")[1])
        self.assertEqual(run_bash(body).stdout.split(), [str(3 * (100 + hold)), "0"])

    def test_the_task_lock_waits_out_a_previous_rep_on_the_state_lock(self):
        unit = lifted("run_one_unit")
        term = unit.index('if [ -n "${state_lock}" ]; then')
        self.assertIn('lock_deadline=$(( lock_deadline + $(state_lock_deadline "${state_lock}") ))', unit[term:])
        self.assertLess(term, unit.index('lock_acquire "${STATE_DIR}/lock-task-${name}"'))

    def test_only_an_exempt_unit_runs_on_a_private_kubeconfig(self):
        body_for = lambda exempt: "\n".join(  # noqa: E731
            [
                lifted_constant("EVAL_INJECT_LOCAL_PORT_BASE"),
                lifted_constant("EVAL_GATEWAY_GONE_RC"),
                lifted_constant("EVAL_INSTALL_GONE_MARKER"),
                lifted_constant("EVAL_TIMING_FILE"),
                lifted("unit_task_path"),
                lifted("finished_rep_count"),
                lifted("skip_unit"),
                lifted("run_one_unit"),
                PerCaseGradingTest.UNIT_STUBS,
                f"infra_lock_exempt() {{ return {0 if exempt else 1}; }}",
                'kubectl() { echo "apiVersion: v1"; }',
                "export KUBECONFIG=/shared/config",
                'uv() { echo "KUBECONFIG=${KUBECONFIG}"; echo "ran 1 task(s); results: /tmp/fake/run_1/results.json"; }',
                'run_one_unit ./tasks/x/task.yaml case-x 1 "" "" 1 >/dev/null 2>&1',
                'cat "${EVAL_LOG_DIR}/eval_case-x_rep1.log"',
                'echo "AFTER=${KUBECONFIG}"',
            ]
        )
        exempt = run_bash(body_for(True)).stdout
        self.assertRegex(exempt, r"KUBECONFIG=\S+/kubeconfig-case-x-rep1\n")
        # The export stays inside the unit's subshell in the real fan-out; here
        # it is enough that the shared file is not what the bench saw.
        self.assertNotIn("KUBECONFIG=/shared/config\n", exempt.split("AFTER=")[0])
        self.assertIn("KUBECONFIG=/shared/config", run_bash(body_for(False)).stdout)


if __name__ == "__main__":
    unittest.main()
