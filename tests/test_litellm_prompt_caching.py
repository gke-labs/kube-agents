"""The gateway adds prompt-caching breakpoints for Anthropic-family models only.

LiteLLM turns `cache_control_injection_points` into Anthropic cache markers on
a Claude route, which is what they are for. On a Gemini route it turns the same
markers into Vertex explicit context caching: a cachedContents list before
every call and a create whenever the cached prefix is new, and a create Vertex holds past its request
credential's life fails the turn. So the chart renders the breakpoints under
the same Claude test as `additional_drop_params`, and the kustomize base keeps
them in an anthropic overlay that the anthropic provider selects. The render
cases need `helm` and skip without it.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_LITELLM = _REPO_ROOT / "k8s-operator" / "config" / "integrations" / "litellm"
_BREAKPOINTS = [
    {"location": "message", "role": "system", "control": {"type": "ephemeral", "ttl": "1h"}},
    {"location": "message", "index": -3},
    {"location": "message", "index": -1},
]
_HELM_BASE_ARGS = [
    "helm",
    "template",
    "test-release",
    str(_CHART),
    "--set-string",
    "platformAgent.harness.clusterName=test-cluster",
    "--set-string",
    "platformAgent.harness.location=us-central1",
    "--set-string",
    "platformAgent.harness.projectId=test-project",
    "-s",
    "templates/litellm.yaml",
]


def _model_args(provider: str, model: str | None = None) -> list[str]:
    args = ["--set-string", f"litellm.modelProvider={provider}"]
    if model is not None:
        args += ["--set-string", f"litellm.modelDefaultName={model}"]
    return args


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestChartRender(unittest.TestCase):
    def _config(self, extra_args: list[str]) -> dict:
        proc = subprocess.run(_HELM_BASE_ARGS + extra_args, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        configmap = next(d for d in documents if d["kind"] == "ConfigMap")
        return yaml.safe_load(configmap["data"]["config.yaml"])

    def test_claude_gets_the_breakpoints(self) -> None:
        for args in (_model_args("anthropic"), _model_args("vertex_ai", "claude-opus-5")):
            with self.subTest(args=args):
                points = self._config(args)["router_settings"]["default_litellm_params"]["cache_control_injection_points"]
                self.assertEqual(points, _BREAKPOINTS)

    def test_gemini_and_the_rest_get_none(self) -> None:
        for args in (
            [],
            _model_args("gemini"),
            _model_args("vertex_ai"),
            _model_args("vertex_ai", "gemini-3.1-pro-preview"),
            _model_args("openai"),
        ):
            with self.subTest(args=args):
                self.assertNotIn("router_settings", self._config(args))


class TestKustomize(unittest.TestCase):
    def _config(self, path: pathlib.Path) -> dict:
        return yaml.safe_load(path.read_text())

    def test_the_base_carries_no_breakpoints(self) -> None:
        self.assertNotIn("router_settings", self._config(_LITELLM / "base" / "config.yaml"))

    def test_the_anthropic_overlay_is_the_base_plus_the_breakpoints(self) -> None:
        base = self._config(_LITELLM / "base" / "config.yaml")
        overlay = self._config(_LITELLM / "overlays" / "anthropic" / "config.yaml")
        points = overlay.pop("router_settings")["default_litellm_params"]["cache_control_injection_points"]
        self.assertEqual(points, _BREAKPOINTS)
        self.assertEqual(overlay, base)

    def test_the_anthropic_overlay_merges_its_config_over_the_base(self) -> None:
        kustomization = self._config(_LITELLM / "overlays" / "anthropic" / "kustomization.yaml")
        self.assertIn("../../base", kustomization["resources"])
        generator = next(g for g in kustomization["configMapGenerator"] if g["name"] == "litellm-config")
        self.assertEqual(generator["behavior"], "merge")
        self.assertEqual(generator["files"], ["config.yaml"])

    @unittest.skipUnless(shutil.which("kubectl"), "kubectl is not installed")
    def test_the_built_anthropic_overlay_keeps_the_base_keys_and_mount(self) -> None:
        proc = subprocess.run(
            ["kubectl", "kustomize", str(_LITELLM / "overlays" / "anthropic")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        configmap = next(d for d in documents if d["kind"] == "ConfigMap")
        self.assertEqual(sorted(configmap["data"]), ["config.yaml", "litellm_thinking_replay_callback.py"])
        self.assertIn("router_settings", yaml.safe_load(configmap["data"]["config.yaml"]))
        deployment = next(d for d in documents if d["kind"] == "Deployment")
        volumes = {v["name"]: v for v in deployment["spec"]["template"]["spec"]["volumes"]}
        self.assertEqual(volumes["config-volume"]["configMap"]["name"], configmap["metadata"]["name"])

    def test_only_the_anthropic_provider_selects_it(self) -> None:
        providers = json.loads((_LITELLM / "providers.json").read_text())["providers"]
        overlays = {p["id"]: p["overlay"] for p in providers}
        self.assertEqual(overlays["anthropic"], "anthropic")
        self.assertEqual([p for p, o in overlays.items() if o == "anthropic"], ["anthropic"])
        makefile = (_REPO_ROOT / "k8s-operator" / "Makefile").read_text()
        branches = [line for line in makefile.splitlines() if '"$$MODEL_PROVIDER" = "anthropic"' in line]
        self.assertEqual(len(branches), 2)
        for line in makefile.splitlines():
            if "litellm/overlays/anthropic" in line:
                self.assertIn("$(KUSTOMIZE) build", line)
        self.assertEqual(makefile.count("config/integrations/litellm/overlays/anthropic"), 2)


if __name__ == "__main__":
    unittest.main()
