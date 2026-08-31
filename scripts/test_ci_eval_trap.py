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
