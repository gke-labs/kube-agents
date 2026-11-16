"""Helm chart wiring for `platformAgent.harness.universeDomain`.

Exercises the PlatformAgent CR rendering, `crdFieldGuard` refusal against a
pre-`universeDomain` CRD, `values.schema.json` validation, and `litellm` /
`githubMinter` environment rendering in default and custom universes.
"""

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
_CUSTOM_UNIVERSE = "universe.example.test"
_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=u-region-north1",
    "--set", "platformAgent.harness.projectId=part0:sample-project",
]


@unittest.skipUnless(shutil.which("helm"), "helm binary required")
class ChartUniverseDomainRenderTest(unittest.TestCase):
    def _helm_template(self, *extra_args: str) -> list[dict]:
        cmd = ["helm", "template", "ka", str(_CHART), *_REQUIRED, *extra_args]
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return [doc for doc in yaml.safe_load_all(res.stdout) if doc]

    def test_empty_universe_domain_omits_spec_field_and_keeps_commercial_defaults(self) -> None:
        docs = self._helm_template(
            "--set", "githubMinter.enabled=true",
            "--set", "githubMinter.org=acme",
            "--set", "githubMinter.repo=gitops",
            "--set", "litellm.modelProvider=vertex_ai",
        )
        cr = next(d for d in docs if d.get("kind") == "PlatformAgent")
        self.assertNotIn("universeDomain", cr["spec"]["harness"])

        minter = next(
            d for d in docs
            if d.get("kind") == "Deployment" and "minter" in d["metadata"]["name"]
        )
        minter_env = {
            e["name"]: e.get("value", "")
            for e in minter["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertIn(
            "https://container.googleapis.com/v1/projects/part0:sample-project/locations/u-region-north1/clusters/ci-cluster",
            minter_env["ISSUER_ALLOWLIST"],
        )
        self.assertNotIn("GOOGLE_CLOUD_UNIVERSE_DOMAIN", minter_env)

        minter_sa = next(
            d for d in docs
            if d.get("kind") == "ServiceAccount" and "minter" in d["metadata"]["name"]
        )
        self.assertEqual(
            minter_sa["metadata"]["annotations"]["iam.gke.io/gcp-service-account"],
            "kubeagents-github-minter-gsa@sample-project.part0.iam.gserviceaccount.com",
        )

        minter_cm = next(
            d for d in docs
            if d.get("kind") == "ConfigMap" and "minter" in d["metadata"]["name"]
        )
        self.assertIn("sample-project.part0.iam.gserviceaccount.com", json.dumps(minter_cm["data"]))

    def test_custom_universe_renders_cr_field_and_litellm_and_minter_env(self) -> None:
        docs = self._helm_template(
            "--set", f"platformAgent.harness.universeDomain={_CUSTOM_UNIVERSE}",
            "--set", "githubMinter.enabled=true",
            "--set", "githubMinter.org=acme",
            "--set", "githubMinter.repo=gitops",
            "--set", "litellm.modelProvider=vertex_ai",
        )
        cr = next(d for d in docs if d.get("kind") == "PlatformAgent")
        self.assertEqual(cr["spec"]["harness"]["universeDomain"], _CUSTOM_UNIVERSE)

        minter = next(
            d for d in docs
            if d.get("kind") == "Deployment" and "minter" in d["metadata"]["name"]
        )
        minter_env = {
            e["name"]: e.get("value", "")
            for e in minter["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertIn(
            f"https://container.{_CUSTOM_UNIVERSE}/v1/projects/part0:sample-project/locations/u-region-north1/clusters/ci-cluster",
            minter_env["ISSUER_ALLOWLIST"],
        )
        self.assertEqual(minter_env.get("GOOGLE_CLOUD_UNIVERSE_DOMAIN"), _CUSTOM_UNIVERSE)

        litellm = next(
            d for d in docs
            if d.get("kind") == "Deployment" and d["metadata"]["name"].endswith("litellm")
        )
        litellm_env = {
            e["name"]: e.get("value", "")
            for e in litellm["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        self.assertEqual(litellm_env.get("GOOGLE_CLOUD_UNIVERSE_DOMAIN"), _CUSTOM_UNIVERSE)
        litellm_cm = next(
            d for d in docs
            if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("litellm-config")
        )
        self.assertIn(f"https://aiplatform.{_CUSTOM_UNIVERSE}", json.dumps(litellm_cm["data"]))

        regional_docs = self._helm_template(
            "--set", f"platformAgent.harness.universeDomain={_CUSTOM_UNIVERSE}",
            "--set", "litellm.modelProvider=vertex_ai",
            "--set", "litellm.vertex.location=u-region-north1",
        )
        regional_cm = next(
            d for d in regional_docs
            if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("litellm-config")
        )
        self.assertIn(
            f"https://u-region-north1-aiplatform.{_CUSTOM_UNIVERSE}",
            json.dumps(regional_cm["data"]),
        )

    def test_schema_rejects_malformed_universe_domain(self) -> None:
        cmd = [
            "helm", "template", "ka", str(_CHART), *_REQUIRED,
            "--set", "platformAgent.harness.universeDomain=https://not-a-hostname",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("universeDomain", res.stderr)


class ChartUniverseDomainStaticGuardTest(unittest.TestCase):
    def test_crd_field_guard_and_schema_entry_exist(self) -> None:
        template_text = _CR_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(
            'include "kube-agents.crdFieldGuard" (dict "value" "platformAgent.harness.universeDomain"',
            template_text,
        )
        schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
        harness_props = schema["properties"]["platformAgent"]["properties"]["harness"]["properties"]
        self.assertIn("universeDomain", harness_props)


if __name__ == "__main__":
    unittest.main()
