"""Tests for the runtime scaffold path in agents/platform/scripts/cluster_agent_profile.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

A cluster profile is created when its cluster is onboarded, which is not a pod start:
nothing rolls the agent, so this path — not docker-entrypoint.sh — is the only thing that
can give the new profile the operator's tuning and the plugins targeted at it. It used to
give it neither, and the profile silently ran on Hermes defaults until an unrelated
restart. These tests pin both halves.

`hermes` and `gcloud` are stubbed: they live in the agent image, and what matters here is
what the function does around them.
"""

import importlib.util
import io
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "agents" / "platform" / "scripts"
_SHARED = _ROOT / "deploy" / "shared"
# Mirrors the image layout: both directories land in /opt/defaults/scripts, so
# cluster_agent_profile imports profile_scaffold, profile_overlay and profile_plugins as
# siblings.
for path in (_SCRIPTS, _SHARED):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_spec = importlib.util.spec_from_file_location("cluster_agent_profile", _SCRIPTS / "cluster_agent_profile.py")
cap = importlib.util.module_from_spec(_spec)
sys.modules["cluster_agent_profile"] = cap
_spec.loader.exec_module(cap)


class CreateProfileTest(unittest.TestCase):
    PROJECT, CLUSTER, LOCATION = "proj", "clu", "us-east1"

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.home_root = self.tmp / "data"
        self.template = self.tmp / "cluster-template"
        self.overlay_dir = self.tmp / "agent-config"
        self.mounts = self.tmp / "agent-plugins"
        self.template.mkdir()
        self.overlay_dir.mkdir()
        (self.template / "config.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["hermes_otel"]}, "toolsets": ["kanban"]})
        )

        self.name = cap.profile_name(self.PROJECT, self.CLUSTER, self.LOCATION)
        self.profile = self.home_root / "profiles" / self.name

        self._patch(cap, "HERMES_HOME", self.home_root)
        self._patch(cap, "PROFILES_BASE", self.home_root / "profiles")
        self._patch(cap, "TEMPLATE_DIR", self.template)
        self._patch(cap, "SHARED_PLUGINS_DIR", self.tmp / "shared-plugins")
        self._patch(cap, "OVERLAY_DIR", self.overlay_dir)
        self._patch(cap, "PLUGIN_MOUNT_ROOT", self.mounts)
        # `hermes profile create` — the real one registers the profile and makes its home.
        self._patch(cap, "ensure_profile", self._fake_ensure_profile)
        # `gcloud container clusters get-credentials`.
        self._patch(subprocess, "run", self._fake_run)

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def _fake_ensure_profile(self, name, description, hermes_home):
        home = pathlib.Path(hermes_home) / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "profile.yaml").write_text(f"name: {name}\n")
        return home

    def _fake_run(self, cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def mount(self, plugin):
        d = self.mounts / self.name / plugin
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")
        return d

    def create(self):
        with redirect_stderr(io.StringIO()) as err:
            name = cap.create_profile(self.PROJECT, self.CLUSTER, self.LOCATION)
        self.stderr = err.getvalue()
        return name

    def config(self):
        return yaml.safe_load((self.profile / "config.yaml").read_text()) or {}

    def test_applies_the_cluster_class_overlay_at_scaffold_time(self):
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"api_max_retries": 8, "max_turns": 150}})
        )

        self.assertEqual(self.create(), self.name)

        cfg = self.config()
        self.assertEqual(cfg["agent"], {"api_max_retries": 8, "max_turns": 150})
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel"], "the template's config must survive")
        self.assertEqual(
            cfg["cluster_identity"],
            {"project": self.PROJECT, "cluster": self.CLUSTER, "location": self.LOCATION},
            "the identity stamp the reconciler matches on must survive the merge",
        )

    def test_links_and_enables_a_plugin_targeting_this_cluster(self):
        self.mount("clusterone")
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"max_turns": 150}})
        )
        (self.overlay_dir / f"profile-{self.name}.overlay.yaml").write_text(
            yaml.safe_dump({"plugins": {"enabled": ["clusterone"]}})
        )

        self.create()

        link = self.profile / "plugins" / "clusterone"
        self.assertTrue(link.is_symlink(), "the plugin mounted for this profile must be linked in")
        self.assertTrue((link / "__init__.py").is_file())
        cfg = self.config()
        self.assertEqual(cfg["plugins"]["enabled"], ["hermes_otel", "clusterone"], "and enabled")
        self.assertEqual(cfg["agent"]["max_turns"], 150, "alongside the class-wide tuning")

    def test_no_operator_overlays_is_not_a_failure(self):
        """A deployment without the operator has no /opt/agent-config and no mounts."""
        self._patch(cap, "OVERLAY_DIR", self.tmp / "does-not-exist")
        self._patch(cap, "PLUGIN_MOUNT_ROOT", self.tmp / "also-missing")

        self.assertEqual(self.create(), self.name)

        cfg = self.config()
        self.assertNotIn("agent", cfg, "nothing to apply means nothing applied")
        self.assertIn("cluster_identity", cfg)

    def test_rescaffolding_does_not_double_apply(self):
        (self.overlay_dir / "profileclass-cluster.overlay.yaml").write_text(
            yaml.safe_dump({"agent": {"max_turns": 150}, "plugins": {"enabled": ["extra"]}})
        )
        self.create()
        first = self.config()
        self.create()
        self.assertEqual(self.config(), first)


if __name__ == "__main__":
    unittest.main()
