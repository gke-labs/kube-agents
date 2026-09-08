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
from tests.testing.release import (
    MOCK_RELEASE_BUNDLE_VERSION,
    create_mock_release_bundle_marker,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL_SH = _REPO_ROOT / "install.sh"
_INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

# install.sh sources the shared helpers from the acquired workspace partway
# through main(), so a validator that leans on one is unreachable from a bare
# KUBE_AGENTS_SOURCE_ONLY source. Prepend this to reach it.
_SOURCE_INSTALLER_COMMON = f'source "{_INSTALLER_COMMON}"; '


class InstallScriptValidationTest(unittest.TestCase):
    def setUp(self):
        """Pin the install configuration to an empty file.

        install.sh loads install.env at source time, so a developer who has a
        real one in this checkout would have its values seeded into every
        PARAM_* these tests read -- and the suite would pass or fail depending
        on whose machine it ran on. Tests that are about the loading itself set
        KUBE_AGENTS_INSTALL_ENV themselves; everything else gets nothing.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self._empty_install_env = pathlib.Path(tmp.name) / "install.env"
        self._empty_install_env.write_text("")

    def _run_install_func(self, func_call, env=None, cwd=None, bin_dir=None):
        """Source install.sh in test mode and run the given function call.

        `bin_dir` is prepended to PATH, for the calls that shell out.
        """
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"
{func_call}
"""
        overrides = {"KUBE_AGENTS_INSTALL_ENV": str(self._empty_install_env)}
        overrides.update(env or {})
        full_env = get_isolated_test_env(overrides=overrides, bin_dir=bin_dir)
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

        install.sh exports CLUSTER_MODE before the tfvars generator
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
        unit test, so this asserts on the source directly.
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

    def test_the_probed_cluster_shape_is_never_written_back(self):
        """There is no persist_effective_cluster_mode, and there must not be.

        It existed so that a later run would not rebuild a deleted cluster in
        the wrong shape, by recording the probe's answer over the interview's.
        That is unnecessary -- write_tfvars_from_state re-probes every run and
        every branch with a live cluster takes the mode from the probe, so a
        stale configured value can never reach a running cluster's tfvars --
        and it was the one place the installer wrote its own findings back into
        the file it reads as configuration. A file that is an input and an
        output at once is the property this refactor removes, so a
        reintroduction is a regression even though it would look like a fix.
        """
        source = _INSTALL_SH.read_text()
        # The name still appears, in the comment explaining why it is gone.
        # What must not come back is a definition or a call.
        # re.MULTILINE, or `^` anchors at offset 0 only and neither guard can
        # ever fail however the function comes back.
        self.assertNotRegex(
            source,
            re.compile(r"^\s*persist_effective_cluster_mode\s*\(\)", re.MULTILINE),
            "persist_effective_cluster_mode must not be redefined",
        )
        self.assertNotRegex(
            source,
            re.compile(r"^\s*persist_effective_cluster_mode\s+", re.MULTILINE),
            "persist_effective_cluster_mode must not be called",
        )
        self.assertNotIn(
            "save_var CLUSTER_MODE",
            source,
            "the probed shape must not be written back into the install "
            "configuration; the probe is authoritative on every run",
        )

    def test_the_installer_no_longer_writes_the_state_file(self):
        """vars.sh is read as a legacy input and never generated.

        Regenerating it would put the old two-file model back: a derived file
        that other tools read, drifting from the input that actually decides
        the install.
        """
        source = _INSTALL_SH.read_text()
        self.assertNotIn(
            "write_state_var",
            source,
            "install.sh must not write vars.sh; install.env is the input and "
            "terraform.tfvars the only derived artifact",
        )
        self.assertIn(
            "load_legacy_vars_file",
            source,
            "an existing install's vars.sh must still be read, so upgrading "
            "needs no action from its owner",
        )

    def test_parse_args_enable_google_chat(self):
        """Verifies parse_args captures --enable-google-chat."""
        cmd = 'parse_args --enable-google-chat; echo "CHAT=$PARAM_ENABLE_GOOGLE_CHAT"'
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("CHAT=true", proc.stdout)

    def test_parse_args_plugin_flags(self):
        """Verifies parse_args captures plugin enablement flags."""
        cmd = (
            'parse_args --enable-pubsub-platform --enable-stockout-investigator; '
            'echo "PUBSUB=$PARAM_ENABLE_PUBSUB_PLATFORM STOCKOUT=$PARAM_ENABLE_STOCKOUT_INVESTIGATOR"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PUBSUB=true STOCKOUT=true", proc.stdout)

    def test_parse_args_vertex_manage_serving_project_flag_is_read(self):
        """--vertex-manage-serving-project=false reaches its PARAM_ unchanged;
        normalisation to true/false happens where it is consumed."""
        cmd = (
            "parse_args --vertex-manage-serving-project=false; "
            'echo "MANAGE=$PARAM_VERTEX_MANAGE_SERVING_PROJECT"'
        )
        proc = self._run_install_func(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("MANAGE=false", proc.stdout)

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
            scripts_dir = pathlib.Path(repo_dir) / "scripts" / "installer"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            (scripts_dir / "installer_common.sh").write_text("# mock installer_common.sh\n")
            git("add", "scripts/installer/installer_common.sh")
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
            scripts_dir = archive_dir / "scripts" / "installer"
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

    def test_verify_local_source_ref_accepts_release_bundle_marker_in_non_git_dir(self):
        """Verifies verify_local_source_ref logs bundle provenance attribution when .release-bundle matches baked version."""
        with tempfile.TemporaryDirectory(prefix="unpacked-bundle-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION="{MOCK_RELEASE_BUNDLE_VERSION}"; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"Verified install sources match official release bundle {MOCK_RELEASE_BUNDLE_VERSION}", proc.stdout)

    def test_verify_local_source_ref_rejects_unbaked_release_bundle_marker_without_override(self):
        """Verifies .release-bundle marker cannot bypass unversioned source directory rejection when baked version is empty."""
        with tempfile.TemporaryDirectory(prefix="unpacked-unbaked-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION=""; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_install_func(cmd, cwd=archive_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing to provision from an unversioned source directory", proc.stdout)

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


class InstallEnvInputTest(unittest.TestCase):
    """install.env is an input, loaded before the parameter block.

    The ordering is the mechanism: every `PARAM_X="${VAR:-}"` seed already knew
    how to inherit from the environment, and loading the file into the
    environment first is what makes inheritance the default path rather than
    something each flag has to remember. That is what closes #1060 as a class
    instead of patching its eight instances, so these tests are about the
    inheritance itself, not about any one flag.
    """

    def _source_with_env_file(self, body, contents=None, env=None, path=None):
        """Source install.sh with KUBE_AGENTS_INSTALL_ENV pointing at a file.

        The explicit path rather than the beside-the-script discovery: a
        developer's real install.env would otherwise decide the result. The
        discovery itself is covered separately below.
        """
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / (path or "install.env")
            if contents is not None:
                env_file.write_text(contents)
            overrides = {"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
            overrides.update(env or {})
            # Cleared so an exported value from the developer's own shell
            # cannot stand in for the file under test.
            full_env = get_isolated_test_env(overrides=overrides)
            for leaking in (
                "PROJECT_ID", "REGION", "CLUSTER_NAME", "MODEL_PROVIDER",
                "ENABLE_GVISOR", "MEMORY", "MEMORY_PROVIDER", "ALLOWED_USERS",
                "GOOGLE_CHAT_ENABLED", "API_SERVER_KEY", "ENABLE_WEBUI",
                "HERMES_DASHBOARD_ENABLED", "PLATFORM_AGENT_PERMISSION_SET",
            ):
                full_env.pop(leaking, None)
            full_env.update(overrides)
            setup = f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}\n'
            return subprocess.run(
                ["bash", "-c", setup],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    def test_values_reach_the_parameter_block(self):
        """The whole point: a value in the file arrives as a PARAM_*."""
        proc = self._source_with_env_file(
            'echo "P=$PARAM_PROJECT_ID R=$PARAM_REGION M=$PARAM_MODEL_PROVIDER"',
            contents="PROJECT_ID=from-the-file\nREGION=europe-west4\nMODEL_PROVIDER=vertex_ai\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("P=from-the-file R=europe-west4 M=vertex_ai", proc.stdout)

    def test_a_flag_beats_the_file(self):
        """Order of authority: flag, then file, then default."""
        proc = self._source_with_env_file(
            'parse_args --project-id=from-the-flag; echo "P=$PARAM_PROJECT_ID"',
            contents="PROJECT_ID=from-the-file\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("P=from-the-flag", proc.stdout)

    def test_the_values_are_exported_not_merely_assigned(self):
        """write_tfvars_from_state and the TF_VAR_* handoff read the
        environment, so a value that parsed but did not export would reach
        neither. `set -a` around the source is what guarantees it."""
        proc = self._source_with_env_file(
            "bash -c 'echo EXPORTED=\"$PROJECT_ID\"'",
            contents="PROJECT_ID=travels-to-children\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("EXPORTED=travels-to-children", proc.stdout)

    def test_a_named_file_that_is_absent_is_an_error(self):
        """Only reachable through an explicit KUBE_AGENTS_INSTALL_ENV. Asking
        for a path by name and not getting it is a mistake, not a first
        install, and silently continuing would provision from defaults."""
        proc = self._source_with_env_file("true", contents=None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stdout + proc.stderr)

    def test_an_unparseable_file_is_reported_by_name(self):
        """Sourcing it would abort through the ERR trap with a bash parse
        error and no indication of which file was at fault."""
        proc = self._source_with_env_file("true", contents='PROJECT_ID="unclosed\n')
        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("not valid shell", combined)

    def test_no_file_at_all_is_the_ordinary_first_install(self):
        """A first install has nothing to inherit and must not be blocked."""
        full_env = get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": ""})
        proc = subprocess.run(
            ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"; echo OK'],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(tempfile.gettempdir()),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("OK", proc.stdout)

    def test_loading_says_nothing_on_stdout(self):
        """Sourcing install.sh must leave stdout clean.

        The load happens at source time, before main(), so a message on stdout
        lands in front of whatever the caller captures next -- including a
        function's echoed return value, which is how most of this file's tests
        read install.sh. That made the suite pass or fail depending on whether
        the developer running it happened to have an install.env, which is the
        worst kind of flake: it looks like the change under test.
        """
        proc = self._source_with_env_file(
            'printf "%s" "ONLY-THIS"',
            contents="PROJECT_ID=noisy\n",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Byte-for-byte: a function that echoes its answer is read exactly this
        # way, so anything else on stdout corrupts it.
        self.assertEqual(proc.stdout, "ONLY-THIS")
        self.assertIn("Loaded install configuration", proc.stderr)

    def test_it_is_discovered_beside_the_script(self):
        """The documented location, and the one a curl | bash install into a
        working directory also finds."""
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "install.sh").write_text(_INSTALL_SH.read_text())
            (home / "install.env").write_text("PROJECT_ID=found-beside-the-script\n")
            proc = subprocess.run(
                [
                    "bash",
                    "-c",
                    f'KUBE_AGENTS_SOURCE_ONLY=true source "{home}/install.sh"; '
                    'echo "P=$PARAM_PROJECT_ID"',
                ],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(overrides={"KUBE_AGENTS_INSTALL_ENV": ""}),
                cwd=str(home),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("P=found-beside-the-script", proc.stdout)


class NonInteractiveRerunInheritanceTest(unittest.TestCase):
    """The eight settings #1060 names, each checked for inheritance.

    Every one of these destroyed something when a non-interactive re-run
    omitted its flag: the Pub/Sub topic, kubeagents-litellm-gsa, the gVisor
    pool, the custom role list, a Hindsight deployment, the GitOps org, the
    allowlist that keeps the agent private, and the Secret every pod holds.
    """

    def _params(self, contents, body):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(contents)
            full_env = get_isolated_test_env(
                overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
            )
            return subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n{body}\n'],
                capture_output=True,
                text=True,
                env=full_env,
                cwd=str(_REPO_ROOT),
            )

    def test_google_chat_inherits_the_way_slack_already_did(self):
        """Google Chat inherits from the loaded configuration, as Slack does.

        The chat gate reads SLACK_ENABLED out of the file; PARAM_ENABLE_GOOGLE_CHAT
        taking the flag alone would revert Chat -- and only Chat -- to false and
        plan its Pub/Sub topic and subscription away. (see #1060)
        """
        proc = self._params(
            "GOOGLE_CHAT_ENABLED=true\n", 'echo "C=$PARAM_ENABLE_GOOGLE_CHAT"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("C=true", proc.stdout)

    def test_the_settings_that_seeded_from_their_own_name(self):
        """Model provider, gVisor, permission set and custom roles, GitOps org.
        These already read an environment variable of the right name; what they
        never had was a file to read it from."""
        proc = self._params(
            "MODEL_PROVIDER=vertex_ai\n"
            "ENABLE_GVISOR=true\n"
            "PLATFORM_AGENT_PERMISSION_SET=custom\n"
            "PLATFORM_AGENT_CUSTOM_ROLES=roles/container.viewer\n"
            "GITHUB_ORG=an-org\n"
            "GITHUB_REPO=a-repo\n",
            'echo "M=$PARAM_MODEL_PROVIDER G=$PARAM_ENABLE_GVISOR '
            'P=$PARAM_PERMISSION_SET C=$PARAM_CUSTOM_ROLES '
            'O=$PARAM_GITOPS_ORG R=$PARAM_GITOPS_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn(
            "M=vertex_ai G=true P=custom C=roles/container.viewer O=an-org R=a-repo",
            proc.stdout,
        )

    def test_memory_inherits_through_the_recorded_spelling(self):
        """--memory and the recorded setting are spelled differently.

        The flag is --memory (file|hindsight|off) and the install records
        MEMORY_PROVIDER, so a file written by a previous install carries only the
        second spelling. Without the translation, omitting --memory deletes a
        Hindsight API and its Postgres. (see #1060)
        """
        for provider, expected in (
            ("kube_agents_memory", "hindsight"),
            ("none", "off"),
            ("multiuser_memory", "file"),
        ):
            with self.subTest(provider=provider):
                proc = self._params(
                    f"MEMORY_PROVIDER={provider}\n", 'echo "M=$PARAM_MEMORY"'
                )
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertIn(f"M={expected}", proc.stdout)

    def test_memory_prefers_the_input_spelling_when_both_are_present(self):
        proc = self._params(
            "MEMORY=off\nMEMORY_PROVIDER=kube_agents_memory\n", 'echo "M=$PARAM_MEMORY"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("M=off", proc.stdout)

    def test_the_dashboard_inherits_through_its_recorded_spelling_too(self):
        proc = self._params(
            "HERMES_DASHBOARD_ENABLED=true\n", 'echo "W=$PARAM_ENABLE_WEBUI"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("W=true", proc.stdout)

    def test_allowed_users_has_a_flag_and_inherits(self):
        """The allowlist survives a non-interactive re-run.

        An empty list allows every user, so losing it opens the agent rather
        than merely dropping a setting. (see #1060)
        """
        proc = self._params(
            "ALLOWED_USERS=a@example.com,b@example.com", 'echo "U=$PARAM_ALLOWED_USERS"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("U=a@example.com,b@example.com", proc.stdout)

        proc = self._params(
            "ALLOWED_USERS=from-the-file@example.com",
            'parse_args --allowed-users=from-the-flag@example.com; '
            'echo "U=$PARAM_ALLOWED_USERS"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("U=from-the-flag@example.com", proc.stdout)

    def test_the_gitops_repo_names_are_gitops_prefixed(self):
        """GITOPS_ORG / GITOPS_REPO are the installer's input names. (see #1026)

        The old pair collided with two other things: GH_ORG / GH_REPO on the rc
        and nightly environments name the *release* repository, and tests/e2e
        uses GITHUB_ORG / GITHUB_REPO for the repository a test acts on. Three
        repositories, two names.
        """
        proc = self._params(
            "GITOPS_ORG=an-org\nGITOPS_REPO=a-repo\n",
            'echo "O=$PARAM_GITOPS_ORG R=$PARAM_GITOPS_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=an-org R=a-repo", proc.stdout)

    def test_the_old_names_still_work_and_say_so(self):
        """A deprecation, not a break: an install.env or a CI environment still
        carrying GITHUB_ORG / GITHUB_REPO keeps working, and is told to rename."""
        proc = self._params(
            "GITHUB_ORG=an-org\nGITHUB_REPO=a-repo\n",
            'source scripts/installer/installer_common.sh; '
            'normalize_gitops_repo_vars; '
            'echo "O=$GITOPS_ORG R=$GITOPS_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=an-org R=a-repo", proc.stdout)
        combined = proc.stdout + proc.stderr
        self.assertIn("GITHUB_ORG is deprecated", combined)
        self.assertIn("GITOPS_ORG", combined)

    def test_the_new_names_win_over_the_old(self):
        """Both present is a mid-migration environment, not an error. The name
        that survives is the one being migrated to."""
        proc = self._params(
            "GITHUB_ORG=old-org\nGITOPS_ORG=new-org\n",
            'source scripts/installer/installer_common.sh; '
            'normalize_gitops_repo_vars; echo "O=$GITOPS_ORG"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=new-org", proc.stdout)

    def test_the_old_names_are_kept_in_step_for_one_release(self):
        """The agent runtime and the chart still speak GITHUB_*. They are
        exported FROM the GITOPS_* value rather than left as a second source of
        truth, so the two can never disagree."""
        proc = self._params(
            "GITOPS_ORG=new-org\nGITOPS_REPO=new-repo\n",
            'source scripts/installer/installer_common.sh; '
            'normalize_gitops_repo_vars; echo "O=$GITHUB_ORG R=$GITHUB_REPO"',
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("O=new-org R=new-repo", proc.stdout)

    def test_the_api_server_key_is_not_minted_by_install_sh(self):
        """API_SERVER_KEY is minted inside write_tfvars_from_state, after recovery.

        The generator's recovery loop skips any key already set, so a key
        exported before it shadows the live Secret: every run would replace the
        Secret and restart every pod. (see #1060)
        """
        source = _INSTALL_SH.read_text()
        self.assertNotIn(
            "openssl rand -hex 16",
            source,
            "install.sh must not mint an API_SERVER_KEY before the generator "
            "has had a chance to recover the live one",
        )
        self.assertIn(
            "KUBE_AGENTS_GENERATE_API_SERVER_KEY=true",
            source,
            "install.sh is the one front door entitled to mint a key, and says so",
        )

    def test_a_configured_api_server_key_is_carried_through(self):
        proc = self._params(
            "API_SERVER_KEY=deadbeefdeadbeef\n", 'echo "K=$API_SERVER_KEY"'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("K=deadbeefdeadbeef", proc.stdout)


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
                "GITOPS_ORG=an-org GITOPS_REPO=a-repo GITHUB_APP_ID=12345 "
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


class InstallEnvIsCreatedInTheCheckoutTest(unittest.TestCase):
    """The configuration file has to land where every other front door looks.

    Under `curl … | bash` -- Method 0 in INSTALL.md, the documented fastest
    install -- ${BASH_SOURCE[0]} names no file, so a script-relative path
    resolves to whatever directory the operator was standing in.
    acquire_source_repo then clones to $HOME/kube-agents and cd's there, while
    every other reader resolves ${repo_dir}/install.env
    (default_install_env_file). Freezing the invocation directory dropped the
    whole configuration -- API_SERVER_KEY and the plaintext model keys included
    -- somewhere no later run would look: upgrade.sh hit its fail-closed
    branch, and a re-run of the one-liner rebuilt every PARAM_* from defaults,
    which is the #1060 class this change exists to close.
    """

    def _resolved_paths(self, cwd, home, extra_env=None):
        """What install.sh picks for install.env and the legacy vars.sh.

        Piped into `bash -s` rather than sourced by path, because that is the
        whole point: `source /abs/path/install.sh` sets BASH_SOURCE and the
        script can see where it lives, while `curl … | bash` leaves the array
        empty and `${BASH_SOURCE[0]:-.}` collapses to the working directory.
        Sourcing by path here would exercise the one case that never had the
        bug.
        """
        overrides = {"HOME": str(home), "KUBE_AGENTS_SOURCE_ONLY": "true"}
        overrides.update(extra_env or {})
        # KUBE_AGENTS_INSTALL_ENV is what get_isolated_test_env normally pins;
        # these cases are about the fallback that runs when it is unset.
        full_env = get_isolated_test_env(overrides=overrides)
        if "KUBE_AGENTS_INSTALL_ENV" not in (extra_env or {}):
            full_env.pop("KUBE_AGENTS_INSTALL_ENV", None)
        script = _INSTALL_SH.read_text() + (
            '\necho "ENV=$INSTALL_ENV_FILE"\necho "LEGACY=$LEGACY_VARS_FILE"\n'
        )
        return subprocess.run(
            ["bash", "-s"], input=script,
            capture_output=True, text=True, env=full_env, cwd=str(cwd),
        )

    def test_a_checkout_run_uses_the_checkout(self):
        """The ordinary `./install.sh` case, unchanged: the script sits in a
        checkout, so that checkout is where the file belongs."""
        with tempfile.TemporaryDirectory() as home:
            proc = self._resolved_paths(_REPO_ROOT, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"ENV={_REPO_ROOT}/install.env", proc.stdout)

    def test_a_piped_run_from_elsewhere_uses_the_clone_not_the_cwd(self):
        """Standing in a directory that is not a checkout, with no install.env to
        hand, the file must be destined for the clone acquire_source_repo will
        make -- not for the cwd."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            proc = self._resolved_paths(tmp, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"ENV={home}/kube-agents/install.env", proc.stdout)
            self.assertNotIn(f"ENV={tmp}/install.env", proc.stdout)

    def test_an_install_env_the_operator_placed_still_wins(self):
        """Backwards compatibility: putting the file in the directory you run
        from is a deliberate act and keeps working."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            (pathlib.Path(tmp) / "install.env").write_text("PROJECT_ID=from-the-cwd\n")
            proc = self._resolved_paths(tmp, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # realpath: this path comes back through `pwd`, and on macOS the
            # temporary directory is /var/... symlinked to /private/var/...
            resolved = pathlib.Path(tmp).resolve()
            self.assertIn(f"ENV={resolved}/install.env", proc.stdout)

    def test_the_explicit_override_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            named = pathlib.Path(tmp) / "named.env"
            named.write_text("PROJECT_ID=from-the-override\n")
            proc = self._resolved_paths(
                tmp, home, extra_env={"KUBE_AGENTS_INSTALL_ENV": str(named)}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"ENV={named}", proc.stdout)

    def test_the_legacy_vars_file_is_looked_for_in_the_same_checkout(self):
        """Same root cause, same fix: resolved script-relative, a piped re-run
        against an existing clone never found the legacy file and silently
        skipped the migration it exists for."""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            legacy = pathlib.Path(home) / "kube-agents" / "k8s-operator" / "scripts"
            legacy.mkdir(parents=True)
            (legacy / "vars.sh").write_text("export PROJECT_ID=from-the-legacy-file\n")
            proc = self._resolved_paths(tmp, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"LEGACY={legacy}/vars.sh", proc.stdout)


class InstallEnvPermissionsTest(unittest.TestCase):
    """A copied install.env is a credential file at the operator's umask.

    install.env.example is tracked 100644 and the documented way to create the
    real file is to copy it, so a stock umask 022 yields 0644 -- and that file
    is where GEMINI_API_KEY, SLACK_BOT_TOKEN and API_SERVER_KEY end up. Nothing
    else reaches it: bootstrap_install_env_file returns the moment the
    destination exists, so its chmod 600 never runs, and save_env_var's is
    reachable only from the Day-2 menu. INSTALL.md meanwhile states flatly that
    the file is 0600, and the predecessor vars.sh always was.
    """

    def _load(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text("PROJECT_ID=a-project\n")
            env_file.chmod(mode)
            proc = subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 'echo "P=$PROJECT_ID"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
                ),
                cwd=str(_REPO_ROOT),
            )
            return proc, stat.S_IMODE(env_file.stat().st_mode)

    def test_a_world_readable_configuration_is_tightened_on_load(self):
        proc, mode = self._load(0o644)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(0o600, mode, "install.sh must chmod 600 a 0644 install.env")
        self.assertIn("P=a-project", proc.stdout, "and still load it")
        self.assertIn("Tightened permissions", proc.stdout + proc.stderr)

    def test_a_group_readable_configuration_is_tightened_too(self):
        _, mode = self._load(0o640)
        self.assertEqual(0o600, mode)

    def test_an_already_private_file_is_left_alone_and_unannounced(self):
        proc, mode = self._load(0o600)
        self.assertEqual(0o600, mode)
        self.assertNotIn("Tightened permissions", proc.stdout + proc.stderr)

    def test_the_copy_recipe_tells_the_operator_to_chmod_it(self):
        """The tightening only helps from the next run onwards, so the recipe
        that creates the file has to say so itself."""
        example = (_REPO_ROOT / "install.env.example").read_text()
        self.assertIn("cp install.env.example install.env", example)
        self.assertIn("chmod 600 install.env", example)
        self.assertIn("chmod 600", (_REPO_ROOT / "INSTALL.md").read_text())


class ChatInterviewInheritsAndStillAsksTest(unittest.TestCase):
    """Inheriting the chat setting must pre-select the menu, not skip it.

    PARAM_ENABLE_GOOGLE_CHAT is now seeded from GOOGLE_CHAT_ENABLED, but the
    gate around the menu was still the one that decides whether to run the
    interview at all -- so it stopped distinguishing "asked for on this run"
    from "inherited from the file". An interactive re-run against a configured
    install never saw the four options, leaving no way to turn Chat off or to
    add Slack. Every other setting reworked here seeds its choice variable and
    still calls prompt_menu, whose default_choice exists for exactly this.
    """

    _SOURCE = _INSTALL_SH.read_text()

    def _chat_block(self):
        block = self._SOURCE.split("6. Chat & Messaging Platform Integration")[1]
        return block.split("local google_chat_enabled")[0]

    def test_the_menu_is_not_inside_the_inheritance_branch(self):
        """prompt_menu for the chat options must be reached unconditionally;
        the seeds above it only pre-select an answer.

        Checked structurally rather than by searching the text before the call.
        Slicing at `chat_block.index("prompt_menu")` cannot work: the slice
        stops at the first occurrence, so by construction it never contains the
        string the pattern needs, and the first occurrence here is the comment
        naming prompt_menu rather than the call. The property that actually
        distinguishes fixed from broken is nesting depth -- the defect had the
        call at four spaces inside an `else` arm, the fix has it at two, in the
        function body.
        """
        code = [
            line for line in self._chat_block().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        calls = [line for line in code if re.match(r"^\s*prompt_menu\b", line)]
        self.assertEqual(
            1, len(calls),
            "expected exactly one chat prompt_menu call in the block; "
            f"found {len(calls)}",
        )
        indent = len(calls[0]) - len(calls[0].lstrip())
        self.assertEqual(
            2, indent,
            "the chat menu must sit at function-body level, not nested in an "
            "if/else arm; seed chat_choice and let prompt_menu default to it",
        )
        previous = code[code.index(calls[0]) - 1].strip()
        self.assertNotEqual(
            "else", previous,
            "the chat menu must not be the else-arm of the inheritance check",
        )

    def test_all_four_options_are_still_offered(self):
        for option in ("Google Chat (Pub/Sub", "Slack (Socket Mode",
                       "Both Google Chat and Slack", "None (CLI & REST"):
            with self.subTest(option=option):
                self.assertIn(option, self._SOURCE)

    def test_a_configured_install_pre_selects_its_current_integration(self):
        """The seeds, which are what makes enter a no-op rather than a
        change."""
        chat_block = self._chat_block()
        self.assertIn('chat_choice="3"', chat_block)
        self.assertIn('chat_choice="1"', chat_block)
        self.assertIn('chat_choice="2"', chat_block)
        # And "None" is still what a non-interactive run with nothing
        # configured gets, rather than option 1.
        self.assertIn('chat_choice="${chat_choice:-4}"', chat_block)


class SlackPromptsKeepTheirCurrentValuesTest(unittest.TestCase):
    """Pressing enter through the Slack interview must not clear the install.

    prompt_read keeps a non-empty current value on the non-interactive path,
    but the interactive branch applies the default argument, and
    `[ -z "$input_val" ] && [ -n "$default_val" ]` is false when that default
    is empty -- so it falls through and assigns the empty string. Passing a
    bare "" therefore cleared SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
    SLACK_HOME_CHANNEL and SLACK_HOME_CHANNEL_NAME, and replaced the Slack
    allowlist with the Google Chat one. The tokens are usually rescued by the
    Secret-recovery loop; the allowlist is not, and an empty slack_allowed_users
    means every workspace member may talk to the agent.

    This is the defect the change already fixed one screen lower, for the
    GitOps prompts, and left in place here.
    """

    _SOURCE = _INSTALL_SH.read_text()

    @staticmethod
    def _logical_lines(source):
        """Join backslash continuations, so a wrapped call is one line.

        Without this the scan below is vacuous for any prompt whose call is
        wrapped: the matched physical line ends in `\\` rather than in the
        argument, so a pattern anchored at end-of-line can never fire. Two of
        the four prompts named here are wrapped.
        """
        joined, buffer = [], ""
        for line in source.splitlines():
            buffer += line.rstrip("\\") if line.rstrip().endswith("\\") else line
            if not line.rstrip().endswith("\\"):
                joined.append(buffer)
                buffer = ""
        if buffer:
            joined.append(buffer)
        return joined

    def test_no_slack_prompt_passes_a_bare_empty_default(self):
        lines = self._logical_lines(self._SOURCE)
        for prompt in ("Slack Bot Token", "Slack App Token",
                       "Slack Home Channel ID", "Slack Home Channel Name"):
            with self.subTest(prompt=prompt):
                matched = [
                    line for line in lines
                    if prompt in line and "prompt_read" in line
                ]
                self.assertTrue(
                    matched,
                    f"no prompt_read call found for {prompt}; this scan would "
                    "otherwise pass by matching nothing",
                )
                for line in matched:
                    self.assertNotRegex(
                        line,
                        re.compile(r'"\s*"\s*(true|false)?\s*$'),
                        f"{prompt} passes an empty default, which clears it "
                        "when the operator presses enter",
                    )

    def test_each_slack_prompt_defaults_to_its_own_current_value(self):
        for var in ("slack_bot_token", "slack_app_token", "slack_allowed_users",
                    "slack_home_channel", "slack_home_channel_name"):
            with self.subTest(var=var):
                self.assertRegex(
                    self._SOURCE,
                    re.compile(rf'{var} "\${var}"'),
                    f"{var} must be prompted with itself as the default",
                )

    def test_the_slack_allowlist_is_not_seeded_from_the_chat_allowlist(self):
        """They are different lists for different platforms; arm 3 configures
        both at once and used the Chat one for Slack."""
        self.assertNotIn('slack_allowed_users "$allowed_users"', self._SOURCE)

    def test_the_tokens_are_not_echoed_back_as_a_visible_default(self):
        """prompt_read renders the default into the prompt text, so a secret
        passed as one would be printed. The label argument is what avoids it."""
        for var in ("slack_bot_token", "slack_app_token"):
            with self.subTest(var=var):
                self.assertRegex(
                    self._SOURCE,
                    re.compile(rf'{var} "\${var}" true "\$\w+_hint"'),
                    f"{var} must pass a label so the value is not displayed",
                )

    def test_both_arms_share_one_definition(self):
        """One helper, called by both arms that ask, so the two cannot drift."""
        self.assertEqual(
            1, self._SOURCE.count("_prompt_slack_settings() {"),
            "the Slack prompts must be defined exactly once",
        )
        self.assertEqual(
            2, len(re.findall(r'^\s*_prompt_slack_settings\s*$',
                              self._SOURCE, re.MULTILINE)),
            "both the Slack-only and the Both arms must call it",
        )


class ChatBooleansAreReadThroughIsTruthyTest(unittest.TestCase):
    """`install.env` is hand-authored, so its booleans arrive in any spelling.

    Every boolean the generator writes goes through `hcl_bool` -> `is_truthy`,
    which accepts `True`, `yes`, `y`, `1`, `on`. These two never reached it on
    install.sh's path: the chat gate string-compared against the lowercase
    literal. `GOOGLE_CHAT_ENABLED=True` therefore dropped `chat_choice` to 4 and
    planned the Pub/Sub topic away on the next `-y` run, while `upgrade.sh` read
    the same file as enabled — two front doors disagreeing about one file. The
    sibling booleans fail loudly on their `^(true|false)$` validators instead;
    only these two were silent.
    """

    def _chat_choice(self, contents):
        """The chat option install.sh resolves for a given install.env."""
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(contents)
            env_file.chmod(0o600)
            return subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 'source scripts/installer/installer_common.sh\n'
                 'resolve_shared_defaults\n'
                 'c=""\n'
                 'if is_truthy "$PARAM_ENABLE_GOOGLE_CHAT" && is_truthy "${SLACK_ENABLED:-false}"; then c=3\n'
                 'elif is_truthy "$PARAM_ENABLE_GOOGLE_CHAT"; then c=1\n'
                 'elif is_truthy "${SLACK_ENABLED:-false}"; then c=2\n'
                 'fi\n'
                 'echo "C=${c:-4}"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
                ),
                cwd=str(_REPO_ROOT),
            )

    def test_the_gate_does_not_string_compare_against_the_lowercase_literal(self):
        """The source-level guard. A reintroduced `= "true"` here is the bug,
        and it is invisible to the behavioural cases below on a `true` file."""
        block = self._SOURCE_BLOCK()
        self.assertNotIn('"$PARAM_ENABLE_GOOGLE_CHAT" = "true"', block)
        self.assertNotIn('"${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}" = "true"', block)
        self.assertIn('is_truthy "$PARAM_ENABLE_GOOGLE_CHAT"', block)
        self.assertIn('is_truthy "${SLACK_ENABLED:-$DEFAULT_SLACK_ENABLED}"', block)

    def _SOURCE_BLOCK(self):
        source = _INSTALL_SH.read_text()
        block = source.split("6. Chat & Messaging Platform Integration")[1]
        return block.split("local google_chat_enabled")[0]

    def test_every_truthy_spelling_enables_chat(self):
        for spelling in ("true", "True", "TRUE", "yes", "y", "1", "on", "On"):
            with self.subTest(spelling=spelling):
                proc = self._chat_choice(f"GOOGLE_CHAT_ENABLED={spelling}\n")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(
                    "C=1", proc.stdout,
                    f"GOOGLE_CHAT_ENABLED={spelling} must enable Google Chat; "
                    "resolving to None plans the Pub/Sub topic away",
                )

    def test_falsy_spellings_still_mean_off(self):
        for spelling in ("false", "False", "no", "0", "off", ""):
            with self.subTest(spelling=spelling):
                proc = self._chat_choice(f"GOOGLE_CHAT_ENABLED={spelling}\n")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("C=4", proc.stdout)

    def test_slack_reads_the_same_way(self):
        for spelling in ("True", "yes", "1"):
            with self.subTest(spelling=spelling):
                proc = self._chat_choice(f"SLACK_ENABLED={spelling}\n")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("C=2", proc.stdout)


class UnrecordedInterviewAnswersAreReportedTest(unittest.TestCase):
    """An interactive answer that `install.env` does not record must be named.

    `install.env` is an input the installer never rewrites, but the interview
    still runs on every interactive invocation and its answers reach
    `terraform.tfvars` and the cluster. So answering "None" at the chat menu
    destroys the Pub/Sub topic on this apply and the next run puts it back,
    because the file still says the integration is on. The only signal was
    "Left your install configuration as you wrote it", which reads as
    reassurance. This warns instead, naming each key and the line to paste.
    """

    def _warn(self, recorded, env_overrides, non_interactive=False):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = pathlib.Path(tmp) / "install.env"
            env_file.write_text(recorded)
            env_file.chmod(0o600)
            assignments = "\n".join(
                f'export {k}={v!r}' .replace("'", '"')
                for k, v in env_overrides.items()
            )
            ni = "true" if non_interactive else "false"
            return subprocess.run(
                ["bash", "-c",
                 f'KUBE_AGENTS_SOURCE_ONLY=true source "{_INSTALL_SH}"\n'
                 f'PARAM_NON_INTERACTIVE={ni}\n'
                 'PARAM_DRY_RUN=false\n'
                 'has_controlling_tty() { return 0; }\n'
                 f'{assignments}\n'
                 f'warn_unrecorded_interview_answers "{env_file}"'],
                capture_output=True, text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_INSTALL_ENV": str(env_file)}
                ),
                cwd=str(_REPO_ROOT),
            )

    def test_a_changed_chat_answer_is_named(self):
        proc = self._warn(
            "GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "false"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("GOOGLE_CHAT_ENABLED=false", combined)

    def test_an_unchanged_answer_says_nothing(self):
        proc = self._warn(
            "GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "true"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_non_interactive_run_says_nothing(self):
        """It typed nothing: its answers came from flags and this very file."""
        proc = self._warn(
            "GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "false"},
            non_interactive=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_key_the_file_does_not_carry_is_not_reported(self):
        """Absent is not drift — the file inherits the default, and warning
        about every unset key would bury the ones that matter."""
        proc = self._warn("PROJECT_ID=a-project\n", {"MEMORY": "hindsight"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_quoted_empty_value_is_not_drift(self):
        """`write_env_var` serialises with `%q`, which spells the empty string
        as the two-character literal `''`.

        `bootstrap_install_env_file` writes seven keys unconditionally, and on a
        stock install — no Slack, no GitOps app — all seven are empty. Comparing
        the recorded `''` against an empty environment value found drift in
        every one of them, on every interactive run, and the line the banner
        printed for each (`KEY=`) changed nothing, so the next run said it
        again. That buries the genuinely changed MEMORY this warning exists for.
        """
        recorded = "".join(
            f"{key}=''\n"
            for key in (
                "ALLOWED_USERS", "SLACK_ALLOWED_USERS", "SLACK_HOME_CHANNEL",
                "SLACK_HOME_CHANNEL_NAME", "GITOPS_ORG", "GITHUB_APP_ID",
                "GITHUB_PEM_PATH",
            )
        )
        proc = self._warn(recorded, {})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_percent_q_escaped_value_is_not_drift(self):
        """`%q` writes `#gke-alerts` as `\\#gke-alerts` and `a b` as `a\\ b`.

        Stripping only a surrounding pair of double quotes returned the escaped
        spelling, which never equals the value the interview holds.
        """
        proc = self._warn(
            "SLACK_HOME_CHANNEL=\\#gke-alerts\n"
            "SLACK_HOME_CHANNEL_NAME=alerts\\ channel\n",
            {
                "SLACK_HOME_CHANNEL": "#gke-alerts",
                "SLACK_HOME_CHANNEL_NAME": "alerts channel",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_hand_authored_quoted_value_is_not_drift(self):
        """The other half: an operator writes `"#gke-alerts"`, not `\\#gke-alerts`.

        Both spellings mean one value, which is why this unquotes rather than
        re-quoting the current value and comparing the quoted forms.
        """
        proc = self._warn(
            'SLACK_HOME_CHANNEL="#gke-alerts"\n'
            "SLACK_ALLOWED_USERS='someone@example.com'\n",
            {
                "SLACK_HOME_CHANNEL": "#gke-alerts",
                "SLACK_ALLOWED_USERS": "someone@example.com",
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_quoted_value_that_really_changed_is_still_named(self):
        """Unquoting must not have made the warning unable to fire."""
        proc = self._warn(
            "SLACK_HOME_CHANNEL=\\#gke-alerts\n",
            {"SLACK_HOME_CHANNEL": "#gke-incidents"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("SLACK_HOME_CHANNEL=#gke-incidents", combined)

    def test_an_export_prefixed_key_is_still_compared(self):
        """`export K=V` is a spelling install.env.example calls harmless.

        Both greps here matched a bare `K=` only, so an `export`-prefixed key
        was skipped outright — and skipping is silent and in the direction of
        no warning. Every other reader of the file accepts the prefix:
        `save_env_var`, `scripts/live_test_lease.py` and
        `admin_console/project_config.py`.
        """
        proc = self._warn(
            "export GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "false"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("GOOGLE_CHAT_ENABLED=false", combined)

    def test_an_unchanged_export_prefixed_key_says_nothing(self):
        """Reading the prefix must not have turned every such key into drift."""
        proc = self._warn(
            "export GOOGLE_CHAT_ENABLED=true\n", {"GOOGLE_CHAT_ENABLED": "true"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_a_changed_memory_answer_is_named(self):
        """The case the whole warning matters most for, and the one an entry
        that reads `$MEMORY` cannot see.

        `install.sh` never re-exports `MEMORY` after the memory interview: the
        answer lands in `PARAM_MEMORY` and in `MEMORY_PROVIDER`, while `MEMORY`
        still holds whatever `install.env` set at startup. So comparing against
        `$MEMORY` always finds them equal. An operator with `MEMORY=file` who
        picks the searchable store gets Hindsight provisioned, no warning, and
        an unchanged file — and the next run derives `multiuser_memory` from it
        and tears the Hindsight API and its Postgres back down.
        """
        proc = self._warn(
            "MEMORY=file\n", {"PARAM_MEMORY": "hindsight"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("does not record", combined)
        self.assertIn("MEMORY=hindsight", combined)

    def test_an_unchanged_memory_answer_says_nothing(self):
        proc = self._warn("MEMORY=file\n", {"PARAM_MEMORY": "file"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("does not record", proc.stdout + proc.stderr)

    def test_settings_with_no_interview_question_are_not_listed(self):
        """ENABLE_GKE_BACKUP_PLAN and GVISOR_POOL_NAME are deliberately kept out
        of the export block because nothing asks about them, so an entry for
        them here could only ever compare a value against itself."""
        source = _INSTALL_SH.read_text()
        body = source.split("warn_unrecorded_interview_answers() {")[1]
        # The key list itself, not the comment above it that names these two as
        # the examples of what to leave out.
        keys = body.split("for key in ")[1].split("; do")[0]
        self.assertIn("MEMORY", keys, "sanity: the list was located")
        self.assertNotIn("ENABLE_GKE_BACKUP_PLAN", keys)
        self.assertNotIn("GVISOR_POOL_NAME", keys)

    def test_a_secret_is_named_without_its_value(self):
        proc = self._warn(
            "SLACK_BOT_TOKEN=xoxb-old\n", {"SLACK_BOT_TOKEN": "xoxb-brand-new"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("SLACK_BOT_TOKEN", combined)
        self.assertNotIn("xoxb-brand-new", combined)

    def test_it_is_reached_when_the_file_already_exists(self):
        """bootstrap_install_env_file returns early on an existing file; the
        warning has to sit before that return or it never runs at all."""
        source = _INSTALL_SH.read_text()
        early_return = source.split("bootstrap_install_env_file() {")[1]
        early_return = early_return.split("if [ \"$PARAM_DRY_RUN\"")[0]
        self.assertIn("warn_unrecorded_interview_answers", early_return)


class TfvarsTempFileIsCleanedUpTest(unittest.TestCase):
    """A partial `terraform.tfvars.tmp` holds every secret the run was given.

    `write_tfvars_from_state` writes `${dest}.tmp`, `chmod 600`s it and then
    `mv`s it. A failure in between used to be covered by a trap branch that
    removed any `$vars_file` ending in `.tmp`; the replacement removed only
    `${INSTALL_ENV_FILE}.tmp`, so the tfvars residue survived — mode 600, full
    of secrets, and named one character from the file the next reader opens.
    """

    def test_the_generator_publishes_and_clears_the_path(self):
        source = (_REPO_ROOT / "scripts" / "installer" / "installer_common.sh").read_text()
        self.assertIn('TFVARS_TMP_FILE="${dest}.tmp"', source)
        self.assertIn('TFVARS_TMP_FILE=""', source)
        # Published before the redirect, cleared after the mv, in that order.
        self.assertLess(
            source.index('TFVARS_TMP_FILE="${dest}.tmp"'),
            source.index('mv -f -- "${dest}.tmp" "$dest"'),
        )
        self.assertLess(
            source.index('mv -f -- "${dest}.tmp" "$dest"'),
            source.index('TFVARS_TMP_FILE=""'),
        )

    def test_every_front_door_removes_it_on_error(self):
        """All three run the same generator, so all three can leave the same
        residue."""
        for name in ("install.sh", "upgrade.sh", "uninstall.sh"):
            with self.subTest(name=name):
                source = (_REPO_ROOT / name).read_text()
                handler = source.split("on_error() {")[1].split("\n}")[0]
                self.assertIn(
                    "TFVARS_TMP_FILE", handler,
                    f"{name}'s ERR trap must remove a partial tfvars",
                )


if __name__ == "__main__":
    unittest.main()
