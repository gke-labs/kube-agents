"""Unit tests for vnext optional AgentProfile scaffolding and Helm templating."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

_HERE = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_ENTRYPOINT = _REPO_ROOT / "deploy" / "shared" / "docker-entrypoint.sh"

sys.path.insert(0, str(_HERE))
from _lift_shell import lift_constant, lift_function  # noqa: E402


class EntrypointOptionalProfilesTest(unittest.TestCase):
    def _run_reconcile(
        self,
        target_dir: pathlib.Path,
        install_dir: pathlib.Path,
        tse_template_dir: pathlib.Path,
        enabled_profiles: str,
        *,
        cwd: pathlib.Path | None = None,
    ) -> subprocess.CompletedProcess:
        entrypoint_text = _ENTRYPOINT.read_text(encoding="utf-8")
        plugins_dir = _REPO_ROOT / "agents" / "chat" / "defaults" / "plugins"
        lifted_reconcile = lift_function(
            "reconcile_optional_profiles", entrypoint_text, _ENTRYPOINT
        )
        script = (
            "set -e\n"
            f'readonly TSE_TEMPLATE="{tse_template_dir}"\n'
            f'readonly DEFAULTS_PLUGINS_DIR="{plugins_dir}"\n'
            + lift_constant("OPTIONAL_PROFILE_STAMP", entrypoint_text, _ENTRYPOINT)
            + lift_constant("DELETED_PROFILES_DIR", entrypoint_text, _ENTRYPOINT)
            + lift_constant("OPTIONAL_SYNC_ITEMS", entrypoint_text, _ENTRYPOINT)
            + lift_function("sync_profile_skills", entrypoint_text, _ENTRYPOINT)
            + "\n"
            + lifted_reconcile
            + "\nreconcile_optional_profiles\n"
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "TARGET_DIR": str(target_dir),
            "INSTALL_DIR": str(install_dir),
            "HERMES_BIN": str(install_dir / ".venv" / "bin" / "hermes"),
            "SCAFFOLD": str(
                _REPO_ROOT / "agents" / "platform" / "scripts" / "profile_scaffold.py"
            ),
            "KUBE_AGENTS_ENABLED_PROFILES": enabled_profiles,
        }
        return subprocess.run(
            ["sh", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd) if cwd else None,
            timeout=60,
        )

    def _setup_fixture(self, root: pathlib.Path):
        target_dir = root / "data"
        (target_dir / "scripts").mkdir(parents=True)
        (target_dir / "profiles" / "platform").mkdir(parents=True)
        (target_dir / "profiles" / "platform" / "SOUL.md").write_text(
            "platform\n", encoding="utf-8"
        )

        install_dir = root / "install"
        venv_bin = install_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        py_wrapper = venv_bin / "python3"
        py_wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
        )
        py_wrapper.chmod(0o755)
        hermes_stub = venv_bin / "hermes"
        hermes_stub.write_text(
            '#!/bin/sh\nmkdir -p "$HERMES_HOME/profiles/$3"\nprintf "name: %s\\n" "$3" > "$HERMES_HOME/profiles/$3/profile.yaml"\n',
            encoding="utf-8",
        )
        hermes_stub.chmod(0o755)

        tse_tpl = root / "opt" / "tse-template"
        shutil.copytree(_REPO_ROOT / "agents" / "tse", tse_tpl)
        return target_dir, install_dir, tse_tpl, hermes_stub

    def test_enable_disable_and_restore_optional_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir, install_dir, tse_tpl, _ = self._setup_fixture(root)

            # 1. Enable built-in `tse` while passing unsupported `chat` and `auditor` names.
            proc = self._run_reconcile(
                target_dir,
                install_dir,
                tse_tpl,
                "tse,chat,auditor",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("ignoring unsupported or reserved optional profile name 'chat'", proc.stderr)
            self.assertFalse((target_dir / "profiles" / "chat").exists())
            self.assertTrue((target_dir / "profiles" / "tse" / "profile.yaml").is_file())
            self.assertTrue((target_dir / "profiles" / "tse" / ".optional-profile").is_file())
            self.assertTrue(
                (
                    target_dir
                    / "profiles"
                    / "tse"
                    / "skills"
                    / "support-escalation-bundle"
                    / "SKILL.md"
                ).is_file()
            )
            self.assertTrue((target_dir / "profiles" / "tse" / "scripts").is_symlink())

            # Write a session artifact inside `tse` to verify archive/restore preserves state.
            sessions_dir = target_dir / "profiles" / "tse" / "sessions"
            sessions_dir.mkdir(parents=True, exist_ok=True)
            marker = sessions_dir / "session-1.json"
            marker.write_text('{"id": "session-1"}\n', encoding="utf-8")

            # 2. Disable `tse`.
            proc = self._run_reconcile(target_dir, install_dir, tse_tpl, "")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((target_dir / "profiles" / "tse").exists())
            self.assertTrue(
                (target_dir / "profiles" / ".deleted" / "tse" / "sessions" / "session-1.json").is_file()
            )
            # `platform` (non-optional) remains intact.
            self.assertTrue((target_dir / "profiles" / "platform" / "SOUL.md").is_file())

            # 3. Re-enable `tse` -> restored from `.deleted/tse` with session state intact.
            proc = self._run_reconcile(target_dir, install_dir, tse_tpl, "tse")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(
                (target_dir / "profiles" / "tse" / "sessions" / "session-1.json").is_file()
            )

    def test_coexisting_live_and_archived_profile_preserves_prior_archive_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir, install_dir, tse_tpl, _ = self._setup_fixture(root)

            # 1. Enable `tse`, write session-old, and disable so `.deleted/tse` holds session-old.
            proc = self._run_reconcile(target_dir, install_dir, tse_tpl, "tse")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            old_sessions = target_dir / "profiles" / "tse" / "sessions"
            old_sessions.mkdir(parents=True, exist_ok=True)
            (old_sessions / "session-old.json").write_text('{"id": "old"}\n', encoding="utf-8")

            proc_dis1 = self._run_reconcile(target_dir, install_dir, tse_tpl, "")
            self.assertEqual(proc_dis1.returncode, 0, proc_dis1.stderr)
            self.assertTrue(
                (target_dir / "profiles" / ".deleted" / "tse" / "sessions" / "session-old.json").is_file()
            )

            # 2. Simulate an operator recreating `profiles/tse` beside the existing `.deleted/tse` archive.
            live_tse = target_dir / "profiles" / "tse"
            (live_tse / "sessions").mkdir(parents=True, exist_ok=True)
            (live_tse / "profile.yaml").write_text("name: tse\n", encoding="utf-8")
            (live_tse / ".optional-profile").write_text("", encoding="utf-8")
            (live_tse / "sessions" / "session-new.json").write_text('{"id": "new"}\n', encoding="utf-8")

            # 3. Disable `tse` while both `profiles/tse` and `profiles/.deleted/tse` exist:
            # prior `.deleted/tse` must be rotated to `.deleted/tse-*` (never `rm -rf`'d),
            # and `profiles/tse` moves to `.deleted/tse`.
            proc_dis2 = self._run_reconcile(target_dir, install_dir, tse_tpl, "")
            self.assertEqual(proc_dis2.returncode, 0, proc_dis2.stderr)
            self.assertFalse(live_tse.exists())
            self.assertTrue(
                (target_dir / "profiles" / ".deleted" / "tse" / "sessions" / "session-new.json").is_file()
            )
            rotated = list((target_dir / "profiles" / ".deleted").glob("tse-*"))
            self.assertEqual(len(rotated), 1, f"expected 1 rotated archive, got {rotated}")
            self.assertTrue((rotated[0] / "sessions" / "session-old.json").is_file())

    def test_failed_first_scaffold_leaves_clean_home_for_next_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir, install_dir, tse_tpl, hermes_stub = self._setup_fixture(root)

            # Simulate `hermes profile create` failing on first boot.
            hermes_stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            proc = self._run_reconcile(target_dir, install_dir, tse_tpl, "tse")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("has no profile.yaml after scaffold; skipping sync", proc.stderr)
            self.assertFalse((target_dir / "profiles" / "tse" / ".optional-profile").exists())
            self.assertFalse((target_dir / "profiles" / "tse" / "SOUL.md").exists())
            self.assertFalse((target_dir / "profiles" / "tse" / "scripts").exists())

            # Restore working `hermes` stub on next boot -> cleanly registers profile.yaml!
            hermes_stub.write_text(
                '#!/bin/sh\nmkdir -p "$HERMES_HOME/profiles/$3"\nprintf "name: %s\\n" "$3" > "$HERMES_HOME/profiles/$3/profile.yaml"\n',
                encoding="utf-8",
            )
            proc2 = self._run_reconcile(target_dir, install_dir, tse_tpl, "tse")
            self.assertEqual(proc2.returncode, 0, proc2.stderr)
            self.assertTrue((target_dir / "profiles" / "tse" / "profile.yaml").is_file())
            self.assertTrue((target_dir / "profiles" / "tse" / "SOUL.md").is_file())

    def test_glob_metacharacters_do_not_expand(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir, install_dir, tse_tpl, _ = self._setup_fixture(root)
            cwd_dir = root / "cwd"
            cwd_dir.mkdir()
            (cwd_dir / "tse").write_text("decoy\n", encoding="utf-8")
            (cwd_dir / "other-file").write_text("decoy\n", encoding="utf-8")

            # Glob `*` in cwd containing a file named `tse` must NOT expand to `tse`!
            proc = self._run_reconcile(
                target_dir, install_dir, tse_tpl, "*,tse?,[a-z]*", cwd=cwd_dir
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((target_dir / "profiles" / "tse").exists())
            self.assertIn("ignoring unsupported or reserved optional profile name '*'", proc.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits this test relies on")
    def test_readonly_stamp_and_archive_do_not_abort(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            target_dir, install_dir, tse_tpl, _ = self._setup_fixture(root)

            # 1. Pre-registered `profiles/tse/profile.yaml` inside a 0o500 directory without `.optional-profile`:
            # `touch` must not kill `/bin/sh` (`dash`) under `set -e`.
            tse_home = target_dir / "profiles" / "tse"
            tse_home.mkdir(parents=True)
            (tse_home / "profile.yaml").write_text("name: tse\n", encoding="utf-8")
            tse_home.chmod(0o500)
            try:
                proc_stamp = self._run_reconcile(target_dir, install_dir, tse_tpl, "tse")
                self.assertEqual(proc_stamp.returncode, 0, proc_stamp.stderr)
            finally:
                tse_home.chmod(0o755)

            # 2. Enable `tse`, then make `profiles` read-only and disable `tse`: must warn and exit 0 under `set -e`.
            proc_en = self._run_reconcile(target_dir, install_dir, tse_tpl, "tse")
            self.assertEqual(proc_en.returncode, 0, proc_en.stderr)
            profiles_dir = target_dir / "profiles"
            profiles_dir.chmod(0o500)
            try:
                proc_ro = self._run_reconcile(target_dir, install_dir, tse_tpl, "")
                self.assertEqual(proc_ro.returncode, 0, proc_ro.stderr)
                self.assertIn("WARN:", proc_ro.stderr)
            finally:
                profiles_dir.chmod(0o755)


class HelmProfilesTemplatingTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_profiles_disabled_by_default(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertNotIn("kind: AgentProfile", proc.stdout)
        self.assertIn('resources: ["platformagents", "agentprofiles"]', proc.stdout)
        self.assertIn("kubectl delete agentprofile --all", proc.stdout)
        self.assertIn("--timeout=30s", proc.stdout)
        self.assertIn("--timeout=120s", proc.stdout)
        self.assertLess(
            proc.stdout.index("kubectl delete agentprofile --all"),
            proc.stdout.index("kubectl delete platformagent"),
        )

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_tse_profile_enabled_and_mirrored(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "profiles.tse.enabled=true",
            "--set",
            "global.imageRegistry=us-docker.pkg.dev/my-proj/my-mirror",
            "--set",
            "platformAgent.deployment.image.tag=v2026.10.20",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("kind: AgentProfile", proc.stdout)
        self.assertIn('name: "tse"', proc.stdout)
        self.assertIn(
            'image: "us-docker.pkg.dev/my-proj/my-mirror/platform-agent:v2026.10.20"',
            proc.stdout,
        )
        self.assertIn(
            'image: "us-docker.pkg.dev/my-proj/my-mirror/a2a-worker:v2026.10.20"',
            proc.stdout,
        )
        self.assertIn('resources: ["platformagents", "agentprofiles"]', proc.stdout)
        self.assertIn("kubectl delete agentprofile --all", proc.stdout)
        self.assertIn("--timeout=30s", proc.stdout)
        self.assertIn("--timeout=120s", proc.stdout)
        self.assertLess(
            proc.stdout.index("kubectl delete agentprofile --all"),
            proc.stdout.index("kubectl delete platformagent"),
        )

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_tse_profile_not_rendered_when_platform_agent_disabled(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.enabled=false",
            "--set",
            "profiles.tse.enabled=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertNotIn("kind: AgentProfile", proc.stdout)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_tse_profile_preserves_zero_max_turns_and_ttl(self):
        cmd = [
            "helm",
            "template",
            "test-release",
            str(_CHART),
            "--set",
            "platformAgent.harness.clusterName=ci-cluster",
            "--set",
            "platformAgent.harness.location=us-central1",
            "--set",
            "platformAgent.harness.projectId=ci-project",
            "--set",
            "profiles.tse.enabled=true",
            "--set",
            "profiles.tse.harness.image.tag=2026",
            "--set",
            "profiles.tse.harness.maxTurns=0",
            "--set",
            "profiles.tse.lifecycle.ttlSecondsAfterFinished=0",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("maxTurns: 0", proc.stdout)
        self.assertIn("ttlSecondsAfterFinished: 0", proc.stdout)
        self.assertIn("a2a-worker:2026", proc.stdout)
        self.assertNotIn("%!s(", proc.stdout)


if __name__ == "__main__":
    unittest.main()
