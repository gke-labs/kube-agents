"""Tests for deploy/shared/otel_config.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

This module is the only thing that makes a configured OTLP endpoint real: the hermes_otel
plugin reads its backend URL from this file, not from OTEL_EXPORTER_OTLP_ENDPOINT. The
cases below cover what is easy to get wrong and invisible when it goes wrong — an endpoint
that already carries the traces path (this reruns on every start against a file that lives
on the PVC), an unset endpoint that must leave the baked default strictly alone, and a
profile copy that would otherwise keep exporting to the old collector forever.
"""

import importlib.util
import io
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

import yaml

_SHARED = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared"
sys.path.insert(0, str(_SHARED))
_spec = importlib.util.spec_from_file_location("otel_config", _SHARED / "otel_config.py")
oc = importlib.util.module_from_spec(_spec)
sys.modules["otel_config"] = oc
_spec.loader.exec_module(oc)

BAKED = "http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces"
CUSTOM = "http://otel-collector.otel-collector.svc.cluster.local:4318"


class TracesURLTest(unittest.TestCase):
    def test_appends_the_signal_path(self):
        self.assertEqual(oc.traces_url("http://col:4318"), "http://col:4318/v1/traces")

    def test_tolerates_a_trailing_slash(self):
        self.assertEqual(oc.traces_url("http://col:4318/"), "http://col:4318/v1/traces")

    def test_is_idempotent(self):
        once = oc.traces_url(CUSTOM)
        self.assertEqual(oc.traces_url(once), once)

    def test_keeps_a_non_root_path_as_a_prefix(self):
        self.assertEqual(oc.traces_url("https://vendor.example/otlp"), "https://vendor.example/otlp/v1/traces")

    def test_assumes_http_for_a_bare_host_port(self):
        self.assertEqual(oc.traces_url("col.ns.svc:4318"), "http://col.ns.svc:4318/v1/traces")

    def test_preserves_https(self):
        self.assertEqual(oc.traces_url("https://col:4318"), "https://col:4318/v1/traces")

    def test_empty_stays_empty(self):
        self.assertEqual(oc.traces_url("  "), "")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = self.write_baked(self.tmp / "plugins" / "hermes_otel" / "config.yaml")

    def write_baked(self, path, **extra):
        """The plugin config exactly as the image bakes it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        backend = {"name": "gke-managed-otel", "type": "otlp", "endpoint": BAKED}
        backend.update(extra)
        path.write_text(yaml.safe_dump({"backends": [backend]}))
        return path

    def load(self, path=None):
        return yaml.safe_load((path or self.config).read_text())

    def test_sets_the_service_name(self):
        self.assertTrue(oc.apply(self.config, service_name="agent-gateway"))
        self.assertEqual(self.load()["resource_attributes"]["service.name"], "agent-gateway")

    def test_clears_the_service_name_when_unset(self):
        oc.apply(self.config, service_name="agent-gateway")
        oc.apply(self.config, service_name=None)
        self.assertNotIn("service.name", self.load().get("resource_attributes", {}))

    def test_unset_endpoint_leaves_the_baked_default_alone(self):
        self.assertTrue(oc.apply(self.config, service_name="agent-gateway"))
        self.assertEqual(self.load()["backends"], [{"name": "gke-managed-otel", "type": "otlp", "endpoint": BAKED}])

    def test_sets_the_endpoint_with_the_traces_path(self):
        self.assertTrue(oc.apply(self.config, endpoint=CUSTOM))
        self.assertEqual(self.load()["backends"][0]["endpoint"], CUSTOM + "/v1/traces")

    def test_preserves_the_rest_of_the_backend(self):
        self.write_baked(self.config, headers={"x-api-key": "abc"})
        oc.apply(self.config, endpoint=CUSTOM)
        backend = self.load()["backends"][0]
        self.assertEqual(backend["name"], "gke-managed-otel")
        self.assertEqual(backend["type"], "otlp")
        self.assertEqual(backend["headers"], {"x-api-key": "abc"})

    def test_disabled_clears_backends_and_sets_enabled_false(self):
        self.assertTrue(oc.apply(self.config, disabled=True))
        cfg = self.load()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])

    def test_re_enabling_from_disabled_state_restores_enabled_true(self):
        oc.apply(self.config, disabled=True)
        self.assertTrue(oc.apply(self.config, endpoint=CUSTOM, disabled=False))
        cfg = self.load()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["backends"][0]["endpoint"], CUSTOM + "/v1/traces")

    def test_re_enabling_with_source_path_restores_enabled_true(self):
        pristine = self.write_baked(self.tmp / "defaults" / "hermes_otel" / "config.yaml")
        # Ensure pristine has no 'enabled' field (matches image default)
        cfg = yaml.safe_load(pristine.read_text())
        cfg.pop("enabled", None)
        pristine.write_text(yaml.safe_dump(cfg))

        # Disable on disk
        oc.apply(self.config, disabled=True)
        self.assertFalse(self.load()["enabled"])

        # Re-enable using pristine source_path
        self.assertTrue(oc.apply(self.config, service_name="agent-gateway", source_path=pristine, disabled=False))
        cfg = self.load()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["backends"][0]["endpoint"], BAKED)

    def test_re_enabling_is_idempotent_across_multiple_restarts(self):
        pristine = self.write_baked(self.tmp / "defaults" / "hermes_otel" / "config.yaml")
        cfg = yaml.safe_load(pristine.read_text())
        cfg.pop("enabled", None)
        pristine.write_text(yaml.safe_dump(cfg))

        # Disable on disk
        oc.apply(self.config, disabled=True)
        self.assertFalse(self.load()["enabled"])

        # Restart 1: Re-enable using pristine source_path
        self.assertTrue(oc.apply(self.config, service_name="agent-gateway", source_path=pristine, disabled=False))
        first_load = self.load()
        self.assertTrue(first_load["enabled"])

        # Restart 2: Apply again with pristine source_path (must preserve enabled: true without rewriting)
        self.assertTrue(oc.apply(self.config, service_name="agent-gateway", source_path=pristine, disabled=False))
        second_load = self.load()
        self.assertEqual(first_load, second_load)
        self.assertTrue(second_load["enabled"])

    def test_pristine_disabled_template_is_not_forced_true(self):
        pristine = self.tmp / "defaults" / "hermes_otel" / "config.yaml"
        pristine.parent.mkdir(parents=True, exist_ok=True)
        pristine.write_text(yaml.safe_dump({"enabled": False, "backends": []}))

        self.assertTrue(oc.apply(self.config, source_path=pristine, disabled=False))
        cfg = self.load()
        self.assertFalse(cfg["enabled"])

    def test_disabled_takes_precedence_over_endpoint(self):
        self.assertTrue(oc.apply(self.config, endpoint=CUSTOM, disabled=True))
        cfg = self.load()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])

    def test_is_idempotent(self):
        oc.apply(self.config, service_name="agent-gateway", endpoint=CUSTOM)
        first = self.config.read_text()
        oc.apply(self.config, service_name="agent-gateway", endpoint=CUSTOM)
        self.assertEqual(self.config.read_text(), first)

    def test_creates_a_backend_when_there_is_none(self):
        self.config.write_text(yaml.safe_dump({"resource_attributes": {}}))
        self.assertTrue(oc.apply(self.config, endpoint=CUSTOM))
        self.assertEqual(self.load()["backends"][0]["endpoint"], CUSTOM + "/v1/traces")

    def test_missing_file_is_not_an_error(self):
        with redirect_stderr(io.StringIO()):
            self.assertFalse(oc.apply(self.tmp / "nope" / "config.yaml", endpoint=CUSTOM))

    def test_bad_yaml_warns_and_leaves_the_file(self):
        self.config.write_text("backends: [ this is not: valid: yaml")
        before = self.config.read_text()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(oc.apply(self.config, endpoint=CUSTOM))
        self.assertIn("WARN", err.getvalue())
        self.assertIn(str(self.config), err.getvalue())
        self.assertEqual(self.config.read_text(), before)

    def test_bad_yaml_in_source_path_warns_and_leaves_file(self):
        pristine = self.tmp / "defaults" / "hermes_otel" / "config.yaml"
        pristine.parent.mkdir(parents=True, exist_ok=True)
        pristine.write_text("backends: [ not valid yaml")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(oc.apply(self.config, source_path=pristine))
        self.assertIn(str(pristine), err.getvalue())

    def test_unwritable_file_warns_rather_than_raising(self):
        self.config.chmod(0o444)
        self.config.parent.chmod(0o555)
        self.addCleanup(self.config.parent.chmod, 0o755)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertFalse(oc.apply(self.config, endpoint=CUSTOM))
        self.assertIn("WARN", err.getvalue())

    def test_source_path_derives_from_the_pristine_copy(self):
        """A stale PVC copy must not survive: the image is the source of truth.

        This is what lets an endpoint be *unset* again — without it, the previous
        customer endpoint would stay pinned on the PVC forever.
        """
        pristine = self.write_baked(self.tmp / "defaults" / "hermes_otel" / "config.yaml")
        self.config.write_text(yaml.safe_dump({"backends": [{"name": "gke-managed-otel", "type": "otlp", "endpoint": "http://stale:4318/v1/traces"}]}))

        self.assertTrue(oc.apply(self.config, service_name="agent-gateway", source_path=pristine))
        self.assertEqual(self.load()["backends"][0]["endpoint"], BAKED)


class ApplyAllTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "data"
        self.defaults = self.tmp / "defaults" / "plugins"
        self.baked(self.defaults / "hermes_otel" / "config.yaml")

    def baked(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"backends": [{"name": "gke-managed-otel", "type": "otlp", "endpoint": BAKED}]}))
        return path

    def endpoint_of(self, path):
        return yaml.safe_load(path.read_text())["backends"][0]["endpoint"]

    def test_sweeps_the_root_home_and_every_profile(self):
        root = self.baked(self.home / "plugins" / "hermes_otel" / "config.yaml")
        platform = self.baked(self.home / "profiles" / "platform" / "plugins" / "hermes_otel" / "config.yaml")
        cluster = self.baked(self.home / "profiles" / "cluster-prod" / "plugins" / "hermes_otel" / "config.yaml")

        results = oc.apply_all(self.home, "agent-gateway", CUSTOM, self.defaults)

        self.assertEqual(set(results), {str(root), str(platform), str(cluster)})
        self.assertTrue(all(results.values()))
        for path in (root, platform, cluster):
            self.assertEqual(self.endpoint_of(path), CUSTOM + "/v1/traces")
            self.assertEqual(yaml.safe_load(path.read_text())["resource_attributes"]["service.name"], "agent-gateway")

    def test_skips_a_profile_without_the_plugin(self):
        self.baked(self.home / "plugins" / "hermes_otel" / "config.yaml")
        (self.home / "profiles" / "chat").mkdir(parents=True)

        results = oc.apply_all(self.home, "agent-gateway", CUSTOM, self.defaults)

        self.assertNotIn(str(self.home / "profiles" / "chat" / "plugins" / "hermes_otel" / "config.yaml"), results)

    def test_unset_endpoint_leaves_every_copy_on_the_baked_default(self):
        root = self.baked(self.home / "plugins" / "hermes_otel" / "config.yaml")
        oc.apply_all(self.home, "agent-gateway", None, self.defaults)
        self.assertEqual(self.endpoint_of(root), BAKED)

    def test_nothing_to_do_is_not_an_error(self):
        self.assertEqual(oc.apply_all(self.home, "agent-gateway", CUSTOM, self.defaults), {})

    def test_disabled_sweeps_all_profiles(self):
        root = self.baked(self.home / "plugins" / "hermes_otel" / "config.yaml")
        platform = self.baked(self.home / "profiles" / "platform" / "plugins" / "hermes_otel" / "config.yaml")
        cluster = self.baked(self.home / "profiles" / "cluster-prod" / "plugins" / "hermes_otel" / "config.yaml")

        results = oc.apply_all(self.home, "agent-gateway", CUSTOM, self.defaults, disabled=True)

        self.assertEqual(set(results), {str(root), str(platform), str(cluster)})
        self.assertTrue(all(results.values()))
        for path in (root, platform, cluster):
            cfg = yaml.safe_load(path.read_text())
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["backends"], [])
            self.assertEqual(cfg["resource_attributes"]["service.name"], "agent-gateway")

    def test_main_handles_disabled_flag(self):
        target = self.baked(self.home / "plugins" / "hermes_otel" / "config.yaml")
        with redirect_stderr(io.StringIO()):
            rc = oc.main([
                "--hermes-home", str(self.home),
                "--service-name", "agent-gateway",
                "--defaults-plugins", str(self.defaults),
                "--disabled",
            ])
        self.assertEqual(rc, 0)
        cfg = yaml.safe_load(target.read_text())
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["backends"], [])

    def test_main_never_fails_the_container_start(self):
        self.baked(self.home / "plugins" / "hermes_otel" / "config.yaml")
        with redirect_stderr(io.StringIO()):
            rc = oc.main([
                "--hermes-home", str(self.home),
                "--service-name", "agent-gateway",
                "--endpoint", CUSTOM,
                "--defaults-plugins", str(self.defaults),
            ])
        self.assertEqual(rc, 0)
        self.assertEqual(self.endpoint_of(self.home / "plugins" / "hermes_otel" / "config.yaml"), CUSTOM + "/v1/traces")


if __name__ == "__main__":
    unittest.main()
