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
import unittest

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
            "collect_gateway_log() { echo 'called collect_gateway_log'; }",
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

    def test_the_gateway_log_is_collected_on_a_green_exit_too(self):
        """A passing nightly whose repetitions ran to the delegation ceiling
        used to leave no gateway log; the capture is on every exit now, and
        ahead of the dumper, which takes the same capture again on a red run."""
        out = run_trap(0).stdout
        self.assertIn("called collect_gateway_log", out)
        self.assertLess(out.index("called collect_gateway_log"), out.index("called dumper"))
        self.assertIn("called collect_gateway_log", run_trap(7).stdout)


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
                "collect_gateway_log() { :; }",
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


if __name__ == "__main__":
    unittest.main()
