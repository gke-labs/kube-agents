"""Helm chart wiring for `platformAgent.integration.googleChat.{projectId,serviceAccountEmail}`."""

import json
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_SCHEMA_FILE = _CHART / "values.schema.json"
_CR_TEMPLATE = _CHART / "templates" / "platform-agent-cr.yaml"
_HARNESS_PROJECT = "gke-host-proj"
_CHAT_PROJECT = "workspace-chat-proj"
_CHAT_SA_EMAIL = f"kubeagents-chat-gsa@{_CHAT_PROJECT}.iam.gserviceaccount.com"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", f"platformAgent.harness.projectId={_HARNESS_PROJECT}",
]


@unittest.skipUnless(shutil.which("helm"), "helm binary required")
class ChartGoogleChatProjectRenderTest(unittest.TestCase):
    def _helm_template(self, *extra_args: str) -> list[dict]:
        cmd = ["helm", "template", "ka", str(_CHART), *_REQUIRED, *extra_args]
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return [doc for doc in yaml.safe_load_all(res.stdout) if doc]

    def test_empty_chat_project_id_falls_back_to_harness_project_and_omits_service_account(self) -> None:
        docs = self._helm_template(
            "--set", "platformAgent.integration.googleChat.enabled=true",
        )
        cr = next(d for d in docs if d.get("kind") == "PlatformAgent")
        gchat = cr["spec"]["integration"]["googleChat"]
        self.assertEqual(gchat["projectId"], _HARNESS_PROJECT)
        self.assertNotIn("serviceAccountEmail", gchat)

    def test_explicit_chat_project_and_service_account_render_into_cr(self) -> None:
        docs = self._helm_template(
            "--set", "platformAgent.integration.googleChat.enabled=true",
            "--set", f"platformAgent.integration.googleChat.projectId={_CHAT_PROJECT}",
            "--set", f"platformAgent.integration.googleChat.serviceAccountEmail={_CHAT_SA_EMAIL}",
        )
        cr = next(d for d in docs if d.get("kind") == "PlatformAgent")
        gchat = cr["spec"]["integration"]["googleChat"]
        self.assertEqual(gchat["projectId"], _CHAT_PROJECT)
        self.assertEqual(gchat["serviceAccountEmail"], _CHAT_SA_EMAIL)


class ChartGoogleChatProjectStaticGuardTest(unittest.TestCase):
    def test_crd_field_guard_and_schema_entries_exist(self) -> None:
        template_text = _CR_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(
            'include "kube-agents.crdFieldGuard" (dict "value" "platformAgent.integration.googleChat.serviceAccountEmail"',
            template_text,
        )
        schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
        gchat_props = (
            schema["properties"]["platformAgent"]["properties"]["integration"]["properties"]["googleChat"]["properties"]
        )
        self.assertIn("projectId", gchat_props)
        self.assertIn("serviceAccountEmail", gchat_props)


if __name__ == "__main__":
    unittest.main()
