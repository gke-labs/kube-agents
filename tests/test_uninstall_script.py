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
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
_INSTALL_DEFAULTS = _REPO_ROOT / "install.defaults.env"


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


def _scratch_repo(tmp):
    """A minimal kube-agents tree for whole-script runs, with no install.env.

    Running against the real checkout is not hermetic: compose_dir is derived
    from the script's own directory, and a checkout that has driven a real
    install carries a gitignored terraform/examples/full-install/
    backend_override.tf, which sends resolve_state_location down the
    local-state branch. Green in CI, red on a maintainer's machine.

    Safe to call more than once in one temporary directory: an install.env a
    previous call's test left in the tree is removed, so every run starts
    from a checkout that has none of its own.
    """
    root = pathlib.Path(tmp) / "repo"
    (root / "terraform" / "examples" / "full-install").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "installer").mkdir(parents=True, exist_ok=True)
    (root / "install.env").unlink(missing_ok=True)
    # Only its existence is tested before the exits under test.
    (root / "terraform" / "examples" / "full-install" / "lifecycle.sh").touch()
    shutil.copy(_UNINSTALL_SH, root / "uninstall.sh")
    shutil.copy(_INSTALLER_COMMON, root / "scripts" / "installer" / "installer_common.sh")
    # installer_common.sh sources the defaults from the repository root and
    # refuses to run without them, so a fake repo needs the real file. A
    # checkout genuinely missing it cannot decide anything about an install,
    # which is why that is a hard failure rather than a fallback.
    shutil.copy(_INSTALL_DEFAULTS, root / "install.defaults.env")
    return root


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

    def test_a_child_exiting_3_inside_a_substitution_is_reported_once_as_1(self):
        # The subshell's handler normalises the code and exits silently; the
        # parent's then fires at the assignment with 1 and prints the one
        # banner (#1798). The echo after the failing child proves the subshell
        # stopped there: command substitution does not inherit errexit.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            body = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UNINSTALL_SH}"
x="$(bash -c "exit 3"; echo "NOT_REACHED_IN_PROBE")"
echo "NOT_REACHED x=[$x]"
"""
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("NOT_REACHED", proc.stdout)
        self.assertEqual(proc.stderr.count("Teardown error encountered"), 1, proc.stderr)
        # A `bash -c` body has no file for bash to name its frames after, so
        # the banner falls back to the script's name; the assignment in the
        # parent is the command it names.
        self.assertIn(" at uninstall.sh:", proc.stderr)
        self.assertIn(' in main (exit code 1): x="$(', proc.stderr)
        self.assertNotIn("exit code 3", proc.stderr)

    def test_a_failure_inside_a_sourced_helper_names_its_file_and_function(self):
        # $LINENO counts from the top of whichever file the failing command
        # sat in, so a bare line number sent the reader to an unrelated line
        # of uninstall.sh (#1798). The banner names the helper and the
        # function it failed in.
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            lib = pathlib.Path(tmp) / "helper_lib.sh"
            lib.write_text("helper_probe() {\n  false\n}\n")
            body = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UNINSTALL_SH}"
source "{lib}"
helper_probe
echo "NOT_REACHED"
"""
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertNotIn("NOT_REACHED", proc.stdout)
            self.assertEqual(proc.stderr.count("Teardown error encountered"), 1, proc.stderr)
            self.assertIn(f"Teardown error encountered at {lib}:", proc.stderr)
            self.assertIn(" in helper_probe (exit code 1): false", proc.stderr)

    def _run_whole_script(self, tmp, gcloud_body):
        bin_dir = create_minimal_tools_bin(tmp)
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(gcloud_body)
        gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
        root = _scratch_repo(tmp)
        return subprocess.run(
            ["bash", str(root / "uninstall.sh"), "--non-interactive", "-y",
             "--gcp-project-id=p1", "--gke-cluster-name=c1", "--gcp-region=r1"],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(
                # get_isolated_test_env strips only GITHUB_*/RUNNER_*/CI/GH_TOKEN,
                # so a maintainer's exported bucket would otherwise reach the
                # explicitly-named-bucket branch and change the answer.
                overrides={
                    "PATH": str(bin_dir),
                    "HOME": str(pathlib.Path(tmp) / "empty-home"),
                    "KUBE_AGENTS_INSTALL_ENV": "",
                    "KUBE_AGENTS_STATE_BUCKET": "",
                },
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
    def _run(
        self,
        ref_carries_uninstall,
        args,
        ref_speaks_domain_scoped=False,
        home_install_env=None,
        cwd_install_env=None,
        install_env_var=None,
        script_in_checkout=False,
    ):
        """Run the real uninstall.sh with a stub git on PATH.

        The stub's `clone` creates the target directory and, when
        `ref_carries_uninstall`, drops an uninstall.sh into it that records
        its argv to DISPATCH_LOG — standing in for the pinned release's own
        uninstaller. fetch/checkout are no-ops.

        `ref_speaks_domain_scoped` makes that stand-in advertise the
        domain-scoped flag names, which is how the hand-over tells which
        dialect the release it is about to exec parses.

        `install_env_var` sets KUBE_AGENTS_INSTALL_ENV, which is otherwise
        cleared so a developer's own shell cannot answer for the test.

        `script_in_checkout` lays the engine marker down beside the copied
        script (which is also the working directory), so the run is a checkout
        run: wrapper_checkout is set, and the $HOME fallback must not be
        consulted at all.
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
                f'      if [ "{str(ref_speaks_domain_scoped).lower()}" = "true" ]; then\n'
                "        echo '# parses --gcp-project-id --gke-cluster-name --gcp-region --agent-namespace'\n"
                "      fi\n"
                "      echo 'printf \"%s\\n\" \"$@\" > \"$DISPATCH_LOG\"'\n"
                "      echo 'if [ -n \"${KUBE_AGENTS_INSTALL_ENV:-}\" ]; then printf \"ENV_FILE=%s\\n\" \"$KUBE_AGENTS_INSTALL_ENV\" >> \"$DISPATCH_LOG\"; fi'\n"
                '    } > "$dest/uninstall.sh"\n'
                "  fi\n"
                "fi\n"
                "exit 0\n"
            )
            git.chmod(git.stat().st_mode | stat.S_IEXEC)
            home_dir = pathlib.Path(tmp) / "home"
            home_dir.mkdir()
            if home_install_env is not None:
                (home_dir / "kube-agents").mkdir()
                (home_dir / "kube-agents" / "install.env").write_text(home_install_env)
            if cwd_install_env is not None:
                (pathlib.Path(tmp) / "install.env").write_text(cwd_install_env)
            full_env = get_isolated_test_env(
                overrides={
                    "DISPATCH_LOG": str(dispatch_log),
                    "HOME": str(home_dir),
                    "KUBE_AGENTS_INSTALL_ENV": (
                        "" if install_env_var is None else str(install_env_var)
                    ),
                },
                bin_dir=str(bin_dir),
            )
            # Copy uninstall.sh out of the repository root so script_dir has no
            # terraform/examples/full-install/lifecycle.sh and cannot pick up a
            # gitignored install.env from the developer's own checkout.
            script_copy = pathlib.Path(tmp) / "uninstall.sh"
            shutil.copy(_UNINSTALL_SH, script_copy)
            if script_in_checkout:
                engine = pathlib.Path(tmp) / "terraform" / "examples" / "full-install"
                engine.mkdir(parents=True)
                (engine / "lifecycle.sh").touch()
            proc = subprocess.run(
                ["bash", str(script_copy), *args],
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
                "--gcp-project-id=p1",
                "--gke-cluster-name=c1",
                "--gcp-region=r1",
            ],
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(log, proc.stdout + proc.stderr)
        # LEGACY spellings coming out, domain-scoped ones going in, and the
        # asymmetry is the assertion. What is dispatched is not this script's
        # flag set but the v0.9.0 uninstall.sh's, which has never heard of
        # --gcp-project-id and would exit 2 on it. --source-ref exists for
        # precisely those pre-Terraform releases, so translating the flags here
        # would break the one path it is for.
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--project-id=p1",
                "--cluster-name=c1",
                "--region=r1",
            ],
        )

    def test_a_release_that_speaks_the_new_dialect_is_handed_the_new_flags(self):
        """--source-ref reaches forwards as well as back.

        Every release cut from the domain-scoped rename on rejects
        --project-id with "Unknown parameter" and exits 2, which an automated
        caller reads as a hard failure rather than the 3 that means "nothing to
        tear down". Translating unconditionally would break the hand-over for
        exactly those refs, so the dialect is read off the cloned script.
        """
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=[
                "--source-ref=v9.9.9",
                "--non-interactive",
                "--gcp-project-id=p1",
                "--gke-cluster-name=c1",
                "--gcp-region=r1",
                "--agent-namespace=ns1",
            ],
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(log, proc.stdout + proc.stderr)
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--gcp-project-id=p1",
                "--gke-cluster-name=c1",
                "--gcp-region=r1",
                "--agent-namespace=ns1",
            ],
        )

    def test_a_legacy_release_is_not_handed_agent_namespace(self):
        """No release in the legacy dialect parses it, so passing it exits 2."""
        _proc, log = self._run(
            ref_carries_uninstall=True,
            args=[
                "--source-ref=v0.9.0",
                "--non-interactive",
                "--gcp-project-id=p1",
                "--agent-namespace=ns1",
            ],
        )
        self.assertIsNotNone(log)
        self.assertNotIn("--agent-namespace=ns1", log.split())

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

    def test_source_ref_forwards_coordinates_from_the_working_directory(self):
        """When the operator runs `--source-ref` from a directory that carries
        an `install.env` (or points `KUBE_AGENTS_INSTALL_ENV` at one), that file
        is an instruction rather than a `$HOME` guess: the wrapper exports
        KUBE_AGENTS_INSTALL_ENV for the child and forwards the coordinates
        (including NAMESPACE) in the target release's flag dialect."""
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=["--source-ref=v0.5.0", "--non-interactive"],
            cwd_install_env=(
                'PROJECT_ID="from-cwd"\n'
                'CLUSTER_NAME="cwd-cluster"\n'
                'REGION="europe-north1"\n'
                'NAMESPACE="custom-agents-ns"\n'
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(log, proc.stdout + proc.stderr)
        tokens = log.split()
        self.assertEqual(
            tokens[:-1],
            [
                "--non-interactive",
                "--gcp-project-id=from-cwd",
                "--gke-cluster-name=cwd-cluster",
                "--gcp-region=europe-north1",
                "--agent-namespace=custom-agents-ns",
            ],
        )
        self.assertTrue(
            tokens[-1].endswith("/install.env")
            and tokens[-1].startswith("ENV_FILE="),
            f"expected exported KUBE_AGENTS_INSTALL_ENV in child environment, got {tokens[-1]}",
        )
        combined = proc.stdout + proc.stderr
        self.assertNotIn("Not forwarding", combined)
        self.assertNotIn("No coordinates are being forwarded", combined)

    def test_source_ref_names_a_coordinate_the_loaded_install_env_does_not_record(self):
        """A loaded install.env that lacks one of the three keys used to drop
        that coordinate in silence: only the no-file and dropped-guess arms
        warned, and the main arm's guessed-coordinate report is never reached
        because this arm execs first. The pinned release — for pre-0.4.0 refs
        a script that reads no install.env — then aimed at its own default.

        The report is now made once, from what is actually about to be
        forwarded, so it covers this arm without a branch of its own.
        """
        env_text = 'PROJECT_ID="from-cwd"\nCLUSTER_NAME="install-a"\n'
        with tempfile.TemporaryDirectory(prefix="source-ref-partial-") as explicit_dir:
            explicit_file = pathlib.Path(explicit_dir) / "install.env"
            explicit_file.write_text(env_text)
            for label, kw in (
                ("cwd", {"cwd_install_env": env_text}),
                ("KUBE_AGENTS_INSTALL_ENV", {"install_env_var": str(explicit_file)}),
            ):
                with self.subTest(source=label):
                    proc, log = self._run(
                        ref_carries_uninstall=True,
                        ref_speaks_domain_scoped=True,
                        args=["--source-ref=v0.3.0", "--non-interactive"],
                        **kw,
                    )
                    combined = proc.stdout + proc.stderr
                    self.assertEqual(proc.returncode, 0, combined)
                    self.assertIn("Loaded install configuration from:", combined)
                    self.assertIn("Not forwarding --gcp-region to the 'v0.3.0' release", combined)
                    # The project IS forwarded, so the project fallback is not mentioned.
                    self.assertNotIn("gcloud's active project", combined)
                    self.assertIn("Pass --gcp-project-id/--gke-cluster-name/--gcp-region, or point KUBE_AGENTS_INSTALL_ENV", combined)
                    self.assertNotIn("--gcp-project-id,", combined)
                    self.assertNotIn("No coordinates are being forwarded", combined)
                    # Still a warning, not a refusal: it hands over with what it has.
                    self.assertIsNotNone(log, combined)
                    self.assertEqual(
                        log.split()[:-1],
                        ["--non-interactive", "--gcp-project-id=from-cwd", "--gke-cluster-name=install-a"],
                    )

    def test_source_ref_reads_home_install_env_when_coordinates_confirm_it(self):
        """When `--source-ref` is given coordinate flags that match
        `$HOME/kube-agents/install.env`, the `$HOME` guess is confirmed and its
        non-coordinate settings (NAMESPACE) travel to the child."""
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=[
                "--source-ref=v0.5.0",
                "--non-interactive",
                "--gcp-project-id=from-home",
                "--gke-cluster-name=home-cluster",
                "--gcp-region=europe-north1",
            ],
            home_install_env=(
                'PROJECT_ID="from-home"\n'
                'CLUSTER_NAME="home-cluster"\n'
                'REGION="europe-north1"\n'
                'NAMESPACE="custom-agents-ns"\n'
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(log, proc.stdout + proc.stderr)
        tokens = log.split()
        self.assertEqual(
            tokens[:-1],
            [
                "--non-interactive",
                "--gcp-project-id=from-home",
                "--gke-cluster-name=home-cluster",
                "--gcp-region=europe-north1",
                "--agent-namespace=custom-agents-ns",
            ],
        )
        self.assertTrue(
            tokens[-1].endswith("/home/kube-agents/install.env")
            and tokens[-1].startswith("ENV_FILE="),
            f"expected exported KUBE_AGENTS_INSTALL_ENV in child environment, got {tokens[-1]}",
        )

    def test_a_stranger_in_home_is_not_forwarded_on_a_flagless_source_ref_run(self):
        """A flagless `--source-ref` run outside a checkout must not aim the
        legacy uninstaller at `$HOME/kube-agents/install.env`.

        `install.env` was introduced in `0.4.0` alongside the Terraform
        lifecycle engine, so any file sitting at `$HOME/kube-agents/install.env`
        belongs to a `>= 0.4.0` install rather than to the pre-Terraform
        install `--source-ref` is being run to tear down. Without CLI flags
        confirming those coordinates, forwarding `$HOME/kube-agents/install.env`
        would aim the old `uninstall.sh` at the workstation's current install.
        """
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=["--source-ref=v0.3.0", "--non-interactive"],
            home_install_env=(
                'PROJECT_ID="project-a"\n'
                'CLUSTER_NAME="install-a"\n'
                'REGION="us-east1"\n'
                'NAMESPACE="install-a-ns"\n'
            ),
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("Not reading", combined)
        self.assertIn("found only by searching $HOME", combined)
        self.assertIn("No coordinates are being forwarded", combined)
        self.assertNotIn("No install configuration (install.env) was found", combined)
        self.assertIsNotNone(log, combined)
        self.assertEqual(log.split(), ["--non-interactive"])

    def test_a_partial_flag_match_does_not_read_the_home_guess_on_source_ref(self):
        """A `--source-ref` run with one or two coordinate flags that happen to
        coincide with `$HOME/kube-agents/install.env` must not fill the omitted
        coordinates (such as `CLUSTER_NAME` or `NAMESPACE`) from `$HOME`."""
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=[
                "--source-ref=v0.3.0",
                "--non-interactive",
                "--gcp-project-id=from-home",
                "--gcp-region=us-east1",
            ],
            home_install_env=(
                'PROJECT_ID="from-home"\n'
                'CLUSTER_NAME="install-a"\n'
                'REGION="us-east1"\n'
                'NAMESPACE="install-a-ns"\n'
            ),
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("Not reading", combined)
        self.assertIn("found only by searching $HOME", combined)
        self.assertIn("Not forwarding --gke-cluster-name to the 'v0.3.0' release", combined)
        self.assertNotIn("Not forwarding --gcp-project-id", combined)
        # The dropped-guess arm already named its own remedy (pointing at the
        # file it did not read); the generic one would repeat it.
        self.assertIn("if that file is the one you mean", combined)
        self.assertNotIn("Pass --gcp-project-id/--gke-cluster-name/--gcp-region, or point KUBE_AGENTS_INSTALL_ENV", combined)
        self.assertNotIn("No install configuration (install.env) was found", combined)
        self.assertIsNotNone(log, combined)
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--gcp-project-id=from-home",
                "--gcp-region=us-east1",
            ],
        )

    def test_source_ref_refuses_when_flags_disagree_with_the_install_checkout(self):
        """A `--source-ref` run that loads an explicitly located `install.env`
        (from `$PWD` or `KUBE_AGENTS_INSTALL_ENV`) while its flags name another
        install must refuse before dispatching."""
        env_text = (
            'PROJECT_ID="from-cwd"\n'
            'CLUSTER_NAME="install-a"\n'
        )
        with tempfile.TemporaryDirectory(prefix="source-ref-explicit-") as explicit_dir:
            explicit_file = pathlib.Path(explicit_dir) / "install.env"
            explicit_file.write_text(env_text)
            for label, kw in (
                ("cwd", {"cwd_install_env": env_text}),
                ("KUBE_AGENTS_INSTALL_ENV", {"install_env_var": str(explicit_file)}),
            ):
                with self.subTest(source=label):
                    proc, log = self._run(
                        ref_carries_uninstall=True,
                        args=[
                            "--source-ref=v0.3.0",
                            "--non-interactive",
                            "--gcp-project-id=from-cwd",
                            "--gke-cluster-name=install-b",
                        ],
                        **kw,
                    )
                    combined = proc.stdout + proc.stderr
                    self.assertEqual(proc.returncode, 1, combined)
                    self.assertIn("records a different install than the flags name", combined)
                    self.assertIsNone(log)

    def test_a_stranger_in_home_does_not_block_a_fully_named_teardown(self):
        """The case --source-ref exists for, on a workstation that is not empty.

        A pre-0.4.0 install has no install.env of its own, so the lookup falls
        through to $HOME/kube-agents/install.env — which belongs to whichever
        install the installer made last, not to the one being torn down.
        Loading that and then refusing on the coordinate conflict left an
        operator with none of the three ways out the refusal names: there is
        nothing to point KUBE_AGENTS_INSTALL_ENV at, the old install's checkout
        carries no file either so the lookup lands back here, and dropping the
        flags aims the teardown at the other install.

        So a guess that contradicts a fully named teardown is not read at all.
        Its other keys matter as much as its coordinates: NAMESPACE here would
        otherwise be exported into the release this arm execs, which for a
        pre-0.4.0 ref does not read install.env but does inherit the
        environment.
        """
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=[
                "--source-ref=v0.3.0",
                "--non-interactive",
                "--gcp-project-id=project-b",
                "--gke-cluster-name=install-b",
                "--gcp-region=us-west1",
            ],
            home_install_env=(
                'PROJECT_ID="project-a"\n'
                'CLUSTER_NAME="install-a"\n'
                'REGION="us-east1"\n'
                'NAMESPACE="install-a-ns"\n'
            ),
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("Not reading", combined)
        self.assertIn("records a different install", combined)
        self.assertNotIn("Refusing to tear down", combined)
        # A file WAS found; saying none was would contradict the warning above.
        self.assertNotIn("No install configuration (install.env) was found", combined)
        self.assertIsNotNone(log, combined)
        # Exactly the flags, and nothing out of the stranger's file: no
        # --agent-namespace from its NAMESPACE, and no exported
        # KUBE_AGENTS_INSTALL_ENV pointing the child back at it.
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--gcp-project-id=project-b",
                "--gke-cluster-name=install-b",
                "--gcp-region=us-west1",
            ],
        )

    def test_source_ref_from_a_checkout_without_install_env_does_not_reach_into_home(self):
        """The --source-ref half of the checkout-run $HOME gate.

        The main arm's half is pinned in TeardownKnowsWhichInstallItIsAimedAtTest.
        Here the script runs from a checkout that has no install.env, and
        $HOME/kube-agents/install.env matches all three flags, which is exactly
        the case the guess arm would read. A checkout run must not consult
        $HOME at all: nothing is loaded, nothing from that file (its NAMESPACE,
        its path) reaches the release the handover execs, and only the flags are
        forwarded.
        """
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            script_in_checkout=True,
            args=[
                "--source-ref=v0.3.0",
                "--non-interactive",
                "--gcp-project-id=project-a",
                "--gke-cluster-name=install-a",
                "--gcp-region=us-east1",
            ],
            home_install_env=(
                'PROJECT_ID="project-a"\n'
                'CLUSTER_NAME="install-a"\n'
                'REGION="us-east1"\n'
                'NAMESPACE="home-ns"\n'
            ),
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertNotIn("Loaded install configuration from:", combined)
        self.assertNotIn("/home/kube-agents/install.env", combined)
        self.assertIsNotNone(log, combined)
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--gcp-project-id=project-a",
                "--gke-cluster-name=install-a",
                "--gcp-region=us-east1",
            ],
        )

    def test_source_ref_skips_an_unparseable_home_guess_when_flags_name_the_install(self):
        """A $HOME guess that is not valid shell must not abort the teardown.

        The load arm exits on a file that fails `bash -n`, which is right for
        an install.env the operator named. A file found only by searching
        $HOME, on a run whose three coordinate flags already name the install,
        is not the operator's to fix before a teardown can proceed: it is
        dropped, and exactly the flags are forwarded.
        """
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=[
                "--source-ref=v0.3.0",
                "--non-interactive",
                "--gcp-project-id=project-b",
                "--gke-cluster-name=install-b",
                "--gcp-region=us-west1",
            ],
            home_install_env='PROJECT_ID="project-a\nif then\n',
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("Not reading", combined)
        self.assertIn("it is not valid shell", combined)
        self.assertNotIn("could not be loaded", combined)
        self.assertIsNotNone(log, combined)
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--gcp-project-id=project-b",
                "--gke-cluster-name=install-b",
                "--gcp-region=us-west1",
            ],
        )

    def test_source_ref_does_not_read_a_home_guess_that_omits_a_coordinate(self):
        """An absent key is not a match.

        Each comparison used to short-circuit to "agrees" when the file lacked
        the key, so a $HOME file with no PROJECT_ID was "confirmed" by any
        --gcp-project-id: its NAMESPACE was forwarded, its state prefix
        exported, and KUBE_AGENTS_INSTALL_ENV pointed the child at it, while
        the flags named an install in another project.
        """
        flags = [
            "--source-ref=v0.4.0",
            "--non-interactive",
            "--gcp-project-id=project-b",
            "--gke-cluster-name=install-a",
            "--gcp-region=us-east1",
        ]
        rest = (
            'CLUSTER_NAME="install-a"\n'
            'REGION="us-east1"\n'
            'NAMESPACE="install-a-ns"\n'
            'KUBE_AGENTS_STATE_PREFIX="install-a-state"\n'
        )
        for label, home_env in (
            ("absent", rest),
            ("empty", 'PROJECT_ID=""\n' + rest),
        ):
            with self.subTest(project_id=label):
                proc, log = self._run(
                    ref_carries_uninstall=True,
                    ref_speaks_domain_scoped=True,
                    args=flags,
                    home_install_env=home_env,
                )
                combined = proc.stdout + proc.stderr
                self.assertEqual(proc.returncode, 0, combined)
                self.assertIn("Not reading", combined)
                self.assertIn("does not record all of PROJECT_ID, CLUSTER_NAME and REGION", combined)
                self.assertNotIn("Loaded install configuration from:", combined)
                self.assertIsNotNone(log, combined)
                # Exactly the flags: no --agent-namespace from its NAMESPACE and
                # no ENV_FILE line pointing the child back at it.
                self.assertEqual(log.split(), flags[1:])

    def test_source_ref_skips_a_home_guess_that_fails_while_sourced(self):
        """`bash -n` checks syntax only. A guess that expands an unset variable
        passes it, used to read as "does not name another install" when the
        probe's subshell died under `set -u`, and then aborted the run in the
        load arm with a bare "unbound variable" -- for a file nobody named, on
        a run whose three flags already name the install.
        """
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=[
                "--source-ref=v0.3.0",
                "--non-interactive",
                "--gcp-project-id=project-a",
                "--gke-cluster-name=install-a",
                "--gcp-region=us-east1",
            ],
            home_install_env=(
                'PROJECT_ID="project-a"\n'
                'CLUSTER_NAME="install-a"\n'
                'REGION="us-east1"\n'
                "GITOPS_TOKEN=$KUBE_AGENTS_TEST_NEVER_SET\n"
            ),
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("Not reading", combined)
        self.assertIn("it failed while being read", combined)
        self.assertNotIn("unbound variable", combined)
        self.assertNotIn("Loaded install configuration from:", combined)
        self.assertIsNotNone(log, combined)
        self.assertEqual(
            log.split(),
            [
                "--non-interactive",
                "--gcp-project-id=project-a",
                "--gke-cluster-name=install-a",
                "--gcp-region=us-east1",
            ],
        )

    def test_source_ref_says_so_when_it_finds_no_install_env(self):
        """The dangerous case the arm used to pass over in silence.

        With no install.env anywhere and no coordinate flags, nothing is
        forwarded and the pinned release — which for pre-0.4.0 refs does not
        read install.env at all — falls back to DEFAULT_CLUSTER_NAME,
        DEFAULT_REGION and gcloud's active project, which on a GCE host is the
        machine's own. A teardown on flags alone is supported, so this warns
        rather than refusing; what it must not do is say nothing.
        """
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=["--source-ref=v0.3.0", "--non-interactive"],
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("No install configuration (install.env) was found", combined)
        self.assertIn("No coordinates are being forwarded", combined)
        self.assertIn("will aim at its own defaults", combined)
        # It still hands over — warning, not refusal.
        self.assertIsNotNone(log, combined)
        self.assertEqual(log.split(), ["--non-interactive"])

    def test_source_ref_stays_quiet_about_coordinates_it_was_given(self):
        """Flags are a complete answer, so the second half of that warning is
        only printed when nothing named the install at all."""
        proc, log = self._run(
            ref_carries_uninstall=True,
            ref_speaks_domain_scoped=True,
            args=[
                "--source-ref=v0.3.0",
                "--non-interactive",
                "--gcp-project-id=p1",
                "--gke-cluster-name=c1",
                "--gcp-region=r1",
            ],
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("No install configuration (install.env) was found", combined)
        self.assertNotIn("No coordinates are being forwarded", combined)
        self.assertNotIn("Not forwarding", combined)
        self.assertIsNotNone(log, combined)

    def test_a_pointer_at_a_missing_file_is_refused_before_anything_is_cloned(self):
        """resolve_uninstall_env_file returns an explicit pointer unchecked, so
        a mistyped one used to reach the `[ -f ]` gate, forward nothing, and
        hand over as if no configuration existed. An explicit pointer is a
        typo, not a lookup order to fall through."""
        proc, log = self._run(
            ref_carries_uninstall=True,
            args=["--source-ref=v0.3.0", "--non-interactive"],
            install_env_var="/nonexistent/kube-agnets/install.env",
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("/nonexistent/kube-agnets/install.env", combined)
        self.assertIn("which does not exist", combined)
        self.assertIsNone(log, "the run handed over despite the bad pointer")

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
    ENABLE_GVISOR is truthy, and returns 1 below the floor. uninstall.sh loads the
    checkout's install.env, and since the installer default
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


class UninstallSummaryDisclosureTest(unittest.TestCase):
    """The uninstall summary discloses preserved cluster-level settings."""

    def test_retained_cluster_settings_disclosed_in_summary(self):
        text = _UNINSTALL_SH.read_text()
        self.assertIn(
            "Cluster-level settings kept on pre-existing clusters: CMEK database encryption, Workload Identity pool, GKE_METADATA node pool migrations, and Calico NetworkPolicy are preserved and not reverted.",
            text,
        )


class UninstallNeverReferencesRetiredVarsFileTest(unittest.TestCase):
    """k8s-operator/scripts/vars.sh is retired and never read, written, or inspected by uninstall.sh."""

    def test_uninstall_never_references_retired_vars_sh(self):
        text = _UNINSTALL_SH.read_text()
        self.assertNotIn("k8s-operator/scripts/vars.sh", text)
        self.assertNotIn("load_legacy_vars_file", text)


class TeardownKnowsWhichInstallItIsAimedAtTest(unittest.TestCase):
    """What a piped teardown reads, and what it admits to guessing.

    Under `curl … | bash` the teardown's repo_dir is a clone it has just made,
    which carries no install.env. It used to look nowhere else, so the
    documented one-liner silently tore down whatever DEFAULT_CLUSTER_NAME and
    gcloud's active project happened to name. Retiring
    k8s-operator/scripts/vars.sh removed the other way a pre-0.4.0 install used
    to be located, so the install checkout is now the only one left.
    """

    def _preview(
        self,
        tmp,
        home,
        args=(),
        dry_run=True,
        cwd_install_env=None,
        repo_install_env=None,
        explicit_install_env=None,
        extra_env=None,
        from_checkout=None,
    ):
        """A --dry-run teardown from a neutral directory, with HOME moved.

        --dry-run because everything under test happens before it: the
        resolution, the coordinates, and what the run says about them. The
        gcloud stub answers the active-project query the way a developer
        machine -- or a GCE metadata server -- would.

        When `from_checkout` is False (the default when `repo_install_env` is
        None), `uninstall.sh` runs as a standalone file outside any checkout so
        `TEMP_REPO_DIR` is populated via a stub `git clone` and the non-checkout
        `${install_checkout}/install.env` fallback in `$HOME/kube-agents` is
        exercised. When `from_checkout` is True (or `repo_install_env` is
        supplied), `uninstall.sh` runs directly out of `_scratch_repo`.
        """
        if from_checkout is None:
            from_checkout = repo_install_env is not None
        bin_dir = create_minimal_tools_bin(tmp)
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(
            "#!/usr/bin/env bash\n"
            'case "$*" in\n'
            '  *"config get-value project"*) echo \'the-machines-own-project\'; exit 0 ;;\n'
            "esac\n"
            "exit 0\n"
        )
        gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
        neutral = pathlib.Path(tmp) / "neutral"
        neutral.mkdir(exist_ok=True)
        (neutral / "install.env").unlink(missing_ok=True)
        if cwd_install_env is not None:
            (neutral / "install.env").write_text(cwd_install_env)
        root = _scratch_repo(tmp)
        if repo_install_env is not None:
            (root / "install.env").write_text(repo_install_env)
        if from_checkout:
            script_to_run = root / "uninstall.sh"
        else:
            standalone_dir = pathlib.Path(tmp) / "standalone"
            standalone_dir.mkdir(exist_ok=True)
            script_to_run = standalone_dir / "uninstall.sh"
            shutil.copy(_UNINSTALL_SH, script_to_run)
            git_stub = bin_dir / "git"
            git_stub.unlink(missing_ok=True)
            git_stub.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "${1:-}" = "clone" ]; then\n'
                '  dest="${!#}"\n'
                f'  cp -R "{root}/." "$dest/"\n'
                "  exit 0\n"
                "fi\n"
                "exit 0\n"
            )
            git_stub.chmod(git_stub.stat().st_mode | stat.S_IEXEC)
        overrides = {
            "PATH": str(bin_dir),
            "HOME": str(home),
            "KUBE_AGENTS_STATE_BUCKET": "",
            "KUBE_AGENTS_INSTALL_ENV": str(explicit_install_env) if explicit_install_env else "",
        }
        if extra_env:
            overrides.update(extra_env)
        cmd = ["bash", str(script_to_run), "--non-interactive"]
        if dry_run:
            cmd.append("--dry-run")
        cmd.extend(args)
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides),
            cwd=str(neutral),
        )

    def test_the_install_checkout_in_home_is_where_the_configuration_comes_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "kube-agents").mkdir(parents=True)
            (home / "kube-agents" / "install.env").write_text(
                'PROJECT_ID="the-installs-project"\n'
                'CLUSTER_NAME="the-installs-cluster"\n'
                'REGION="europe-north1"\n'
            )

            proc = self._preview(tmp, home)

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertIn(
                f"Loaded install configuration from: {home}/kube-agents/install.env", combined
            )
            self.assertIn("the-installs-cluster in the-installs-project (europe-north1)", combined)
            self.assertNotIn("the-machines-own-project", combined)
            self.assertNotIn("is installer_common.sh's default, not this install's", combined)

    def test_a_checkout_run_without_its_own_install_env_does_not_reach_into_home(self):
        """Running `./uninstall.sh` from a checkout with no `install.env` of its
        own must NOT load `$HOME/kube-agents/install.env`, matching `install.sh`
        and `upgrade.sh`."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "kube-agents").mkdir(parents=True)
            (home / "kube-agents" / "install.env").write_text(
                'PROJECT_ID="another-installs-project"\n'
                'CLUSTER_NAME="another-installs-cluster"\n'
                'REGION="europe-north1"\n'
            )

            proc = self._preview(tmp, home, from_checkout=True)

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertNotIn("Loaded install configuration from", combined)
            self.assertNotIn("another-installs-cluster", combined)
            self.assertIn("No install configuration (install.env) was found", combined)
            self.assertIn("is installer_common.sh's default, not this install's", combined)

    def test_a_teardown_with_nothing_to_read_says_what_it_is_guessing(self):
        """It is still allowed to run on defaults -- `./uninstall.sh` in a
        checkout is exactly that -- but not to present a guess as the install's
        own coordinates. The confirmation prompt reads these very lines."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            home.mkdir()

            proc = self._preview(tmp, home)

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertNotIn("Loaded install configuration from", combined)
            self.assertIn("No install configuration (install.env) was found", combined)
            self.assertIn("is installer_common.sh's default, not this install's", combined)
            self.assertIn(
                "project 'the-machines-own-project' came from gcloud's active configuration",
                combined,
            )

    def test_coordinates_given_on_the_command_line_are_not_a_guess(self):
        """The control: naming all three leaves nothing to warn about, so the
        documented one-liner does not grow a warning it cannot act on."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            home.mkdir()

            proc = self._preview(
                tmp,
                home,
                args=(
                    "--gcp-project-id=named-project",
                    "--gke-cluster-name=named-cluster",
                    "--gcp-region=named-region",
                ),
            )

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertIn("named-cluster in named-project (named-region)", combined)
            self.assertNotIn("is installer_common.sh's default, not this install's", combined)
            self.assertNotIn("came from gcloud's active configuration", combined)

    def test_the_resolution_order_prefers_explicit_then_repo_then_pwd_over_home(self):
        """Pins every arm of resolve_uninstall_env_file in precedence order:
        KUBE_AGENTS_INSTALL_ENV > repo_dir/install.env > $(pwd)/install.env >
        $HOME/kube-agents/install.env.

        Only case 1 is a non-checkout run, so only it contests $HOME. Cases 2
        and 3 supply repo_dir/install.env, which makes them checkout runs, and
        a checkout run never consults $HOME at all (pinned by
        test_a_checkout_run_without_its_own_install_env_does_not_reach_into_home)."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "kube-agents").mkdir(parents=True)
            (home / "kube-agents" / "install.env").write_text(
                'PROJECT_ID="p"\nCLUSTER_NAME="from-home"\nREGION="r"\n'
            )
            explicit_file = pathlib.Path(tmp) / "explicit.env"
            explicit_file.write_text(
                'PROJECT_ID="p"\nCLUSTER_NAME="from-explicit"\nREGION="r"\n'
            )

            # 1. $(pwd)/install.env beats $HOME/kube-agents/install.env
            proc_pwd = self._preview(
                tmp,
                home,
                cwd_install_env='PROJECT_ID="p"\nCLUSTER_NAME="from-pwd"\nREGION="r"\n',
            )
            self.assertIn("from-pwd in p (r)", proc_pwd.stdout + proc_pwd.stderr)

            # 2. repo_dir/install.env beats $(pwd)/install.env (a checkout run: $HOME is not consulted)
            proc_repo = self._preview(
                tmp,
                home,
                cwd_install_env='PROJECT_ID="p"\nCLUSTER_NAME="from-pwd"\nREGION="r"\n',
                repo_install_env='PROJECT_ID="p"\nCLUSTER_NAME="from-repo"\nREGION="r"\n',
            )
            self.assertIn("from-repo in p (r)", proc_repo.stdout + proc_repo.stderr)

            # 3. KUBE_AGENTS_INSTALL_ENV beats repo_dir/install.env and $(pwd)/install.env
            proc_explicit = self._preview(
                tmp,
                home,
                cwd_install_env='PROJECT_ID="p"\nCLUSTER_NAME="from-pwd"\nREGION="r"\n',
                repo_install_env='PROJECT_ID="p"\nCLUSTER_NAME="from-repo"\nREGION="r"\n',
                explicit_install_env=explicit_file,
            )
            self.assertIn("from-explicit in p (r)", proc_explicit.stdout + proc_explicit.stderr)

    def test_a_real_teardown_refuses_when_the_configuration_belongs_to_another_install(self):
        """$HOME/kube-agents/install.env belongs to install-a; the flags name
        install-b. Without this check, install-a's NAMESPACE and custom
        KUBE_AGENTS_STATE_PREFIX stay exported and steer the state lookup and
        terraform.tfvars regeneration for install-b."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "kube-agents").mkdir(parents=True)
            (home / "kube-agents" / "install.env").write_text(
                'PROJECT_ID="my-gcp-project"\nCLUSTER_NAME="install-a"\nREGION="us-central1"\n'
            )

            proc = self._preview(
                tmp,
                home,
                dry_run=False,
                args=("--gke-cluster-name=install-b",),
            )

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 1, combined)
            self.assertIn("records a different install than the flags name", combined)
            self.assertIn("--gke-cluster-name=install-b, but CLUSTER_NAME=install-a", combined)

    def test_a_fully_named_piped_teardown_skips_a_home_file_it_only_guessed_at(self):
        """The local arm's half of the rule the --source-ref arm applies.

        The piped one-liner with all three flags, on a workstation whose
        $HOME/kube-agents/install.env belongs to another install, used to load
        it and then refuse: "Refusing to tear down: … records a different
        install". Every way out that refusal names is closed — the install
        being torn down has no install.env here, and dropping the flags aims
        the run at the other install. A file that could not be loaded aborted
        the run instead. Each shape is now skipped with its reason, and the
        run goes on to the flags.
        """
        flags = ("--gcp-project-id=project-b", "--gke-cluster-name=install-b", "--gcp-region=us-west1")
        cases = (
            (
                "differs",
                'PROJECT_ID="project-a"\nCLUSTER_NAME="install-a"\nREGION="us-east1"\nNAMESPACE="install-a-ns"\n',
                "it records a different install",
            ),
            (
                "incomplete",
                'CLUSTER_NAME="install-b"\nREGION="us-west1"\nNAMESPACE="install-a-ns"\n',
                "does not record all of PROJECT_ID, CLUSTER_NAME and REGION",
            ),
            (
                "unreadable",
                'PROJECT_ID="project-b"\nCLUSTER_NAME="install-b"\nREGION="us-west1"\n'
                "GITOPS_TOKEN=$KUBE_AGENTS_TEST_NEVER_SET\n",
                "it failed while being read",
            ),
            ("not valid shell", 'PROJECT_ID="project-a\nif then\n', "it is not valid shell"),
        )
        for label, home_env, reason in cases:
            for dry_run in (False, True):
                with self.subTest(file=label, dry_run=dry_run), tempfile.TemporaryDirectory() as tmp:
                    home = pathlib.Path(tmp) / "home"
                    (home / "kube-agents").mkdir(parents=True)
                    (home / "kube-agents" / "install.env").write_text(home_env)

                    proc = self._preview(tmp, home, dry_run=dry_run, args=flags)

                    combined = proc.stdout + proc.stderr
                    self.assertIn("Not reading", combined)
                    self.assertIn(reason, combined)
                    self.assertIn("Tearing down on --gcp-project-id=project-b", combined)
                    self.assertNotIn("Loaded install configuration from", combined)
                    self.assertNotIn("Refusing to tear down", combined)
                    self.assertNotIn("was written for another install", combined)
                    self.assertNotIn("could not be loaded", combined)
                    self.assertNotIn("unbound variable", combined)
                    if dry_run:
                        self.assertEqual(proc.returncode, 0, combined)
                        self.assertIn("Dry-Run Uninstall Preview", combined)
                        self.assertIn("install-b in project-b (us-west1)", combined)

    def test_a_fully_named_piped_teardown_still_reads_a_home_file_that_confirms_it(self):
        """The skip is for guesses the flags do not confirm, not for every
        $HOME file: one recording the same three coordinates is still read, so
        its NAMESPACE and state settings travel with the teardown."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "kube-agents").mkdir(parents=True)
            (home / "kube-agents" / "install.env").write_text(
                'PROJECT_ID="project-b"\nCLUSTER_NAME="install-b"\nREGION="us-west1"\n'
            )

            proc = self._preview(
                tmp,
                home,
                args=("--gcp-project-id=project-b", "--gke-cluster-name=install-b", "--gcp-region=us-west1"),
            )

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertIn(f"Loaded install configuration from: {home}/kube-agents/install.env", combined)
            self.assertNotIn("Not reading", combined)

    def test_a_dry_run_over_another_installs_configuration_warns_and_goes_on(self):
        """A --dry-run preview reports the disagreement and continues, matching
        upgrade.sh's preview split."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "kube-agents").mkdir(parents=True)
            (home / "kube-agents" / "install.env").write_text(
                'PROJECT_ID="my-gcp-project"\nCLUSTER_NAME="install-a"\nREGION="us-central1"\n'
            )

            proc = self._preview(
                tmp,
                home,
                dry_run=True,
                args=("--gke-cluster-name=install-b",),
            )

            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, combined)
            self.assertIn("was written for another install", combined)
            self.assertIn("--gke-cluster-name=install-b, but CLUSTER_NAME=install-a", combined)
            self.assertIn("Dry-Run Uninstall Preview", combined)

    def test_a_shell_exported_coordinate_does_not_blame_install_env_or_mask_a_guess(self):
        """An exported REGION in the operator's shell is neither a key in
        install.env nor an explicit flag: it must not trigger a false conflict
        against --gcp-region, and when no flag is given it must still be warned
        about as a default."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp) / "home"
            (home / "kube-agents").mkdir(parents=True)
            (home / "kube-agents" / "install.env").write_text(
                'PROJECT_ID="my-gcp-project"\nCLUSTER_NAME="install-a"\n'
            )

            proc_flag = self._preview(
                tmp,
                home,
                dry_run=True,
                args=("--gcp-region=us-central1",),
                extra_env={"REGION": "europe-west1"},
            )
            combined_flag = proc_flag.stdout + proc_flag.stderr
            self.assertEqual(proc_flag.returncode, 0, combined_flag)
            self.assertNotIn("was written for another install", combined_flag)
            self.assertNotIn("records a different install", combined_flag)

            proc_guess = self._preview(
                tmp,
                home,
                dry_run=True,
                extra_env={"REGION": "europe-west1"},
            )
            combined_guess = proc_guess.stdout + proc_guess.stderr
            self.assertEqual(proc_guess.returncode, 0, combined_guess)
            self.assertIn("region 'us-central1' is installer_common.sh's default", combined_guess)


if __name__ == "__main__":
    unittest.main()
