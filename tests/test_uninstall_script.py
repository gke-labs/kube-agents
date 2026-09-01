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
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import create_minimal_tools_bin, get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UNINSTALL_SH = _REPO_ROOT / "uninstall.sh"
_INSTALLER_COMMON = _REPO_ROOT / "k8s-operator" / "scripts" / "installer_common.sh"


class ResolveStateLocationTest(unittest.TestCase):
    # What a real `gcloud storage cat` says for each case. The probe reads the
    # message, not just the exit code, so a stub that only exits 1 would assert
    # nothing about the distinction the function exists to draw.
    PROBE_ABSENT = (
        "ERROR: (gcloud.storage.cat) The following URLs matched no objects or files: "
        "gs://test-project-kube-agents-tfstate/kube-agents/test-cluster/default.tfstate"
    )
    PROBE_DENIED = (
        "ERROR: (gcloud.storage.cat) HTTPError 403: caller does not have "
        "storage.objects.get access to the Google Cloud Storage object."
    )

    def _run(self, remote_state_exists, env=None, compose_files=(), probe_stderr=None):
        """Run resolve_state_location against a stub gcloud and a temp compose dir.

        `remote_state_exists` drives the stub's `storage cat` exit code.
        `probe_stderr` is what the stub writes to stderr when it fails,
        defaulting to a genuine not-found; pass PROBE_DENIED for the case where
        the object may well exist and the caller simply cannot read it.
        `compose_files` are created empty in the temp composition directory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            compose_dir = pathlib.Path(tmp) / "full-install"
            compose_dir.mkdir()
            for name in compose_files:
                (compose_dir / name).touch()
            failure_message = self.PROBE_ABSENT if probe_stderr is None else probe_stderr
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f'  *"storage cat"*)\n'
                f"      {'exit 0' if remote_state_exists else f'echo {shlex.quote(failure_message)} >&2; exit 1'} ;;\n"
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
                    # Neutralised, not inherited: get_isolated_test_env strips
                    # only GITHUB_*/RUNNER_*/CI/GH_TOKEN, and a maintainer with
                    # this exported reaches the explicitly-named-bucket branch
                    # in four of these cases. `env` still overrides it, which is
                    # how the two tests that want a bucket set one.
                    "KUBE_AGENTS_STATE_BUCKET": "",
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

    def test_an_unreadable_probe_is_not_reported_as_nothing_to_tear_down(self):
        # The failure this guards is specific: the RC pipeline's WIF principal
        # loses storage.objects.get, the probe fails, and a bare exit-code test
        # calls that "clean project" — so provision_environment.sh takes the
        # benign arm, raises no annotation, ignores RC_TEARDOWN_STRICT, and
        # installs over the live cluster. Anything that is not a clean absent
        # must be a failure.
        proc = self._run(remote_state_exists=False, probe_stderr=self.PROBE_DENIED)
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("Could not read the Terraform state", proc.stdout)
        self.assertNotIn("rc=3", proc.stdout)

    def test_an_unreadable_probe_outranks_local_state(self):
        # Falling through to the local-state branch on a permission error is
        # the same mistake one level down: it would pick up an unrelated
        # checkout's state and destroy against it.
        proc = self._run(
            remote_state_exists=False,
            probe_stderr=self.PROBE_DENIED,
            compose_files=("terraform.tfstate",),
        )
        self.assertIn("rc=1", proc.stdout, proc.stderr)
        self.assertIn("Could not read the Terraform state", proc.stdout)

    def test_no_state_anywhere_refuses_and_names_source_ref(self):
        # Also the transient-failure case: a gcloud that cannot read the
        # object is indistinguishable from no state, and the safe answer to
        # both is a refusal that names the recovery path, never a destroy.
        #
        # rc=3, not 1: this is the one non-zero exit that is not a failure, and
        # an automated caller (scripts/release/provision_environment.sh) has
        # to tell "nothing was installed" from "the teardown broke".
        proc = self._run(remote_state_exists=False)
        self.assertIn("rc=3", proc.stdout, proc.stderr)
        self.assertIn("--source-ref", proc.stdout)


class DiagnosticsTest(unittest.TestCase):
    """The two ways this teardown used to fail without saying anything."""

    def test_the_process_lock_does_not_silence_stderr(self):
        # `exec 200>"$LOCK_FILE" 2>/dev/null` applied BOTH redirections to the
        # shell permanently, so every later error message — this script's abort
        # banner, lifecycle.sh's, terraform's — went to /dev/null. The release
        # pipeline's teardown failed that way on every run, exiting non-zero
        # with an empty stderr. A stub flock is on PATH because the lock block
        # is skipped entirely where flock is absent (macOS), which would make
        # this pass without exercising anything.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            flock = bin_dir / "flock"
            flock.write_text("#!/usr/bin/env bash\nexit 0\n")
            flock.chmod(flock.stat().st_mode | stat.S_IEXEC)
            body = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UNINSTALL_SH}"
echo "stderr-survived-the-lock" >&2
"""
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertIn("stderr-survived-the-lock", proc.stderr, proc.stdout)

    def test_a_child_exiting_3_does_not_leak_the_reserved_code(self):
        # on_error exits with the FAILING COMMAND's status, so without
        # normalisation any child that exits 3 — a gcloud wrapper, a nested
        # script under lifecycle.sh — would speak the "nothing to tear down"
        # contract and tell provision_environment.sh to install over a live
        # environment.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            body = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UNINSTALL_SH}"
bash -c "exit 3"
"""
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        # The banner proves on_error actually ran and normalised. Without it
        # this passes vacuously whenever /tmp/kube-agents-uninstall.lock is
        # held: the lock branch also exits 1, having asserted nothing.
        self.assertIn("Teardown error encountered", proc.stderr)
        self.assertIn("exit code 1", proc.stderr)

    def _scratch_repo(self, tmp):
        """A minimal kube-agents tree for whole-script runs.

        Running against the real checkout is not hermetic: compose_dir is
        derived from the script's own directory, and a checkout that has driven
        a real install carries a gitignored
        terraform/examples/full-install/backend_override.tf, which sends
        resolve_state_location down the local-state branch. Green in CI, red on
        a maintainer's machine.
        """
        root = pathlib.Path(tmp) / "repo"
        (root / "terraform" / "examples" / "full-install").mkdir(parents=True)
        (root / "k8s-operator" / "scripts").mkdir(parents=True)
        # Only its existence is tested before the exits under test.
        (root / "terraform" / "examples" / "full-install" / "lifecycle.sh").touch()
        shutil.copy(_UNINSTALL_SH, root / "uninstall.sh")
        shutil.copy(_INSTALLER_COMMON, root / "k8s-operator" / "scripts" / "installer_common.sh")
        return root

    def _run_whole_script(self, tmp, gcloud_body):
        bin_dir = create_minimal_tools_bin(tmp)
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(gcloud_body)
        gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
        root = self._scratch_repo(tmp)
        return subprocess.run(
            ["bash", str(root / "uninstall.sh"), "--non-interactive", "-y",
             "--project-id=p1", "--cluster-name=c1", "--region=r1"],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                # get_isolated_test_env strips only GITHUB_*/RUNNER_*/CI/GH_TOKEN,
                # so a maintainer's exported bucket would otherwise reach the
                # explicitly-named-bucket branch and change the answer.
                overrides={"PATH": str(bin_dir), "KUBE_AGENTS_STATE_BUCKET": ""},
            ),
            cwd=str(tmp),
        )

    def test_no_state_and_no_terraform_still_exits_3(self):
        # The state probe runs before the terraform gate, so "there is nothing
        # here" outranks "your machine is missing the engine". Getting this
        # backwards made exit 3 unreachable on exactly the runner the RC
        # pipeline uses, which has no terraform.
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_whole_script(
                tmp,
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                f"  *\"storage cat\"*) echo {shlex.quote(ResolveStateLocationTest.PROBE_ABSENT)} >&2; exit 1 ;;\n"
                '  *"clusters describe"*) echo "NOT_FOUND" >&2; exit 1 ;;\n'
                "esac\n"
                "exit 0\n",
            )
            self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
            self.assertIn("No Terraform state found", proc.stdout)
            self.assertNotIn("terraform is not installed", proc.stdout)

    def test_a_missing_terraform_is_refused_by_name(self):
        # terraform is the teardown engine; without it lifecycle.sh dies on a
        # bare "terraform: command not found" from inside a subshell. PATH is
        # restricted to a minimal tool set so the assertion does not depend on
        # whether the runner happens to ship terraform. The gcloud stub reports
        # live state, because with none the run exits 3 above this gate — which
        # is the point of the test above.
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_whole_script(
                tmp,
                "#!/usr/bin/env bash\n"
                'case "$*" in\n'
                '  *"storage cat"*) echo "{}"; exit 0 ;;\n'
                "esac\n"
                "exit 0\n",
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("terraform is not installed", proc.stdout)


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

    def test_baked_release_version_does_not_trigger_recursive_source_ref_dispatch(self):
        """Verifies a stamped uninstall.sh (BAKED_RELEASE_VERSION set) does not trigger handover dispatch."""
        with tempfile.TemporaryDirectory() as tmp:
            script_path = pathlib.Path(tmp) / "uninstall.sh"
            content = _UNINSTALL_SH.read_text().replace(
                'BAKED_RELEASE_VERSION=""',
                'BAKED_RELEASE_VERSION="0.2.0"',
            )
            script_path.write_text(content)
            script_path.chmod(0o755)

            # Sourcing the script should leave PARAM_SOURCE_REF empty
            check_cmd = f'KUBE_AGENTS_SOURCE_ONLY=true source "{script_path}"; echo "REF=$PARAM_SOURCE_REF"'
            proc = subprocess.run(
                ["bash", "-c", check_cmd],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("REF=", proc.stdout)
            self.assertNotIn("REF=0.2.0", proc.stdout)


class GvisorFloorCannotBlockTheTeardownTest(unittest.TestCase):
    """A destroy is not refusable on the sandbox's account.

    `write_tfvars_from_state` runs the Autopilot version-floor check whenever
    ENABLE_GVISOR is truthy, and returns 1 below the floor. uninstall.sh sources
    vars.sh whenever the checkout has one, and since the installer default
    flipped that file says "true" on every new install -- so the ordinary
    teardown, from the checkout that installed, is the case the floor can abort.
    The `false` fallback inside write_tfvars_from_state does not cover it; only
    the export in uninstall.sh does.

    Asserted against the script's text rather than by running it, because the
    call sits inside the teardown's confirmation and lock machinery. What makes
    the assertion meaningful is the ordering: an export placed after the call
    would read as a fix and change nothing.
    """

    def test_uninstall_forces_gvisor_off_before_generating_tfvars(self):
        text = _UNINSTALL_SH.read_text()
        export_at = text.find('export ENABLE_GVISOR="false"')
        self.assertNotEqual(
            export_at,
            -1,
            "uninstall.sh must export ENABLE_GVISOR=false; without it a "
            "sub-floor Autopilot cluster cannot be torn down from the checkout "
            "that installed it.",
        )
        # The invocation, not the two comments that name the function.
        call = re.search(r"^\s*write_tfvars_from_state \"", text, re.MULTILINE)
        self.assertIsNotNone(call, "write_tfvars_from_state call not found")
        call_at = call.start()
        self.assertLess(
            export_at,
            call_at,
            "the ENABLE_GVISOR export must come before write_tfvars_from_state, "
            "which is what runs the floor check.",
        )


if __name__ == "__main__":
    unittest.main()
