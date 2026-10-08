"""LiteLLM reasoning effort, the fallback alias, and Vertex inputs from a Secret.

All of these are off by default and must stay invisible when unset: the
ConfigMap and its rollout checksum are byte-identical to a render without the
keys, so an existing install does not roll on upgrade. Set, they must render
the shape the gateway reads (reasoning_effort per alias, router_settings
fallbacks, drop_params) and the pod spec a service-account key needs. The
render cases need `helm` and skip without it, as `test_litellm_max_tokens.py`'s
do.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_MODEL = "gemini-3.8-flash"
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
_VERTEX_VALUES = f"""
litellm:
  modelProvider: vertex_ai
  modelDefaultName: {_MODEL}
  vertex:
    location: global
"""
_LAB_VALUES = f"""
litellm:
  modelProvider: vertex_ai
  modelDefaultName: {_MODEL}
  reasoningEffort: high
  dropParams: true
  fallback:
    reasoningEffort: low
    timeoutSeconds: 120
    numRetries: 1
  vertex:
    location: global
    credentialsSecretRef:
      name: platform-agent-secrets
      key: VERTEX_SA_JSON
    projectSecretRef:
      name: platform-agent-secrets
      key: VERTEX_PROJECT_ID
"""


def _render(values_text: str | None = None, extra_args: list[str] | None = None):
    command = list(_HELM_BASE_ARGS)
    values_path = None
    if values_text is not None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(values_text)
            values_path = handle.name
        command += ["-f", values_path]
    command += extra_args or []
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    finally:
        if values_path:
            os.unlink(values_path)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class ReasoningFallbackRenderTest(unittest.TestCase):
    def _documents(self, values_text: str | None = None, extra_args: list[str] | None = None):
        proc = _render(values_text, extra_args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        by_kind = {d["kind"]: d for d in documents if d["kind"] in ("ConfigMap", "Deployment")}
        return by_kind["ConfigMap"], by_kind["Deployment"]

    def _fails(self, values_text: str, needle: str) -> None:
        proc = _render(values_text)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(needle, proc.stderr)

    @staticmethod
    def _config(configmap: dict) -> dict:
        return yaml.safe_load(configmap["data"]["config.yaml"])

    @staticmethod
    def _checksum(deployment: dict) -> str:
        return deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    @staticmethod
    def _container(deployment: dict) -> dict:
        return deployment["spec"]["template"]["spec"]["containers"][0]

    def test_unset_and_empty_values_leave_the_configmap_and_checksum_alone(self) -> None:
        unset_cm, unset_deploy = self._documents()
        explicit = """
litellm:
  reasoningEffort: ""
  dropParams: false
  fallback: {modelName: "", reasoningEffort: "", timeoutSeconds: 0, numRetries: 0}
"""
        empty_cm, empty_deploy = self._documents(explicit)
        self.assertEqual(unset_cm["data"], empty_cm["data"])
        self.assertEqual(self._checksum(unset_deploy), self._checksum(empty_deploy))
        text = unset_cm["data"]["config.yaml"]
        for key in ("reasoning_effort", "drop_params", "fallbacks", "num_retries", "timeout"):
            self.assertNotIn(key, text)

    def test_reasoning_effort_lands_on_every_primary_and_adds_an_effort_alias(self) -> None:
        configmap, _ = self._documents(_VERTEX_VALUES + "  reasoningEffort: high\n")
        config = self._config(configmap)
        params = {e["model_name"]: e["litellm_params"] for e in config["model_list"]}
        self.assertEqual(
            sorted(params), sorted(["model-default", "hermes-agent", _MODEL, f"{_MODEL}-high"])
        )
        for alias, entry in params.items():
            self.assertEqual(entry["reasoning_effort"], "high", alias)
            self.assertEqual(entry["model"], f"vertex_ai/{_MODEL}", alias)
        self.assertTrue(config["litellm_settings"]["drop_params"])

    def test_reasoning_effort_medium_automatically_enables_drop_params(self) -> None:
        configmap, _ = self._documents(_VERTEX_VALUES + "  reasoningEffort: medium\n")
        config = self._config(configmap)
        self.assertTrue(config["litellm_settings"]["drop_params"])

    def test_lab_shaped_values_render_fallbacks_drop_params_and_router_limits(self) -> None:
        configmap, _ = self._documents(_LAB_VALUES)
        config = self._config(configmap)
        params = {e["model_name"]: e["litellm_params"] for e in config["model_list"]}
        self.assertEqual(params[f"{_MODEL}-low"]["reasoning_effort"], "low")
        self.assertTrue(config["litellm_settings"]["drop_params"])
        self.assertEqual(config["litellm_settings"]["callbacks"], ["prometheus"])
        router = config["router_settings"]
        primaries = ["model-default", "hermes-agent", _MODEL, f"{_MODEL}-high"]
        self.assertEqual(router["fallbacks"], [{p: [f"{_MODEL}-low"]} for p in primaries])
        self.assertEqual(router["timeout"], 120)
        self.assertEqual(router["num_retries"], 1)
        self.assertIn("default_litellm_params", router)

    def test_named_fallback_alias(self) -> None:
        values = _VERTEX_VALUES + "  fallback:\n    modelName: slow-lane\n    reasoningEffort: low\n"
        configmap, _ = self._documents(values)
        config = self._config(configmap)
        names = [e["model_name"] for e in config["model_list"]]
        self.assertIn("slow-lane", names)
        self.assertIn({"model-default": ["slow-lane"]}, config["router_settings"]["fallbacks"])

    def test_fallback_alias_colliding_with_a_primary_fails(self) -> None:
        values = (
            _VERTEX_VALUES + "  reasoningEffort: low\n  fallback:\n    reasoningEffort: low\n"
        )
        self._fails(values, "cannot fall back to itself")

    def test_schema_rejects_an_unknown_effort_and_unknown_fallback_keys(self) -> None:
        self._fails("litellm:\n  reasoningEffort: extreme\n", "reasoningEffort")
        self._fails("litellm:\n  fallback:\n    model: x\n", "fallback")
        self._fails("litellm:\n  fallback:\n    numRetries: -1\n", "numRetries")

    def test_invalid_fallback_model_name_is_rejected_at_render_time(self) -> None:
        self._fails(
            _VERTEX_VALUES + '  fallback:\n    modelName: "slow: lane"\n    reasoningEffort: low\n',
            "modelName",
        )
        self._fails(
            _VERTEX_VALUES + '  fallback:\n    modelName: "a, b"\n    reasoningEffort: low\n',
            "modelName",
        )
        self._fails(
            _VERTEX_VALUES + '  fallback:\n    modelName: "no"\n    reasoningEffort: low\n',
            "not be a bare YAML boolean/null/number literal",
        )
        self._fails(
            _VERTEX_VALUES + '  fallback:\n    modelName: "1.0"\n    reasoningEffort: low\n',
            "not be a bare YAML boolean/null/number literal",
        )

    def test_vertex_secret_refs_mount_the_key_and_read_the_project(self) -> None:
        _, deployment = self._documents(_LAB_VALUES)
        container = self._container(deployment)
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(
            env["VERTEXAI_PROJECT"]["valueFrom"]["secretKeyRef"],
            {"name": "platform-agent-secrets", "key": "VERTEX_PROJECT_ID"},
        )
        self.assertEqual(
            env["GOOGLE_APPLICATION_CREDENTIALS"]["value"], "/var/run/secrets/vertex/sa.json"
        )
        mount = next(m for m in container["volumeMounts"] if m["name"] == "vertex-sa-vol")
        self.assertEqual(mount, {"name": "vertex-sa-vol", "mountPath": "/var/run/secrets/vertex", "readOnly": True})
        volume = next(
            v for v in deployment["spec"]["template"]["spec"]["volumes"] if v["name"] == "vertex-sa-vol"
        )
        self.assertEqual(volume["secret"]["secretName"], "platform-agent-secrets")
        self.assertEqual(volume["secret"]["defaultMode"], 0o440)
        self.assertEqual(volume["secret"]["items"], [{"key": "VERTEX_SA_JSON", "path": "sa.json"}])

    def test_project_secret_ref_stands_in_for_a_missing_project_id(self) -> None:
        no_project = ["--set-string", "platformAgent.harness.projectId=", "--set", "platformAgent.enabled=false"]
        bare = "litellm:\n  modelProvider: vertex_ai\n"
        self.assertNotEqual(_render(bare, no_project).returncode, 0)
        with_ref = bare + "  vertex:\n    projectSecretRef: {name: s, key: k}\n"
        proc = _render(with_ref, no_project)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_vertex_refs_unset_leave_the_pod_spec_alone(self) -> None:
        _, deployment = self._documents(_VERTEX_VALUES)
        container = self._container(deployment)
        names = {e["name"] for e in container["env"]}
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", names)
        self.assertEqual(
            {"name": "VERTEXAI_PROJECT", "value": "test-project"},
            next(e for e in container["env"] if e["name"] == "VERTEXAI_PROJECT"),
        )
        volumes = {v["name"] for v in deployment["spec"]["template"]["spec"]["volumes"]}
        self.assertNotIn("vertex-sa-vol", volumes)

    def test_vertex_refs_fail_for_another_provider_or_without_a_key(self) -> None:
        self._fails(
            "litellm:\n  vertex:\n    credentialsSecretRef: {name: s, key: k}\n",
            "read only for vertex_ai",
        )
        self._fails(_VERTEX_VALUES + "    projectSecretRef: {name: s}\n", "without a key")


if __name__ == "__main__":
    unittest.main()
