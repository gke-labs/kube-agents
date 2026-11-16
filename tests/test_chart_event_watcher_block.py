"""The chart renders `spec.harness.eventWatcher.alertExclude` from `platformAgent.harness.eventWatcher`.

The chart default is an empty list, which renders no block: with no rules the
operator renders an empty rules file, so an absent block and an empty list
are the same. A rule list reaches the CR as it is. The schema holds the CRD's
bounds, so a rule the API server would refuse fails the render instead. The
text-structural tests run everywhere; the render tests need a helm binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_event_watcher_block.py' -v
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]

RULES = [
    {"namespace": "payments"},
    {"reason": "FailedScheduling", "cluster": {"clusterName": "batch"}},
    {"cluster": {"projectId": "p", "location": "us-central1", "clusterName": "staging-a"}},
    {"cluster": {"projectId": "example.com:my-project", "clusterName": "batch"}},
    {"name": "node-1.example"},
    {"namespace": "shop", "kind": "Pod", "name": "inventory-api-*"},
]


class EventWatcherBlockShapeTest(unittest.TestCase):
    """What the template and the values say, readable without helm."""

    def test_the_chart_default_is_an_empty_list(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertEqual(values["platformAgent"]["harness"]["eventWatcher"], {"alertExclude": []})

    def test_the_block_is_guarded_against_a_crd_without_the_field(self):
        # helm upgrade does not apply crds/, so an older CRD would prune the
        # rules while the release record keeps them.
        template = _TEMPLATE.read_text()
        self.assertIn('{{- if not (hasKey $eventWatcherProps "alertExclude") }}', template)
        self.assertIn(
            "platformAgent.harness.eventWatcher.alertExclude is set, but the installed PlatformAgent CRD predates",
            template,
        )


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class EventWatcherBlockRenderTest(unittest.TestCase):
    def _render(self, *extra):
        return subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True, check=False,
        )

    def _block_of(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"]["harness"].get("eventWatcher", "ABSENT")
        self.fail("no PlatformAgent in the render")

    def _values_file(self, rules):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        yaml.safe_dump({"platformAgent": {"harness": {"eventWatcher": {"alertExclude": rules}}}}, handle)
        handle.close()
        self.addCleanup(pathlib.Path(handle.name).unlink)
        return handle.name

    def test_the_default_renders_no_block(self):
        self.assertEqual(self._block_of(self._render()), "ABSENT")

    def test_an_empty_list_renders_no_block(self):
        self.assertEqual(self._block_of(self._render("-f", self._values_file([]))), "ABSENT")

    def test_rules_reach_the_block_verbatim(self):
        self.assertEqual(self._block_of(self._render("-f", self._values_file(RULES))), {"alertExclude": RULES})

    def test_sixty_four_rules_render(self):
        rules = [{"namespace": "payments"}] * 64
        self.assertEqual(self._block_of(self._render("-f", self._values_file(rules))), {"alertExclude": rules})

    def test_rules_the_crd_refuses_fail_the_render(self):
        for bad in (
            [{}],
            [{"name": "*"}],
            [{"name": "api-*-x"}],
            [{"cluster": {"projectId": "p"}}],
            [{"cluster": {"clusterName": ""}}],
            [{"namespace": "Payments"}],
            [{"reason": ""}],
            [{"kind": "pods/status"}],
            [{"namespaces": ["payments"]}],
            [{"namespace": "payments"}] * 65,
            [{"namespace": "shop", "name": "*"}],
            [{"name": "Payments-*"}],
            [{"reason": "<Back&Off>"}],
            [{"cluster": {"clusterName": "a" * 64}}],
            [{"cluster": {"projectId": "p" + "a" * 128, "clusterName": "batch"}}],
            [{"cluster": {"projectId": "Example.com:my-project", "clusterName": "batch"}}],
            [{"cluster": {"location": "US-Central1", "clusterName": "batch"}}],
            [{"name": "payments-api-"}],
            [{"name": "a..b"}],
            [{"cluster": {"location": "us-central1-", "clusterName": "batch"}}],
            [{"cluster": {"projectId": "my-project-", "clusterName": "batch"}}],
        ):
            with self.subTest(bad=bad):
                proc = self._render("-f", self._values_file(bad))
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("alertExclude", proc.stderr)


if __name__ == "__main__":
    unittest.main()
