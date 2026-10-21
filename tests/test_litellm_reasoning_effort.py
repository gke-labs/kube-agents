"""`litellm.reasoningEffort`: the gateway's default reasoning effort, rendered or not.

Two things have to hold. Unset (and empty) must render nothing, so an existing
install's ConfigMap and its rollout checksum are byte-identical and the
gateway does not roll on upgrade. Set, the value must land as
`reasoning_effort` under every `model_list` alias, because the three are one
upstream model and a caller that names `hermes-agent` or the concrete model
should get the same thinking as one that names `model-default`. The render
cases need `helm` and skip without it, as `test_litellm_max_tokens.py`'s do.
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
_VALUE = "litellm.reasoningEffort"
_ALIASES = ("model-default", "hermes-agent")
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


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestReasoningEffortRender(unittest.TestCase):
    @staticmethod
    def _render(extra_args: list[str] | None = None, values_text: str | None = None):
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

    def _documents(self, extra_args: list[str] | None = None, values_text: str | None = None):
        proc = self._render(extra_args, values_text)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        by_kind = {d["kind"]: d for d in documents if d["kind"] in ("ConfigMap", "Deployment")}
        return by_kind["ConfigMap"], by_kind["Deployment"]

    @staticmethod
    def _checksum(deployment: dict) -> str:
        return deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    @staticmethod
    def _params_by_alias(configmap: dict) -> dict:
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        return {entry["model_name"]: entry["litellm_params"] for entry in config["model_list"]}

    def test_unset_and_empty_render_no_key_and_the_same_configmap(self) -> None:
        unset_cm, unset_deploy = self._documents()
        empty_cm, empty_deploy = self._documents(["--set-string", f"{_VALUE}="])
        self.assertNotIn("reasoning_effort", unset_cm["data"]["config.yaml"])
        self.assertEqual(unset_cm, empty_cm)
        self.assertEqual(self._checksum(unset_deploy), self._checksum(empty_deploy))

    def test_a_value_lands_under_every_alias_and_rolls_the_gateway(self) -> None:
        for provider in ("gemini", "vertex_ai", "anthropic", "openai"):
            with self.subTest(provider=provider):
                base = ["--set-string", f"litellm.modelProvider={provider}"]
                unset_cm, unset_deploy = self._documents(base)
                set_cm, set_deploy = self._documents([*base, "--set-string", f"{_VALUE}=high"])
                params = self._params_by_alias(set_cm)
                # The third alias is the concrete model name, whatever the
                # provider's default is; every entry present has to carry it.
                self.assertEqual(len(params), 3)
                for alias in _ALIASES:
                    self.assertIn(alias, params)
                for alias, entry in params.items():
                    with self.subTest(alias=alias):
                        self.assertEqual(entry["reasoning_effort"], "high")
                # Only the key was added: the rest of each alias is untouched.
                for alias, entry in self._params_by_alias(unset_cm).items():
                    self.assertEqual({k: v for k, v in params[alias].items() if k != "reasoning_effort"}, entry)
                self.assertNotEqual(self._checksum(unset_deploy), self._checksum(set_deploy))

    def test_it_sits_beside_max_tokens_and_the_openai_compatible_keys(self) -> None:
        set_cm, _ = self._documents(
            [
                "--set-string",
                "litellm.modelProvider=openai_compatible",
                "--set-string",
                "litellm.modelDefaultName=google/gemma-3-27b-it",
                "--set-string",
                "litellm.openaiCompatible.apiBase=http://vllm.models.svc.cluster.local:8000/v1",
                "--set",
                "litellm.openaiCompatible.targetPort=8000",
                "--set",
                "litellm.maxTokens=4096",
                "--set-string",
                f"{_VALUE}=low",
            ]
        )
        for alias, entry in self._params_by_alias(set_cm).items():
            with self.subTest(alias=alias):
                self.assertEqual(entry["model"], "hosted_vllm/google/gemma-3-27b-it")
                self.assertEqual(entry["max_tokens"], 4096)
                self.assertEqual(entry["reasoning_effort"], "low")

    def test_a_values_file_renders_the_same_as_a_set_flag(self) -> None:
        # Terraform hands the chart a yamlencoded values document, not --set.
        set_cm, _ = self._documents(["--set-string", f"{_VALUE}=medium"])
        file_cm, _ = self._documents(values_text="litellm:\n  reasoningEffort: medium\n")
        self.assertEqual(set_cm, file_cm)

    def test_a_level_outside_the_enum_fails_the_schema(self) -> None:
        # "minimal" is excluded on purpose: Vertex AI answers it with a 400 on
        # Gemini 3.x Flash (deploy/shared/defaults/config.yaml, auxiliary).
        # Only the value's name is asserted: the rest of the message belongs to
        # Helm's schema library, whose wording changed between Helm 3 releases.
        for args in (
            ["--set-string", f"{_VALUE}=minimal"],
            ["--set-string", f"{_VALUE}=HIGH"],
            ["--set", f"{_VALUE}=true"],
        ):
            with self.subTest(args=args):
                proc = self._render(args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("reasoningEffort", proc.stderr)


if __name__ == "__main__":
    unittest.main()
