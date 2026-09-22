"""The eval job's EXIT trap must reach the artifact dumper on a RED run.

`hack/ci-eval-pr.sh` runs under `set -euo pipefail`, and errexit stays in force
inside an EXIT trap. The trap sets `$?` for the dumper by running
`(exit "${exit_code}")` -- which, with errexit live, aborts the trap on that
very line and skips the dumper entirely. The failure is silent and it only
happens on red runs, which are exactly the runs whose kubectl logs, pod
descriptions and events someone needs.

This test runs the real function out of the real file rather than grepping it
for `set +e`, so it fails if the guard is removed OR if a later edit
reintroduces an errexit-fatal command ahead of the dumper.
"""

import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from eval_dashboard import collect  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
TRAP = "profile_and_dump_on_exit"


def trap_body() -> str:
    """The trap function as written, lifted from the script."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{TRAP}\(\) \{{\n.*?^\}}$", src, re.S | re.M)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{TRAP}() not found in {SCRIPT}")
    return match.group(0)


def run_trap(exit_code: int) -> subprocess.CompletedProcess:
    """Exit a `set -euo pipefail` shell with `exit_code`, trap installed.

    The three functions the trap calls are stubbed: this is a test of control
    flow through the trap, not of what the real callees do.
    """
    script = "\n".join(
        [
            "set -euo pipefail",
            "collect_bench_results() { echo 'called collect_bench_results'; }",
            "report_partial_verdict() { echo 'called report_partial_verdict'; }",
            "profile_report() { echo \"called profile_report $1\"; }",
            "dump_prow_artifacts_on_failure() { echo \"called dumper with $?\"; }",
            trap_body(),
            f"trap {TRAP} EXIT",
            f"exit {exit_code}",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


class ExitTrapTest(unittest.TestCase):
    def test_the_dumper_runs_on_a_failing_exit(self):
        result = run_trap(7)
        self.assertIn("called dumper with 7", result.stdout)

    def test_the_original_exit_code_survives_the_trap(self):
        """Prow reads the job's status; the trap must not launder it to 0."""
        self.assertEqual(run_trap(7).returncode, 7)
        self.assertEqual(run_trap(0).returncode, 0)

    def test_results_are_collected_on_a_green_exit_too(self):
        """The reason the trap was widened: the store is fed by PASSING runs."""
        result = run_trap(0)
        self.assertIn("called collect_bench_results", result.stdout)
        self.assertIn("called dumper with 0", result.stdout)


class EvalLifetimeHeartbeatTest(unittest.TestCase):
    """The eval keeps its own Boskos heartbeat for the whole step (#1491).

    The Prow wrapper's `boskosctl heartbeat` stops after boskosctl's default
    5h --timeout and the first full nightly lost its lease at 05:01Z of a
    05:57Z run. The script runs hack/boskos_heartbeat.sh itself, after the
    traps are installed (so the trap can always kill it), disowned (so the
    fan-out's `jobs -rp` lane count and the final `wait` never see it), and
    the trap kills it last, so the lease is beaten through the trap's own
    tail and the daemon is still gone minutes before the wrapper's release.
    """

    src = SCRIPT.read_text(encoding="utf-8")

    def test_the_daemon_starts_after_the_traps_and_is_disowned(self):
        trap_install = self.src.index(f"trap {TRAP} EXIT")
        start = re.search(
            r'^"\$\{SCRIPT_DIR\}/boskos_heartbeat\.sh" &\n'
            r'EVAL_HEARTBEAT_PID=\$!\n'
            r'(?:#[^\n]*\n)*'
            r'disown "\$\{EVAL_HEARTBEAT_PID\}" 2>/dev/null \|\| true$',
            self.src,
            re.M,
        )
        self.assertIsNotNone(start, "eval-lifetime heartbeat start not found")
        self.assertGreater(start.start(), trap_install,
                           "the daemon must start after the EXIT trap exists")
        fanout = self.src.index('$(jobs -rp | wc -l')  # the lane count itself
        self.assertLess(start.start(), fanout,
                        "the daemon must be running before the fan-out")

    def test_the_daemon_derives_the_wrapper_owner_convention(self):
        block = self.src[self.src.index("Boskos lease heartbeat for the eval"):
                         self.src.index("EVAL_HEARTBEAT_PID=$!")]
        self.assertIn('BOSKOS_OWNER_NAME="${BOSKOS_OWNER_NAME:-${JOB_NAME}-${BUILD_ID}}"', block)
        self.assertIn('BOSKOS_RESOURCE_NAME="${BOSKOS_RESOURCE_NAME:-${PROJECT_ID}}"', block)
        self.assertIn('http://boskos.boskos.svc.cluster.local', block)

    def test_the_trap_kills_the_daemon_after_everything_slow(self):
        # The trap's tail (result collection, the artifact dump on a red run,
        # the dashboard publish) is not bounded by the reaper window, and on
        # a run past boskosctl's 5h --timeout this daemon is the only thing
        # beating; killing it first would reopen the gap it closes.
        body = trap_body()
        kill = body.index('kill "${EVAL_HEARTBEAT_PID:-}"')
        self.assertGreater(kill, body.index("publish_eval_dashboard"))
        # An unset PID (every run outside Prow, and the lifted-trap tests
        # above) must not turn into a failure of the trap.
        self.assertEqual(run_trap(0).returncode, 0)
        self.assertIn("called dumper with 7", run_trap(7).stdout)

    def test_collection_precedes_the_profile_and_the_dump(self):
        out = run_trap(7).stdout
        self.assertLess(
            out.index("called collect_bench_results"), out.index("called profile_report")
        )
        self.assertLess(out.index("called profile_report"), out.index("called dumper"))

    def test_the_cut_off_report_runs_before_anything_slow(self):
        """On a deadline kill the grace period is five minutes; the partial
        table goes first, ahead of the artifact dump and the dashboard."""
        out = run_trap(143).stdout
        self.assertLess(out.index("called collect_bench_results"), out.index("called report_partial_verdict"))
        self.assertLess(out.index("called report_partial_verdict"), out.index("called profile_report"))
        self.assertLess(out.index("called report_partial_verdict"), out.index("called dumper"))

    def test_the_regression_this_guards(self):
        """Without the errexit guard the dumper is unreachable on a red run.

        Asserted rather than described, so the comment in the script cannot
        drift away from being true.
        """
        unguarded = trap_body().replace("  set +e\n", "", 1)
        self.assertNotIn("set +e", unguarded)
        script = "\n".join(
            [
                "set -euo pipefail",
                "collect_bench_results() { :; }",
                "profile_report() { :; }",
                "dump_prow_artifacts_on_failure() { echo 'called dumper'; }",
                unguarded,
                f"trap {TRAP} EXIT",
                "exit 7",
            ]
        )
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, check=False
        )
        self.assertNotIn("called dumper", result.stdout)


class CutOffReportTest(unittest.TestCase):
    """report_partial_verdict tables the cases graded before a deadline kill.

    It runs `bench-gate suite --partial` over the cases the fan-out marked
    `.graded`, prints a line that names how many were graded and recorded,
    and stays quiet when the run wrote its own verdict, never got as far as
    the fan-out, or graded nothing. The line is deliberately not a verdict
    line: the collector must keep `eval_verdict` null so the night reads as
    truncated -- now with its graded cases counted.
    """

    def lifted(self) -> str:
        src = SCRIPT.read_text(encoding="utf-8")
        match = re.search(r"^report_partial_verdict\(\) \{\n.*?^\}$", src, re.S | re.M)
        if match is None:  # pragma: no cover
            raise AssertionError(f"report_partial_verdict() not found in {SCRIPT}")
        return match.group(0)

    def run_report(self, setup: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
        tmp = pathlib.Path(tempfile.mkdtemp())
        capture = tmp / "uv-args"
        script = "\n".join(
            [
                "set -uo pipefail",
                self.lifted(),
                # The fake bench-gate: records its arguments and writes the table.
                f'uv() {{ printf "%s\\n" "$@" > "{capture}"; echo "| table |"; : > "${{ARTIFACT_DIR}}/eval-verdict.md"; }}',
                f'BENCH_DIR="{tmp}"',
                f'STATE_DIR="{tmp}/state"; mkdir -p "${{STATE_DIR}}"',
                f'ARTIFACT_DIR="{tmp}/artifacts"; mkdir -p "${{ARTIFACT_DIR}}"',
                "TASK_NAMES=(case-a case-b case-c)",
                f'EVAL_RECORDED_MANIFEST="{tmp}/artifacts/baseline-recorded.jsonl"',
                setup,
                "report_partial_verdict",
            ]
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
        return result, capture

    GRADED_ONE = "\n".join(
        [
            ': > "${STATE_DIR}/case-a.graded"; echo "{}" > "${ARTIFACT_DIR}/case-case-a.json"',
            # Marked graded but its JSON never landed: not a graded case.
            ': > "${STATE_DIR}/case-b.graded"',
            'echo \'{"case": "case-a"}\' > "${EVAL_RECORDED_MANIFEST}"',
        ]
    )

    def test_it_tables_the_graded_cases_and_says_how_many(self):
        result, capture = self.run_report(self.GRADED_ONE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "Eval ended before its verdict: 1 of 3 cases graded, 1 recorded to the baseline store; partial table in",
            result.stdout,
        )
        args = capture.read_text().splitlines()
        self.assertEqual(args[:2], ["run", "bench-gate"])
        self.assertEqual(args[2], "suite")
        self.assertEqual(args.count("--case-result"), 1)
        self.assertTrue(args[args.index("--case-result") + 1].endswith("/case-case-a.json"))
        self.assertIn("1 of 3 cases had every repetition graded by then", args[args.index("--partial") + 1])
        self.assertTrue(args[args.index("--markdown-out") + 1].endswith("/eval-verdict.md"))
        self.assertTrue(args[args.index("--json-out") + 1].endswith("/eval-verdict.json"))
        # The suite's own stdout (the markdown) stays out of the job log.
        self.assertNotIn("| table |", result.stdout)

    def test_the_line_is_not_a_verdict_line(self):
        result, _ = self.run_report(self.GRADED_ONE)
        line = [l for l in result.stdout.splitlines() if "Eval ended before its verdict" in l][0]
        self.assertIsNone(collect.parse_build_log(line)["eval_verdict"])
        self.assertTrue(line.startswith("==="), "a header, so it closes any open grading block")

    def test_it_stays_quiet_when_the_run_wrote_its_own_verdict(self):
        result, capture = self.run_report(self.GRADED_ONE + "\nEVAL_SUITE_REACHED=1")
        self.assertEqual(result.stdout, "")
        self.assertFalse(capture.exists())

    def test_it_stays_quiet_before_the_fan_out_exists(self):
        result, capture = self.run_report("unset STATE_DIR")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(capture.exists())

    def test_nothing_graded_is_said_not_tabled(self):
        result, capture = self.run_report("")
        self.assertIn("Eval ended before its verdict with no case fully graded (3 in the matrix)", result.stdout)
        self.assertFalse(capture.exists())

    def test_a_failed_suite_is_a_warning_not_a_lost_line(self):
        result, _ = self.run_report(self.GRADED_ONE + "\nuv() { return 1; }")
        self.assertIn("WARNING: the partial verdict table could not be written", result.stdout)
        self.assertIn("1 of 3 cases graded", result.stdout)


if __name__ == "__main__":
    unittest.main()
