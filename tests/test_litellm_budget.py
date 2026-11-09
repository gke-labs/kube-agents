"""The gateway spend ceiling, litellm.budget.

Off by default and invisible when unset: the ConfigMap and its rollout
checksum are byte-identical to a render without the keys, so an existing
install does not roll on upgrade. Set, it renders
router_settings.provider_budget_config for the one provider every alias routes
to, with the ceiling split evenly across litellm.replicaCount (LiteLLM counts
spend per gateway process). The render cases need `helm` and skip without it,
as `test_litellm_reasoning_fallback.py`'s do.
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
class BudgetRenderTest(unittest.TestCase):
    def _documents(self, values_text: str | None = None):
        proc = _render(values_text)
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

    def _router(self, values_text: str) -> dict:
        configmap, _ = self._documents(values_text)
        return self._config(configmap)["router_settings"]

    def test_unset_zero_and_explicit_defaults_leave_the_configmap_and_checksum_alone(self) -> None:
        unset_cm, unset_deploy = self._documents()
        for explicit in (
            "litellm:\n  budget:\n    usd: 0\n",
            "litellm:\n  budget:\n    usd: 0\n    period: 1d\n",
            # The period alone is inert: only usd above 0 renders anything.
            "litellm:\n  budget:\n    period: 7d\n",
        ):
            with self.subTest(explicit=explicit):
                cm, deploy = self._documents(explicit)
                self.assertEqual(unset_cm["data"], cm["data"])
                self.assertEqual(self._checksum(unset_deploy), self._checksum(deploy))
        text = unset_cm["data"]["config.yaml"]
        for key in ("provider_budget_config", "budget_limit", "time_period", "router_settings"):
            self.assertNotIn(key, text)

    def test_a_budget_renders_the_providers_share_per_replica(self) -> None:
        # The chart default is two replicas, so each holds half the ceiling.
        router = self._router(_VERTEX_VALUES + "  budget:\n    usd: 10\n")
        self.assertEqual(
            router,
            {"provider_budget_config": {"vertex_ai": {"budget_limit": 5.0, "time_period": "1d"}}},
        )

    def test_the_share_follows_the_replica_count_and_the_period_is_passed_through(self) -> None:
        router = self._router(_VERTEX_VALUES + "  replicaCount: 4\n  budget:\n    usd: 3\n    period: 30d\n")
        self.assertEqual(router["provider_budget_config"]["vertex_ai"], {"budget_limit": 0.75, "time_period": "30d"})
        router = self._router(_VERTEX_VALUES + "  replicaCount: 1\n  budget:\n    usd: 2.5\n    period: 12h\n")
        self.assertEqual(router["provider_budget_config"]["vertex_ai"], {"budget_limit": 2.5, "time_period": "12h"})

    def test_the_share_is_a_float_with_a_dot_so_pyyaml_reads_a_number(self) -> None:
        # LiteLLM loads config.yaml with PyYAML, whose YAML 1.1 float needs a
        # dot: "1e-07" would arrive as a string. Four decimals, always a dot.
        configmap, _ = self._documents(_VERTEX_VALUES + "  replicaCount: 3\n  budget:\n    usd: 1\n")
        self.assertIn("budget_limit: 0.3333\n", configmap["data"]["config.yaml"])
        self.assertIsInstance(self._config(configmap)["router_settings"]["provider_budget_config"]["vertex_ai"]["budget_limit"], float)

    def test_the_key_is_the_provider_prefix_every_alias_carries(self) -> None:
        for values, provider in (
            ("litellm:\n  modelProvider: gemini\n", "gemini"),
            ("litellm:\n  modelProvider: anthropic\n", "anthropic"),
            ("litellm:\n  modelProvider: openai\n", "openai"),
            (
                "litellm:\n  modelProvider: openai_compatible\n  modelDefaultName: org/model\n"
                "  openaiCompatible:\n    apiBase: https://ai.example.com/v1\n",
                "hosted_vllm",
            ),
        ):
            with self.subTest(provider=provider):
                configmap, _ = self._documents(values + "  budget:\n    usd: 1\n")
                config = self._config(configmap)
                self.assertEqual(list(config["router_settings"]["provider_budget_config"]), [provider])
                for entry in config["model_list"]:
                    self.assertTrue(entry["litellm_params"]["model"].startswith(provider + "/"))

    def test_a_budget_sits_beside_the_other_router_settings(self) -> None:
        router = self._router(
            _VERTEX_VALUES + "  fallback:\n    reasoningEffort: low\n    timeoutSeconds: 120\n  budget:\n    usd: 1\n"
        )
        self.assertEqual(router["timeout"], 120)
        self.assertIn("fallbacks", router)
        self.assertEqual(router["provider_budget_config"]["vertex_ai"]["budget_limit"], 0.5)

    def test_a_budget_changes_the_rollout_checksum(self) -> None:
        _, base = self._documents(_VERTEX_VALUES)
        _, capped = self._documents(_VERTEX_VALUES + "  budget:\n    usd: 1\n")
        self.assertNotEqual(self._checksum(base), self._checksum(capped))

    def test_a_share_that_rounds_to_zero_fails_the_render(self) -> None:
        # LiteLLM treats a budget_limit of 0 as no limit, so a ceiling that
        # would render as one is refused rather than silently lifted.
        self._fails(_VERTEX_VALUES + "  budget:\n    usd: 0.00009\n", "under 0.0001 USD per replica")

    def test_a_budget_without_a_period_fails_the_render(self) -> None:
        self._fails(_VERTEX_VALUES + "  budget:\n    usd: 1\n    period: \"\"\n", "is not a LiteLLM duration")

    def test_the_schema_refuses_a_negative_budget_a_bad_period_and_a_string(self) -> None:
        self._fails(_VERTEX_VALUES + "  budget:\n    usd: -1\n", "litellm.budget.usd")
        self._fails(_VERTEX_VALUES + "  budget:\n    usd: 1\n    period: 1x\n", "litellm.budget.period")
        self._fails(_VERTEX_VALUES + "  budget:\n    usd: 1\n    period: 0d\n", "litellm.budget.period")
        self._fails(_VERTEX_VALUES + "  budget:\n    usd: \"0.5\"\n", "litellm.budget.usd")

    def test_the_default_render_carries_no_router_settings_the_kustomize_base_lacks(self) -> None:
        unset_cm, _ = self._documents()
        base = yaml.safe_load((_REPO_ROOT / "k8s-operator/config/integrations/litellm/base/config.yaml").read_text())
        self.assertEqual(set(self._config(unset_cm)), set(base))


class BudgetPeriodGrammarParityTest(unittest.TestCase):
    """The period's duration grammar is read by four machines: the chart's
    schema, the template's own check (for a values file that skips the
    schema), the tfvars generator and the Terraform example's validation.
    The schema's pattern is the source; the other three must spell the same
    grammar, or a period one front door accepts is refused by the next."""

    _SCHEMA = _CHART / "values.schema.json"
    _HELPERS = _CHART / "templates" / "_helpers.tpl"
    _INSTALLER = _REPO_ROOT / "scripts" / "installer" / "installer_common.sh"
    _VARIABLES = _REPO_ROOT / "terraform" / "examples" / "full-install" / "variables.tf"

    def _grammar(self) -> str:
        import json

        schema = json.loads(self._SCHEMA.read_text())
        pattern = schema["properties"]["litellm"]["properties"]["budget"]["properties"]["period"]["pattern"]
        # The schema also admits the empty string, the inert default; the
        # other copies see a period only beside an amount, so they take the
        # grammar without that alternative.
        prefix, suffix = "^(", ")?$"
        self.assertTrue(pattern.startswith(prefix) and pattern.endswith(suffix), pattern)
        return "^" + pattern[len(prefix) : -len(suffix)] + "$"

    def test_the_template_the_installer_and_terraform_spell_the_schemas_grammar(self) -> None:
        grammar = self._grammar()
        self.assertIn(f"$budgetPeriodPattern := `{grammar}`", self._HELPERS.read_text())
        self.assertIn(f'[[ "${{1:-}}" =~ {grammar} ]]', self._INSTALLER.read_text())
        self.assertIn(f'can(regex("{grammar}", var.litellm_budget.period))', self._VARIABLES.read_text())


if __name__ == "__main__":
    unittest.main()
