#!/usr/bin/env python3
"""Tests for the cluster profile boot stamp.

Run:  python3 agents/platform/scripts/test_cluster_boot_stamp.py
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import cluster_boot_stamp as stamp  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
_ENTRYPOINT = _REPO / "deploy" / "shared" / "docker-entrypoint.sh"


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.template = self.root / "cluster-template"
        (self.template / "skills" / "k8s-triage").mkdir(parents=True)
        (self.template / "SOUL.md").write_text("persona\n", encoding="utf-8")
        (self.template / "config.yaml").write_text("a: 1\n", encoding="utf-8")
        (self.template / "skills" / "k8s-triage" / "SKILL.md").write_text("skill\n", encoding="utf-8")
        self.plugins = self.root / "plugins"
        (self.plugins / "tool_call_audit").mkdir(parents=True)
        (self.plugins / "tool_call_audit" / "audit.py").write_text("print(1)\n", encoding="utf-8")
        self.script = self.root / "cluster_agent_profile.py"
        self.script.write_text("# scaffold\n", encoding="utf-8")
        self.inputs = [str(self.template), str(self.plugins / "tool_call_audit"),
                       str(self.plugins / "common"), str(self.script)]
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()

    def digest(self):
        return stamp.digest(self.inputs)

    def make_profiles(self, count, digest=None):
        for i in range(count):
            home = self.profiles / f"cluster-p-c{i:04d}-us-central1"
            home.mkdir()
            (home / "config.yaml").write_text("cluster_identity: {}\n", encoding="utf-8")
            if digest is not None:
                (home / stamp.STAMP_FILE).write_text(digest + "\n", encoding="utf-8")


class DigestTest(_Fixture):
    """The digest follows every input the cluster loop reads, and nothing else."""

    def test_the_digest_is_stable_across_runs(self):
        self.assertEqual(self.digest(), self.digest())

    def test_a_changed_template_file_changes_it(self):
        before = self.digest()
        (self.template / "SOUL.md").write_text("persona v2\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_a_new_skill_changes_it(self):
        before = self.digest()
        (self.template / "skills" / "new-skill").mkdir()
        (self.template / "skills" / "new-skill" / "SKILL.md").write_text("x\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_a_removed_skill_changes_it(self):
        before = self.digest()
        shutil.rmtree(self.template / "skills" / "k8s-triage")
        self.assertNotEqual(before, self.digest())

    def test_a_plugin_file_changes_it(self):
        before = self.digest()
        (self.plugins / "tool_call_audit" / "audit.py").write_text("print(2)\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_a_scaffold_script_changes_it(self):
        before = self.digest()
        self.script.write_text("# scaffold v2\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_an_absent_input_is_hashed_not_refused(self):
        # `common` does not exist in this fixture. A build that gains it moves the digest.
        before = self.digest()
        (self.plugins / "common").mkdir()
        (self.plugins / "common" / "audit_sink.py").write_text("x\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_the_executable_bit_counts(self):
        # Skills carry scripts; `cp -a` preserves the mode, so the loop must run when it changes.
        script = self.template / "skills" / "k8s-triage" / "run.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        before = self.digest()
        script.chmod(script.stat().st_mode | 0o111)
        self.assertNotEqual(before, self.digest())

    def test_a_symlink_is_hashed_by_its_target_and_not_followed(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "big.bin").write_bytes(b"x" * 10)
        (self.template / "link").symlink_to(outside)
        before = self.digest()
        (outside / "big.bin").write_bytes(b"y" * 10)
        self.assertEqual(before, self.digest(), "a symlink's target contents were followed")
        (self.template / "link").unlink()
        (self.template / "link").symlink_to(self.root)
        self.assertNotEqual(before, self.digest(), "a symlink's target path was not hashed")

    def test_the_mtime_does_not_count(self):
        # Image builds with deterministic timestamps and `cp` without -p both move
        # mtimes without moving bytes; neither should re-run the loop.
        before = self.digest()
        past = time.time() - 86400
        os.utime(self.template / "SOUL.md", (past, past))
        self.assertEqual(before, self.digest())

    def test_the_order_of_inputs_counts(self):
        self.assertNotEqual(stamp.digest(self.inputs), stamp.digest(list(reversed(self.inputs))))

    def test_the_root_path_does_not_count(self):
        # Only the contents: the same tree under another root is the same digest, so a
        # test or a relocated template cannot invalidate every stamp on the volume.
        copy = self.root / "copy"
        shutil.copytree(self.template, copy / "cluster-template", symlinks=True)
        shutil.copytree(self.plugins, copy / "plugins", symlinks=True)
        shutil.copy(self.script, copy / "cluster_agent_profile.py")
        moved = [p.replace(str(self.root), str(copy), 1) for p in self.inputs]
        self.assertEqual(stamp.digest(self.inputs), stamp.digest(moved))

    def test_the_digest_carries_its_version(self):
        self.assertTrue(self.digest().startswith(stamp.DIGEST_VERSION + "-"))


class StaleTest(_Fixture):
    """Which homes the loop runs for."""

    def test_an_unstamped_home_is_stale(self):
        self.make_profiles(2)
        self.assertEqual(stamp.stale_profiles(self.profiles, self.digest()),
                         ["cluster-p-c0000-us-central1", "cluster-p-c0001-us-central1"])

    def test_a_home_stamped_with_the_current_digest_is_current(self):
        self.make_profiles(2, self.digest())
        self.assertEqual(stamp.stale_profiles(self.profiles, self.digest()), [])

    def test_a_home_stamped_by_an_older_image_is_stale(self):
        self.make_profiles(1, self.digest())
        (self.template / "SOUL.md").write_text("persona v2\n", encoding="utf-8")
        self.assertEqual(stamp.stale_profiles(self.profiles, self.digest()), ["cluster-p-c0000-us-central1"])

    def test_a_garbled_or_empty_stamp_is_stale(self):
        self.make_profiles(2, self.digest())
        (self.profiles / "cluster-p-c0000-us-central1" / stamp.STAMP_FILE).write_bytes(b"\xff\xfe")
        (self.profiles / "cluster-p-c0001-us-central1" / stamp.STAMP_FILE).write_text("", encoding="utf-8")
        self.assertEqual(stamp.stale_profiles(self.profiles, self.digest()),
                         ["cluster-p-c0000-us-central1", "cluster-p-c0001-us-central1"])

    def test_only_cluster_homes_are_listed(self):
        self.make_profiles(1)
        (self.profiles / "platform").mkdir()
        (self.profiles / "cluster-file").write_text("not a home\n", encoding="utf-8")
        self.assertEqual(stamp.stale_profiles(self.profiles, self.digest()), ["cluster-p-c0000-us-central1"])

    def test_a_missing_profiles_dir_lists_nothing(self):
        self.assertEqual(stamp.stale_profiles(self.root / "absent", self.digest()), [])

    def test_five_hundred_stamped_homes_are_checked_in_under_a_second(self):
        # The issue's acceptance: a warm start on a large fleet must not pay per profile.
        self.make_profiles(500, self.digest())
        started = time.monotonic()
        self.assertEqual(stamp.stale_profiles(self.profiles, self.digest()), [])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_template_change_makes_all_five_hundred_stale(self):
        self.make_profiles(500, self.digest())
        (self.template / "skills" / "k8s-triage" / "SKILL.md").write_text("skill v2\n", encoding="utf-8")
        self.assertEqual(len(stamp.stale_profiles(self.profiles, self.digest())), 500)

    def test_a_name_the_shell_cannot_carry_refuses_the_whole_listing(self):
        self.make_profiles(1)
        (self.profiles / "cluster-odd name").mkdir()
        with self.assertRaises(ValueError):
            stamp.stale_profiles(self.profiles, self.digest())


class CliTest(_Fixture):
    """The two commands the entrypoint calls."""

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = stamp.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_digest_prints_one_line(self):
        code, out, _ = self.run_main("digest", *sum((["--input", p] for p in self.inputs), []))
        self.assertEqual(code, 0)
        self.assertEqual(out, self.digest() + "\n")

    def test_stale_prints_one_name_per_line(self):
        self.make_profiles(2)
        code, out, _ = self.run_main("stale", "--profiles-dir", str(self.profiles), "--digest", self.digest())
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), ["cluster-p-c0000-us-central1", "cluster-p-c0001-us-central1"])

    def test_stale_prints_nothing_when_every_home_is_current(self):
        self.make_profiles(3, self.digest())
        code, out, _ = self.run_main("stale", "--profiles-dir", str(self.profiles), "--digest", self.digest())
        self.assertEqual((code, out), (0, ""))

    def test_an_unlistable_name_exits_non_zero(self):
        (self.profiles / "cluster-odd name").mkdir()
        code, out, err = self.run_main("stale", "--profiles-dir", str(self.profiles), "--digest", self.digest())
        self.assertEqual(code, stamp.EXIT_UNLISTABLE)
        self.assertEqual(out, "")
        self.assertIn("cannot be listed", err)

    def test_the_entrypoint_names_the_same_stamp_file(self):
        source = _ENTRYPOINT.read_text(encoding="utf-8")
        self.assertIn(f'readonly CLUSTER_BOOT_STAMP_FILE="{stamp.STAMP_FILE}"', source)


if __name__ == "__main__":
    unittest.main()
