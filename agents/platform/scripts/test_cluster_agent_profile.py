"""Unit tests for cluster_agent_profile.profile_name (the kanban assignee resolver).

Run: python3 -m unittest agents.platform.scripts.test_cluster_agent_profile

profile_name is a pure, deterministic function; the module imports without pyyaml
(that import is lazy, only on the scaffold path).
"""

import io
import os
import re
import shutil
import subprocess
import yaml
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# profile_overlay and profile_plugins ship beside these scripts in the image
# (/opt/defaults/scripts); in the repo they live in deploy/shared.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy" / "shared"))

import cluster_agent_profile as cap  # noqa: E402

MAX = cap.MAX_NAME_LEN  # 63


class ProfileNameTest(unittest.TestCase):
    def test_basic_shape(self):
        self.assertEqual(
            cap.profile_name("agentic-harness-demo", "kage-management", "us-central1"),
            "cluster-agentic-harness-demo-kage-management-us-central1",
        )

    def test_deterministic(self):
        a = cap.profile_name("p", "c", "us-central1")
        b = cap.profile_name("p", "c", "us-central1")
        self.assertEqual(a, b)

    def test_valid_profile_id_chars(self):
        # Only lowercase alnum + dashes; matches Hermes _PROFILE_ID_RE expectations.
        name = cap.profile_name("Proj_X", "My.Cluster", "US-Central1")
        self.assertRegex(name, r"^[a-z0-9][a-z0-9-]*$")
        self.assertLessEqual(len(name), MAX)

    def test_collapses_and_lowercases(self):
        # Uppercase + non-alnum runs collapse to single dashes.
        name = cap.profile_name("A__B", "c//d", "e")
        self.assertEqual(name, "cluster-a-b-c-d-e")

    def test_long_name_is_hashed_and_bounded(self):
        long_cluster = "x" * 120
        name = cap.profile_name("proj", long_cluster, "us-central1")
        self.assertLessEqual(len(name), MAX)
        # hashed form ends with -<8 hex>
        self.assertRegex(name, r"-[0-9a-f]{8}$")

    def test_long_name_stable_hash(self):
        long_cluster = "y" * 120
        self.assertEqual(
            cap.profile_name("proj", long_cluster, "loc"),
            cap.profile_name("proj", long_cluster, "loc"),
        )


class PinKubeconfigEnvTest(unittest.TestCase):
    def test_writes_kubeconfig_line(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        cap._pin_kubeconfig_env(home, kubeconfig)
        self.assertEqual((home / ".env").read_text(), f"KUBECONFIG={kubeconfig}\n")

    def test_idempotent_and_preserves_other_lines(self):
        home = Path(tempfile.mkdtemp())
        kubeconfig = home / "kubeconfig.yaml"
        (home / ".env").write_text("FOO=bar\nKUBECONFIG=/stale/path\n")
        cap._pin_kubeconfig_env(home, kubeconfig)
        cap._pin_kubeconfig_env(home, kubeconfig)  # second run must not duplicate
        text = (home / ".env").read_text()
        self.assertEqual(text.count("KUBECONFIG="), 1)
        self.assertIn("FOO=bar\n", text)
        self.assertIn(f"KUBECONFIG={kubeconfig}\n", text)


# --- The runtime scaffold path --------------------------------------------------
#
# A cluster profile is created when its cluster is onboarded, which is not a pod start:
# nothing rolls the agent, so this path — not docker-entrypoint.sh — is the only thing
# that can give the new profile the operator's tuning and the plugins targeted at it.


class CreateProfileTest(unittest.TestCase):
    PROJECT, CLUSTER, LOCATION = "proj", "clu", "us-east1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
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
        self.runs = []
        self._patch(subprocess, "run", self._fake_run)
        # Whether that command needs --dns-endpoint. Patched explicitly rather
        # than left to fall out of the fake above: gke_endpoint reads gcloud's
        # help text to decide the flag exists at all, the fake answers every
        # command with empty stdout, and the resulting "no flag" would be an
        # accident of the mock rather than a decision the test made. The
        # predicate itself is covered in test_gke_endpoint.py.
        self.dns_args = []
        self._patch(cap, "dns_endpoint_args", lambda *a, **k: self.dns_args)

    def _patch(self, obj, attr, value):
        original = getattr(obj, attr)
        setattr(obj, attr, value)
        self.addCleanup(setattr, obj, attr, original)

    def _fake_ensure_profile(self, name, description, hermes_home):
        home = Path(hermes_home) / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "profile.yaml").write_text(f"name: {name}\n")
        return home

    def _fake_run(self, cmd, **kwargs):
        self.runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def get_credentials_argv(self):
        for cmd in self.runs:
            if cmd[:4] == ["gcloud", "container", "clusters", "get-credentials"]:
                return cmd
        raise AssertionError(f"no get-credentials call in {self.runs}")

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

    def test_fetches_credentials_over_the_ip_endpoint_by_default(self):
        self.create()
        self.assertEqual(
            self.get_credentials_argv(),
            [
                "gcloud", "container", "clusters", "get-credentials", self.CLUSTER,
                f"--location={self.LOCATION}",
                f"--project={self.PROJECT}",
            ],
        )

    def test_fetches_credentials_over_the_dns_endpoint_when_detected(self):
        # An onboarded cluster whose control plane is only reachable by DNS. The
        # profile's pinned kubeconfig is what the Cluster Agent runs against for
        # its whole life, so the flag has to be present when it is scaffolded.
        self.dns_args = ["--dns-endpoint"]

        self.create()

        self.assertEqual(self.get_credentials_argv()[-1], "--dns-endpoint")

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

    def test_every_user_md_field_is_readable_by_preflight(self):
        """USER.md's shape is a contract with cluster_preflight.sh's user_md_field().

        That reader matches only `- <key>: <value>` bullets. The kubeconfig line
        shipped for a long time without the leading `- `, so it parsed as
        nothing — and it is precisely the line a human edits when trying to
        repair a bad pin. Assert all four fields are bullets, so the writer
        cannot drift back out of the shape the reader can see.
        """
        self.create()
        user_md = (self.profile / "USER.md").read_text().lower()

        expected = {
            "project": self.PROJECT,
            "cluster": self.CLUSTER,
            "location": self.LOCATION,
            "kubeconfig": str(self.profile / "kubeconfig.yaml").lower(),
        }
        for key, value in expected.items():
            # The Python equivalent of the script's
            # `sed -n "s/^[[:space:]]*-[[:space:]]*<key>:[[:space:]]*//p"`.
            found = re.findall(rf"^[ \t]*-[ \t]*{key}:[ \t]*(.*)$", user_md, re.MULTILINE)
            self.assertTrue(found, f"`{key}` is not a `- {key}:` bullet, so preflight cannot read it")
            self.assertEqual(found[0].strip(), value)

    def test_preflight_still_parses_user_md_with_a_dash_anchor(self):
        """The other half of the contract above: if the reader stops requiring
        the `- ` anchor, this test is the note that USER.md's shape was written
        to satisfy it and can be relaxed too."""
        script = (Path(__file__).parent / "cluster_preflight.sh").read_text()
        self.assertIn(
            r"s/^[[:space:]]*-[[:space:]]*$1:[[:space:]]*//p",
            script,
            "user_md_field() changed; re-check the USER.md format written by create_profile",
        )


    # --- telemetry ---
    #
    # The profile copies the image's plugin config, so it also copies its endpoint.
    # Nothing rolls the pod when a cluster is onboarded, so the entrypoint's startup sweep
    # never sees this profile: without the scaffold-time pin, a cluster agent would export
    # to the GKE managed collector no matter what the operator resolved.

    BAKED = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces"
    CUSTOM = "http://otel-collector.otel-collector.svc.cluster.local:4318"

    def bake_shared_plugin(self):
        """The hermes_otel config as the image ships it, ready for the overlay to copy."""
        shared = self.tmp / "shared-plugins" / "hermes_otel"
        shared.mkdir(parents=True, exist_ok=True)
        (shared / "config.yaml").write_text(
            yaml.safe_dump({"backends": [{"name": "gke-managed-otel", "type": "otlp", "endpoint": self.BAKED}]})
        )

    def plugin_config(self):
        return yaml.safe_load((self.profile / "plugins" / "hermes_otel" / "config.yaml").read_text())

    def test_pins_the_resolved_endpoint(self):
        self.bake_shared_plugin()
        with mock.patch.dict(
            os.environ,
            {"OTEL_EXPORTER_OTLP_ENDPOINT": self.CUSTOM, "OTEL_SERVICE_NAME": "agent-gateway"},
        ):
            self.create()

        cfg = self.plugin_config()
        self.assertEqual(cfg["backends"][0]["endpoint"], self.CUSTOM + "/v1/traces")
        self.assertEqual(cfg["resource_attributes"]["service.name"], "agent-gateway")

    def test_unset_endpoint_keeps_the_image_default(self):
        self.bake_shared_plugin()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
            os.environ.pop("OTEL_SERVICE_NAME", None)
            self.create()

        self.assertEqual(self.plugin_config()["backends"][0]["endpoint"], self.BAKED)


if __name__ == "__main__":
    unittest.main()
