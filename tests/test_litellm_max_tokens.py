"""`litellm.maxTokens`: the gateway's output-token budget, rendered or not.

Two things have to hold. Unset (and 0) must render nothing, so an existing
install's ConfigMap and its rollout checksum are byte-identical and the
gateway does not roll on upgrade. Set, the value must land as an integer
`max_tokens` under every `model_list` alias, because the three are one
upstream model and a backend with one combined prompt-plus-output budget
refuses whichever alias arrives without it. The render cases need `helm` and
skip without it, as `test_litellm_redaction.py`'s do.
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
_VALUE = "litellm.maxTokens"
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
class TestMaxTokensRender(unittest.TestCase):
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

    def test_unset_and_zero_render_no_key_and_the_same_configmap(self) -> None:
        unset_cm, unset_deploy = self._documents()
        zero_cm, zero_deploy = self._documents(["--set", f"{_VALUE}=0"])
        self.assertNotIn("max_tokens", unset_cm["data"]["config.yaml"])
        self.assertEqual(unset_cm, zero_cm)
        self.assertEqual(self._checksum(unset_deploy), self._checksum(zero_deploy))

    def test_a_value_lands_as_an_integer_under_every_alias_and_rolls_the_gateway(self) -> None:
        unset_cm, unset_deploy = self._documents()
        set_cm, set_deploy = self._documents(["--set", f"{_VALUE}=4096"])
        params = self._params_by_alias(set_cm)
        # The third alias is the concrete model name, whatever the provider's
        # default is; every entry present has to carry the budget.
        self.assertEqual(len(params), 3)
        for alias in _ALIASES:
            self.assertIn(alias, params)
        for alias, entry in params.items():
            with self.subTest(alias=alias):
                self.assertIs(type(entry["max_tokens"]), int)
                self.assertEqual(entry["max_tokens"], 4096)
        # Only the key was added: the model routing is untouched.
        for alias, entry in self._params_by_alias(unset_cm).items():
            self.assertEqual(params[alias]["model"], entry["model"])
        self.assertNotEqual(self._checksum(unset_deploy), self._checksum(set_deploy))

    def test_a_values_file_number_renders_the_same_as_a_set_flag(self) -> None:
        # Terraform hands the chart a yamlencoded values document, not --set,
        # and Helm reads a YAML number through a different path than a flag.
        set_cm, _ = self._documents(["--set", f"{_VALUE}=512"])
        file_cm, _ = self._documents(values_text="litellm:\n  maxTokens: 512\n")
        self.assertEqual(set_cm, file_cm)

    def test_a_string_or_a_negative_value_fails_the_schema(self) -> None:
        for args, needle in (
            (["--set-string", f"{_VALUE}=4096"], "want integer"),
            (["--set", f"{_VALUE}=-1"], "minimum"),
        ):
            with self.subTest(args=args):
                proc = self._render(args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("/litellm/maxTokens", proc.stderr)
                self.assertIn(needle, proc.stderr)


if __name__ == "__main__":
    unittest.main()
