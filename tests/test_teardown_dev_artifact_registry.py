"""The dev Artifact Registry teardown: a dry run changes nothing, and a refused delete is reported, not hidden."""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = pathlib.Path("scripts/dev/teardown_dev_01_gcp_artifact_registry.sh")
# The script sources ../installer/common.sh and keeps its state file beside it, so both trees
# travel together into a scratch root and the real checkout's vars.sh is never touched.
COPIED_TREES = ("scripts/installer", "scripts/dev")
COPIED_FILES = ("install.defaults.env",)
VARS_FILE = pathlib.Path("scripts/installer/vars.sh")
OWNERSHIP_CLEARED = "export DEV_ARTIFACT_REGISTRY_CREATED=false"
RUN_TIMEOUT_SECONDS = 60
STUB_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$GCLOUD_CALLS"
case "$*" in
  "artifacts repositories describe"*)
    case "${STUB_DESCRIBE:-found}" in
      found) echo "projects/p/locations/r/repositories/kube-agents" ;;
      not-found) echo "ERROR: (gcloud.artifacts.repositories.describe) NOT_FOUND: Requested entity was not found." >&2; exit 1 ;;
      *) echo "ERROR: (gcloud.artifacts.repositories.describe) PERMISSION_DENIED: Permission denied on resource" >&2; exit 1 ;;
    esac ;;
  "artifacts repositories delete"*)
    if [ "${STUB_DELETE_EXIT:-0}" -ne 0 ]; then
      echo "ERROR: (gcloud.artifacts.repositories.delete) PERMISSION_DENIED" >&2
      exit "$STUB_DELETE_EXIT"
    fi ;;
esac
exit 0
"""


class TeardownDevArtifactRegistryTest(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="teardown-dev-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for tree in COPIED_TREES:
            shutil.copytree(REPO / tree, self.root / tree)
        for name in COPIED_FILES:
            shutil.copy(REPO / name, self.root / name)
        (self.root / VARS_FILE).unlink(missing_ok=True)
        stub_bin = self.root / "bin"
        stub_bin.mkdir()
        gcloud = stub_bin / "gcloud"
        gcloud.write_text(STUB_GCLOUD)
        gcloud.chmod(0o755)
        self.calls = self.root / "gcloud-calls.log"
        self.env = {
            **{k: v for k, v in os.environ.items() if k in ("TMPDIR", "LANG", "LC_ALL")},
            "PATH": f"{stub_bin}:/usr/bin:/bin",
            "HOME": str(self.root),
            "PROJECT_ID": "dev-project",
            "REGION": "us-east4",
            "GCLOUD_CALLS": str(self.calls),
        }

    def run_script(self, *args, delete_exit=0, describe="found"):
        env = {**self.env, "STUB_DELETE_EXIT": str(delete_exit), "STUB_DESCRIBE": describe}
        return subprocess.run(
            ["bash", str(self.root / SCRIPT), *args],
            cwd=self.root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
        )

    def gcloud_calls(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def vars_text(self):
        path = self.root / VARS_FILE
        return path.read_text() if path.exists() else ""

    def test_a_dry_run_runs_no_gcloud_write_and_leaves_the_state_alone(self):
        proc = self.run_script("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("[DRY-RUN] Would delete", proc.stdout)
        self.assertEqual(self.gcloud_calls(), [], "a dry run called gcloud")
        self.assertNotIn(OWNERSHIP_CLEARED, self.vars_text())

    def test_a_refused_delete_fails_the_script_and_keeps_the_ownership_flag(self):
        proc = self.run_script("-y", delete_exit=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("successfully deleted", proc.stdout)
        self.assertIn("Could not delete Artifact Registry repository", proc.stdout + proc.stderr)
        self.assertNotIn(OWNERSHIP_CLEARED, self.vars_text())

    def test_a_probe_that_fails_for_any_reason_but_not_found_stops_the_script(self):
        proc = self.run_script("-y", describe="denied")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("PERMISSION_DENIED", proc.stdout + proc.stderr)
        self.assertNotIn("already deleted", proc.stdout)
        self.assertNotIn("successfully deleted", proc.stdout)
        self.assertNotIn(OWNERSHIP_CLEARED, self.vars_text())

    def test_a_repository_that_is_not_found_reads_as_already_deleted(self):
        proc = self.run_script("-y", describe="not-found")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("already deleted or does not exist", proc.stdout)
        self.assertIn(OWNERSHIP_CLEARED, self.vars_text())

    def test_a_delete_that_succeeds_clears_the_ownership_flag(self):
        proc = self.run_script("-y")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("successfully deleted", proc.stdout)
        self.assertIn(OWNERSHIP_CLEARED, self.vars_text())


if __name__ == "__main__":
    unittest.main()
