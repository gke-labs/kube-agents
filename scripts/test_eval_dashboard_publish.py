"""The eval-dashboard publish hook must NEVER change the eval job's exit code.

`publish_eval_dashboard` in hack/ci-eval-pr.sh runs from the script's EXIT
trap on every run, red or green. The dashboard observes the presubmit; a
dashboard failure that reds the presubmit -- or launders a red to green --
would break the thing it observes. So the contract is: any failure mode
(EVAL_DASHBOARD_TARGET unset, scripts/eval_dashboard/ absent because the
sibling PRs have not merged, a crashing collector, a broken publisher) prints
exactly one "eval-dashboard publish skipped: <reason>" line and leaves the
job's exit code exactly what it was.

A second contract rides on the first: ONLY MAIN-BRANCH RUNS PUBLISH. The
presubmit runs branch-authored code, so a publish from one would let any pull
request rewrite the dashboard everyone reads; the hook re-derives the baseline
recorder's gate (JOB_TYPE postsubmit/periodic, no PULL_NUMBER) and anything
else is one more loud skip.

Like scripts/test_ci_eval_trap.py, this runs the real function -- and the real
trap body around it -- lifted out of the real file, rather than grepping for
guards, so it fails if the fail-safe is weakened by any future edit.
"""

import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
SKIP = "eval-dashboard publish skipped:"


def lifted(name: str) -> str:
    """A function as written, lifted from the script."""
    src = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", src, re.S | re.M)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{name}() not found in {SCRIPT}")
    return match.group(0)


def run_hook(
    exit_code: int,
    script_dir: pathlib.Path,
    target: str,
    job_type: str = "periodic",
    pull_number: str = "",
    artifacts: str = "",
    timeout_s: str = "",
) -> subprocess.CompletedProcess:
    """Exit a `set -euo pipefail` shell with `exit_code`, real trap installed.

    The trap body is the script's own (its other three callees stubbed, as in
    test_ci_eval_trap.py); publish_eval_dashboard is the real thing, pointed
    at `script_dir` so a test controls whether collect.py exists and what the
    pipeline stubs do. Defaults to a main-branch shape (periodic, no
    PULL_NUMBER) because everything else is gated off before it can publish.
    """
    script = "\n".join(
        [
            "set -euo pipefail",
            f'SCRIPT_DIR="{script_dir}"',
            f'export JOB_TYPE="{job_type}"',
            f'export PULL_NUMBER="{pull_number}"',
            # Always pinned: the suite itself runs under Prow, where a real
            # $ARTIFACTS is set, and the hook copies its log there.
            f'export ARTIFACTS="{artifacts}"',
            f'export EVAL_DASHBOARD_TIMEOUT="{timeout_s}"',
            f'export EVAL_DASHBOARD_TARGET="{target}"',
            "collect_bench_results() { :; }",
            "profile_report() { :; }",
            "dump_prow_artifacts_on_failure() { echo \"called dumper with $?\"; }",
            lifted("publish_eval_dashboard"),
            lifted("profile_and_dump_on_exit"),
            "trap profile_and_dump_on_exit EXIT",
            f"exit {exit_code}",
        ]
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def dashboard_stubs(root: pathlib.Path, collect_body: str) -> pathlib.Path:
    """A fake repo layout: hack/ beside scripts/eval_dashboard/ stubs.

    Returns the fake SCRIPT_DIR. render.py and publish.py record that they
    ran; collect.py's behaviour is the test's to choose.
    """
    (root / "hack").mkdir()
    dash = root / "scripts" / "eval_dashboard"
    dash.mkdir(parents=True)
    (dash / "collect.py").write_text(textwrap.dedent(collect_body))
    for name in ("render.py", "publish.py"):
        (dash / name).write_text(
            f"import pathlib, sys\n"
            f"pathlib.Path(sys.path[0], '{name}.ran').touch()\n"
        )
    return root / "hack"


class PublishHookFailSafeTest(unittest.TestCase):
    def assert_skipped_once(self, result, exit_code: int, reason_fragment: str):
        """One skip line naming the reason, dumper reached, exit code intact."""
        self.assertEqual(result.returncode, exit_code, result.stderr)
        self.assertEqual(result.stdout.count(SKIP), 1, result.stdout)
        skip_line = next(l for l in result.stdout.splitlines() if SKIP in l)
        self.assertIn(reason_fragment, skip_line)
        self.assertIn(f"called dumper with {exit_code}", result.stdout)

    def test_unset_target_skips_and_preserves_the_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            for code in (0, 7):
                result = run_hook(code, pathlib.Path(tmp), target="")
                self.assert_skipped_once(result, code, "EVAL_DASHBOARD_TARGET")

    def test_a_presubmit_never_publishes_even_fully_armed(self):
        """The trust boundary: a presubmit runs branch-authored code, so the
        hook skips even with the target set and a working pipeline on disk --
        nothing in scripts/eval_dashboard/ executes at all."""
        marker = "import pathlib, sys\npathlib.Path(sys.path[0], 'collect.py.ran').touch()\n"
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(pathlib.Path(tmp), marker)
            for code in (0, 7):
                result = run_hook(
                    code,
                    fake_hack,
                    target="gs://kube-agents-dashboards/evals/",
                    job_type="presubmit",
                    pull_number="1043",
                )
                self.assert_skipped_once(result, code, "not a main-branch run")
            dash = pathlib.Path(tmp) / "scripts" / "eval_dashboard"
            self.assertEqual(list(dash.glob("*.ran")), [])

    def test_an_unset_job_type_never_publishes(self):
        """No JOB_TYPE means no proof this is main: skip, do not publish."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(pathlib.Path(tmp), "pass\n")
            for code in (0, 7):
                result = run_hook(
                    code,
                    fake_hack,
                    target="gs://kube-agents-dashboards/evals/",
                    job_type="",
                )
                self.assert_skipped_once(result, code, "not a main-branch run")

    def test_a_pull_number_never_publishes_whatever_job_type_claims(self):
        """PULL_NUMBER present means pull-request content, as for the
        baseline recorder; the job type label alone is not trusted."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(pathlib.Path(tmp), "pass\n")
            for code in (0, 7):
                result = run_hook(
                    code,
                    fake_hack,
                    target="gs://kube-agents-dashboards/evals/",
                    job_type="periodic",
                    pull_number="1043",
                )
                self.assert_skipped_once(result, code, "PULL_NUMBER=1043")

    def test_missing_collect_py_skips_fast_and_preserves_the_exit_code(self):
        """This PR may merge before the siblings that add scripts/eval_dashboard/."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = pathlib.Path(tmp) / "hack"
            fake_hack.mkdir()
            for code in (0, 7):
                result = run_hook(
                    code, fake_hack, target="gs://kube-agents-dashboards/evals/"
                )
                self.assert_skipped_once(result, code, "does not exist")

    def test_partial_siblings_skip_before_any_stage_runs(self):
        """collect.py merged first (#1044): with render.py/publish.py still
        absent the guard must skip CHEAPLY -- the collector's GCS sweep must
        not run just to die at the missing render stage."""
        marker = (
            "import pathlib, sys\n"
            "pathlib.Path(sys.path[0], 'collect.py.ran').touch()\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "hack").mkdir()
            dash = root / "scripts" / "eval_dashboard"
            dash.mkdir(parents=True)
            (dash / "collect.py").write_text(marker)
            for code in (0, 7):
                result = run_hook(
                    code,
                    root / "hack",
                    target="gs://kube-agents-dashboards/evals/",
                )
                self.assert_skipped_once(result, code, "render.py does not exist")
            self.assertEqual(list(dash.glob("*.ran")), [])

    def test_a_crashing_collector_skips_and_preserves_the_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(
                pathlib.Path(tmp), "import sys; sys.exit(3)\n"
            )
            for code in (0, 7):
                result = run_hook(
                    code, fake_hack, target="gs://kube-agents-dashboards/evals/"
                )
                self.assert_skipped_once(result, code, "pipeline exited 3")

    def test_a_working_pipeline_publishes_and_preserves_the_exit_code(self):
        """The hook runs on green AND red: a red run is still a data point."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(
                pathlib.Path(tmp),
                """\
                import json, pathlib, sys
                out = sys.argv[sys.argv.index("--out") + 1]
                pathlib.Path(out).write_text(json.dumps({"runs": [{"build": "1"}]}))
                """,
            )
            for code in (0, 7):
                result = run_hook(
                    code, fake_hack, target="gs://kube-agents-dashboards/evals/"
                )
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertIn(
                    "eval-dashboard: published to gs://kube-agents-dashboards/evals/",
                    result.stdout,
                )
                self.assertNotIn(SKIP, result.stdout)

    def test_zero_collected_runs_skip_instead_of_publishing_empty(self):
        """The evidence_store lesson: an unreadable source is not an empty
        one. collect.py warns-and-continues on gsutil failures and still
        exits 0 with runs: [], so without the floor a 403 on the sweep would
        overwrite a good dashboard with an empty one and log success."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(
                pathlib.Path(tmp),
                """\
                import json, pathlib, sys
                out = sys.argv[sys.argv.index("--out") + 1]
                pathlib.Path(out).write_text(json.dumps({"runs": []}))
                """,
            )
            for code in (0, 7):
                result = run_hook(
                    code, fake_hack, target="gs://kube-agents-dashboards/evals/"
                )
                self.assert_skipped_once(result, code, "collected zero runs")
            dash = pathlib.Path(tmp) / "scripts" / "eval_dashboard"
            self.assertEqual(list(dash.glob("*.ran")), [])

    def test_the_pipeline_log_rides_to_artifacts_on_failure_and_success(self):
        """rm -rf must not eat the only evidence: with $ARTIFACTS present the
        publish log survives as eval-dashboard-publish.log, on the skip path
        and the publish path both."""
        collect_ok = """\
            import json, pathlib, sys
            out = sys.argv[sys.argv.index("--out") + 1]
            pathlib.Path(out).write_text(json.dumps({"runs": [{"build": "1"}]}))
            """
        for body in ("import sys; sys.exit(3)\n", collect_ok):
            with tempfile.TemporaryDirectory() as tmp:
                fake_hack = dashboard_stubs(pathlib.Path(tmp), body)
                artifacts = pathlib.Path(tmp) / "artifacts"
                artifacts.mkdir()
                run_hook(
                    7,
                    fake_hack,
                    target="gs://kube-agents-dashboards/evals/",
                    artifacts=str(artifacts),
                )
                log = artifacts / "eval-dashboard-publish.log"
                self.assertTrue(log.is_file(), list(artifacts.iterdir()))

    def test_a_hung_pipeline_times_out_into_the_skip_line(self):
        """The timeout path for real, not a stub of it: a collector that
        sleeps past the budget becomes one skip line naming 124 and the
        budget, exit code intact. EVAL_DASHBOARD_TIMEOUT is honoured so the
        job config can size the budget to the sweep without a code change."""
        if subprocess.run(
            ["bash", "-c", "command -v timeout"], capture_output=True, check=False
        ).returncode:
            self.skipTest("no `timeout` binary; the hook degrades to unbounded")
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(
                pathlib.Path(tmp), "import time; time.sleep(30)\n"
            )
            result = run_hook(
                7,
                fake_hack,
                target="gs://kube-agents-dashboards/evals/",
                timeout_s="1",
            )
            self.assert_skipped_once(result, 7, "exited 124 (124 means the 1s timeout)")

    def test_the_pipeline_stops_at_the_first_failing_stage(self):
        """collect crashing means render/publish never run -- errexit lives
        inside the child pipeline, and only there."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(
                pathlib.Path(tmp), "import sys; sys.exit(3)\n"
            )
            run_hook(7, fake_hack, target="gs://x/")
            dash = pathlib.Path(tmp) / "scripts" / "eval_dashboard"
            self.assertEqual(list(dash.glob("*.ran")), [])

    def test_the_hook_survives_errexit_without_the_traps_guard(self):
        """The function is errexit-safe in its own right: even called under
        live `set -e` (not just after the trap's `set +e`), a crashing
        pipeline neither aborts the caller nor changes its exit."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_hack = dashboard_stubs(
                pathlib.Path(tmp), "import sys; sys.exit(3)\n"
            )
            script = "\n".join(
                [
                    "set -euo pipefail",
                    f'SCRIPT_DIR="{fake_hack}"',
                    'export JOB_TYPE="periodic" PULL_NUMBER="" ARTIFACTS=""',
                    'export EVAL_DASHBOARD_TARGET="gs://x/"',
                    lifted("publish_eval_dashboard"),
                    "publish_eval_dashboard",
                    "echo reached-after-hook",
                ]
            )
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, check=False
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("reached-after-hook", result.stdout)
            self.assertEqual(result.stdout.count(SKIP), 1, result.stdout)


if __name__ == "__main__":
    unittest.main()
