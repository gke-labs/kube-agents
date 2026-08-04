"""Tests for the scaffold gate in agents/platform/scripts/profile_scaffold.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

`hermes profile create` is the only thing that builds a profile, and it used to be skipped
whenever the profile's directory already existed. A directory is not evidence: the kubelet
creates a mounted volume's mount point inside the data PVC before anything here runs, so a
profile could have a directory and nothing else — no registration, no skills — with every
later start skipping the scaffold again. These cover the gate and the recovery.

`hermes` itself is stubbed: the real binary lives in the agent image, and what matters here
is which conditions call it, not what it does.
"""

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
import io

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "agents" / "platform" / "scripts" / "profile_scaffold.py"
)
_spec = importlib.util.spec_from_file_location("profile_scaffold", _MODULE_PATH)
ps = importlib.util.module_from_spec(_spec)
sys.modules["profile_scaffold"] = ps
_spec.loader.exec_module(ps)


class EnsureProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "profiles" / "platform"
        self.calls = []
        self.real_run = ps.subprocess.run
        self.addCleanup(setattr, ps.subprocess, "run", self.real_run)
        ps.subprocess.run = self.fake_run
        self.fail_create = False

    def fake_run(self, cmd, **kwargs):
        """Stand in for `hermes profile create`, which writes profiles/<name>/profile.yaml."""
        self.calls.append(cmd)
        if self.fail_create:
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="profile already exists")
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "profile.yaml").write_text("name: platform\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ensure(self):
        with redirect_stderr(io.StringIO()) as err:
            home = ps.ensure_profile("platform", "desc", self.tmp)
        self.stderr = err.getvalue()
        return home

    def test_creates_a_profile_that_does_not_exist(self):
        self.assertEqual(self.ensure(), self.home)
        self.assertEqual(len(self.calls), 1)

    def test_skips_a_profile_already_registered(self):
        self.home.mkdir(parents=True)
        (self.home / "profile.yaml").write_text("name: platform\n")
        self.ensure()
        self.assertEqual(self.calls, [], "a registered profile must not be re-created")

    def test_a_bare_mount_point_does_not_count_as_a_scaffold(self):
        """The regression: profiles/platform/plugins/<plugin>/ made by the kubelet.

        The old gate read that directory as a finished profile and skipped the scaffold —
        so the profile had no skills, was never registered, and never self-healed.
        """
        (self.home / "plugins" / "stockout").mkdir(parents=True)

        self.ensure()

        self.assertEqual(len(self.calls), 1, "the scaffold must still run")
        self.assertTrue((self.home / "profile.yaml").is_file())
        self.assertFalse((self.home / "plugins" / "stockout").exists(), "the skeleton is cleared")

    def test_a_home_holding_files_is_never_deleted(self):
        """Only provably empty directories are cleared; a file means somebody's state."""
        self.home.mkdir(parents=True)
        (self.home / "USER.md").write_text("cluster identity\n")
        self.fail_create = True

        self.ensure()

        self.assertTrue((self.home / "USER.md").is_file())
        self.assertIn("continuing", self.stderr, "the refusal is logged, not fatal")

    def test_a_failed_create_on_a_fresh_home_is_fatal(self):
        self.fail_create = True
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            ps.ensure_profile("platform", "desc", self.tmp)

    def test_is_scaffolded_reads_the_marker_not_the_directory(self):
        self.assertFalse(ps.is_scaffolded(self.home))
        self.home.mkdir(parents=True)
        self.assertFalse(ps.is_scaffolded(self.home))
        (self.home / "config.yaml").write_text("plugins: {}\n")
        self.assertFalse(ps.is_scaffolded(self.home), "a config alone proves nothing")
        (self.home / "profile.yaml").write_text("name: platform\n")
        self.assertTrue(ps.is_scaffolded(self.home))


if __name__ == "__main__":
    unittest.main()
