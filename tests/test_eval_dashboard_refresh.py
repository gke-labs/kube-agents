"""hack/ci-dashboard-refresh.sh: the periodic's collect->render->publish run.

Unlike the publish hook in hack/ci-eval-pr.sh (tested by
scripts/test_eval_dashboard_publish.py), whose contract is "never change the
eval job's exit code", this script IS the job and a red run is the freshness
alert -- so past the dormancy and trust gates it must FAIL LOUD. These tests
run the real script end to end: the local-target tests execute the actual
collect -> zero-runs floor -> render -> publish pipeline against the real
fixtures under scripts/eval_dashboard/testdata/, and nothing here ever
touches a bucket (a stub gsutil on PATH proves the gs:// wiring instead).
"""

import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-dashboard-refresh.sh"
TESTDATA = REPO_ROOT / "scripts" / "eval_dashboard" / "testdata"
SKIP = "eval-dashboard refresh skipped:"

# Prow-ish variables that must not leak from the environment running the
# tests into the environment the script sees.
_SCRUB = (
    "EVAL_DASHBOARD_TARGET",
    "EVAL_DASHBOARD_PR_GLOB",
    "EVAL_DASHBOARD_SINCE_DAYS",
    "EVAL_DASHBOARD_TIMEOUT",
    "EVAL_DASHBOARD_FROM_DIR",
    "JOB_TYPE",
    "PULL_NUMBER",
    "ARTIFACTS",
)


def run_script(env=None, path_prepend=None) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    full_env.update(env or {})
    if path_prepend:
        full_env["PATH"] = f"{path_prepend}{os.pathsep}{full_env['PATH']}"
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def write_stub(directory: pathlib.Path, name: str, body: str) -> None:
    stub = directory / name
    stub.write_text("#!/usr/bin/env bash\n" + body)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class RefreshScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = self.tmp / "published"
        self.target.mkdir()

    # ── Dormancy and trust gates: exit 0, one line, no side effects ────────

    def test_unset_target_is_dormant(self):
        proc = run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"{SKIP} EVAL_DASHBOARD_TARGET is not set", proc.stdout)

    def test_a_pull_number_never_writes_the_dashboard(self):
        proc = run_script(
            env={
                "EVAL_DASHBOARD_TARGET": str(self.target),
                "EVAL_DASHBOARD_FROM_DIR": str(TESTDATA),
                "PULL_NUMBER": "1234",
                "JOB_TYPE": "periodic",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"{SKIP} PULL_NUMBER=1234", proc.stdout)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_a_bucket_target_requires_a_main_branch_job_type(self):
        """gs:// + non-main JOB_TYPE skips BEFORE gsutil is ever invoked."""
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        write_stub(stubs, "gsutil", f'touch "{self.tmp}/gsutil-was-called"\nexit 1\n')
        for job_type in ("", "presubmit", "batch"):
            with self.subTest(job_type=job_type or "unset"):
                env = {"EVAL_DASHBOARD_TARGET": "gs://kube-agents-dashboards/evals/"}
                if job_type:
                    env["JOB_TYPE"] = job_type
                proc = run_script(env=env, path_prepend=str(stubs))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("may not write a bucket dashboard", proc.stdout)
        self.assertFalse((self.tmp / "gsutil-was-called").exists())

    def test_a_local_target_needs_no_job_type(self):
        """The offline path a laptop or these tests use crosses no boundary."""
        proc = run_script(
            env={
                "EVAL_DASHBOARD_TARGET": str(self.target),
                "EVAL_DASHBOARD_FROM_DIR": str(TESTDATA),
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    # ── The pipeline itself, against the real fixtures ─────────────────────

    def test_first_run_publishes_the_fixture_dashboard(self):
        proc = run_script(
            env={
                "EVAL_DASHBOARD_TARGET": str(self.target),
                "EVAL_DASHBOARD_FROM_DIR": str(TESTDATA),
                "JOB_TYPE": "periodic",
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("first run against this directory", proc.stdout)
        self.assertIn(f"eval-dashboard: refreshed {self.target}", proc.stdout)
        data = json.loads((self.target / "data.json").read_text())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(len(data["runs"]), 3)
        self.assertIn("<html", (self.target / "index.html").read_text().lower())

    def test_second_run_merges_with_the_published_prior(self):
        env = {
            "EVAL_DASHBOARD_TARGET": str(self.target),
            "EVAL_DASHBOARD_FROM_DIR": str(TESTDATA),
            "JOB_TYPE": "periodic",
        }
        self.assertEqual(run_script(env=env).returncode, 0)
        proc = run_script(env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("merged 3 prior runs", proc.stdout)
        data = json.loads((self.target / "data.json").read_text())
        self.assertEqual(len(data["runs"]), 3)  # deduped, not doubled

    def test_zero_collected_runs_fail_loud_and_publish_nothing(self):
        empty = self.tmp / "no-builds"
        empty.mkdir()
        proc = run_script(
            env={
                "EVAL_DASHBOARD_TARGET": str(self.target),
                "EVAL_DASHBOARD_FROM_DIR": str(empty),
                "JOB_TYPE": "periodic",
            }
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("refusing to publish an empty dashboard", proc.stdout)
        self.assertIn("ERROR: eval-dashboard refresh pipeline exited", proc.stderr)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_the_stage_log_rides_to_artifacts_on_failure(self):
        empty = self.tmp / "no-builds"
        empty.mkdir()
        artifacts = self.tmp / "artifacts"
        artifacts.mkdir()
        proc = run_script(
            env={
                "EVAL_DASHBOARD_TARGET": str(self.target),
                "EVAL_DASHBOARD_FROM_DIR": str(empty),
                "JOB_TYPE": "periodic",
                "ARTIFACTS": str(artifacts),
            }
        )
        self.assertNotEqual(proc.returncode, 0)
        log = artifacts / "eval-dashboard-refresh.log"
        self.assertIn("refusing to publish an empty dashboard", log.read_text())

    def test_a_failing_gsutil_source_hits_the_floor_not_the_bucket(self):
        """gs:// end to end with a gsutil that always fails: the prior
        download degrades to the first-run message, the sweep collects
        nothing, and the floor reds the run before publish."""
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        write_stub(stubs, "gsutil", "exit 1\n")
        artifacts = self.tmp / "artifacts"
        artifacts.mkdir()
        proc = run_script(
            env={
                "EVAL_DASHBOARD_TARGET": "gs://fake-dashboards/evals/",
                "JOB_TYPE": "periodic",
                "ARTIFACTS": str(artifacts),
            },
            path_prepend=str(stubs),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("first armed run, or a transient read failure", proc.stdout)
        self.assertIn("refusing to publish an empty dashboard", proc.stdout)
        self.assertIn(
            "gsutil ls failed", (artifacts / "eval-dashboard-refresh.log").read_text()
        )

    @unittest.skipUnless(shutil.which("timeout"), "needs coreutils timeout")
    def test_a_hung_pipeline_times_out_red(self):
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        write_stub(stubs, "python3", "sleep 60\n")
        proc = run_script(
            env={
                "EVAL_DASHBOARD_TARGET": str(self.target),
                "EVAL_DASHBOARD_FROM_DIR": str(TESTDATA),
                "JOB_TYPE": "periodic",
                "EVAL_DASHBOARD_TIMEOUT": "1",
            },
            path_prepend=str(stubs),
        )
        self.assertEqual(proc.returncode, 124)
        self.assertIn("timed out after 1s", proc.stderr)


if __name__ == "__main__":
    unittest.main()
