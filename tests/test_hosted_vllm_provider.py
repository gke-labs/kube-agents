"""MODEL_PROVIDER=hosted_vllm: LiteLLM routed to a vLLM server in the cluster.

The provider rides the existing base config (`hosted_vllm/<model>` renders from
the same template every provider uses) and one environment variable,
HOSTED_VLLM_API_BASE, which LiteLLM reads for that provider when the config
carries no api_base. It has no defaults: the model id, the base URL, and the
server pod's port are all required, and the chart refuses to render without
them. The installer half is covered in test_installer_common.py.
"""

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_EXAMPLE = _REPO_ROOT / "examples" / "litellm-hosted-vllm"
_MODEL = "some-org/some-model"
_API_BASE = "http://model-server.kubeagents-system.svc.cluster.local/v1"
_PORT = 8000
_BASE_ARGS = [
    "--set", "platformAgent.harness.projectId=my-proj",
    "--set", "platformAgent.harness.clusterName=my-cluster",
    "--set", "platformAgent.harness.location=us-central1",
]
_HOSTED_ARGS = [
    "--set", "litellm.modelProvider=hosted_vllm",
    "--set", f"litellm.modelDefaultName={_MODEL}",
    "--set", f"litellm.hostedVllm.apiBase={_API_BASE}",
    "--set", f"litellm.hostedVllm.targetPort={_PORT}",
]


def _render(*extra):
    return subprocess.run(
        ["helm", "template", "t", str(_CHART), *_BASE_ARGS, *extra],
        capture_output=True, text=True,
    )


def _litellm_objects(rendered):
    return {(d["kind"], d["metadata"]["name"]): d for d in yaml.safe_load_all(rendered) if d}


def _same_namespace_rules(policy):
    return [r for r in policy["spec"]["egress"] if r.get("to") == [{"podSelector": {}}]]


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ChartRenderTest(unittest.TestCase):
    def test_hosted_vllm_renders_the_provider_line_the_env_var_and_one_egress_rule(self):
        proc = _render(*_HOSTED_ARGS)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        objects = _litellm_objects(proc.stdout)
        config = objects[("ConfigMap", "litellm-config")]["data"]["config.yaml"]
        self.assertIn(f"model: hosted_vllm/{_MODEL}", config)
        self.assertNotIn("api_key", config)
        self.assertNotIn("api_base", config)
        container = objects[("Deployment", "litellm")]["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        self.assertEqual(env["HOSTED_VLLM_API_BASE"], _API_BASE)
        rules = _same_namespace_rules(objects[("NetworkPolicy", "litellm-policy")])
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["ports"], [{"port": _PORT, "protocol": "TCP"}])

    def test_each_missing_value_fails_the_render_naming_it(self):
        for dropped, named in (
            (f"litellm.modelDefaultName={_MODEL}", "litellm.modelDefaultName"),
            (f"litellm.hostedVllm.apiBase={_API_BASE}", "litellm.hostedVllm.apiBase"),
            (f"litellm.hostedVllm.targetPort={_PORT}", "litellm.hostedVllm.targetPort"),
        ):
            with self.subTest(missing=named):
                values = [v for v in _HOSTED_ARGS if v not in ("--set", dropped)]
                proc = _render(*sum([["--set", v] for v in values], []))
                self.assertNotEqual(proc.returncode, 0, "rendered without " + named)
                self.assertIn(named, proc.stderr)

    def test_other_providers_render_none_of_it(self):
        for provider in ("gemini", "anthropic", "openai"):
            with self.subTest(provider=provider):
                proc = _render("--set", f"litellm.modelProvider={provider}")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertNotIn("HOSTED_VLLM", proc.stdout)
                policy = _litellm_objects(proc.stdout)[("NetworkPolicy", "litellm-policy")]
                self.assertEqual(_same_namespace_rules(policy), [])


class ExampleTest(unittest.TestCase):
    """examples/litellm-hosted-vllm is the hand-applied twin of the chart branch."""

    def test_the_example_carries_the_provider_line_the_env_var_and_the_egress_rule(self):
        config = next(yaml.safe_load_all((_EXAMPLE / "configmap.yaml").read_text()))["data"]["config.yaml"]
        params = yaml.safe_load(config)["model_list"][0]["litellm_params"]
        self.assertTrue(params["model"].startswith("hosted_vllm/"))
        self.assertNotIn("api_key", params)
        deployment = next(d for d in yaml.safe_load_all((_EXAMPLE / "deployment.yaml").read_text()) if d["kind"] == "Deployment")
        env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertIn("HOSTED_VLLM_API_BASE", env)
        self.assertNotIn("GEMINI_API_KEY", env)
        policy = next(d for d in yaml.safe_load_all((_EXAMPLE / "networkpolicy.yaml").read_text()) if d["kind"] == "NetworkPolicy")
        self.assertEqual(len(_same_namespace_rules(policy)), 1)

    def test_the_example_needs_no_secret(self):
        self.assertFalse((_EXAMPLE / "secret.yaml").exists())


if __name__ == "__main__":
    unittest.main()
