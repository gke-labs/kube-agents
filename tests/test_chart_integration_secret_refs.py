"""The chart exposes Secret reference values for Slack and Teams integrations.

Issue #3275: Slack bot/app token Secret refs were hard-coded to credentials.secretName
and default keys (SLACK_BOT_TOKEN, SLACK_APP_TOKEN). Split-Secret installs whose tokens
live in dedicated Secrets were reverted to platform-agent-secrets on every upgrade.sh run.

This test asserts:
1. values.schema.json declares botTokenSecretRef and appTokenSecretRef under
   platformAgent.integration.slack, and appIdSecretRef and appPasswordSecretRef
   under platformAgent.integration.teams.
2. Rendering with defaults keeps today's behaviour (pointing to credentials.secretName
   and default keys).
3. Rendering with custom Secret refs forwards the specified name and key to the
   PlatformAgent CR.
4. Partial overrides (specifying name only or key only) fall back cleanly to defaults.

Run: python3 -m unittest discover -s tests -p 'test_chart_integration_secret_refs.py' -v
"""

import json
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_SCHEMA = _CHART / "values.schema.json"
_VALUES = _CHART / "values.yaml"
_HELM = shutil.which("helm")
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]


def _render(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["helm", "template", "kube-agents", str(_CHART), "--namespace", "kubeagents-system", *_REQUIRED, *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _cr(stdout: str) -> dict:
    crs = [d for d in yaml.safe_load_all(stdout) if d and d.get("kind") == "PlatformAgent"]
    if len(crs) != 1:
        raise AssertionError(f"expected one PlatformAgent, rendered {len(crs)}")
    return crs[0]


class SchemaDeclaresIntegrationSecretRefsTest(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(_SCHEMA.read_text())
        self.integ = self.schema["properties"]["platformAgent"]["properties"]["integration"]["properties"]

    def test_slack_schema_declares_secret_refs(self):
        slack = self.integ["slack"]["properties"]
        self.assertIn("botTokenSecretRef", slack)
        self.assertIn("appTokenSecretRef", slack)

        for ref_name in ("botTokenSecretRef", "appTokenSecretRef"):
            ref = slack[ref_name]
            self.assertEqual(ref.get("type"), "object")
            self.assertFalse(ref.get("additionalProperties", True))
            self.assertIn("name", ref["properties"])
            self.assertIn("key", ref["properties"])
            self.assertEqual(ref["properties"]["name"].get("type"), "string")
            self.assertEqual(ref["properties"]["key"].get("type"), "string")
            self.assertEqual(ref["properties"]["name"].get("maxLength"), 253)
            self.assertEqual(ref["properties"]["key"].get("maxLength"), 253)
            self.assertIn("pattern", ref["properties"]["name"])
            self.assertIn("pattern", ref["properties"]["key"])
            self.assertIn("not", ref["properties"]["key"])
            self.assertEqual(ref["properties"]["key"]["not"].get("pattern"), r"^\.$|^\.\.")

    def test_teams_schema_declares_secret_refs(self):
        teams = self.integ["teams"]["properties"]
        self.assertIn("appIdSecretRef", teams)
        self.assertIn("appPasswordSecretRef", teams)

        for ref_name in ("appIdSecretRef", "appPasswordSecretRef"):
            ref = teams[ref_name]
            self.assertEqual(ref.get("type"), "object")
            self.assertFalse(ref.get("additionalProperties", True))
            self.assertIn("name", ref["properties"])
            self.assertIn("key", ref["properties"])
            self.assertEqual(ref["properties"]["name"].get("type"), "string")
            self.assertEqual(ref["properties"]["key"].get("type"), "string")
            self.assertEqual(ref["properties"]["name"].get("maxLength"), 253)
            self.assertEqual(ref["properties"]["key"].get("maxLength"), 253)
            self.assertIn("pattern", ref["properties"]["name"])
            self.assertIn("pattern", ref["properties"]["key"])
            self.assertIn("not", ref["properties"]["key"])
            self.assertEqual(ref["properties"]["key"]["not"].get("pattern"), r"^\.$|^\.\.")


@unittest.skipUnless(_HELM, "helm binary is required for template render tests")
class RenderIntegrationSecretRefsTest(unittest.TestCase):
    def test_slack_defaults_render_credentials_secret_and_canonical_keys(self):
        res = _render("--set", "platformAgent.integration.slack.enabled=true")
        self.assertEqual(res.returncode, 0, res.stderr)
        cr = _cr(res.stdout)
        slack = cr["spec"]["integration"]["slack"]
        self.assertEqual(slack["botTokenSecretRef"]["name"], "platform-agent-secrets")
        self.assertEqual(slack["botTokenSecretRef"]["key"], "SLACK_BOT_TOKEN")
        self.assertEqual(slack["appTokenSecretRef"]["name"], "platform-agent-secrets")
        self.assertEqual(slack["appTokenSecretRef"]["key"], "SLACK_APP_TOKEN")

    def test_slack_custom_secret_refs_render(self):
        res = _render(
            "--set", "platformAgent.integration.slack.enabled=true",
            "--set", "platformAgent.integration.slack.botTokenSecretRef.name=my-slack-secret",
            "--set", "platformAgent.integration.slack.botTokenSecretRef.key=MY_BOT_TOKEN",
            "--set", "platformAgent.integration.slack.appTokenSecretRef.name=my-app-secret",
            "--set", "platformAgent.integration.slack.appTokenSecretRef.key=MY_APP_TOKEN",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        cr = _cr(res.stdout)
        slack = cr["spec"]["integration"]["slack"]
        self.assertEqual(slack["botTokenSecretRef"]["name"], "my-slack-secret")
        self.assertEqual(slack["botTokenSecretRef"]["key"], "MY_BOT_TOKEN")
        self.assertEqual(slack["appTokenSecretRef"]["name"], "my-app-secret")
        self.assertEqual(slack["appTokenSecretRef"]["key"], "MY_APP_TOKEN")

    def test_slack_partial_override_falls_back_to_defaults(self):
        res = _render(
            "--set", "platformAgent.integration.slack.enabled=true",
            "--set", "platformAgent.integration.slack.botTokenSecretRef.name=my-slack-secret",
            "--set", "platformAgent.integration.slack.appTokenSecretRef.key=CUSTOM_APP_TOKEN",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        cr = _cr(res.stdout)
        slack = cr["spec"]["integration"]["slack"]
        # botTokenSecretRef had name only -> key falls back to SLACK_BOT_TOKEN
        self.assertEqual(slack["botTokenSecretRef"]["name"], "my-slack-secret")
        self.assertEqual(slack["botTokenSecretRef"]["key"], "SLACK_BOT_TOKEN")
        # appTokenSecretRef had key only -> name falls back to credentials.secretName
        self.assertEqual(slack["appTokenSecretRef"]["name"], "platform-agent-secrets")
        self.assertEqual(slack["appTokenSecretRef"]["key"], "CUSTOM_APP_TOKEN")

    def test_teams_defaults_render_credentials_secret_and_canonical_keys(self):
        res = _render("--set", "platformAgent.integration.teams.enabled=true")
        self.assertEqual(res.returncode, 0, res.stderr)
        cr = _cr(res.stdout)
        teams = cr["spec"]["integration"]["teams"]
        self.assertEqual(teams["appIdSecretRef"]["name"], "platform-agent-secrets")
        self.assertEqual(teams["appIdSecretRef"]["key"], "TEAMS_APP_ID")
        self.assertEqual(teams["appPasswordSecretRef"]["name"], "platform-agent-secrets")
        self.assertEqual(teams["appPasswordSecretRef"]["key"], "TEAMS_APP_PASSWORD")

    def test_teams_custom_secret_refs_render(self):
        res = _render(
            "--set", "platformAgent.integration.teams.enabled=true",
            "--set", "platformAgent.integration.teams.appIdSecretRef.name=my-teams-secret",
            "--set", "platformAgent.integration.teams.appIdSecretRef.key=MY_APP_ID",
            "--set", "platformAgent.integration.teams.appPasswordSecretRef.name=my-teams-secret",
            "--set", "platformAgent.integration.teams.appPasswordSecretRef.key=MY_APP_PASSWORD",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        cr = _cr(res.stdout)
        teams = cr["spec"]["integration"]["teams"]
        self.assertEqual(teams["appIdSecretRef"]["name"], "my-teams-secret")
        self.assertEqual(teams["appIdSecretRef"]["key"], "MY_APP_ID")
        self.assertEqual(teams["appPasswordSecretRef"]["name"], "my-teams-secret")
        self.assertEqual(teams["appPasswordSecretRef"]["key"], "MY_APP_PASSWORD")

    def test_invalid_secret_name_refused_by_schema(self):
        res = _render(
            "--set", "platformAgent.integration.slack.enabled=true",
            "--set", "platformAgent.integration.slack.botTokenSecretRef.name=INVALID_NAME",
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not match pattern", res.stderr)

    def test_invalid_secret_key_refused_by_schema(self):
        res = _render(
            "--set", "platformAgent.integration.slack.enabled=true",
            "--set", "platformAgent.integration.slack.botTokenSecretRef.key=bad key with spaces",
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("does not match pattern", res.stderr)

        for invalid_key in (".", "..", "..token"):
            res = _render(
                "--set", "platformAgent.integration.slack.enabled=true",
                f"--set=platformAgent.integration.slack.botTokenSecretRef.key={invalid_key}",
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("'not' failed", res.stderr)


class ValuesDocumentationTest(unittest.TestCase):
    def test_values_documents_secret_refs(self):
        text = _VALUES.read_text()
        self.assertIn("botTokenSecretRef", text)
        self.assertIn("appTokenSecretRef", text)
        self.assertIn("appIdSecretRef", text)
        self.assertIn("appPasswordSecretRef", text)


if __name__ == "__main__":
    unittest.main()
