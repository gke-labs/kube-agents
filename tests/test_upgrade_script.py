"""Unit tests for upgrade.sh validation and execution routines.

Tests pure numeric SemVer (X.Y.Z) references, 40-character commit SHAs,
piped stdin execution, and source ref alignment in upgrade.sh.
"""

import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

from tests.testing.common import (
    INVALID_IMMUTABLE_REFS,
    UPGRADER_HELP_BANNER,
    VALID_IMMUTABLE_REFS,
    create_minimal_tools_bin,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_RELEASE_BUNDLE_VERSION,
    create_mock_release_bundle_marker,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_UPGRADE_SH = _REPO_ROOT / "upgrade.sh"


class UpgradeScriptValidationTest(unittest.TestCase):
    def _run_upgrade_func(self, func_call, env=None, cwd=None):
        """Source upgrade.sh in test mode and run the given function call."""
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"
{func_call}
"""
        full_env = get_isolated_test_env(overrides=env or {})
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=full_env,
            cwd=str(cwd or _REPO_ROOT),
        )

    def test_an_unhandled_failure_inside_a_substitution_prints_one_banner_from_the_parent(self):
        # The handler exits a subshell silently and the parent reports once
        # (#1798); command substitution does not inherit errexit, so the exit
        # is also what stops the probe at its failing step.
        proc = self._run_upgrade_func(
            'probe() { false; echo "NOT_REACHED_IN_PROBE"; }\n'
            'x="$(probe)"\n'
            'echo "NOT_REACHED x=[$x]"'
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("NOT_REACHED", proc.stdout)
        self.assertEqual(proc.stderr.count("Upgrade error encountered"), 1, proc.stderr)
        self.assertIn(' in main (exit code 1): x="$(probe)"', proc.stderr)

    def test_validate_immutable_ref_accepts_valid_refs(self):
        for ref in VALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_upgrade_func(cmd)
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"upgrade.sh: expected ref '{ref}' to be valid, stderr: {proc.stderr}",
                )

    def test_validate_immutable_ref_rejects_invalid_refs(self):
        for ref in INVALID_IMMUTABLE_REFS:
            with self.subTest(ref=ref):
                cmd = f'validate_immutable_ref "{ref}"'
                proc = self._run_upgrade_func(cmd)
                self.assertNotEqual(
                    proc.returncode,
                    0,
                    f"upgrade.sh: expected ref '{ref}' to be rejected",
                )

    def test_piped_stdin_executes_main(self):
        """Ensures piped curl | bash invocations execute main and do not exit early."""
        upgrade_script_content = _UPGRADE_SH.read_text()
        with tempfile.TemporaryDirectory(prefix="upgrade-piped-help-") as tmp:
            proc = subprocess.run(
                ["bash", "-s", "--", "--help"],
                input=upgrade_script_content,
                capture_output=True,
                text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_LOCK_FILE": str(pathlib.Path(tmp) / "upgrade.lock")}
                ),
                cwd=str(_REPO_ROOT),
            )
        self.assertEqual(proc.returncode, 0, f"Piped execution failed: {proc.stderr}")
        self.assertIn(UPGRADER_HELP_BANNER, proc.stdout)

    def test_verify_local_source_ref_accepts_baked_release_in_non_git_dir(self):
        """Verifies verify_local_source_ref succeeds for unpacked release archive without Git repository."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unpacked-upgrade-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.2.0"
            archive_dir.mkdir(parents=True)

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{archive_dir}" "0.2.0"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Verified upgrade sources match baked official release 0.2.0", proc.stdout)

    def test_verify_local_source_ref_accepts_release_bundle_marker_in_non_git_dir(self):
        """Verifies verify_local_source_ref in upgrade.sh logs bundle provenance attribution when .release-bundle matches baked version."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unpacked-upgrade-bundle-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION="{MOCK_RELEASE_BUNDLE_VERSION}"; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(f"Verified upgrade sources match official release bundle {MOCK_RELEASE_BUNDLE_VERSION}", proc.stdout)

    def test_verify_local_source_ref_rejects_unbaked_release_bundle_marker_in_non_git_dir(self):
        """Verifies .release-bundle marker cannot bypass unversioned source directory rejection in upgrade.sh when baked version is empty."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unpacked-unbaked-upgrade-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / f"kube-agents-{MOCK_RELEASE_BUNDLE_VERSION}"
            create_mock_release_bundle_marker(archive_dir)

            cmd = f'BAKED_RELEASE_VERSION=""; verify_local_source_ref "{archive_dir}" "{MOCK_RELEASE_BUNDLE_VERSION}"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing to upgrade from an unversioned source directory", proc.stdout)

    def test_verify_local_source_ref_rejects_a_bundle_of_another_release(self):
        """A piped release script carries its own baked version wherever it runs.

        Standing in an unpacked 0.5.0 bundle and piping the 0.6.0 one-liner used
        to fall through matches_release_bundle_ref into "match baked official
        release 0.6.0", and then applied the old tree's Terraform and charts at
        the new tag. The directory says which release it is; that wins.
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unpacked-older-bundle-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.5.0"
            create_mock_release_bundle_marker(archive_dir, version="0.5.0")

            cmd = f'BAKED_RELEASE_VERSION="0.6.0"; verify_local_source_ref "{archive_dir}" "0.6.0"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("it is release '0.5.0', not '0.6.0'", proc.stdout)
            self.assertNotIn("Verified", proc.stdout)

    def test_verify_local_source_ref_rejects_a_stale_tree_carrying_no_marker(self):
        """The marker is one way a tree names its release, not the only one.

        A copy of a bundle with .release-bundle removed, or a bundle from a
        release predating the marker, still carries the version the packager
        stamps into every root script. Keying the refusal on the marker alone
        let exactly those trees through with a green "verified".
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix="unmarked-older-tree-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.5.0"
            archive_dir.mkdir(parents=True)
            (archive_dir / "upgrade.sh").write_text('#!/usr/bin/env bash\nBAKED_RELEASE_VERSION="0.5.0"\n')

            cmd = f'BAKED_RELEASE_VERSION="0.6.0"; verify_local_source_ref "{archive_dir}" "0.6.0"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("it is release '0.5.0', not '0.6.0'", proc.stdout)
            self.assertNotIn("Verified", proc.stdout)

    def test_verify_local_source_ref_rejects_a_tree_whose_own_upgrade_sh_was_replaced(self):
        """The stamp cannot be read from the file the operator just downloaded.

        `curl -o upgrade.sh …/0.6.0/upgrade.sh && ./upgrade.sh` inside an
        unpacked 0.5.0 tree overwrites the one root script the marker-less
        fallback used to consult, so the tree reported 0.6.0 and its 0.5.0
        Terraform, charts and CRDs were applied at the 0.6.0 tag. The packager
        stamps install.sh and uninstall.sh with the same version and neither is
        in the way of that download, so they answer for the tree.
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix="overwritten-upgrade-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.5.0"
            archive_dir.mkdir(parents=True)
            for sibling in ("install.sh", "uninstall.sh"):
                (archive_dir / sibling).write_text(
                    '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION="0.5.0"\n'
                )
            # What the download left behind: the tree's upgrade.sh now names the
            # release the operator asked for, not the release the tree is.
            (archive_dir / "upgrade.sh").write_text(
                '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION="0.6.0"\n'
            )

            cmd = f'BAKED_RELEASE_VERSION="0.6.0"; verify_local_source_ref "{archive_dir}" "0.6.0"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("it is release '0.5.0', not '0.6.0'", proc.stdout)
            self.assertNotIn("Verified", proc.stdout)

    def test_release_version_of_source_tree_skips_an_unstamped_sibling(self):
        """An empty BAKED_RELEASE_VERSION is the plain repository content, not
        an answer, so the search keeps going rather than stopping at it."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="partly-stamped-tree-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.5.0"
            archive_dir.mkdir(parents=True)
            (archive_dir / "install.sh").write_text(
                '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION=""\n'
            )
            (archive_dir / "uninstall.sh").write_text(
                '#!/usr/bin/env bash\nBAKED_RELEASE_VERSION="0.5.0"\n'
            )

            cmd = f'release_version_of_source_tree "{archive_dir}"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "0.5.0")

    def test_verify_local_source_ref_accepts_a_marker_that_names_only_the_tag(self):
        """deploy/release-versioning.md promises a match on version *or* tag.

        Requiring version= before tag= was consulted turned a marker naming the
        requested release into a refusal of it.
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix="tag-only-bundle-") as outer_dir:
            archive_dir = pathlib.Path(outer_dir) / "kube-agents-0.6.0"
            archive_dir.mkdir(parents=True)
            (archive_dir / ".release-bundle").write_text("name=kube-agents\ntag=0.6.0\n")

            cmd = f'BAKED_RELEASE_VERSION="0.6.0"; verify_local_source_ref "{archive_dir}" "0.6.0"'
            proc = self._run_upgrade_func(cmd, cwd=archive_dir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("official release bundle 0.6.0", proc.stdout)

    def test_verify_local_source_ref_in_git_worktree_enforces_git_alignment(self):
        """Verifies verify_local_source_ref in upgrade.sh enforces clean git status in real git checkouts."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="git-upgrade-repo-") as repo_dir:
            repo_path = pathlib.Path(repo_dir)
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
            (repo_path / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_path), check=True)

            # Make checkout dirty
            (repo_path / "file.txt").write_text("dirty uncommitted change\n")

            cmd = f'BAKED_RELEASE_VERSION="0.2.0"; verify_local_source_ref "{repo_path}" "0.2.0"'
            proc = self._run_upgrade_func(cmd, cwd=repo_path)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("dirty checkout", proc.stdout)

    def test_a_plan_previews_a_dirty_checkout_instead_of_refusing_it(self):
        """--plan says it changes nothing, so a stray edit is something to report, not refuse.

        Previews reuse the install checkout once it is at the ref — the steady
        state after any successful upgrade — so refusing here would take the
        drift report away from the one command that answers "what have I
        edited". verify_local_source_clean already warns both previews.
        """
        import tempfile

        with tempfile.TemporaryDirectory(prefix="git-upgrade-plan-dirty-") as repo_dir:
            repo_path = pathlib.Path(repo_dir)
            subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), check=True)
            (repo_path / "file.txt").write_text("initial\n")
            subprocess.run(["git", "add", "file.txt"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True)
            subprocess.run(["git", "tag", "0.2.0"], cwd=str(repo_path), check=True)
            (repo_path / "file.txt").write_text("a hand edit the operator wants to see planned\n")

            cmd = (
                'BAKED_RELEASE_VERSION="0.2.0"; PARAM_PLAN="true"; '
                f'verify_local_source_ref "{repo_path}" "0.2.0"'
            )
            proc = self._run_upgrade_func(cmd, cwd=repo_path)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("preview is using uncommitted source changes", proc.stdout)
            self.assertNotIn("Refusing", proc.stdout)


class UpgradeRunContractTest(unittest.TestCase):
    """Properties of the run main() performs, checked against the script source."""

    def test_health_verification_covers_the_pods_that_run_the_commands(self):
        """A healthy gateway is not a working install.

        The agent executes nothing in its own pod: shell commands go to the
        sandbox StatefulSet over ssh and credentialed ones through the proxy.
        Step 5 verified the gateway alone, so an upgrade that left either of
        those unready still printed "verified healthy" -- and the symptom
        arrives later, as an agent that cannot run kubectl.
        """
        source = _UPGRADE_SH.read_text()
        # Spelled through installer_common.sh's chart-contract constants, so the
        # constant's value is checked too: a renamed constant that no longer
        # holds the object's name would otherwise still pass.
        common = (_REPO_ROOT / "scripts" / "installer" / "installer_common.sh").read_text()
        for kind, constant, name in (
            ("statefulset", "PLATFORM_AGENT_SHELL_STATEFULSET", "platform-agent-shell"),
            ("deployment", "PLATFORM_AGENT_CREDENTIAL_PROXY_DEPLOYMENT", "platform-agent-credential-proxy"),
        ):
            with self.subTest(target=f"{kind}/{name}"):
                self.assertIn(f'readonly {constant}="{name}"', common)
                self.assertIn(f'kubectl rollout status "{kind}/${{{constant}}}"', source)

    def test_the_coordinate_overrides_reach_the_environment(self):
        """--gcp-project-id/--gke-cluster-name/--gcp-region have to travel: the
        exports are what the credentials fetch, the tfvars generator and the
        helm release all read, and nothing else carries them."""
        source = _UPGRADE_SH.read_text()
        for var in ("PROJECT_ID", "CLUSTER_NAME", "REGION"):
            with self.subTest(var=var):
                self.assertIn(f'export {var}="$target_', source)

    def test_the_generator_call_asks_for_a_memory_answer(self):
        """upgrade.sh's half of the Hindsight guard.

        When the generator cannot tell whether the cluster runs the Hindsight
        memory store and nothing named a memory mode, it refuses only for
        callers that opt in through KUBE_AGENTS_REQUIRE_MEMORY_ANSWER; without
        it, it takes the warn-and-continue arm that uninstall.sh wants and
        writes memory_provider = "multiuser_memory". An upgrade then applies
        that, and a --plan renders the same tfvars for the apply that follows
        it — so a `full` upgrade of an install whose install.env carries no
        MEMORY, against a cluster kubectl cannot read, would plan the database
        away.

        Nothing else in this file would notice its loss. No whole-run test
        reaches the generator: --dry-run runs exit at the preview above it,
        --plan runs stop on the coordinate conflict, and the real runs are
        driven with a failing gcloud and stop at the credentials fetch. The
        snapshot test matches the bare `write_tfvars_from_state …` line, which
        a deleted continuation line above it does not affect. The CI matrix's
        upgrade step is --dry-run only.

        Enumerated rather than matched as a substring, for the reason the
        install.sh version of this test gives: a substring is satisfied by one
        call site while a second walks into the default.
        """
        lines = _UPGRADE_SH.read_text().splitlines()
        call_sites, unguarded = [], []
        for index, line in enumerate(lines):
            code_line = line.split("#", 1)[0].strip()
            if not re.search(r"\bwrite_tfvars_from_state\b", code_line) or code_line.startswith("write_tfvars_from_state()"):
                continue
            # Collect the `VAR=value \` continuation lines the call hangs off,
            # plus the call line itself for single-line `VAR=value fn ...`.
            prefix, back = [line], index - 1
            while back >= 0 and lines[back].rstrip().endswith("\\"):
                prefix.append(lines[back])
                back -= 1
            # Named by the function it sits in, not by a line number an edit
            # anywhere above would move.
            enclosing = next(
                (
                    lines[up][: lines[up].index("()")]
                    for up in range(index, -1, -1)
                    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\(\) \{$", lines[up])
                ),
                f"<top level, line {index + 1}>",
            )
            call_sites.append(enclosing)
            if "KUBE_AGENTS_REQUIRE_MEMORY_ANSWER=true" not in "\n".join(prefix):
                unguarded.append(enclosing)

        self.assertEqual(
            call_sites,
            ["main"],
            "upgrade.sh gained or lost a tfvars generator call site",
        )
        self.assertEqual(
            unguarded,
            [],
            "a generator call in upgrade.sh lost the memory opt-in; every upgrade "
            "applies, and --plan renders the same tfvars, so none of them may fall "
            "through to multiuser_memory when the cluster could not be asked",
        )

    def test_the_apply_gate_sits_after_every_refusal_in_its_arm(self):
        """UPGRADE_APPLY_STARTED is what keeps cleanup() from putting an adopted
        checkout back, so a run that raises it and then refuses strands someone
        else's clone on this run's ref. Both previews exit before the mode
        dispatch, but refusals do not stop there: full still runs the minter/KMS
        guard and the service-account 409 check, and harness still reads the
        release's values to learn which plugin tags to move. So the gate cannot
        be raised once above the `case`; each arm has to raise it on its own,
        after its last refusal and before its first write.

        A refusing run cannot observe this -- the refusal is all it produces,
        whichever side of it the flag was set on -- so the placement is pinned
        against the source text instead.
        """
        source = _UPGRADE_SH.read_text()
        gate = 'UPGRADE_APPLY_STARTED="true"'
        self.assertEqual(
            source.count(gate),
            3,
            f"one assignment per mode arm expected, found {source.count(gate)}",
        )

        plan_exit = 'exit "$plan_status"'
        self.assertIn(plan_exit, source)
        dispatch_at = source.index('case "$PARAM_UPGRADE_MODE" in', source.index(plan_exit))
        self.assertEqual(
            source[:dispatch_at].count(gate),
            0,
            "the gate is raised before the mode dispatch, where two arms can still refuse",
        )

        # Anchored to the start of a command line: every arm explains itself in
        # a comment first, and those comments name the very commands this test
        # orders the gate against.
        def command(literal):
            return "\n      " + literal

        for mode, last_refusal, first_write in (
            ("operator", None, "apply_crd_upgrades"),
            (
                "harness",
                'harness_retag_keys "$KUBE_AGENTS_HELM_RELEASE"',
                'helm_retag "${HARNESS_RETAG_KEYS[@]}"',
            ),
            ("full", "check_service_account_ownership || exit 1", "apply_crd_upgrades"),
        ):
            with self.subTest(mode=mode):
                opener = f"\n    {mode})\n"
                self.assertIn(opener, source[dispatch_at:], f"no {mode} arm in the dispatch")
                arm_at = source.index(opener, dispatch_at)
                arm = source[arm_at : source.index("\n      ;;\n", arm_at)]
                self.assertIn(
                    command(gate), arm, f"{mode} never raises the gate ({len(arm)} chars read)"
                )
                self.assertIn(
                    command(first_write),
                    arm,
                    f"{mode}'s first write has moved out of its arm ({len(arm)} chars read)",
                )
                self.assertLess(
                    arm.index(command(gate)),
                    arm.index(command(first_write)),
                    f"{mode} writes to the cluster before raising the gate, so a failure there "
                    "would restore a checkout the run had already started applying",
                )
                if last_refusal is not None:
                    self.assertIn(
                        command(last_refusal),
                        arm,
                        f"{mode}'s last refusal has moved out of its arm ({len(arm)} chars read)",
                    )
                    self.assertLess(
                        arm.index(command(last_refusal)),
                        arm.index(command(gate)),
                        f"{mode} raises the gate before its last refusal, so refusing there "
                        "would leave the adopted checkout detached",
                    )

    def test_tfvars_generation_is_snapshotted_before_writing(self):
        """write_tfvars_from_state runs before full's and harness's refusals, so main() must snapshot first while still regenerating tfvars across all modes so the next full apply agrees with the release."""
        source = _UPGRADE_SH.read_text()
        # main() names the path once, so the snapshot and the write cannot
        # drift onto different files.
        path = 'local tfvars_file="${repo_dir}/terraform/examples/full-install/terraform.tfvars"'
        snap = 'snapshot_moved_checkout_tfvars "$tfvars_file"'
        write = 'write_tfvars_from_state "$tfvars_file" "$PARAM_IMAGE_TAG"'
        dispatch = 'case "$PARAM_UPGRADE_MODE" in'
        self.assertEqual(source.count(path), 1)
        self.assertIn(snap, source)
        self.assertIn(write, source)
        self.assertLess(source.index(path), source.index(snap))
        self.assertLess(source.index(snap), source.index(write))
        # Two `case "$PARAM_UPGRADE_MODE"` blocks: parse-time validation, then
        # the dispatch into the mode arms. The write has to come before the
        # second so every mode regenerates. Searching for the dispatch from the
        # write's own index, as this used to, finds a later match by
        # construction and could never fail.
        self.assertEqual(source.count(dispatch), 2, "expected the validation case and the mode dispatch")
        mode_dispatch = source.rindex(dispatch)
        self.assertNotIn(
            "Unsupported upgrade mode",
            source[mode_dispatch:mode_dispatch + 400],
            "the last mode case is the validation block, not the dispatch",
        )
        self.assertLess(source.index(write), mode_dispatch)


class DirtyCheckoutRefusalTest(unittest.TestCase):
    """A tagless upgrade still applies this checkout to a live install.

    `--image-tag` makes three refusals possible at once, and only the middle one
    — does HEAD match the requested ref — actually needs a tag. Gating the whole
    set on the tag's presence would let `--keep-image-tag` carry uncommitted
    edits to `terraform/` or `charts/` into a real `terraform apply`: an install
    running a composition that exists in no commit and that nobody can diff.
    """

    def _run(self, func_call, env=None, cwd=None):
        setup = (f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"\n'
                 f"{func_call}\n")
        return subprocess.run(
            ["bash", "-c", setup], capture_output=True, text=True,
            env=get_isolated_test_env(overrides=env), cwd=str(cwd or _REPO_ROOT),
        )

    def _repo(self, tmp, dirty):
        """A real git checkout, clean or with a tracked file modified."""
        subprocess.run(["git", "init", "-q", tmp], check=True)
        for cmd in (["config", "user.email", "t@example.com"],
                    ["config", "user.name", "T"]):
            subprocess.run(["git", "-C", tmp, *cmd], check=True)
        target = os.path.join(tmp, "main.tf")
        with open(target, "w") as handle:
            handle.write("# committed\n")
        subprocess.run(["git", "-C", tmp, "add", "."], check=True)
        subprocess.run(["git", "-C", tmp, "commit", "-qm", "init"], check=True)
        if dirty:
            with open(target, "a") as handle:
                handle.write("# uncommitted local edit\n")
        return tmp

    def test_a_dirty_checkout_is_refused_without_a_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, dirty=True)
            proc = self._run(f'verify_local_source_clean "{repo}"')
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("dirty checkout", proc.stdout + proc.stderr)

    def test_a_clean_checkout_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, dirty=False)
            proc = self._run(f'verify_local_source_clean "{repo}"')
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_an_unversioned_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(f'verify_local_source_clean "{tmp}"')
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("unversioned source directory", proc.stdout + proc.stderr)

    def test_the_previews_warn_instead_of_refusing(self):
        """--plan and --dry-run change nothing, and a plan of a tree mid-edit is
        the one command that answers "what have I changed here"."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp, dirty=True)
            for flag in ("PARAM_PLAN", "PARAM_DRY_RUN"):
                with self.subTest(flag=flag):
                    proc = self._run(
                        f'{flag}=true; verify_local_source_clean "{repo}"')
                    self.assertEqual(proc.returncode, 0,
                                     proc.stdout + proc.stderr)
                    self.assertIn("uncommitted source changes",
                                  proc.stdout + proc.stderr)

    def test_the_tagless_paths_call_it(self):
        """A run with no ref is verified clean, whichever arm found its sources.

        acquire_upgrade_sources verifies once, after the arms: with a ref it
        compares against the ref, and without one it still refuses a dirty tree,
        because a tagless run applies that tree to a live install all the same.
        """
        source = _UPGRADE_SH.read_text()
        self.assertEqual(source.count('verify_local_source_clean "$resolved_dir"'), 1)
        guard = source.index('if [ -n "$expected_ref" ]; then\n    verify_local_source_ref "$resolved_dir" "$expected_ref"\n  else\n    verify_local_source_clean "$resolved_dir"')
        self.assertLess(source.index("acquire_upgrade_sources() {"), guard)


class InteractiveImageTagPromptTest(unittest.TestCase):
    """A bare Enter at the tag prompt has to be a hard error.

    `--plan` and `--keep-image-tag` make the tag optional, so
    `validate_immutable_ref` — whose first branch rejects an empty ref — runs
    only when a tag is present. Nothing else catches an empty answer: without an
    explicit check it skips `verify_local_source_ref` (the dirty-checkout
    refusal) and silently becomes `--keep-image-tag`.

    Driven through a pty rather than asserted against the source, because the
    prompt reads from /dev/tty specifically so that it cannot be fed on stdin.
    """

    def _answer_prompt_with_enter(self):
        import pty
        import select

        lock_dir = tempfile.TemporaryDirectory(prefix="upgrade-pty-lock-")
        self.addCleanup(lock_dir.cleanup)
        lock_file = str(pathlib.Path(lock_dir.name) / "upgrade.lock")
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - replaced by execve
            # os._exit, not an exception: a raise here would unwind inside a
            # forked copy of the test runner and report a second suite result.
            try:
                os.chdir(str(_REPO_ROOT))
                os.execve(
                    "/bin/bash",
                    ["bash", str(_UPGRADE_SH)],
                    {
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "HOME": os.environ.get("HOME", "/tmp"),
                        "TERM": "dumb",
                        "KUBE_AGENTS_LOCK_FILE": lock_file,
                    },
                )
            finally:
                os._exit(127)
        out = b""
        answered = False
        # A cap rather than a wait: if the guard ever regresses, the run does
        # not hang the suite, it proceeds and this fails on the exit code.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:  # the child closed the pty
                    break
                if not chunk:
                    break
                out += chunk
            if not answered and b"Target image tag" in out:
                os.write(fd, b"\n")
                answered = True
        else:
            os.kill(pid, 9)
            self.fail("upgrade.sh did not exit within 30s of the empty answer")
        _, status = os.waitpid(pid, 0)
        self.assertTrue(answered, "the tag prompt never appeared")
        return status, out.decode(errors="replace")

    def test_a_bare_enter_at_the_prompt_aborts(self):
        status, out = self._answer_prompt_with_enter()
        self.assertTrue(os.WIFEXITED(status), f"upgrade.sh was signalled: {out}")
        self.assertEqual(os.WEXITSTATUS(status), 1, out)
        self.assertIn("--image-tag is required", out)
        # And it names the flag that asks for what an empty answer looked like
        # it might have meant, rather than leaving the reader to find it.
        self.assertIn("--keep-image-tag", out)

    def test_it_stops_before_touching_the_install(self):
        """Nothing may run between the empty answer and the exit."""
        _, out = self._answer_prompt_with_enter()
        for forbidden in ("get-credentials", "terraform", "helm"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, out)

    def test_full_upgrade_checks_service_account_ownership_before_the_apply(self):
        """A minter or Vertex GSA is first planned on the upgrade that enables it (#1294)."""
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        check_idx = text.index("check_service_account_ownership || exit 1")
        apply_idx = text.index("apply -auto-approve -input=false")
        self.assertLess(check_idx, apply_idx)

    def test_upgrade_invokes_ensure_clean_helm_release(self):
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        self.assertIn('ensure_clean_helm_release "$KUBE_AGENTS_HELM_RELEASE" "$target_namespace"', text)

    def test_upgrade_confirms_agent_image_before_rollout_status(self):
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        confirm_idx = text.index('confirm_agent_image.sh" "$target_namespace" "$PLATFORM_AGENT_DEPLOYMENT"')
        rollout_idx = text.index('rollout status "deployment/${PLATFORM_AGENT_DEPLOYMENT}" -n "$target_namespace" --timeout=900s')
        self.assertLess(confirm_idx, rollout_idx)

    def test_the_harness_retag_uses_the_assembled_key_list(self):
        """The branch re-tags exactly what harness_retag_keys assembled.

        The assembly itself runs under test in HarnessRetagKeysTest; what the
        branch owes is to call it and to hand the whole list to helm_retag,
        which turns each key into `--set key=<tag>` (#1808).

        Order, not adjacency: the arm raises UPGRADE_APPLY_STARTED between the
        two, because the read is the last thing here that can fail without
        having changed anything. That placement is
        UpgradeRunContractTest.test_the_apply_gate_sits_after_every_refusal_in_its_arm's
        to keep.
        """
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        harness = text[text.index("    harness)") : text.index("    full)")]
        assemble = '\n      harness_retag_keys "$KUBE_AGENTS_HELM_RELEASE" "$target_namespace"\n'
        retag_call = '\n      helm_retag "${HARNESS_RETAG_KEYS[@]}"\n'
        self.assertIn(assemble, harness, f"the key assembly left the arm ({len(harness)} chars read)")
        self.assertIn(retag_call, harness, f"the re-tag left the arm ({len(harness)} chars read)")
        self.assertLess(
            harness.index(assemble),
            harness.index(retag_call),
            "the arm re-tags before it knows which keys to move",
        )
        self.assertNotIn("mapfile", harness, "macOS ships bash 3.2, which has no mapfile")
        retag = text[text.index("  helm_retag() {") : text.index("  }", text.index("  helm_retag() {"))]
        self.assertIn('set_args+=(--set "${set_key}=${PARAM_IMAGE_TAG}")', retag)

    def test_jq_is_required_for_the_modes_that_read_with_it(self):
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        self.assertIn('if [ "$PARAM_UPGRADE_MODE" != "operator" ]; then\n    required_tools+=(jq)', text)

    def test_upgrade_confirms_agent_image_scoped_to_harness_and_full_modes(self):
        text = (_REPO_ROOT / "upgrade.sh").read_text()
        self.assertIn('[ "$PARAM_UPGRADE_MODE" = "harness" ] || [ "$PARAM_UPGRADE_MODE" = "full" ]', text)
        self.assertIn('kubectl get deployment "$PLATFORM_AGENT_DEPLOYMENT" -n "$target_namespace"', text)

class AgentNamespaceFlagTest(unittest.TestCase):
    """`--agent-namespace` decides every namespace this script touches.

    It steers the regenerated terraform.tfvars, the Helm release guard, the
    generator's Secret-recovery reads and every `kubectl -n`. An install in a
    non-default namespace upgraded from a fresh clone has nothing else to say
    so: without the flag the run resolves DEFAULT_NAMESPACE, renders tfvars for
    it, and is refused by lifecycle.sh's guard_release_namespace with a message
    telling the operator to edit an install.env the clone does not have.
    """

    def _parse_args(self, *args):
        quoted = " ".join(args)
        script = (
            f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"\n'
            f"parse_args {quoted}\n"
            'echo "PARAM=[$PARAM_AGENT_NAMESPACE]"\n'
        )
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(),
            cwd=str(_REPO_ROOT),
        )

    def test_both_argument_forms_reach_the_parameter(self):
        """`--flag value` as well as `--flag=value`: this script takes both for
        its other coordinates, and a half-added flag is the kind that works in
        the example and not in the operator's wrapper."""
        for args in (("--agent-namespace=chosen-ns",), ("--agent-namespace", "chosen-ns")):
            with self.subTest(args=args):
                proc = self._parse_args(*args)
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                self.assertIn("PARAM=[chosen-ns]", proc.stdout)

    def _resolution_line(self):
        """The `target_namespace` assignment, lifted out of main().

        main() needs gcloud, kubectl and a live cluster before it reaches this
        line, so the line is evaluated on its own. Taken from the source rather
        than restated here, which is what makes the evaluation below a check on
        upgrade.sh and not on a copy of it.
        """
        for line in _UPGRADE_SH.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("local target_namespace="):
                return stripped[len("local ") :]
        self.fail("upgrade.sh no longer assigns a target_namespace")

    def _resolve(self, **variables):
        assignments = "".join(f'{key}="{value}"\n' for key, value in variables.items())
        script = f"{assignments}{self._resolution_line()}\necho \"NS=[$target_namespace]\"\n"
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, cwd=str(_REPO_ROOT)
        )

    def test_the_flag_beats_the_loaded_configuration(self):
        proc = self._resolve(
            PARAM_AGENT_NAMESPACE="from-flag",
            NAMESPACE="from-install-env",
            DEFAULT_NAMESPACE="the-default",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("NS=[from-flag]", proc.stdout)

    def test_without_the_flag_the_recorded_value_still_wins_over_the_default(self):
        """The flag must not cost an install.env-driven run its namespace: that
        is how every upgrade resolved one before the flag existed, and it is the
        route reconcile_environment.sh takes, whose UPGRADE_ARGS carry no
        namespace at all."""
        proc = self._resolve(
            PARAM_AGENT_NAMESPACE="",
            NAMESPACE="from-install-env",
            DEFAULT_NAMESPACE="the-default",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("NS=[from-install-env]", proc.stdout)

    def test_with_neither_it_falls_back_to_the_default(self):
        proc = self._resolve(
            PARAM_AGENT_NAMESPACE="", NAMESPACE="", DEFAULT_NAMESPACE="the-default"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("NS=[the-default]", proc.stdout)

    def test_the_resolved_namespace_is_exported(self):
        """write_tfvars_from_state reads the environment, not this variable, so
        the resolution reaching nothing is a distinct way for the flag to have
        no effect."""
        self.assertIn(
            'export NAMESPACE="$target_namespace"', _UPGRADE_SH.read_text()
        )

    def test_the_help_text_names_the_flag(self):
        """Nothing in the tree passes it, so `--help` is the only place an
        operator can find it."""
        with tempfile.TemporaryDirectory(prefix="upgrade-ns-help-") as tmp:
            proc = subprocess.run(
                ["bash", str(_UPGRADE_SH), "--help"],
                capture_output=True,
                text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_LOCK_FILE": str(pathlib.Path(tmp) / "upgrade.lock")}
                ),
                cwd=str(_REPO_ROOT),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("--agent-namespace", proc.stdout)


class _StubHelm:
    """Sources upgrade.sh with a stub `helm` on PATH and runs a snippet after it.

    The stub prints `stdout_json` on stdout, `stderr_text` on stderr, and
    exits `helm_exit`. The script's own ERR trap is stood in for by one that
    writes a banner, so a failure inside the functions shows where the real
    run would abort.
    """

    def _run_with_helm(self, snippet, stdout_json, helm_exit=0, stderr_text=""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_dir = pathlib.Path(tmp.name) / "bin"
        bin_dir.mkdir()
        helm = bin_dir / "helm"
        helm.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' {shlex.quote(stderr_text)} >&2\n"
            f"cat <<'JSON'\n{stdout_json}\nJSON\n"
            f"exit {helm_exit}\n"
        )
        helm.chmod(0o755)
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"
trap 'echo "ABORT BANNER line $LINENO" >&2' ERR
{snippet}
"""
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=get_isolated_test_env(bin_dir=str(bin_dir)),
        )


class RecordedPluginImageTagKeysTest(_StubHelm, unittest.TestCase):
    """recorded_plugin_image_tag_keys against a stub helm, under the system bash."""

    _SNIPPET = 'recorded_plugin_image_tag_keys kube-agents kubeagents-system\nprintf "%s\\n" "$RECORDED_PLUGIN_IMAGE_TAG_KEYS"\necho "rc=$?"'

    def _run(self, values_json, helm_exit=0, stderr_text=""):
        return self._run_with_helm(self._SNIPPET, values_json, helm_exit=helm_exit, stderr_text=stderr_text)

    def test_every_plugin_tag_the_release_records_is_printed(self):
        proc = self._run(
            '{"plugins":{"pubsubPlatform":{"enabled":false,"image":{"tag":"abc"}},'
            '"stockoutInvestigator":{"image":{"tag":"abc"}},'
            '"aThirdPlugin":{"image":{"tag":"abc"}}}}'
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.split()[:-1],
            [
                "plugins.pubsubPlatform.image.tag",
                "plugins.stockoutInvestigator.image.tag",
                "plugins.aThirdPlugin.image.tag",
            ],
        )

    def test_a_plugin_without_a_recorded_tag_is_left_out(self):
        proc = self._run('{"plugins":{"pubsubPlatform":{"image":{"tag":"abc"}},"stockoutInvestigator":{"enabled":false}}}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split()[:-1], ["plugins.pubsubPlatform.image.tag"])

    def test_nothing_without_recorded_plugins(self):
        proc = self._run('{"operator":{"image":{"tag":"abc"}}}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["rc=0"])
        self.assertNotIn("ABORT BANNER", proc.stderr)

    def test_a_helm_warning_on_stderr_does_not_break_the_read(self):
        """Helm warns on stderr on successful commands (a group-readable kubeconfig)."""
        proc = self._run(
            '{"plugins":{"pubsubPlatform":{"image":{"tag":"abc"}}}}',
            stderr_text="WARNING: Kubernetes configuration file is group-readable. This is insecure.",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split()[:-1], ["plugins.pubsubPlatform.image.tag"])
        self.assertNotIn("ABORT BANNER", proc.stderr)

    def test_a_malformed_plugins_value_is_an_error_not_an_empty_list(self):
        """upgrade.sh runs under set -e, so the failed call ends the sourced run."""
        proc = self._run('{"plugins":"oops"}')
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("rc=", proc.stdout)
        self.assertIn("Could not read the plugin image tags", proc.stdout)
        self.assertIn("plugins is not an object", proc.stdout)
        # One banner, from the caller: a second one would mean the trap fired
        # inside the jq substitution as well, which `trap - ERR` there prevents.
        self.assertEqual(proc.stderr.count("ABORT BANNER"), 1, proc.stderr)

    def test_a_failing_helm_read_is_an_error_that_names_the_cause(self):
        """An empty list would run the pre-fix re-tag and leave the plugins behind."""
        proc = self._run("", helm_exit=1, stderr_text="Error: release: not found")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("rc=", proc.stdout)
        self.assertIn("Could not read the values of Helm release", proc.stdout)
        self.assertIn("Error: release: not found", proc.stdout)
        self.assertEqual(proc.stderr.count("ABORT BANNER"), 1, proc.stderr)


class HarnessRetagKeysTest(_StubHelm, unittest.TestCase):
    """harness_retag_keys against a stub helm: the list helm_retag receives."""

    _SNIPPET = 'harness_retag_keys kube-agents kubeagents-system\nprintf "%s\\n" "${HARNESS_RETAG_KEYS[@]}"'

    def _run(self, values_json, helm_exit=0):
        return self._run_with_helm(self._SNIPPET, values_json, helm_exit=helm_exit)

    def test_the_plugin_keys_follow_the_agent_and_sandbox_keys(self):
        proc = self._run('{"plugins":{"pubsubPlatform":{"image":{"tag":"abc"}},"stockoutInvestigator":{"image":{"tag":"abc"}}}}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.split(),
            [
                "platformAgent.deployment.image.tag",
                "agentSandbox.image.tag",
                "plugins.pubsubPlatform.image.tag",
                "plugins.stockoutInvestigator.image.tag",
            ],
        )

    def test_without_recorded_plugins_the_list_is_the_agent_and_sandbox_alone(self):
        proc = self._run('{"operator":{"image":{"tag":"abc"}}}')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["platformAgent.deployment.image.tag", "agentSandbox.image.tag"])
        self.assertNotIn("ABORT BANNER", proc.stderr)

    def test_a_failed_read_stops_before_any_list_is_handed_on(self):
        proc = self._run("{}", helm_exit=1)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("platformAgent.deployment.image.tag", proc.stdout)
        self.assertIn("Could not read the values of Helm release", proc.stdout)


class UpgradeReusesTheInstallCheckoutTest(unittest.TestCase):
    """upgrade.sh moves the checkout install.sh left behind, rather than fetching its own.

    The install one-liner leaves its sources — and the install's install.env —
    in HOME/kube-agents. Before this, the upgrade one-liner fetched a fresh copy
    into a temporary directory, which has sources and no configuration, and the
    run then refused to upgrade without configuration. These tests mirror
    tests/test_install_script.py, whose refresh_existing_clone this one copies.
    """

    @staticmethod
    def _git(*args, cwd):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
        ).stdout.strip()

    def _existing_clone_fixture(
        self, checked_out_tag, full_clone=False, with_install_env=True, real_installer_common=False
    ):
        """A clone of an earlier release under HOME, the way an install leaves one.

        A bare "upstream" holds tags 0.2.0 and 0.3.0, each tracking install.sh
        (the marker refresh_existing_clone requires) and the
        scripts/installer/installer_common.sh every kube-agents checkout has —
        the file the source-resolution arms test for, so a fixture without it
        would make those arms unreachable and the tests vacuous. The clone is
        taken while only 0.2.0 exists, so it has never seen 0.3.0 — the shape of
        a checkout from an earlier install. Returns (home_dir, clone_dir,
        upstream_url, {tag: commit}).

        A one-line marker is enough for the arms that only look for the file.
        `real_installer_common` puts this repository's helpers in it instead,
        for the runs that go on to source it: main() takes load_install_env and
        the DEFAULT_* coordinates from there, and a stub would stop the run
        before it reached what the test is about. The marker line is kept at the
        top either way, so the release a checkout is on stays readable.
        """
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp_dir.cleanup)
        base = pathlib.Path(temp_dir.name)
        work_dir = base / "work"
        bare_dir = base / "upstream.git"
        home_dir = base / "home"
        clone_dir = home_dir / "kube-agents"
        home_dir.mkdir()
        git = self._git

        helpers = (_REPO_ROOT / "scripts" / "installer" / "installer_common.sh").read_text()

        def installer_common(release):
            marker = f"# release {release}\n"
            return marker + helpers if real_installer_common else marker

        # installer_common.sh reads the defaults file at the root of the
        # checkout it was loaded from, and says so and stops when it is not
        # there, so the real helpers only work in a fixture that ships it too.
        tracked = ["install.sh", "scripts/installer/installer_common.sh"]
        if real_installer_common:
            tracked.append("install.defaults.env")

        def write_release(release):
            (work_dir / "install.sh").write_text(f"release {release}\n")
            (work_dir / "scripts" / "installer" / "installer_common.sh").write_text(installer_common(release))
            if real_installer_common:
                shutil.copy(_REPO_ROOT / "install.defaults.env", work_dir / "install.defaults.env")
            git("add", *tracked, cwd=work_dir)
            git("commit", "-m", f"release {release}", cwd=work_dir)
            git("tag", release, cwd=work_dir)

        work_dir.mkdir()
        git("init", "-b", "main", cwd=work_dir)
        git("config", "user.name", "Test", cwd=work_dir)
        git("config", "user.email", "test@example.com", cwd=work_dir)
        git("config", "commit.gpgsign", "false", cwd=work_dir)
        (work_dir / "scripts" / "installer").mkdir(parents=True)
        write_release("0.2.0")
        git("clone", "--bare", "--quiet", str(work_dir), str(bare_dir), cwd=base)
        upstream_url = bare_dir.as_uri()
        if full_clone:
            git("clone", "--quiet", upstream_url, str(clone_dir), cwd=base)
        else:
            git("clone", "--quiet", "--filter=blob:none", "--no-checkout", upstream_url, str(clone_dir), cwd=base)

        write_release("0.3.0")
        git("push", "--quiet", upstream_url, "main", "--tags", cwd=work_dir)
        commits = {tag: git("rev-parse", f"{tag}^{{commit}}", cwd=work_dir) for tag in ("0.2.0", "0.3.0")}

        if full_clone:
            # The full clone is taken before 0.3.0 is pushed, so it can only be
            # checked out at 0.2.0 — the one tag every full_clone caller uses.
            git("checkout", "--quiet", "--detach", checked_out_tag, cwd=clone_dir)
        else:
            refspec = f"+refs/tags/{checked_out_tag}:refs/tags/{checked_out_tag}"
            git("fetch", "--quiet", "--depth=1", upstream_url, refspec, cwd=clone_dir)
            git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=clone_dir)
            self.assertEqual(git("rev-parse", "--is-shallow-repository", cwd=clone_dir), "true")
        if with_install_env:
            (clone_dir / "install.env").write_text('PROJECT_ID="my-gcp-project"\n')
        return home_dir, clone_dir, upstream_url, commits

    def _refresh_from_outside(self, home_dir, clone_dir, upstream_url, requested_ref):
        """Run refresh_existing_clone from a copy of upgrade.sh outside any checkout.

        KUBE_AGENTS_REPO_URL is overridden after sourcing, because upgrade.sh
        assigns it unconditionally.
        """
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        isolated_upgrade_sh = outside_dir / "upgrade.sh"
        isolated_upgrade_sh.write_text(_UPGRADE_SH.read_text())
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_upgrade_sh}"
KUBE_AGENTS_REPO_URL="{upstream_url}"
refresh_existing_clone "{clone_dir}" "{requested_ref}"
"""
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
            cwd=str(outside_dir),
        )

    def _head_of(self, clone_dir):
        return self._git("rev-parse", "HEAD", cwd=clone_dir)

    def test_the_clone_is_moved_to_the_requested_release(self):
        """The upgrade one-liner's whole point: an install at N ends up at N+1."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fetching '0.3.0'", proc.stdout)
        self.assertIn("Moved", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_the_install_env_in_the_clone_survives_the_move(self):
        """The configuration is why the clone is preferred, so the move must keep it."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.env").write_text('PROJECT_ID="my-gcp-project"\n')

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((clone_dir / "install.env").read_text(), 'PROJECT_ID="my-gcp-project"\n')

    def test_a_clone_already_at_the_release_is_not_fetched_into(self):
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.2.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("already at '0.2.0'", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_dirty_clone_is_left_alone(self):
        """Local changes are never fetched over; the run stops at verify_local_source_ref instead."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.sh").write_text("local edits\n")

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("the checkout is dirty", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_directory_that_is_not_a_kube_agents_repository_is_left_alone(self):
        """A repository that merely shares the directory name is never moved."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        unrelated = home_dir / "unrelated"
        unrelated.mkdir()
        self._git("init", "-b", "main", cwd=unrelated)
        self._git("config", "user.name", "Test", cwd=unrelated)
        self._git("config", "user.email", "test@example.com", cwd=unrelated)
        self._git("config", "commit.gpgsign", "false", cwd=unrelated)
        (unrelated / "README.md").write_text("not kube-agents\n")
        self._git("add", "README.md", cwd=unrelated)
        self._git("commit", "-m", "init", cwd=unrelated)
        before = self._head_of(unrelated)

        proc = self._refresh_from_outside(home_dir, unrelated, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("is not a kube-agents revision", proc.stdout)
        self.assertEqual(self._head_of(unrelated), before)

    def test_a_complete_clone_does_not_become_shallow(self):
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0", full_clone=True)

        proc = self._refresh_from_outside(home_dir, clone_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])
        self.assertEqual(self._git("rev-parse", "--is-shallow-repository", cwd=clone_dir), "false")

    def test_the_tagless_arms_never_reach_the_clone(self):
        """--plan and --keep-image-tag still require a checkout; HOME is not a substitute.

        A CI job that checked out the ref it reconciles must keep that tree, so
        the preference for the installer's clone lives inside the arm that has a
        tag and no checkout, after the tagless refusal. The behaviour of that
        arm is covered below; this pins where it sits.
        """
        text = _UPGRADE_SH.read_text()
        tagless_refusal = text.index('--plan and --keep-image-tag have to run from a kube-agents checkout')
        clone_preference = text.index('refresh_existing_clone "$resolved_dir" "$expected_ref"')
        self.assertLess(tagless_refusal, clone_preference)

    def test_a_tagless_preview_refuses_even_with_an_install_checkout_in_home(self):
        """The behaviour the line order above is a proxy for.

        A string compare would keep passing if the refusal were moved below the
        clone preference but its text left where it is, so this runs the arm:
        HOME holds a checkout this script would happily adopt for a tagged run,
        and a tagless run still has to refuse rather than reach for it.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "", preview_flag="PARAM_PLAN")

        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("have to run from a kube-agents checkout", combined)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertIsNone(self._reported(proc, "REPO_DIR"))

    def _acquire_from_outside(
        self, home_dir, upstream_url, requested_ref, preview_flag=None, cwd=None, baked_version=None
    ):
        """Run acquire_upgrade_sources from a copy of upgrade.sh outside any checkout.

        The copy is what makes the clone arm reachable: sourced from a directory
        with no scripts/installer/installer_common.sh, the script has no checkout
        of its own, which is the shape of `curl … | bash`.
        """
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        isolated_upgrade_sh = outside_dir / "upgrade.sh"
        isolated_upgrade_sh.write_text(_UPGRADE_SH.read_text())
        preview_line = f'{preview_flag}="true"' if preview_flag else ":"
        baked_line = f'BAKED_RELEASE_VERSION="{baked_version}"' if baked_version else ":"
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_upgrade_sh}"
KUBE_AGENTS_REPO_URL="{upstream_url}"
{preview_line}
{baked_line}
repo_dir=""
install_checkout=""
acquire_upgrade_sources repo_dir install_checkout "{requested_ref}"
echo "REPO_DIR=$repo_dir"
echo "INSTALL_CHECKOUT=$install_checkout"
"""
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
            cwd=str(cwd or outside_dir),
        )

    @staticmethod
    def _reported(proc, key):
        for line in proc.stdout.splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1]
        return None

    def test_the_install_checkout_becomes_the_upgrade_sources(self):
        """The documented one-liner: no checkout of its own, so it uses the install's."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_the_release_is_fetched_from_the_canonical_url_not_the_clones_origin(self):
        """A fork as `origin` must not decide what the tag means.

        Every other fixture clones $HOME/kube-agents from the same repository
        KUBE_AGENTS_REPO_URL names, so `fetch origin` and `fetch
        "$KUBE_AGENTS_REPO_URL"` could not be told apart. It matters more than
        it looks: a tag that arrives by fetch in this run is exempt from the
        remote_release_tag_commit cross-check (SOURCES_ADOPTED_CHECKOUT is set
        only for a ref the checkout already had), so a fetch from the fork would
        be applied as the release with no second opinion.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        git = self._git
        base = home_dir.parent
        fork_dir = base / "fork.git"
        fork_work = base / "fork-work"
        git("clone", "--bare", "--quiet", upstream_url, str(fork_dir), cwd=base)
        fork_url = fork_dir.as_uri()
        git("clone", "--quiet", fork_url, str(fork_work), cwd=base)
        git("config", "user.name", "Fork", cwd=fork_work)
        git("config", "user.email", "fork@example.com", cwd=fork_work)
        git("config", "commit.gpgsign", "false", cwd=fork_work)
        git("checkout", "--quiet", "--detach", "0.2.0", cwd=fork_work)
        (fork_work / "install.sh").write_text("fork's own 0.3.0\n")
        git("commit", "--quiet", "-am", "fork 0.3.0", cwd=fork_work)
        git("tag", "-f", "0.3.0", cwd=fork_work)
        git("push", "--quiet", "--force", fork_url, "refs/tags/0.3.0", cwd=fork_work)
        fork_commit = git("rev-parse", "0.3.0^{commit}", cwd=fork_work)
        self.assertNotEqual(fork_commit, commits["0.3.0"])
        git("remote", "set-url", "origin", fork_url, cwd=clone_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def _forge_local_tag(self, clone_dir, tag):
        """Give the clone a tag of its own that upstream does not agree with.

        The shape a fork fetch or a hand-run `git tag 0.3.0` leaves: the name
        resolves in the checkout's own object database, so every local
        resolution answers, and answers with the wrong commit.
        """
        self._git("tag", tag, "HEAD", cwd=clone_dir)
        return self._git("rev-parse", f"{tag}^{{commit}}", cwd=clone_dir)

    def test_a_locally_forged_release_tag_is_refused(self):
        """A tag the release did not create must not be applied as the release.

        Resolving the ref against the adopted checkout is what makes this
        reachable: before the upgrade reused a checkout it always fetched the
        tag from KUBE_AGENTS_REPO_URL, so "verified 0.3.0" meant the remote's
        0.3.0. Here the checkout carries its own.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        forged = self._forge_local_tag(clone_dir, "0.3.0")
        self.assertNotEqual(forged, commits["0.3.0"])

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn("Refusing to upgrade from", combined)
        self.assertIn(commits["0.3.0"], combined)
        self.assertNotIn("Verified upgrade scripts and image ref", combined)

    def test_a_preview_over_a_forged_tag_warns_instead_of_refusing(self):
        """Previews change nothing, so they report the disagreement and go on.

        Same split as the dirty-checkout branch immediately below it in
        verify_local_source_ref, and for the same reason.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        self._forge_local_tag(clone_dir, "0.3.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", preview_flag="PARAM_PLAN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn(f"but {upstream_url} names {commits['0.3.0']}", combined)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def test_an_unreachable_remote_refuses_an_adopted_checkouts_tag(self):
        """When KUBE_AGENTS_REPO_URL cannot be reached, a real upgrade refuses an adopted tag."""
        home_dir, clone_dir, _, _ = self._existing_clone_fixture("0.2.0")
        self._forge_local_tag(clone_dir, "0.3.0")
        missing_remote = str(home_dir.parent / "does-not-exist.git")

        proc = self._acquire_from_outside(home_dir, missing_remote, "0.3.0")

        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn(f"Could not ask {missing_remote} what '0.3.0' names", combined)
        self.assertIn("cannot be confirmed as the release", combined)
        self.assertNotIn("Verified upgrade scripts and image ref", combined)

    def test_a_preview_with_an_unreachable_remote_warns_and_trusts_the_local_tag(self):
        """A preview is the command an operator runs when the network is down, so it warns and goes on."""
        home_dir, clone_dir, _, _ = self._existing_clone_fixture("0.2.0")
        self._forge_local_tag(clone_dir, "0.3.0")
        missing_remote = str(home_dir.parent / "does-not-exist.git")

        proc = self._acquire_from_outside(
            home_dir, missing_remote, "0.3.0", preview_flag="PARAM_PLAN"
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn(f"Could not ask {missing_remote} what '0.3.0' names", combined)
        self.assertIn(f"so this preview is trusting the tag in {clone_dir}", combined)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def test_a_tag_the_remote_does_not_carry_is_refused_as_unpublished(self):
        """A reachable remote that lacks the tag is an answer, not a network failure.

        `git ls-remote` exits 0 with no output for a missing ref. Reporting that
        as "could not ask" sends the operator to retry the network, when the
        remedy is to delete the hand-made tag.
        """
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        self._forge_local_tag(clone_dir, "0.9.9")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.9.9")

        self.assertNotEqual(proc.returncode, 0)
        combined = proc.stdout + proc.stderr
        self.assertIn(f"{upstream_url} does not carry '0.9.9'", combined)
        self.assertIn(f"git -C {clone_dir} tag -d 0.9.9", combined)
        self.assertNotIn("Could not ask", combined)
        self.assertNotIn("Verified upgrade scripts and image ref", combined)

    def test_a_preview_over_a_tag_the_remote_does_not_carry_says_so(self):
        """The preview arm of the same answer: it warns and reads the local tag."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        self._forge_local_tag(clone_dir, "0.9.9")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.9.9", preview_flag="PARAM_PLAN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn(f"{upstream_url} does not carry '0.9.9'", combined)
        self.assertNotIn("Could not ask", combined)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def test_an_adopted_checkout_at_a_commit_sha_skips_remote_tag_verification(self):
        """A 40-hex commit SHA is self-verifying (`! ref_is_commit_sha "$expected_ref"`),
        so `verify_local_source_ref` skips `remote_release_tag_commit` even when
        `SOURCES_ADOPTED_CHECKOUT="true"` and `KUBE_AGENTS_REPO_URL` is unreachable."""
        home_dir, clone_dir, _, commits = self._existing_clone_fixture("0.2.0")
        missing_remote = str(home_dir.parent / "does-not-exist.git")

        proc = self._acquire_from_outside(home_dir, missing_remote, commits["0.2.0"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("Could not ask", combined)
        self.assertIn(
            f"Verified upgrade scripts and image ref resolve to commit {commits['0.2.0']}.",
            combined,
        )
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def _acquire_then_exit(
        self, home_dir, upstream_url, requested_ref, exit_code, apply_started=False, between=""
    ):
        """Move the clone, then leave with a status, through the real EXIT trap.

        Written as one bash run rather than by calling restore_moved_checkout
        directly, because what is under test is the wiring: the bookkeeping
        acquire_upgrade_sources records, the condition cleanup applies to it,
        and the trap that gets it called at all.
        """
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        isolated_upgrade_sh = outside_dir / "upgrade.sh"
        isolated_upgrade_sh.write_text(_UPGRADE_SH.read_text())
        applied_line = 'UPGRADE_APPLY_STARTED="true"' if apply_started else ":"
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{isolated_upgrade_sh}"
KUBE_AGENTS_REPO_URL="{upstream_url}"
repo_dir=""
install_checkout=""
acquire_upgrade_sources repo_dir install_checkout "{requested_ref}"
{between}
{applied_line}
exit {exit_code}
"""
        return subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env={"HOME": str(home_dir), "PATH": os.environ["PATH"]},
            cwd=str(outside_dir),
        )

    def test_a_run_that_fails_before_applying_returns_the_checkout(self):
        """Nothing reached the cluster, so the operator's directory goes back.

        Leaving the checkout on the new release after a refusal puts the next
        install.sh, uninstall.sh or hand-run terraform in that directory on an
        engine the install is not running.

        What this test covers is the mechanism: an exit before the gate was
        raised restores the checkout. Which refusals actually land on that side
        of the gate is a question about where each mode arm raises it, and
        UpgradeRunContractTest.test_the_apply_gate_sits_after_every_refusal_in_its_arm
        is what pins that.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_then_exit(home_dir, upstream_url, "0.3.0", exit_code=1)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("Nothing was applied, so", proc.stdout)
        self.assertIn("was returned to", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_run_that_fails_before_applying_returns_a_branch_checkout_to_its_branch(self):
        """When `$HOME/kube-agents` was sitting on a branch (`main`) rather than
        detached at a tag, `restore_moved_checkout` returns it to that branch
        and reports `branch '<name>'`."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture(
            "0.2.0", full_clone=True
        )
        subprocess.run(
            ["git", "-C", str(clone_dir), "checkout", "-B", "main", "0.2.0", "--quiet"],
            check=True,
        )

        proc = self._acquire_then_exit(home_dir, upstream_url, "0.3.0", exit_code=1)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("was returned to branch 'main'", proc.stdout)
        branch = subprocess.run(
            ["git", "-C", str(clone_dir), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertEqual(branch, "main")
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_an_adopted_checkout_whose_tag_was_fetched_by_this_run_sets_sources_fetched(self):
        """When `refresh_existing_clone` had to fetch the requested tag from
        `KUBE_AGENTS_REPO_URL` because `$HOME/kube-agents` did not already carry
        it, `acquire_upgrade_sources` leaves `SOURCES_ADOPTED_CHECKOUT=false` so
        `verify_local_source_ref` does not make a redundant `git ls-remote` call
        and cannot refuse the checkout as 'not fetched by this run'."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_then_exit(
            home_dir,
            upstream_url,
            "0.3.0",
            exit_code=0,
            between=(
                'echo "ADOPTED=$SOURCES_ADOPTED_CHECKOUT"\n'
                'KUBE_AGENTS_REPO_URL="file:///nonexistent-after-fetch"\n'
                'verify_local_source_ref "$repo_dir" "0.3.0"'
            ),
        )

        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("ADOPTED=false", proc.stdout)
        self.assertIn(
            f"Verified upgrade scripts and image ref resolve to commit {commits['0.3.0']}.",
            proc.stdout,
        )

    def test_a_run_that_fails_after_step3_secret_backfill_does_not_say_nothing_was_applied(self):
        """Step 3 (`backfill_session_kv_keys` / `backfill_sandbox_ssh_key`) patches
        `platform-agent-secrets` before `UPGRADE_APPLY_STARTED` is raised, so a
        refusal after step 3 still restores the checkout to the old release but
        must not claim 'Nothing was applied' to the cluster."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_then_exit(
            home_dir,
            upstream_url,
            "0.3.0",
            exit_code=1,
            between='SESSION_KV_KEYS_PATCHED="true"',
        )

        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("Nothing was applied", proc.stdout)
        self.assertIn("The new release was not applied (after reconciling Secret keys in step 3)", proc.stdout)
        self.assertIn("was returned to", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_run_that_has_started_applying_keeps_the_checkout_moved(self):
        """Once the cluster is moving, the checkout belongs on the new release."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_then_exit(home_dir, upstream_url, "0.3.0", exit_code=1, apply_started=True)

        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("was returned to", proc.stdout)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_a_run_that_fails_after_tfvars_generation_restores_the_previous_tfvars(self):
        """A refusal after write_tfvars_from_state must not leave N+1's tfvars beside N's composition.

        terraform.tfvars is gitignored, so `git checkout <prev>` alone leaves
        the newly rendered file in place: a hand-run `terraform apply` in the
        returned checkout would then apply N+1's image_tag and variables with
        N's composition.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        tfvars = clone_dir / "terraform" / "examples" / "full-install" / "terraform.tfvars"
        tfvars.parent.mkdir(parents=True, exist_ok=True)
        tfvars.write_text('image_tag = "0.2.0"\n')

        proc = self._acquire_then_exit(
            home_dir,
            upstream_url,
            "0.3.0",
            exit_code=1,
            between=(
                'snapshot_moved_checkout_tfvars "${repo_dir}/terraform/examples/full-install/terraform.tfvars"\n'
                'printf \'image_tag = "0.3.0"\\n\' > "${repo_dir}/terraform/examples/full-install/terraform.tfvars"'
            ),
        )

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertEqual(tfvars.read_text(), 'image_tag = "0.2.0"\n')

    def test_a_run_that_fails_after_tfvars_generation_removes_newly_created_tfvars(self):
        """When the checkout had no terraform.tfvars before the run, restoring removes the rendered one."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        tfvars = clone_dir / "terraform" / "examples" / "full-install" / "terraform.tfvars"
        tfvars.parent.mkdir(parents=True, exist_ok=True)

        proc = self._acquire_then_exit(
            home_dir,
            upstream_url,
            "0.3.0",
            exit_code=1,
            between=(
                'snapshot_moved_checkout_tfvars "${repo_dir}/terraform/examples/full-install/terraform.tfvars"\n'
                'printf \'image_tag = "0.3.0"\\n\' > "${repo_dir}/terraform/examples/full-install/terraform.tfvars"'
            ),
        )

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertFalse(tfvars.exists())

    def test_a_run_that_fails_after_helm_release_repair_notes_the_repair_in_the_restore_notice(self):
        """When `ensure_clean_helm_release` repairs a stuck `pending-*` release
        (`HELM_RELEASE_REPAIRED=true`) and the run then aborts before the new
        release is applied, `restore_moved_checkout` must note the Helm repair
        rather than claiming `Nothing was applied`."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_then_exit(
            home_dir,
            upstream_url,
            "0.3.0",
            exit_code=1,
            between='HELM_RELEASE_REPAIRED="true"',
        )

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotIn("Nothing was applied", proc.stdout)
        self.assertIn(
            "The new release was not applied (after repairing the pending Helm release)",
            proc.stdout,
        )

    def test_a_plain_directory_at_the_clone_path_is_not_adopted(self):
        """A stale bundle or a copied tree in HOME is not verified release sources.

        verify_local_source_ref accepts anything that is not a Git worktree once
        the baked version equals the requested ref — the default on a release
        copy — so adopting the directory on its existence alone would announce
        an unrelated tree as verified and then apply it to a live install.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        stale_bundle = home_dir / "kube-agents-plain"
        stale_bundle.mkdir()
        (stale_bundle / "install.sh").write_text("an unpacked bundle of some other release\n")
        # Give it an install.env so checkout_owns_run_config passes and only
        # is_kube_agents_clone stands between this directory and adoption.
        (stale_bundle / "install.env").write_text('PROJECT_ID="my-gcp-project"\n')
        # Put the plain directory where the upgrader looks.
        clone_dir.rename(home_dir / "kube-agents-real")
        stale_bundle.rename(clone_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", baked_version="0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertNotIn("baked official release", proc.stdout)
        self.assertEqual((clone_dir / "install.sh").read_text(), "an unpacked bundle of some other release\n")
        self.assertIn(commits["0.3.0"], proc.stdout)

    def test_an_unrelated_repository_at_the_clone_path_is_not_adopted(self):
        """Sharing the directory name or a generic root install.sh is not enough; HEAD has to track kube-agents' own installer layout."""
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture("0.2.0")
        clone_dir.rename(home_dir / "kube-agents-real")
        clone_dir.mkdir()
        self._git("init", "-b", "main", cwd=clone_dir)
        self._git("config", "user.name", "Test", cwd=clone_dir)
        self._git("config", "user.email", "test@example.com", cwd=clone_dir)
        self._git("config", "commit.gpgsign", "false", cwd=clone_dir)
        (clone_dir / "README.md").write_text("not kube-agents\n")
        (clone_dir / "install.sh").write_text("#!/usr/bin/env bash\necho foreign installer\n")
        (clone_dir / "install.env").write_text('PROJECT_ID="my-gcp-project"\n')
        self._git("add", "README.md", "install.sh", cwd=clone_dir)
        self._git("commit", "-m", "init", cwd=clone_dir)
        before = self._head_of(clone_dir)

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), before)

    def test_a_plan_does_not_move_the_install_checkout(self):
        """--plan says it changes nothing, and the operator's checkout is part of nothing."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", preview_flag="PARAM_PLAN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        # Still found, because the install's configuration lives in it.
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertIn("a preview does not move it", proc.stdout)

    def test_a_dry_run_does_not_move_the_install_checkout(self):
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", preview_flag="PARAM_DRY_RUN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def test_a_preview_uses_the_checkout_when_nothing_has_to_move(self):
        """Fetching a second copy of what is already there would be waste, not safety."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.2.0", preview_flag="PARAM_PLAN")

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertIn("already at '0.2.0'", proc.stdout)

    def test_a_run_from_outside_standing_in_the_install_checkout_moves_it(self):
        """Standing in ~/kube-agents when running an outside copy of upgrade.sh moves the checkout rather than failing the ref check."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(home_dir, upstream_url, "0.3.0", cwd=clone_dir)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_a_plan_from_outside_standing_in_the_install_checkout_does_not_move_it(self):
        """An outside --plan copy run while standing in ~/kube-agents keeps it at its current ref and still finds install.env."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_from_outside(
            home_dir, upstream_url, "0.3.0", preview_flag="PARAM_PLAN", cwd=clone_dir
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertIn("a preview does not move it", proc.stdout)

    def test_a_missing_cli_tool_does_not_move_the_install_checkout(self):
        """Like install.sh, missing CLI tools and conflicting preview flags fail before acquire_upgrade_sources moves ~/kube-agents."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        outside_dir = home_dir.parent / "outside"
        outside_dir.mkdir(exist_ok=True)
        # git and the core utilities are on PATH, so acquire_upgrade_sources
        # would fetch into and move the checkout if it ran; gcloud, kubectl and
        # helm are not, so the tool check must stop the run first. HEAD alone
        # cannot tell: restore_moved_checkout returns a moved checkout on any
        # pre-apply exit. So each run also asserts acquire never touched it.
        sterile_bin = create_minimal_tools_bin(home_dir.parent / "sterile")
        self.assertIsNotNone(shutil.which("git", path=str(sterile_bin)))
        isolated_upgrade_sh = outside_dir / "upgrade.sh"
        isolated_upgrade_sh.write_text(
            _UPGRADE_SH.read_text().replace(
                'KUBE_AGENTS_REPO_URL="https://github.com/gke-labs/kube-agents.git"',
                f'KUBE_AGENTS_REPO_URL="{upstream_url}"',
            )
        )
        isolated_env = get_isolated_test_env(
            overrides={
                "HOME": str(home_dir),
                "PATH": str(sterile_bin),
                "KUBE_AGENTS_INSTALL_ENV": "",
                "KUBE_AGENTS_LOCK_FILE": str(outside_dir / "upgrade.lock"),
            }
        )
        proc = subprocess.run(
            [shutil.which("bash") or "/bin/bash", str(isolated_upgrade_sh), "--image-tag=0.3.0", "--non-interactive"],
            capture_output=True,
            text=True,
            env=isolated_env,
            cwd=str(outside_dir),
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("Required CLI tool", combined)
        self.assertNotIn("Using existing repository", combined)
        self.assertNotIn("Moved ", combined)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

        conflicting = subprocess.run(
            [
                shutil.which("bash") or "/bin/bash",
                str(isolated_upgrade_sh),
                "--image-tag=0.3.0",
                "--dry-run",
                "--plan",
                "--non-interactive",
            ],
            capture_output=True,
            text=True,
            env=isolated_env,
            cwd=str(outside_dir),
        )
        combined = conflicting.stdout + conflicting.stderr
        self.assertEqual(conflicting.returncode, 1, combined)
        self.assertIn("--dry-run and --plan are different previews", combined)
        self.assertNotIn("Using existing repository", combined)
        self.assertNotIn("Moved ", combined)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def _acquire_through_a_real_pipe(
        self, home_dir, upstream_url, requested_ref, preview_flag=None, cwd=None, extra_env=None
    ):
        """Run acquire_upgrade_sources with the script arriving on stdin.

        The distinction this makes against _acquire_from_outside is the point:
        there the script is a file, so BASH_SOURCE[0] names it. Under
        `curl … | bash` there is no file: BASH_SOURCE[0] is empty at the top
        level, and inside a function, where acquire_upgrade_sources reads it,
        bash reports `main` or nothing on older releases and `$0` on 5.3 —
        `bash`, or the interpreter's path when invoked by path. None of them
        names a file of this script's, so sourcing a copy from disk cannot
        reach the arms a real pipe takes, whichever bash runs the suite.
        """
        preview_line = f'{preview_flag}="true"' if preview_flag else ":"
        piped = "\n".join(
            [
                "KUBE_AGENTS_SOURCE_ONLY=true",
                _UPGRADE_SH.read_text(),
                f'KUBE_AGENTS_REPO_URL="{upstream_url}"',
                preview_line,
                'repo_dir=""',
                'install_checkout=""',
                f'acquire_upgrade_sources repo_dir install_checkout "{requested_ref}"',
                'echo "REPO_DIR=$repo_dir"',
                'echo "INSTALL_CHECKOUT=$install_checkout"',
            ]
        )
        run_env = {"HOME": str(home_dir), "PATH": os.environ["PATH"]}
        if extra_env:
            run_env.update(extra_env)
        return subprocess.run(
            ["bash", "-s"],
            input=piped,
            capture_output=True,
            text=True,
            env=run_env,
            cwd=str(cwd or home_dir),
        )

    def test_a_real_pipe_from_inside_the_install_checkout_moves_it(self):
        """The documented one-liner, run the way the docs say: standing in the install checkout.

        BASH_SOURCE[0] is non-empty here, so the guard cannot be "is it set";
        it has to be "does it name a file".
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_through_a_real_pipe(home_dir, upstream_url, "0.3.0", cwd=clone_dir)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_a_real_pipe_plan_from_inside_the_install_checkout_does_not_move_it(self):
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")

        proc = self._acquire_through_a_real_pipe(
            home_dir, upstream_url, "0.3.0", preview_flag="PARAM_PLAN", cwd=clone_dir
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))

    def test_a_real_pipe_from_a_neutral_directory_still_finds_the_install_checkout(self):
        """Nothing in the invocation directory, so HOME's checkout is the one to move."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        neutral = home_dir.parent / "neutral"
        neutral.mkdir(exist_ok=True)

        proc = self._acquire_through_a_real_pipe(home_dir, upstream_url, "0.3.0", cwd=neutral)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_a_real_pipe_in_a_dev_clone_without_install_env_moves_the_home_checkout(self):
        """A clean dev clone in $(pwd) carrying no install.env yields to ~/kube-agents."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.env").write_text('CLUSTER_NAME="home-install"\n')
        dev_clone = home_dir.parent / "dev-clone"
        self._git("clone", "--branch", "0.2.0", str(upstream_url), str(dev_clone), cwd=home_dir)

        proc = self._acquire_through_a_real_pipe(home_dir, upstream_url, "0.3.0", cwd=dev_clone)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(dev_clone), commits["0.2.0"])
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])

    def test_a_real_pipe_in_a_dev_clone_with_no_home_checkout_does_not_move_the_dev_clone(self):
        """When neither $(pwd) nor ~/kube-agents holds install.env, a piped run does not detach the developer clone."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        shutil.rmtree(clone_dir)
        dev_clone = home_dir.parent / "dev-clone"
        self._git("clone", "--branch", "0.2.0", str(upstream_url), str(dev_clone), cwd=home_dir)

        proc = self._acquire_through_a_real_pipe(home_dir, upstream_url, "0.3.0", cwd=dev_clone)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(dev_clone), commits["0.2.0"])
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(dev_clone))

    def test_a_run_configured_from_pwd_install_env_does_not_adopt_the_home_checkout(self):
        """Standing in install B's directory (with install.env) fetches a temporary copy rather than moving or writing into ~/kube-agents."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.env").write_text('CLUSTER_NAME="install-a"\n')
        install_b = home_dir.parent / "install-b"
        install_b.mkdir(exist_ok=True)
        (install_b / "install.env").write_text('CLUSTER_NAME="install-b"\n')

        proc = self._acquire_through_a_real_pipe(home_dir, upstream_url, "0.3.0", cwd=install_b)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def test_a_run_configured_from_explicit_install_env_does_not_adopt_the_home_checkout(self):
        """An explicit KUBE_AGENTS_INSTALL_ENV outside ~/kube-agents does not adopt ~/kube-agents; pointing at ~/kube-agents/install.env does."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.env").write_text('CLUSTER_NAME="install-a"\n')
        external_env = home_dir.parent / "ci-install.env"
        external_env.write_text('CLUSTER_NAME="ci-install"\n')
        neutral = home_dir.parent / "neutral"
        neutral.mkdir(exist_ok=True)

        external_proc = self._acquire_through_a_real_pipe(
            home_dir,
            upstream_url,
            "0.3.0",
            cwd=neutral,
            extra_env={"KUBE_AGENTS_INSTALL_ENV": str(external_env)},
        )
        self.assertEqual(external_proc.returncode, 0, external_proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertEqual(self._reported(external_proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(external_proc, "REPO_DIR"), str(clone_dir))

        home_proc = self._acquire_through_a_real_pipe(
            home_dir,
            upstream_url,
            "0.3.0",
            cwd=neutral,
            extra_env={"KUBE_AGENTS_INSTALL_ENV": str(clone_dir / "install.env")},
        )
        self.assertEqual(home_proc.returncode, 0, home_proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])
        self.assertEqual(self._reported(home_proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._reported(home_proc, "REPO_DIR"), str(clone_dir))

    def test_install_env_pointing_at_the_checkout_by_another_name_still_adopts_it(self):
        """The variable names a file, not a spelling.

        A symlink is the shape a CI step leaves when it stages the install's
        config under a fixed path, and a relative path is the shape a hand-run
        upgrade leaves. Both name the checkout's own install.env; comparing the
        strings would answer "no" and send the run to a temporary clone, which
        is the one thing this arm exists to avoid.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0")
        (clone_dir / "install.env").write_text('CLUSTER_NAME="install-a"\n')
        linked_env = home_dir.parent / "staged-install.env"
        linked_env.symlink_to(clone_dir / "install.env")
        neutral = home_dir.parent / "neutral"
        neutral.mkdir(exist_ok=True)

        proc = self._acquire_through_a_real_pipe(
            home_dir,
            upstream_url,
            "0.3.0",
            cwd=neutral,
            extra_env={"KUBE_AGENTS_INSTALL_ENV": str(linked_env)},
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

        subprocess.run(["git", "-C", str(clone_dir), "checkout", "--quiet", "0.2.0"], check=True)
        rel_env = os.path.relpath(clone_dir / "install.env", neutral)
        rel_proc = self._acquire_through_a_real_pipe(
            home_dir,
            upstream_url,
            "0.3.0",
            cwd=neutral,
            extra_env={"KUBE_AGENTS_INSTALL_ENV": rel_env},
        )
        self.assertEqual(rel_proc.returncode, 0, rel_proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.3.0"])
        self.assertEqual(self._reported(rel_proc, "INSTALL_CHECKOUT"), str(clone_dir))
        self.assertEqual(self._reported(rel_proc, "REPO_DIR"), str(clone_dir))

    def test_a_home_checkout_without_install_env_is_not_moved(self):
        """A clone in ~/kube-agents carrying no install.env is not detached onto the release before main() refuses."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture("0.2.0", with_install_env=False)
        neutral = home_dir.parent / "neutral"
        neutral.mkdir(exist_ok=True)

        proc = self._acquire_through_a_real_pipe(home_dir, upstream_url, "0.3.0", cwd=neutral)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        self.assertEqual(self._reported(proc, "INSTALL_CHECKOUT"), "")
        self.assertNotEqual(self._reported(proc, "REPO_DIR"), str(clone_dir))

    def _whole_run_through_a_real_pipe(
        self, home_dir, upstream_url, args, cwd=None, gcloud_exit=0, extra_env=None
    ):
        """Run main() end to end, with the script arriving on stdin.

        Everything above this point stops at a function boundary: the
        acquisition tests call acquire_upgrade_sources and read what it set, and
        ConfigurationLookupOrderTest calls resolve_install_env_file with
        directories it chose itself. The line that hands one to the other is
        main()'s alone, and only a whole run executes it.

        The remote is the fixture's bare repository, substituted into the piped
        text because upgrade.sh assigns KUBE_AGENTS_REPO_URL unconditionally and
        a run that reached github.com would not be hermetic. The CLI tools are
        stubs: this is about what the run reads before it touches a cluster.
        """
        base = home_dir.parent
        stub_bin = base / "stub-bin"
        stub_bin.mkdir(exist_ok=True)
        for tool, exit_code in (
            ("gcloud", gcloud_exit),
            ("kubectl", 0),
            ("helm", 0),
            ("terraform", 0),
            ("jq", 0),
        ):
            # Each call is recorded, so a run stopped by a failing stub can be
            # pinned to the call that stopped it.
            stub = stub_bin / tool
            stub.write_text(
                "#!/usr/bin/env bash\n"
                f'printf "%s\\n" "$*" >> "{base / (tool + ".calls")}"\n'
                f"exit {exit_code}\n"
            )
            stub.chmod(0o755)
        neutral = base / "neutral"
        neutral.mkdir(exist_ok=True)
        script = _UPGRADE_SH.read_text()
        default_remote = 'KUBE_AGENTS_REPO_URL="https://github.com/gke-labs/kube-agents.git"'
        self.assertIn(default_remote, script, "the remote is no longer a plain assignment to substitute")
        env = get_isolated_test_env(
            overrides={
                "HOME": str(home_dir),
                "KUBE_AGENTS_LOCK_FILE": str(base / "upgrade.lock"),
            },
            bin_dir=str(stub_bin),
        )
        # The run has to resolve the file for itself; a pointer in the
        # developer's environment would answer before the hand-off did.
        env.pop("KUBE_AGENTS_INSTALL_ENV", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", "-s", "--", *args],
            input=script.replace(default_remote, f'KUBE_AGENTS_REPO_URL="{upstream_url}"', 1),
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd or neutral),
        )

    def test_a_piped_preview_reads_the_configuration_out_of_the_install_checkout(self):
        """The whole of #1754 in one run: sources from HOME, configuration too.

        A preview builds its own sources, so repo_dir is a temporary clone with
        no install.env in it — which is exactly the shape that used to make the
        upgrade one-liner refuse. The install checkout is the last place the
        resolution looks, after KUBE_AGENTS_INSTALL_ENV, repo_dir and the working
        directory, and the project below is proof the file was not merely found
        but read.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture(
            "0.2.0", real_installer_common=True
        )

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            ["--image-tag=0.3.0", "--upgrade-mode=operator", "--dry-run", "--non-interactive"],
        )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn(f"Loaded install configuration from: {clone_dir}/install.env", combined)
        self.assertIn("GCP Target Project", combined)
        self.assertIn("my-gcp-project", combined)
        # A preview changes nothing, including where the install's checkout sits.
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_piped_preview_without_any_install_env_says_where_it_looked(self):
        """The negative control: the same run, with nothing to find.

        Without it the assertion above could pass on a resolution that names the
        file for reasons of its own, rather than because the hand-off carried
        the install checkout into it. A checkout with no install.env is not
        adopted at all, so the only directory left to search is this run's own
        temporary clone — and the warning names it.

        The preview then refuses, exactly as the real run would: a preview of a
        run that cannot happen would be worth nothing, and the skill offers
        --dry-run as the pre-flight check.
        """
        home_dir, clone_dir, upstream_url, _ = self._existing_clone_fixture(
            "0.2.0", with_install_env=False, real_installer_common=True
        )

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            ["--image-tag=0.3.0", "--upgrade-mode=operator", "--dry-run", "--non-interactive"],
        )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("No install configuration (install.env) was found in", combined)
        self.assertNotIn("Loaded install configuration from", combined)
        self.assertNotIn(str(clone_dir), combined.split("was found in", 1)[1])
        self.assertIn("Refusing to upgrade without the installation's configuration", combined)
        self.assertNotIn("Dry-Run Upgrade Plan Preview", combined)

    def test_a_piped_upgrade_gets_past_the_refusal_that_sent_1754_here(self):
        """The reported failure, run: `curl … | bash` after a documented install.

        Unlike the preview above, a real run adopts the install checkout onto
        the requested ref and relies on restore_moved_checkout to roll it back
        if the run fails before applying. So this is a real run, stopped at the
        first thing that needs a cluster: the credentials fetch. What it owes is
        the ground it covered before that — configuration loaded, no refusal —
        and the checkout it detached handed back, since nothing was applied.

        It is not the test that pins the hand-off's second argument: a real run
        adopts the install checkout as its sources, so repo_dir and
        install_checkout are the same directory and either would find the file.
        The preview is where they differ, and where dropping the argument
        brings the refusal back.
        """
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture(
            "0.2.0", real_installer_common=True
        )

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            ["--image-tag=0.3.0", "--upgrade-mode=operator", "--non-interactive"],
            gcloud_exit=1,
        )

        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(f"Loaded install configuration from: {clone_dir}/install.env", combined)
        self.assertNotIn("Refusing to upgrade without", combined)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])
        # Stopped where the docstring says: the credentials fetch is the last
        # thing the run asked gcloud for, and no cluster tool was reached. A
        # refusal earlier in main() would also exit non-zero and restore the
        # checkout, so without this the assertions above could not tell.
        base = home_dir.parent
        gcloud_log = base / "gcloud.calls"
        self.assertTrue(gcloud_log.exists(), "the run stopped before it called gcloud at all")
        gcloud_calls = gcloud_log.read_text().splitlines()
        self.assertTrue(gcloud_calls[-1].startswith("container clusters get-credentials"), gcloud_calls)
        for tool in ("kubectl", "helm", "terraform"):
            self.assertFalse((base / f"{tool}.calls").exists(), f"{tool} ran before the credentials fetch")

    def _fixture_configured_for(self, cluster):
        """A clone whose install.env belongs to a named install."""
        home_dir, clone_dir, upstream_url, commits = self._existing_clone_fixture(
            "0.2.0", real_installer_common=True
        )
        (clone_dir / "install.env").write_text(
            f'PROJECT_ID="my-gcp-project"\nCLUSTER_NAME="{cluster}"\n'
        )
        return home_dir, clone_dir, upstream_url, commits

    def test_a_run_refuses_when_the_configuration_belongs_to_another_install(self):
        """Two installs on one workstation, and the piped run has nowhere to stand.

        The lookup order is what usually keeps the configuration and the target
        in step — standing in an install's directory upgrades that install — and
        the one-liner has no directory to stand in, so whatever is in
        $HOME/kube-agents is loaded however the flags aim the run. A full
        upgrade re-renders the PlatformAgent CR out of that file, so this is not
        just the wrong install: it is one install's chat space, allowed users
        and model provider written into another.
        """
        home_dir, clone_dir, upstream_url, commits = self._fixture_configured_for("install-a")

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            [
                "--image-tag=0.3.0",
                "--upgrade-mode=operator",
                "--non-interactive",
                "--gke-cluster-name=install-b",
            ],
        )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertIn("records a different install than the flags name", combined)
        self.assertIn("--gke-cluster-name=install-b, but CLUSTER_NAME=install-a", combined)
        # Refused before anything was applied, so the checkout goes back.
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_preview_over_another_installs_configuration_warns_and_goes_on(self):
        """Previews change nothing, so they report the disagreement and continue.

        The same split the dirty checkout and the release-tag cross-check take:
        the operator asked what would happen, and the answer includes which file
        the run would have read.
        """
        home_dir, clone_dir, upstream_url, _ = self._fixture_configured_for("install-a")

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            [
                "--image-tag=0.3.0",
                "--upgrade-mode=operator",
                "--non-interactive",
                "--dry-run",
                "--gke-cluster-name=install-b",
            ],
        )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("was written for another install", combined)
        self.assertIn("--gke-cluster-name=install-b, but CLUSTER_NAME=install-a", combined)
        self.assertIn("Dry-Run Upgrade Plan Preview", combined)

    def test_a_plan_refuses_when_the_configuration_belongs_to_another_install(self):
        """Unlike --dry-run, --plan renders terraform.tfvars and runs lifecycle.sh plan.

        When the checkout in $HOME/kube-agents is already at the target ref,
        acquire_upgrade_sources adopts it for --plan too; letting --plan proceed
        over a coordinate conflict would rewrite install A's terraform.tfvars
        and .terraform/ backend for cluster B while diffing B's state against
        A's configuration.
        """
        home_dir, clone_dir, upstream_url, _ = self._fixture_configured_for("install-a")
        tfvars = clone_dir / "terraform" / "examples" / "full-install" / "terraform.tfvars"
        tfvars.parent.mkdir(parents=True, exist_ok=True)
        tfvars.write_text('cluster_name = "install-a"\n')

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            [
                "--image-tag=0.2.0",
                "--plan",
                "--non-interactive",
                "--gke-cluster-name=install-b",
            ],
        )

        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 1, combined)
        self.assertNotIn("Required CLI tool", combined)
        self.assertIn("records a different install than the flags name", combined)
        self.assertEqual(tfvars.read_text(), 'cluster_name = "install-a"\n')

    def test_a_flag_that_agrees_with_the_configuration_is_not_a_conflict(self):
        """The ordinary case: the documented one-liner names its own install.

        Without this the refusal above could be firing on the flag's presence
        rather than on a disagreement, and every documented upgrade would stop.
        """
        home_dir, clone_dir, upstream_url, commits = self._fixture_configured_for("install-a")

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            [
                "--image-tag=0.3.0",
                "--upgrade-mode=operator",
                "--non-interactive",
                "--gke-cluster-name=install-a",
                "--gcp-project-id=my-gcp-project",
            ],
            gcloud_exit=1,
        )

        combined = proc.stdout + proc.stderr
        self.assertNotIn("another install", combined)
        self.assertNotIn("Refusing to upgrade", combined)
        # It stopped where the previous test's run did: at the credentials fetch.
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])

    def test_a_shell_exported_coordinate_is_not_mistaken_for_a_key_in_install_env(self):
        """An ambient `export REGION=...` in the caller's shell is not a key in
        install.env, so when install.env omits REGION and the caller passes
        --gcp-region, the cross-check must not blame install.env for a value the
        file never recorded."""
        home_dir, clone_dir, upstream_url, commits = self._fixture_configured_for("install-a")

        proc = self._whole_run_through_a_real_pipe(
            home_dir,
            upstream_url,
            [
                "--image-tag=0.3.0",
                "--upgrade-mode=operator",
                "--non-interactive",
                "--gke-cluster-name=install-a",
                "--gcp-project-id=my-gcp-project",
                "--gcp-region=us-central1",
            ],
            gcloud_exit=1,
            extra_env={"REGION": "europe-west1"},
        )

        combined = proc.stdout + proc.stderr
        self.assertNotIn("records a different install", combined)
        self.assertNotIn("was written for another install", combined)
        self.assertNotIn("Refusing to upgrade", combined)
        self.assertEqual(self._head_of(clone_dir), commits["0.2.0"])


class ConfigurationLookupOrderTest(unittest.TestCase):
    """Which install.env an upgrade loads, when more than one is reachable.

    install.sh resolves: KUBE_AGENTS_INSTALL_ENV -> the script's own checkout ->
    $(pwd) -> the clone in HOME. The upgrade had only the first and the last, and
    when it gained $(pwd) it put the HOME checkout ahead of it — so with two
    installs on one workstation, standing in B's directory and passing
    --gke-cluster-name=B loaded A's chat space, allowed users, model provider,
    NAMESPACE and GitOps repo out of ~/kube-agents/install.env.
    """

    _INSTALLER_COMMON = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"

    def _resolve(self, repo_dir, install_checkout, cwd, home=None, install_env_var=None):
        setup = f"""
KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"
# default_install_env_file is the last candidate, and it lives here.
source "{self._INSTALLER_COMMON}"
resolve_install_env_file "{repo_dir}" "{install_checkout}"
"""
        env = {"HOME": str(home if home is not None else cwd), "PATH": os.environ["PATH"]}
        if install_env_var is not None:
            env["KUBE_AGENTS_INSTALL_ENV"] = str(install_env_var)
        proc = subprocess.run(
            ["bash", "-c", setup],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _layout(self):
        """A run's own sources, the operator's working directory, and the install checkout."""
        temp_dir = tempfile.TemporaryDirectory(prefix="install-env-order-")
        self.addCleanup(temp_dir.cleanup)
        base = pathlib.Path(temp_dir.name)
        for name in ("sources", "cwd", "checkout", "home"):
            (base / name).mkdir()
        (base / "home" / "kube-agents").mkdir()
        return base

    def test_the_explicit_pointer_wins(self):
        base = self._layout()
        named = base / "named.env"
        named.write_text("")
        (base / "sources" / "install.env").write_text("")
        (base / "cwd" / "install.env").write_text("")
        (base / "checkout" / "install.env").write_text("")

        resolved = self._resolve(base / "sources", base / "checkout", base / "cwd", home=base / "home", install_env_var=named)

        self.assertEqual(resolved, str(named))

    def test_the_run_s_own_checkout_wins_over_the_working_directory(self):
        """Running ./upgrade.sh from a checkout loads that checkout's configuration."""
        base = self._layout()
        (base / "sources" / "install.env").write_text("")
        (base / "cwd" / "install.env").write_text("")

        resolved = self._resolve(base / "sources", base / "checkout", base / "cwd", home=base / "home")

        self.assertEqual(resolved, str(base / "sources" / "install.env"))

    def test_the_working_directory_wins_over_the_install_checkout(self):
        """The regression this order exists to stop: two installs, one $HOME/kube-agents.

        The piped run's sources ARE the install checkout, so preferring the
        sources' directory would silently load the wrong install's chat space,
        allowed users and namespace.
        """
        base = self._layout()
        (base / "cwd" / "install.env").write_text("")
        (base / "checkout" / "install.env").write_text("")

        resolved = self._resolve(base / "checkout", base / "checkout", base / "cwd", home=base / "home")

        self.assertEqual(resolved, str(base / "cwd" / "install.env"))

    def test_the_install_checkout_is_used_when_nothing_is_nearer(self):
        """The documented one-liner, run from a directory with no configuration in it."""
        base = self._layout()
        (base / "checkout" / "install.env").write_text("")

        resolved = self._resolve(base / "checkout", base / "checkout", base / "cwd", home=base / "home")

        self.assertEqual(resolved, str(base / "checkout" / "install.env"))

    def test_a_checkout_run_without_its_own_install_env_does_not_reach_into_home(self):
        """Running `./upgrade.sh` from a fresh git clone or unpacked bundle leaves
        `install_checkout` empty in `acquire_upgrade_sources`. Matching
        `install.sh`'s `_resolve_repo_dir_for_state`, a checkout run with no
        `install.env` of its own must return its own default path (and refuse in
        `main()`) rather than loading a different install's configuration from
        `$HOME/kube-agents/install.env`."""
        base = self._layout()
        (base / "home" / "kube-agents" / "install.env").write_text("")

        resolved = self._resolve(base / "sources", "", base / "cwd", home=base / "home")

        self.assertEqual(resolved, str(base / "sources" / "install.env"))

    def test_with_nothing_anywhere_it_names_the_sources_directory(self):
        """Nothing to load: the refusal that follows names where one would live."""
        base = self._layout()

        resolved = self._resolve(base / "sources", "", base / "cwd", home=base / "home")

        self.assertEqual(resolved, str(base / "sources" / "install.env"))

    def test_upgrade_never_references_retired_vars_sh(self):
        """k8s-operator/scripts/vars.sh is retired and never read, written, or inspected by upgrade.sh or installer_common.sh."""
        for path in (_UPGRADE_SH, self._INSTALLER_COMMON):
            with self.subTest(file=path.name):
                text = path.read_text()
                self.assertNotIn("k8s-operator/scripts/vars.sh", text)
                self.assertNotIn("load_legacy_vars_file", text)


class FrontDoorsAgreeOnTheInstallCheckoutTest(unittest.TestCase):
    """install.sh and upgrade.sh have to find and move the same checkout.

    These helpers cannot live in scripts/installer/installer_common.sh, which is
    sourced out of the very checkout they go and find, so each front door
    carries a copy — the arrangement installer_common.sh already describes for
    the install.env loader. Copies drift, so they are pinned here.
    """

    _INSTALL_SH = _REPO_ROOT / "install.sh"

    @staticmethod
    def _function_text(source, name):
        opening = f"\n{name}() {{\n"
        start = source.index(opening) + 1
        end = source.index("\n}\n", start) + len("\n}\n")
        return source[start:end]

    def test_the_helpers_are_identical(self):
        install_sh = self._INSTALL_SH.read_text()
        upgrade_sh = _UPGRADE_SH.read_text()
        for name in ("fetch_source_ref", "refresh_existing_clone"):
            with self.subTest(function=name):
                self.assertEqual(
                    self._function_text(install_sh, name),
                    self._function_text(upgrade_sh, name),
                    f"{name} has drifted between install.sh and upgrade.sh",
                )

    def test_the_clone_constants_are_identical(self):
        install_sh = self._INSTALL_SH.read_text()
        upgrade_sh = _UPGRADE_SH.read_text()
        for constant in ("KUBE_AGENTS_CLONE_MARKER", "KUBE_AGENTS_FETCH_DEPTH_OPT"):
            with self.subTest(constant=constant):
                pattern = rf'^{constant}="([^"]+)"$'
                install_value = re.search(pattern, install_sh, re.MULTILINE)
                upgrade_value = re.search(pattern, upgrade_sh, re.MULTILINE)
                self.assertIsNotNone(install_value, f"install.sh does not declare {constant}")
                self.assertIsNotNone(upgrade_value, f"upgrade.sh does not declare {constant}")
                self.assertEqual(install_value.group(1), upgrade_value.group(1))

    def test_both_front_doors_name_the_same_directory(self):
        """The message HOME:? carries differs; the path it builds may not."""
        with tempfile.TemporaryDirectory(prefix="front-doors-install-env-") as env_dir:
            empty_install_env = pathlib.Path(env_dir) / "install.env"
            empty_install_env.write_text("")
            paths = {}
            for script in (self._INSTALL_SH, _UPGRADE_SH):
                proc = subprocess.run(
                    ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{script}"; kube_agents_clone_dir'],
                    capture_output=True,
                    text=True,
                    env={
                        "HOME": "/h",
                        "PATH": os.environ["PATH"],
                        "KUBE_AGENTS_INSTALL_ENV": str(empty_install_env),
                    },
                    cwd=str(_REPO_ROOT),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                paths[script.name] = proc.stdout.strip()
        self.assertEqual(paths["install.sh"], "/h/kube-agents")
        self.assertEqual(paths["upgrade.sh"], paths["install.sh"])
        # uninstall.sh takes its lock at source time, so only its definition
        # is evaluated rather than the whole script sourced.
        definition = re.search(
            r"^kube_agents_clone_dir\(\) \{.*\}$", (_REPO_ROOT / "uninstall.sh").read_text(), re.MULTILINE
        )
        self.assertIsNotNone(definition, "uninstall.sh does not define kube_agents_clone_dir")
        proc = subprocess.run(
            ["bash", "-c", f"set -u\n{definition.group(0)}\nkube_agents_clone_dir"],
            capture_output=True,
            text=True,
            env={"HOME": "/h", "PATH": os.environ["PATH"]},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), paths["install.sh"])


class UpgradeRunsAreSerialisedTest(unittest.TestCase):
    """install.sh and upgrade.sh share one checkout lock; uninstall.sh has its own.

    Both install.sh and upgrade.sh move $HOME/kube-agents onto the release they
    are applying (each through its own `refresh_existing_clone`; upgrade.sh
    also returns it with `restore_moved_checkout` on a pre-apply refusal) and
    write `terraform.tfvars` inside it, so an install and an upgrade running at
    once would take turns moving that one checkout while both read terraform
    and charts out of it.
    """

    _FRONT_DOORS = ("install.sh", "uninstall.sh", "upgrade.sh")

    def test_install_and_upgrade_share_the_checkout_lock(self):
        """install.sh and upgrade.sh lock the same file; uninstall.sh locks its own."""
        defaults = {}
        for name in self._FRONT_DOORS:
            with self.subTest(script=name):
                text = (_REPO_ROOT / name).read_text()
                match = re.search(
                    r'LOCK_FILE="(?:\$\{KUBE_AGENTS_LOCK_FILE:-)?([^"}]+)\}?"', text
                )
                self.assertIsNotNone(match, f"{name} takes no lock ({len(text)} chars read)")
                self.assertIn("flock -n 200", text, f"{name}'s lock waits instead of reporting")
                defaults[name] = match.group(1)
        self.assertEqual(defaults["install.sh"], "/tmp/kube-agents-install.lock")
        self.assertEqual(defaults["upgrade.sh"], defaults["install.sh"])
        self.assertEqual(defaults["uninstall.sh"], "/tmp/kube-agents-uninstall.lock")

    def test_the_lock_is_not_taken_when_the_script_is_only_sourced(self):
        """The suite sources upgrade.sh; a lock at source time would serialise it
        against itself, and against any upgrade running on the same machine.

        This pins the guard's placement; the behaviour itself (a source-only
        load with the lock held still succeeds) is asserted in
        test_a_second_run_stops_while_the_first_holds_the_lock."""
        text = _UPGRADE_SH.read_text()
        guard = text.index('if [ "${KUBE_AGENTS_SOURCE_ONLY:-false}" != "true" ] && command -v flock')
        self.assertLess(guard, text.index("flock -n 200"))

    def test_a_second_run_stops_while_the_first_holds_the_lock(self):
        """The property, not its spelling: with the lock held, the run reports and exits."""
        if shutil.which("flock") is None:
            self.skipTest("flock is not available on this host")
        with tempfile.TemporaryDirectory(prefix="upgrade-lock-") as tmp:
            lock_file = pathlib.Path(tmp) / "upgrade.lock"
            lock_file.touch()
            # Its own session, so the whole group can be killed: flock(1) runs
            # `sleep` as a child that inherits the locked descriptor, and
            # killing flock alone would leave that child holding the lock.
            holder = subprocess.Popen(
                ["flock", str(lock_file), "-c", "sleep 30"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                # Given a moment to take it: flock(1) opens the file and blocks
                # before exec'ing, and a race here would test nothing at all.
                deadline = time.time() + 10
                while time.time() < deadline:
                    probe = subprocess.run(
                        ["flock", "-n", str(lock_file), "-c", "true"], capture_output=True
                    )
                    if probe.returncode != 0:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("the holder never took the lock")

                proc = subprocess.run(
                    ["bash", "-s", "--", "--help"],
                    input=_UPGRADE_SH.read_text(),
                    capture_output=True,
                    text=True,
                    env=get_isolated_test_env(
                        overrides={"KUBE_AGENTS_LOCK_FILE": str(lock_file), "HOME": tmp}
                    ),
                    cwd=tmp,
                )
                # And the source-only load the suite relies on is not stopped by
                # the same held lock.
                sourced = subprocess.run(
                    ["bash", "-c", f'KUBE_AGENTS_SOURCE_ONLY=true source "{_UPGRADE_SH}"; echo SOURCED'],
                    capture_output=True,
                    text=True,
                    env=get_isolated_test_env(
                        overrides={"KUBE_AGENTS_LOCK_FILE": str(lock_file), "HOME": tmp}
                    ),
                    cwd=tmp,
                )
            finally:
                os.killpg(holder.pid, signal.SIGKILL)
                holder.wait()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Another instance of the kube-agents installer or upgrade", proc.stdout + proc.stderr)
        self.assertEqual(sourced.returncode, 0, sourced.stdout + sourced.stderr)
        self.assertIn("SOURCED\n", sourced.stdout)
        self.assertNotIn("Another instance", sourced.stdout + sourced.stderr)

    def test_a_free_lock_lets_the_run_through(self):
        """The control: the same run with nothing holding the lock answers normally."""
        if shutil.which("flock") is None:
            self.skipTest("flock is not available on this host")
        with tempfile.TemporaryDirectory(prefix="upgrade-lock-free-") as tmp:
            lock_file = pathlib.Path(tmp) / "upgrade.lock"
            proc = subprocess.run(
                ["bash", "-s", "--", "--help"],
                input=_UPGRADE_SH.read_text(),
                capture_output=True,
                text=True,
                env=get_isolated_test_env(
                    overrides={"KUBE_AGENTS_LOCK_FILE": str(lock_file), "HOME": tmp}
                ),
                cwd=tmp,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Another instance", proc.stdout + proc.stderr)


class PipedUpgradeResolvesItsSourcesTest(unittest.TestCase):
    def test_a_piped_run_does_not_abort_on_an_unset_bash_source(self):
        """Under `curl | bash` with `set -u`, BASH_SOURCE[0] may name no file.

        install.sh has always defaulted it; upgrade.sh did not, so the source
        resolution could abort with an unbound variable instead of reporting
        what was wrong. --keep-image-tag reaches that resolution and then stops
        for its own, expected reason.
        """
        with tempfile.TemporaryDirectory(prefix="outside-checkout-") as outside:
            empty_install_env = pathlib.Path(outside) / "pinned-install.env"
            empty_install_env.write_text("")
            lock_file = pathlib.Path(outside) / "upgrade.lock"
            # main() checks its CLI tools before resolving sources, the way
            # install.sh does. Stub them, so this test reads the source
            # resolution it is about on any host, rather than whichever tool the
            # runner happens to be missing. The list has to be upgrade.sh's
            # required_tools for this mode, not a subset: the default mode is
            # full, which asks for jq as well since the harness step reads the
            # release's plugin image tags with it, and a host without jq would
            # otherwise stop at the tool gate and fail the assertion below on a
            # message that has nothing to do with what is under test.
            stub_bin = pathlib.Path(outside) / "bin"
            stub_bin.mkdir()
            for tool in ("gcloud", "kubectl", "helm", "jq", "terraform"):
                stub = stub_bin / tool
                stub.write_text("#!/usr/bin/env bash\nexit 0\n")
                stub.chmod(0o755)
            proc = subprocess.run(
                ["bash", "-s", "--", "--keep-image-tag", "--non-interactive", "--gcp-project-id=my-gcp-project"],
                input=_UPGRADE_SH.read_text(),
                capture_output=True,
                text=True,
                env=get_isolated_test_env(
                    overrides={
                        "KUBE_AGENTS_INSTALL_ENV": str(empty_install_env),
                        "KUBE_AGENTS_LOCK_FILE": str(lock_file),
                        "IMAGE_TAG": "",
                    },
                    bin_dir=str(stub_bin),
                ),
                cwd=outside,
            )
        combined = proc.stdout + proc.stderr
        self.assertNotIn("unbound variable", combined)
        self.assertNotIn("Required CLI tool", combined)
        self.assertIn("have to run from a kube-agents checkout", combined)


class ExplicitInstallEnvPointerTest(unittest.TestCase):
    """An explicit KUBE_AGENTS_INSTALL_ENV naming a file that is not there.

    The lookup order used to hand the nonexistent path to load_install_env,
    which returned 1 silently, and the run ended at "No install configuration
    (install.env) was found in <a directory the operator never chose>" — with
    "point KUBE_AGENTS_INSTALL_ENV at one" as the advice, which is what they
    had just done. On a piped run it was worse: the pointer also makes
    checkout_owns_run_config reject $HOME/kube-agents, so the run first cloned
    the engine into /tmp and then blamed that temporary directory.
    """

    def _run_piped(self, install_env, cwd, home=None, image_tag_args=("--keep-image-tag",)):
        stub_bin = pathlib.Path(cwd) / "bin"
        stub_bin.mkdir(exist_ok=True)
        # The tool gate sits after the refusal under test, but a host missing
        # one of these would still reach it if the refusal ever moved, and the
        # assertions below would then read a message about the wrong thing.
        for tool in ("gcloud", "kubectl", "helm", "jq", "terraform", "git"):
            stub = stub_bin / tool
            stub.write_text("#!/usr/bin/env bash\necho \"STUB $0 $*\"\nexit 0\n")
            stub.chmod(0o755)
        overrides = {
            "KUBE_AGENTS_INSTALL_ENV": str(install_env),
            "KUBE_AGENTS_LOCK_FILE": str(pathlib.Path(cwd) / "upgrade.lock"),
            "IMAGE_TAG": "",
        }
        if home is not None:
            overrides["HOME"] = str(home)
        return subprocess.run(
            ["bash", "-s", "--", *image_tag_args, "--non-interactive"],
            input=_UPGRADE_SH.read_text(),
            capture_output=True,
            text=True,
            env=get_isolated_test_env(overrides=overrides, bin_dir=str(stub_bin)),
            cwd=str(cwd),
        )

    def test_a_pointer_at_a_missing_file_is_refused_by_the_name_the_operator_gave(self):
        with tempfile.TemporaryDirectory(prefix="mistyped-pointer-") as outside:
            missing = pathlib.Path(outside) / "kube-agnets" / "install.env"
            proc = self._run_piped(missing, outside)

        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn(str(missing), combined)
        self.assertIn("which does not exist", combined)
        # Not the message that blames a directory nobody named.
        self.assertNotIn("No install configuration (install.env) was found", combined)

    def test_the_refusal_comes_before_the_run_fetches_anything(self):
        """Placement matters: the same pointer makes checkout_owns_run_config
        reject the install checkout, so a refusal after source acquisition
        would clone the engine from GitHub first and then name /tmp.

        A concrete --image-tag rather than --keep-image-tag, because a piped
        run without a tag has no ref to fetch the engine at and dies inside
        acquire_upgrade_sources before reaching the clone — which would make
        these assertions hold whether the refusal is placed correctly or not.
        """
        with tempfile.TemporaryDirectory(prefix="mistyped-pointer-clone-") as outside:
            home = pathlib.Path(outside) / "home"
            home.mkdir()
            missing = home / "kube-agnets" / "install.env"
            proc = self._run_piped(
                missing,
                outside,
                home=home,
                image_tag_args=(f"--image-tag={'a' * 40}",),
            )

        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertNotIn("STUB", combined, "no tool should have run before the refusal")
        self.assertNotIn("Fetching", combined)

    def test_the_comment_on_the_string_compare_fallback_names_where_the_refusal_is(self):
        """The fallback's comment claimed resolve_install_env_file refuses a
        nonexistent pointer. It never did; main() does, and the comment has to
        say so or the next reader removes the check as redundant."""
        source = _UPGRADE_SH.read_text()
        refusal = 'print_error "KUBE_AGENTS_INSTALL_ENV names'
        self.assertIn(refusal, source)
        self.assertLess(
            source.index(refusal),
            source.index("acquire_upgrade_sources repo_dir install_checkout"),
        )
        self.assertNotIn(
            "resolve_install_env_file refuses on its own",
            source,
        )


if __name__ == "__main__":
    unittest.main()
