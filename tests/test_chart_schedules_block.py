"""The chart renders `spec.schedules` from `platformAgent.schedules`, as written.

Unset renders nothing, so every built-in schedule follows its shipped default.
Both the block and `suspendAll` sit behind the shared CRD guard, because an
installed CRD that predates either would prune it while the release record keeps
it, and a pruned `suspendAll` leaves every job running. `lookup` is empty under
`helm template`, so the guards are pinned as text; the render tests need a helm
binary.

Run: python3 -m unittest discover -s tests -p 'test_chart_schedules_block.py' -v
"""

import json
import pathlib
import shutil
import subprocess
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
_BLOCK_GUARD = ('include "kube-agents.crdFieldGuard" (dict "value" "platformAgent.schedules" '
                '"path" (list) "field" "schedules")')
_SUSPEND_ALL_GUARD = ('include "kube-agents.crdFieldGuard" (dict "value" "platformAgent.schedules.suspendAll" '
                      '"path" (list "schedules") "field" "suspendAll")')
_SCHEDULES = {"suspendAll": True, "overrides": [{"name": "compliance-audit", "suspend": False}]}


class SchedulesGuardTest(unittest.TestCase):
    def test_the_block_and_suspend_all_are_guarded_by_the_shared_crd_guard(self):
        template = _TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(_BLOCK_GUARD, template)
        self.assertIn(_SUSPEND_ALL_GUARD, template)

    def test_the_chart_default_sets_nothing(self):
        values = yaml.safe_load((_CHART / "values.yaml").read_text())
        self.assertEqual(values["platformAgent"]["schedules"], {})


@unittest.skipUnless(shutil.which("helm"), "helm not installed")
class SchedulesRenderTest(unittest.TestCase):
    def _spec(self, *extra):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_REQUIRED, *extra,
             "--show-only", "templates/platform-agent-cr.yaml"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for document in yaml.safe_load_all(proc.stdout):
            if isinstance(document, dict) and document.get("kind") == "PlatformAgent":
                return document["spec"]
        self.fail("no PlatformAgent in the render")

    def test_the_default_renders_no_schedules_block(self):
        self.assertNotIn("schedules", self._spec())

    def test_the_value_is_rendered_as_written(self):
        spec = self._spec("--set-json", "platformAgent.schedules=" + json.dumps(_SCHEDULES))
        self.assertEqual(spec["schedules"], _SCHEDULES)


if __name__ == "__main__":
    unittest.main()
