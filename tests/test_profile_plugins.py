"""Tests for deploy/shared/profile_plugins.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

This is the half of plugin delivery the operator cannot do: it mounts a targeted plugin's
image outside the data PVC, and the link made here is what puts it where Hermes looks. The
failure mode is silent — the plugin is simply never imported, and the symptom arrives much
later as a skill the agent cannot find — so the cases below cover the ones that are easy to
get wrong: a profile that does not exist yet, a plugin that has been removed, and a name
collision with image-shipped content.
"""

import importlib.util
import io
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

_SHARED = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared"
# profile_plugins imports profile_overlay for the profile-name rule, which has one home.
sys.path.insert(0, str(_SHARED))
_spec = importlib.util.spec_from_file_location("profile_plugins", _SHARED / "profile_plugins.py")
pp = importlib.util.module_from_spec(_spec)
sys.modules["profile_plugins"] = pp
_spec.loader.exec_module(pp)


class LinkPluginsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "data"
        self.mounts = self.tmp / "agent-plugins"
        self.profile = self.scaffolded("platform")

    def scaffolded(self, name):
        """A profile as `hermes profile create` plus the template overlay leaves it."""
        home = self.home / "profiles" / name
        home.mkdir(parents=True)
        (home / "profile.yaml").write_text(f"name: {name}\n")
        (home / "config.yaml").write_text("plugins: {}\n")
        return home

    def mount(self, profile, plugin, filename="__init__.py"):
        d = self.mounts / profile / plugin
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_text("")
        return d

    def linked(self, profile="platform"):
        plugins = self.home / "profiles" / profile / "plugins"
        return sorted(p.name for p in plugins.iterdir()) if plugins.is_dir() else []

    def test_links_the_mounted_plugin_into_the_profile(self):
        src = self.mount("platform", "stockout")
        self.assertEqual(pp.link_plugins(self.profile, self.mounts), ["stockout"])

        link = self.profile / "plugins" / "stockout"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), src.resolve())
        # Reachable by the name Hermes imports, through the link.
        self.assertTrue((link / "__init__.py").is_file())

    def test_is_idempotent(self):
        self.mount("platform", "stockout")
        pp.link_plugins(self.profile, self.mounts)
        pp.link_plugins(self.profile, self.mounts)
        self.assertEqual(self.linked(), ["stockout"])

    def test_removed_plugin_leaves_no_dangling_link(self):
        """The AgentPlugin was deleted, so the pod spec no longer carries its volume."""
        self.mount("platform", "stockout")
        pp.link_plugins(self.profile, self.mounts)
        shutil.rmtree(self.mounts / "platform" / "stockout")

        pp.link_plugins(self.profile, self.mounts)
        self.assertEqual(self.linked(), [])

    def test_a_real_directory_is_never_replaced(self):
        """The shared plugins overlay copies real directories in; those are image content."""
        shipped = self.profile / "plugins" / "hermes_otel"
        shipped.mkdir(parents=True)
        (shipped / "config.yaml").write_text("backends: []\n")
        self.mount("platform", "hermes_otel")

        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(pp.link_plugins(self.profile, self.mounts), [])
        self.assertFalse(shipped.is_symlink())
        self.assertTrue((shipped / "config.yaml").is_file())
        self.assertIn("not a link", err.getvalue())

    def test_replaces_the_old_layouts_empty_mount_point(self):
        """Upgrade path: the PVC still holds the directory the old mount left behind."""
        leftover = self.profile / "plugins" / "stockout"
        leftover.mkdir(parents=True)
        src = self.mount("platform", "stockout")

        self.assertEqual(pp.link_plugins(self.profile, self.mounts), ["stockout"])
        self.assertTrue(leftover.is_symlink())
        self.assertEqual(leftover.resolve(), src.resolve())

    def test_no_mounts_for_this_profile_is_a_no_op(self):
        self.assertEqual(pp.link_plugins(self.profile, self.mounts), [])
        self.assertFalse((self.profile / "plugins").exists())

    def test_link_all_sweeps_every_profile(self):
        self.scaffolded("cluster-proj-prod-us-east1")
        self.mount("platform", "stockout")
        self.mount("cluster-proj-prod-us-east1", "clusterone")

        results = pp.link_all(self.home, self.mounts)
        self.assertEqual(results["platform"], ["stockout"])
        self.assertEqual(results["cluster-proj-prod-us-east1"], ["clusterone"])

    def test_link_all_reports_a_mount_whose_profile_is_missing(self):
        """A cluster profile is scaffolded when its cluster is onboarded, not at startup."""
        self.mount("cluster-proj-prod-us-east1", "clusterone")
        err = io.StringIO()
        with redirect_stderr(err):
            results = pp.link_all(self.home, self.mounts)
        self.assertEqual(results, {})
        self.assertIn("cluster-proj-prod-us-east1", err.getvalue())

    def test_link_all_prunes_a_profile_whose_mount_root_entry_is_gone(self):
        self.mount("platform", "stockout")
        pp.link_plugins(self.profile, self.mounts)
        shutil.rmtree(self.mounts / "platform")

        pp.link_all(self.home, self.mounts)
        self.assertEqual(self.linked(), [])

    def test_link_all_leaves_a_bare_mount_skeleton_alone(self):
        """A directory the old layout left on the PVC must stay clearable.

        Linking into it would add content that profile_scaffold's skeleton cleanup then
        refuses to delete, and the profile would never be scaffolded.
        """
        skeleton = self.home / "profiles" / "cluster-proj-prod-us-east1"
        (skeleton / "plugins" / "clusterone").mkdir(parents=True)
        self.mount("cluster-proj-prod-us-east1", "clusterone")

        err = io.StringIO()
        with redirect_stderr(err):
            results = pp.link_all(self.home, self.mounts)

        self.assertNotIn("cluster-proj-prod-us-east1", results)
        self.assertEqual(list((skeleton / "plugins").iterdir()), [skeleton / "plugins" / "clusterone"])
        self.assertFalse((skeleton / "plugins" / "clusterone").is_symlink())
        self.assertIn("not a scaffolded profile", err.getvalue())

    def test_link_all_isolates_a_failing_profile(self):
        """One unwritable profile must not cost every other profile its plugins."""
        broken = self.scaffolded("cluster-proj-broken-us-east1")
        (broken / "plugins").mkdir()
        (broken / "plugins").chmod(0o500)  # readable, not writable
        self.addCleanup((broken / "plugins").chmod, 0o700)
        self.mount("cluster-proj-broken-us-east1", "clusterone")
        self.mount("platform", "stockout")

        err = io.StringIO()
        with redirect_stderr(err):
            results = pp.link_all(self.home, self.mounts)

        self.assertEqual(results.get("platform"), ["stockout"], "the healthy profile must still be linked")
        self.assertNotIn("cluster-proj-broken-us-east1", results)
        self.assertIn("linking failed", err.getvalue())

    def test_refuses_a_traversing_profile_name(self):
        with self.assertRaises(ValueError):
            pp.link_plugins(self.home / "profiles" / "..", self.mounts)


if __name__ == "__main__":
    unittest.main()
