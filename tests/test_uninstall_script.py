"""Unit tests for uninstall.sh's resolve_state_location decision and its
--source-ref dispatch.

The four-branch decision of where the install's Terraform state lives is the
safety gate of the whole teardown: pinning the GCS backend when the state is
actually local makes `terraform init -reconfigure` abandon that local state,
so the destroy plans nothing and reports success with the CR and backups
already gone and every GCP resource still live. Each branch is asserted here
because no other automated path reaches them — the installer matrix's
uninstall leg exits at the --dry-run gate first.

The --source-ref dispatch is the recovery path for installs made before the
Terraform engine: the pinned release's own uninstall.sh must be run in place
of this one, because this script's engine (installer_common.sh, lifecycle.sh)
exists at no pre-Terraform ref. The dispatch tests pin that hand-over: the
cloned release's script receives the caller's flags, never --source-ref
itself, and a ref with no uninstall.sh is refused rather than driven with an
engine it does not carry.
"""

import pathlib
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UNINSTALL_SH = _REPO_ROOT / "uninstall.sh"
_INSTALLER_COMMON = _REPO_ROOT / "k8s-operator" / "scripts" / "installer_common.sh"


class ResolveStateLocationTest(unittest.TestCase):
    def _run(self, remote_state_exists, env=None, compose_files=()):
        """Run resolve_state_location against a stub gcloud and a temp compose dir.

        `remote_state_exists` drives the stub's `storage cat` exit code —
        uninstall.sh probes only existence, never content. `compose_files`
        are created empty in the temp composition directory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            compose_dir = pathlib.Path(tmp) / "full-install"
            compose_dir.mkdir()
            for name in compose_files:
                (compose_dir / name).touch()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"storage cat"*) exit {0 if remote_state_exists else 1} ;;\n'
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UNINSTALL_SH}"
source "{_INSTALLER_COMMON}"
rc=0
resolve_state_location "{compose_dir}" || rc=$?
echo "rc=$rc bucket=${{KUBE_AGENTS_STATE_BUCKET:-<unset>}}"
"""
            full_env = get_isolated_test_env(
                overrides={
                    "PROJECT_ID": "test-project",
                    "CLUSTER_NAME": "test-cluster",
                    "REGION": "us-central1",
                    **(env or {}),
                },
                bin_dir=str(bin_dir),
            )
            return subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    def test_remote_state_pins_the_backend(self):
        proc = self._run(remote_state_exists=True)
        self.assertIn("rc=0 bucket=auto", proc.stdout, proc.stderr)

    def test_remote_state_keeps_an_explicit_bucket(self):
        proc = self._run(
            remote_state_exists=True,
            env={"KUBE_AGENTS_STATE_BUCKET": "my-bucket"},
        )
        self.assertIn("rc=0 bucket=my-bucket", proc.stdout, proc.stderr)

    def test_explicit_bucket_with_no_state_is_an_error(self):
        # An explicitly named bucket holding no state for this cluster must
        # refuse, not fall back to guessing another location.
        proc = self._run(
            remote_state_exists=False,
            env={"KUBE_AGENTS_STATE_BUCKET": "my-bucket"},
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("set explicitly", proc.stdout)

    def test_local_tfstate_leaves_the_backend_unpinned(self):
        # A hand-driven install's local state: pinning the backend here is
        # the destroy-plans-nothing failure the decision exists to prevent.
        proc = self._run(
            remote_state_exists=False, compose_files=("terraform.tfstate",)
        )
        self.assertIn("rc=0 bucket=<unset>", proc.stdout, proc.stderr)

    def test_backend_override_leaves_the_backend_unpinned(self):
        proc = self._run(
            remote_state_exists=False, compose_files=("backend_override.tf",)
        )
        self.assertIn("rc=0 bucket=<unset>", proc.stdout, proc.stderr)

    def test_no_state_anywhere_refuses_and_names_source_ref(self):
        # Also the transient-failure case: a gcloud that cannot read the
        # object is indistinguishable from no state, and the safe answer to
        # both is a refusal that names the recovery path, never a destroy.
        proc = self._run(remote_state_exists=False)
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("--source-ref", proc.stdout)


class SourceRefDispatchTest(unittest.TestCase):
    def _run(self, ref_carries_uninstall, args):
        """Run the real uninstall.sh with a stub git on PATH.

        The stub's `clone` creates the target directory and, when
        `ref_carries_uninstall`, drops an uninstall.sh into it that records
        its argv to DISPATCH_LOG — standing in for the pinned release's own
        uninstaller. fetch/checkout are no-ops.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            dispatch_log = pathlib.Path(tmp) / "dispatch.log"
            git = bin_dir / "git"
            git.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "clone" ]; then\n'
                '  dest="${@: -1}"\n'
                '  mkdir -p "$dest"\n'
                f'  if [ "{str(ref_carries_uninstall).lower()}" = "true" ]; then\n'
                "    {\n"
                "      echo '#!/usr/bin/env bash'\n"
                "      echo 'printf \"%s\\n\" \"$@\" > \"$DISPATCH_LOG\"'\n"
                '    } > "$dest/uninstall.sh"\n'
                "  fi\n"
                "fi\n"
                "exit 0\n"
            )
            git.chmod(git.stat().st_mode | stat.S_IEXEC)
            full_env = get_isolated_test_env(
                overrides={"DISPATCH_LOG": str(dispatch_log)},
                bin_dir=str(bin_dir),
            )
            proc = subprocess.run(
                ["bash", str(_UNINSTALL_SH), *args],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=tmp,  # outside the checkout, so only --source-ref decides
            )
            log = dispatch_log.read_text() if dispatch_log.exists() else None
            return proc, log

    def test_source_ref_hands_over_to_the_cloned_uninstaller(self):
        proc, log = self._run(
            ref_carries_uninstall=True,
            args=[
                "--source-ref=v0.9.0",
                "--non-interactive",
                "--project-id=p1",
                "--cluster-name=c1",
                "--region=r1",
            ],
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(log, proc.stdout + proc.stderr)
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--project-id=p1",
                "--cluster-name=c1",
                "--region=r1",
            ],
        )

    def test_source_ref_without_an_uninstaller_refuses(self):
        # Driving a ref that carries no uninstall.sh with this script's own
        # engine is exactly the failure the dispatch exists to prevent, so
        # the answer is a refusal, not a fallback.
        proc, log = self._run(
            ref_carries_uninstall=False,
            args=["--source-ref=v0.0.1", "--non-interactive"],
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("carries no uninstall.sh", proc.stdout)
        self.assertIsNone(log)


if __name__ == "__main__":
    unittest.main()
