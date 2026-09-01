"""Unit tests for install.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin (curl | bash) execution, local script path resolution, and the
NetworkPolicy enablement sequence install.sh runs against adopted clusters.
"""

import os
import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    INSTALLER_HELP_BANNER,
    INVALID_IMMUTABLE_REFS,
    MOCK_GOOGLE_CHAT_MODE,
    VALID_IMMUTABLE_REFS,
    create_mock_git_repo,
    get_isolated_test_env,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL_SH = _REPO_ROOT / "install.sh"
_INSTALLER_COMMON = _REPO_ROOT / "k8s-operator" / "scripts" / "installer_common.sh"

# install.sh sources the shared helpers from the acquired workspace partway
# through main(), so a validator that leans on one is unreachable from a bare
# KUBE_AGENTS_SOURCE_ONLY source. Prepend this to reach it.
_SOURCE_INSTALLER_COMMON = f'source "{_INSTALLER_COMMON}"; '


class InstallScriptValidationTest(unittest.TestCase):
    def _run_install_func(self, func_call, env=None, cwd=None, bin_dir=None):
        """Source install.sh in test mode and run the given function call.

        `bin_dir` is prepended to PATH, for the calls that shell out.
        """
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env, bin_dir=bin_dir)
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
        )

    def test_validate_immutable_ref_accepts_valid_refs(self):
        for ref in VALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_install_func(cmd)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected ref '{ref}' to be valid, stderr: {proc.stderr}",
                )

    def test_validate_immutable_ref_rejects_invalid_refs(self):
        for ref in INVALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_install_func(cmd)
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"install.sh: expected ref '{ref}' to be rejected",
                )

    def test_piped_stdin_executes_main(self):
        """Ensures piped curl | bash invocations execute main and do not exit early."""
        install_script_content = _INSTALL_SH.read_text()
        proc = subprocess.run(
            ["bash", "-s", "--", "--help"],
            input=install_script_content,
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        self.assertEqual(proc.returncode, 0, f"Piped execution failed: {proc.stderr}")
        self.assertIn(INSTALLER_HELP_BANNER, proc.stdout)

    def test_acquire_source_repo_resolves_script_directory(self):
        """Verifies acquire_source_repo finds local repo scripts via BASH_SOURCE."""
        cmd = 'out_dir=""; PARAM_ALLOW_UNVERIFIED_SOURCE=true acquire_source_repo out_dir ""; echo "DIR=$out_dir"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"DIR={_REPO_ROOT}", proc.stdout)

    def test_acquire_source_repo_refuses_to_mutate_dirty_existing_repo(self):
        """Verifies acquire_source_repo uses existing HOME/kube-agents and verify_local_source_ref rejects dirty checkout."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            home_dir = pathlib.Path(temp_dir.name) / "home"
            repo_dir = home_dir / "kube-agents"
            repo_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
            (repo_dir / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_dir), check=True)

            # Make working tree dirty
            (repo_dir / "file.txt").write_text("dirty changes\n")

            outside_dir = pathlib.Path(temp_dir.name) / "outside"
            outside_dir.mkdir()
            isolated_install_sh = outside_dir / "install.sh"
            isolated_install_sh.write_text(_INSTALL_SH.read_text())

            cmd = 'out_dir=""; acquire_source_repo out_dir "0.2.0"'
            setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_install_sh}"
{cmd}
"""
            proc = subprocess.run(
                ["bash", "-c", setup],
                capture_output=True,
                text=True,
                env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
                cwd=str(outside_dir),
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Using existing repository", proc.stdout)
            self.assertIn("without modifying local changes", proc.stdout)
            self.assertIn("dirty checkout", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_acquire_source_repo_uses_clean_existing_repo_without_modifying_changes(self):
        """Verifies acquire_source_repo uses clean existing HOME/kube-agents without mutating branch/checkout."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            home_dir = pathlib.Path(temp_dir.name) / "home"
            repo_dir = home_dir / "kube-agents"
            repo_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
            (repo_dir / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_dir), check=True)

            outside_dir = pathlib.Path(temp_dir.name) / "outside"
            outside_dir.mkdir()
            isolated_install_sh = outside_dir / "install.sh"
            isolated_install_sh.write_text(_INSTALL_SH.read_text())

            cmd = 'out_dir=""; acquire_source_repo out_dir "0.2.0"; echo "RESOLVED=$out_dir"'
            setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_install_sh}"
{cmd}
"""
            proc = subprocess.run(
                ["bash", "-c", setup],
                capture_output=True,
                text=True,
                env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
                cwd=str(outside_dir),
            )
            self.assertEqual(proc.returncode, 0, f"Failed: {proc.stderr}")
            self.assertIn("Using existing repository", proc.stdout)
            self.assertIn("without modifying local changes", proc.stdout)
            self.assertIn(f"RESOLVED={repo_dir}", proc.stdout)
        finally:
            temp_dir.cleanup()

    def test_parse_args_google_chat_mode(self):
        """Verifies parse_args captures --google-chat-mode."""
        cmd = f'parse_args --google-chat-mode={MOCK_GOOGLE_CHAT_MODE}; echo "MODE=$PARAM_GOOGLE_CHAT_MODE"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"MODE={MOCK_GOOGLE_CHAT_MODE}", proc.stdout)

    def test_parse_args_cluster_mode(self):
        """Verifies parse_args captures --cluster-mode."""
        cmd = 'parse_args --cluster-mode=autopilot; echo "MODE=$PARAM_CLUSTER_MODE"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MODE=autopilot", proc.stdout)

    def test_cluster_mode_defaults_to_unset(self):
        """An unpassed --cluster-mode leaves the interview free to ask."""
        proc = self._run_install_func('echo "MODE=[$PARAM_CLUSTER_MODE]"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MODE=[]", proc.stdout)

    def test_require_creatable_cluster_mode_accepts_both_shapes(self):
        for mode in ("autopilot", "standard"):
            with self.subTest(mode=mode):
                proc = self._run_install_func(
                    f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode "{mode}" us-central1'
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_require_creatable_cluster_mode_rejects_an_unknown_shape(self):
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode autopiloot us-central1'
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        # install.sh's print_error writes to stdout.
        self.assertIn("autopiloot", proc.stdout)

    def test_require_creatable_cluster_mode_rejects_a_zone_for_autopilot(self):
        """Autopilot clusters are regional; the module rejects a zone at plan
        time, which is after the whole interview has been paid for."""
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode autopilot us-central1-a'
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("us-central1-a", proc.stdout)
        # Standard clusters are zonal-capable, so the same location is fine.
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}require_creatable_cluster_mode standard us-central1-a'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_resolve_creatable_cluster_mode_defaults_to_autopilot(self):
        """The line that decides what a bare ./install.sh builds.

        install.sh writes CLUSTER_MODE into vars.sh before the tfvars generator
        reads it, so installer_common.sh's own `:-$DEFAULT_CLUSTER_MODE` never
        decides anything for this front door. This is the assertion that goes
        red if the installer default is put back to standard.
        """
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode "" us-central1'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "autopilot")

    def test_resolve_creatable_cluster_mode_honours_an_explicit_request(self):
        for mode in ("standard", "autopilot"):
            with self.subTest(mode=mode):
                proc = self._run_install_func(
                    f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode {mode} us-central1'
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), mode)

    def test_resolve_creatable_cluster_mode_steps_aside_for_a_zone(self):
        """A defaulted Autopilot demotes rather than writing a config Terraform
        rejects. Reachable non-interactively via --cluster-name, where nothing
        else checks the mode/location pair."""
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode "" us-central1-a'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "standard")

    def test_resolve_creatable_cluster_mode_does_not_rescue_an_explicit_autopilot(self):
        """An impossible request stays impossible: the demotion is for a shape
        nobody chose, not a way to silently build something else."""
        proc = self._run_install_func(
            f'{_SOURCE_INSTALLER_COMMON}resolve_creatable_cluster_mode autopilot us-central1-a'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "autopilot")

    def test_main_resolves_the_creatable_shape_through_the_resolver(self):
        """Pins the call site, not just the function.

        resolve_creatable_cluster_mode is covered directly above, but nothing
        made main() consult it: reverting the deciding line to the inline
        `cluster_mode="${cluster_mode:-standard}"` it replaced left every
        installer test green, so the headline behaviour of this change was
        unpinned. main() is the whole interview and is not drivable from a
        unit test, so this asserts on the source the way
        tests/test_install_pubsub_platform.py asserts on workflow YAML.
        """
        source = _INSTALL_SH.read_text()
        self.assertIn(
            'cluster_mode="$(resolve_creatable_cluster_mode "$cluster_mode" "$region")"',
            source,
            "install.sh's interview must resolve the creatable shape through "
            "resolve_creatable_cluster_mode: an inline default is untested and "
            "skips the zonal demotion entirely.",
        )
        self.assertNotRegex(
            source,
            r'cluster_mode="\$\{cluster_mode:-\w+\}"',
            "an inline `:-` default for cluster_mode is the exact shape this "
            "test exists to keep out.",
        )

    def test_cluster_shape_menu_is_ordered_by_the_resolver(self):
        """prompt_menu's enter default is option 1, so a hardcoded
        Autopilot-first order turns pressing enter into an *explicit*
        autopilot request -- which the resolver is then right to refuse to
        demote, aborting a zonal interactive install that used to build
        Standard. Deriving the order keeps the label, the enter key and the
        resolver in agreement at both kinds of location.
        """
        source = _INSTALL_SH.read_text()
        self.assertIn(
            'menu_default="$(resolve_creatable_cluster_mode "" "$region")"',
            source,
            "the cluster-shape menu must take its order from the resolver.",
        )
        # Whichever branch runs, the option carrying "(Default)" is option 1
        # and is the shape its own case arm assigns.
        self.assertRegex(
            source,
            r'"\$\{autopilot_option\} \(Default\)"[\s\S]{0,400}?1\) cluster_mode="autopilot"',
        )
        self.assertRegex(
            source,
            r'"\$\{standard_option\} \(Default\)"[\s\S]{0,400}?1\) cluster_mode="standard"',
        )

    def test_location_is_region_distinguishes_regions_from_zones(self):
        for location, expected in (
            ("us-central1", 0),
            ("europe-west4", 0),
            ("us-central1-a", 1),
            ("europe-west4-b", 1),
        ):
            with self.subTest(location=location):
                proc = self._run_install_func(
                    f'{_SOURCE_INSTALLER_COMMON}location_is_region {location}'
                )
                self.assertEqual(proc.returncode, expected, proc.stdout)

    def _run_persist(self, requested, effective, starting_line):
        """persist_effective_cluster_mode against a throwaway vars.sh."""
        with tempfile.TemporaryDirectory() as tmp:
            vars_file = pathlib.Path(tmp) / "vars.sh"
            vars_file.write_text(starting_line)
            call = (
                f'{_SOURCE_INSTALLER_COMMON}'
                f'VARS_FILE="{vars_file}"; TFVARS_CLUSTER_MODE="{effective}"; '
                f'persist_effective_cluster_mode "{requested}"'
            )
            proc = self._run_install_func(call)
            return proc, vars_file.read_text()

    def test_persist_effective_cluster_mode_records_the_probed_shape(self):
        """vars.sh must record what the install HAS, not what was asked for.

        The generator's probe overrules --cluster-mode on any cluster that
        already exists. Leaving the request on disk is how the value that
        rebuilds a deleted cluster — and that uninstall.sh and upgrade.sh
        regenerate from — comes to name the wrong shape.
        """
        for requested, effective in (
            ("autopilot", "standard"),
            ("standard", "autopilot"),
        ):
            with self.subTest(requested=requested, effective=effective):
                proc, content = self._run_persist(
                    requested, effective, f"export CLUSTER_MODE={requested}\n"
                )
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertIn(f"export CLUSTER_MODE={effective}", content)
                self.assertNotIn(f"export CLUSTER_MODE={requested}", content)

    def test_persist_effective_cluster_mode_leaves_an_agreeing_file_alone(self):
        proc, content = self._run_persist(
            "autopilot", "autopilot", "export CLUSTER_MODE=autopilot\n"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(content, "export CLUSTER_MODE=autopilot\n")

    def test_persist_effective_cluster_mode_without_a_generator_answer(self):
        """No TFVARS_CLUSTER_MODE means the generator never ran; do not guess."""
        with tempfile.TemporaryDirectory() as tmp:
            vars_file = pathlib.Path(tmp) / "vars.sh"
            vars_file.write_text("export CLUSTER_MODE=autopilot\n")
            proc = self._run_install_func(
                f'{_SOURCE_INSTALLER_COMMON}VARS_FILE="{vars_file}"; '
                'persist_effective_cluster_mode standard'
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertEqual(vars_file.read_text(), "export CLUSTER_MODE=autopilot\n")

    def test_parse_args_enable_google_chat(self):
        """Verifies parse_args captures --enable-google-chat."""
        cmd = 'parse_args --enable-google-chat; echo "CHAT=$PARAM_ENABLE_GOOGLE_CHAT"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CHAT=true", proc.stdout)

    def test_parse_args_vertex_location_overrides_the_default(self):
        """An explicit --vertex-location still wins over DEFAULT_VERTEX_LOCATION."""
        cmd = (
            "parse_args --vertex-location=us-east4; "
            'echo "LOC=$PARAM_VERTEX_LOCATION"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LOC=us-east4", proc.stdout)

    def test_default_vertex_location_is_in_scope_for_install_sh(self):
        """install.sh resolves $DEFAULT_VERTEX_LOCATION at its own runtime.

        Both default sites live in run_menu_system/main, which a unit test
        cannot call, so this covers the half that can silently break: whether
        sourcing the helpers actually puts the constant in scope. Under
        `set -u` an unsourced constant would abort rather than expand empty.
        """
        cmd = (
            'source_provisioning_helpers "$PWD" >/dev/null; '
            'echo "LOC=$DEFAULT_VERTEX_LOCATION"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LOC=global", proc.stdout)

    def test_vertex_location_defaults_never_fall_back_to_the_region(self):
        """Every vertex_location default in install.sh uses the shared constant.

        Defaulting the Vertex location to the cluster region is the bug: the
        vertex_ai default model is not served from DEFAULT_REGION, and on a
        zonal cluster the region variable is not even a valid Vertex location.
        There are two such sites -- the main install path and the --menu
        reconfigure path -- and missing either leaves the broken value reachable.
        """
        defaults = [
            line.strip()
            for line in _INSTALL_SH.read_text().splitlines()
            if re.match(r"^\s*local vertex_location=", line)
        ]
        self.assertEqual(len(defaults), 2, f"unexpected vertex_location sites: {defaults}")
        for line in defaults:
            with self.subTest(line=line):
                self.assertIn("DEFAULT_VERTEX_LOCATION", line)
                self.assertNotIn("$region", line)

    def test_default_image_tag_returns_baked_release_version(self):
        """Verifies default_image_tag prioritizes BAKED_RELEASE_VERSION when defined."""
        cmd = 'BAKED_RELEASE_VERSION="0.2.0"; default_image_tag'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "0.2.0")

    def test_default_image_tag_label_returns_official_release(self):
        """Verifies default_image_tag_label formats baked release version label."""
        cmd = 'BAKED_RELEASE_VERSION="0.2.0"; default_image_tag_label'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "official release 0.2.0")

    def test_default_image_tag_falls_back_to_head_sha(self):
        """Verifies default_image_tag defaults to local HEAD SHA in developer checkouts."""
        cmd = 'BAKED_RELEASE_VERSION=""; default_image_tag'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(
            proc.stdout.strip(),
            r"^([0-9a-f]{40}|[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?)$",
            f"Expected valid 40-character SHA or SemVer tag, got: {proc.stdout.strip()}",
        )

    def test_default_image_tag_resolves_semver_when_multiple_tags_present(self):
        """Verifies default_image_tag prefers numeric SemVer tag over rc_*_validated tags on the same commit."""
        temp_dir, repo_dir, git = create_mock_git_repo()
        try:
            # Add installer_common.sh so repo is recognized as kube-agents
            scripts_dir = pathlib.Path(repo_dir) / "k8s-operator" / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            (scripts_dir / "installer_common.sh").write_text("# mock installer_common.sh\n")
            git("add", "k8s-operator/scripts/installer_common.sh")
            git("commit", "-m", "chore: add installer_common.sh")

            # Apply both an rc_* tag and a 0.2.0 GA tag on the same commit
            git("tag", "rc_20260827_validated")
            git("tag", "0.2.0")

            cmd = 'BAKED_RELEASE_VERSION=""; default_image_tag'
            proc = self._run_install_func(cmd, cwd=repo_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0.2.0")
        finally:
            temp_dir.cleanup()

    def test_default_image_tag_extracts_version_from_archive_directory(self):
        """Verifies default_image_tag resolves version from unpacked archive directory name."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="archive-test-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.2.0"
            archive_dir.mkdir(parents=True)
            scripts_dir = archive_dir / "k8s-operator" / "scripts"
            scripts_dir.mkdir(parents=True)
            (scripts_dir / "installer_common.sh").write_text("# mock installer_common.sh\n")

            cmd = 'BAKED_RELEASE_VERSION=""; default_image_tag'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0.2.0")

    def test_verify_local_source_ref_accepts_baked_release_in_non_git_dir(self):
        """Verifies verify_local_source_ref succeeds for unpacked release archive without Git repository."""
        with tempfile.TemporaryDirectory(prefix="unpacked-release-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.2.0"
            archive_dir.mkdir(parents=True)

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{archive_dir}" "0.2.0"'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Verified install sources match baked official release 0.2.0", proc.stdout)

    def test_verify_local_source_ref_in_git_worktree_enforces_git_alignment_even_with_baked_version(self):
        """Verifies verify_local_source_ref strictly runs Git alignment in real Git checkouts even with baked version."""
        with tempfile.TemporaryDirectory(prefix="git-repo-") as repo_dir:
            repo_path = pathlib.Path(repo_dir)
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
            (repo_path / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_path), check=True)

            # Add an uncommitted modification to make working tree dirty
            (repo_path / "file.txt").write_text("dirty uncommitted change\n")

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{repo_path}" "0.2.0"'
            proc = self._run_install_func(cmd, cwd=repo_path)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("dirty checkout", proc.stdout)

    def test_gvisor_defaults_to_on(self):
        """The agent runs model-authored commands; the sandbox is the default."""
        proc = self._run_install_func('echo "GVISOR=$PARAM_ENABLE_GVISOR"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GVISOR=true", proc.stdout)

    def test_parse_args_keeps_an_empty_gvisor_value_empty(self):
        """`--gvisor=` must reach main's validator rather than read as a default.

        main uses ${PARAM_ENABLE_GVISOR-true} for exactly this: parse_args
        leaves the empty string in place, the `:-` form would silently
        substitute it back to the default, and the validator rejects it.
        """
        cmd = 'parse_args --gvisor=; echo "GVISOR=[$PARAM_ENABLE_GVISOR]"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("GVISOR=[]", proc.stdout)

    def test_prompt_menu_defaults_to_the_first_option(self):
        """The premise the gVisor prompt's ordering rests on.

        main lists the incoming value as option 1 and treats option 2 as "the
        other one", so that answering the prompt with nothing confirms what
        `--gvisor` asked for and the `(Default)` label matches what that
        produces. It holds only while prompt_menu resolves an unanswered
        prompt to option 1; if that moves, the prompt starts inverting the
        caller's choice in silence.

        With no controlling TTY this takes prompt_read's auto-select branch
        rather than a literal empty line, but both resolve through the same
        default_val="1" that prompt_menu passes.
        """
        cmd = (
            'gvisor_choice=""; prompt_menu "Pick" "first" "second" gvisor_choice; '
            'echo "CHOICE=$gvisor_choice"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CHOICE=1", proc.stdout)

    def _run_with_kubectl_stub(self, func_call, kubectl_script, env=None):
        """Run `func_call` with a stub `kubectl` on PATH.

        `@COUNTER@` in either string becomes a scratch file private to this
        run, for a stub that has to answer differently on each call.

        The poll interval is flattened after sourcing rather than through the
        environment: install.sh assigns it outright, the way it does every
        other timing constant, so only a post-source assignment takes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            counter = str(pathlib.Path(tmp) / "calls")
            kubectl = bin_dir / "kubectl"
            kubectl.write_text(
                "#!/usr/bin/env bash\n" + kubectl_script.replace("@COUNTER@", counter) + "\n"
            )
            kubectl.chmod(kubectl.stat().st_mode | stat.S_IEXEC)
            return self._run_install_func(
                "DEPLOYMENT_POLL_INTERVAL_SECS=0\n" + func_call.replace("@COUNTER@", counter),
                env=env,
                bin_dir=str(bin_dir),
            )

    def test_wait_for_deployment_object_returns_once_it_exists(self):
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_deployment_object dep ns 0 || rc=$?; echo "RC=$rc"',
            "exit 0",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)

    def test_wait_for_deployment_object_waits_for_a_late_deployment(self):
        """The reason the health check waits rather than asking once.

        The operator writes the agent Deployment after the apply returns, and
        later still when it has a RuntimeClass to resolve first, so a single
        unretried `kubectl get` reports a Deployment that is merely late as one
        that was never created.
        """
        stub = (
            'n=$(cat @COUNTER@ 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > @COUNTER@; '
            '[ "$n" -ge 3 ] && exit 0; exit 1'
        )
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_deployment_object dep ns 30 || rc=$?; '
            'echo "RC=$rc TRIES=$(cat @COUNTER@)"',
            stub,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0 TRIES=3", proc.stdout)

    def test_wait_for_deployment_object_gives_up_after_the_budget(self):
        """A Deployment that is never coming still has to end the run."""
        proc = self._run_with_kubectl_stub(
            'rc=0; wait_for_deployment_object dep ns 0 || rc=$?; echo "RC=$rc"',
            "exit 1",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=1", proc.stdout)


class EnsureExistingClusterNetworkPolicyTest(unittest.TestCase):
    """ensure_existing_cluster_network_policy's two-call enablement sequence.

    GKE rejects `--enable-network-policy` with HTTP 400 until the Calico addon
    is on the control plane, and gcloud refuses `--update-addons` and
    `--enable-network-policy` in one invocation, so the order of the two
    `clusters update` calls is the behaviour under test.
    """

    def _run(self, datapath="", legacy_np=""):
        """Run the function against a stub gcloud that records every call.

        Returns (CompletedProcess, [argv-strings in call order]). The stub
        answers `clusters describe` on the --format it is given: an empty
        string stands for a field gcloud did not print.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            log = pathlib.Path(tmp) / "gcloud.log"
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                'case "$*" in\n'
                f"  *datapathProvider*) printf '{datapath}\\n' ;;\n"
                f"  *networkPolicy.enabled*) printf '{legacy_np}\\n' ;;\n"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                "ensure_existing_cluster_network_policy proj cluster region\n"
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return proc, calls

    @staticmethod
    def _updates(calls):
        return [c for c in calls if "clusters update" in c]

    def test_addon_is_enabled_before_enforcement(self):
        # The bug: a lone --enable-network-policy against a cluster whose
        # addon is off fails with "The network policy addon must be enabled
        # before updating the nodes" (HTTP 400).
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        updates = self._updates(calls)
        self.assertEqual(len(updates), 2, updates)
        self.assertIn("--update-addons=NetworkPolicy=ENABLED", updates[0])
        self.assertIn("--enable-network-policy", updates[1])
        # Neither call may carry both flags: gcloud puts them in the same
        # "exactly one of these must be specified" group.
        self.assertNotIn("--enable-network-policy", updates[0])
        self.assertNotIn("--update-addons", updates[1])

    def test_addon_state_is_not_probed(self):
        # Skipping the addon call when it is already on would be free, but
        # addonsConfig.networkPolicyConfig.disabled cannot say so: GKE omits
        # false booleans, so "on" and "describe failed" both print nothing.
        # A gate on it either never fires or reintroduces the 400 — hence the
        # unconditional call, and hence this test, which fails if someone
        # reintroduces the probe.
        _, calls = self._run()
        self.assertEqual(
            [c for c in calls if "networkPolicyConfig" in c], [], calls
        )

    def test_dataplane_v2_cluster_is_left_alone(self):
        _, calls = self._run(datapath="ADVANCED_DATAPATH")
        self.assertEqual(self._updates(calls), [])

    def test_cluster_already_enforcing_is_left_alone(self):
        _, calls = self._run(legacy_np="True")
        self.assertEqual(self._updates(calls), [])


class ImportGithubPemKmsKeyTest(unittest.TestCase):
    """The KMS signing key import_github_pem creates for the token minter.

    KMS refuses an import-only key created without
    --skip-initial-version-creation -- `INVALID_ARGUMENT: Import-only keys
    must skip initial version creation` -- which made the minter impossible
    to provision at all. The flag sits mid-way through a five-line wrapped
    invocation, so dropping it again would look like nothing in a diff.
    """

    def _run(self, creates_fail=False):
        """import_github_pem against a stub gcloud that records every call.

        The stub reports no ENABLED key version, so the import is not
        short-circuited, and fails `keys describe`, which takes the
        could-not-be-confirmed branch. That branch returns before the Minty
        CLI clone, which is what keeps this a unit test.

        creates_fail makes both `kms … create` calls exit non-zero on stderr,
        the way KMS answers a re-run once the keyring exists. That is the only
        path that exercises the error capture at all, so the default of 0
        leaves it untested -- see the ERR-trap test below.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = pathlib.Path(tmp) / "bin"
            bin_dir.mkdir()
            log = pathlib.Path(tmp) / "gcloud.log"
            pem = pathlib.Path(tmp) / "app.pem"
            pem.write_text("-----BEGIN RSA PRIVATE KEY-----\n")
            create_case = (
                "  *'kms keyrings create'* | *'kms keys create'*)\n"
                "    echo 'ALREADY_EXISTS: it already exists' >&2; exit 1 ;;\n"
                if creates_fail
                else ""
            )
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                'case "$*" in\n'
                "  *'kms keys versions list'*) exit 0 ;;\n"
                "  *'kms keys describe'*) exit 1 ;;\n"
                f"{create_case}"
                "esac\n"
                "exit 0\n"
            )
            gcloud.chmod(gcloud.stat().st_mode | stat.S_IEXEC)
            body = (
                f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                f'source "{_INSTALLER_COMMON}"\n'
                "GITHUB_ORG=an-org GITHUB_REPO=a-repo GITHUB_APP_ID=12345 "
                f'GITHUB_PEM_PATH="{pem}" import_github_pem a-project us-central1-a\n'
            )
            proc = subprocess.run(
                ["bash", "-c", body],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(bin_dir=str(bin_dir)),
                cwd=str(_REPO_ROOT),
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return proc, calls

    def test_the_import_only_key_is_created_skipping_the_initial_version(self):
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        creates = [c for c in calls if "kms keys create" in c]
        self.assertEqual(
            len(creates), 1, f"expected exactly one `kms keys create`, got: {calls}"
        )
        create = creates[0]
        for flag in (
            "--skip-initial-version-creation",
            "--import-only",
            "--purpose=asymmetric-signing",
        ):
            self.assertIn(
                flag,
                create,
                f"`gcloud kms keys create` must pass {flag}; KMS rejects an "
                f"import-only key without --skip-initial-version-creation. Call: {create}",
            )

    def test_a_zonal_region_is_reduced_to_the_kms_region(self):
        """KMS locations are regional. The caller passes install.sh's --region,
        which may be a zone."""
        _, calls = self._run()
        creates = [c for c in calls if "kms keys create" in c]
        self.assertIn("--location=us-central1 ", creates[0] + " ", creates)

    def test_a_key_that_cannot_be_confirmed_warns_instead_of_importing(self):
        """The describe assertion, not the create, is what surfaces a failure.

        Without it the run continues to the PEM import and fails two steps
        later against a key that is not there.
        """
        proc, calls = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # install.sh's print_warning / print_info write to stdout.
        self.assertIn("could not be confirmed to exist", proc.stdout)
        self.assertIn("--skip-initial-version-creation", proc.stdout)
        self.assertEqual(
            [c for c in calls if "versions import" in c],
            [],
            "the PEM must not be imported into a key that could not be confirmed",
        )

    def test_a_failing_create_is_reported_without_a_spurious_abort_banner(self):
        """"Already exists" is the expected answer on a re-run, not an abort.

        install.sh:54 installs an ERR trap, and bash 3.2 -- macOS's /bin/bash,
        the curl|bash audience -- runs an inherited ERR trap inside a command
        substitution even when `|| true` handles the failure outside it. Without
        `trap - ERR` in the substitution the ordinary re-run prints on_error's
        fatal banner twice and leaves a FAILED install report behind, while the
        install carries on regardless.
        """
        proc, _ = self._run(creates_fail=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(
            "Error encountered",
            combined,
            "a handled `gcloud kms ... create` failure must not fire the ERR trap; "
            "add `trap - ERR` inside the command substitution",
        )
        # The other half of the hunk's purpose: the captured stderr is surfaced
        # rather than discarded, which is what 2>/dev/null used to hide.
        self.assertIn("ALREADY_EXISTS: it already exists", proc.stdout)


if __name__ == "__main__":
    unittest.main()
