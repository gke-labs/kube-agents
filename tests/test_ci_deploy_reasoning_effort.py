"""hack/ci-deploy.sh takes the reasoning effort a run asks for and gives it to the chart.

A certification run names its model and the effort the gateway asks for beside
it (gke-labs/kube-agents#3332). The script reads `EVAL_REASONING_EFFORT` from
the job environment, refuses anything but low, medium, high or empty before
the lease is spent, and passes the value as `litellm.reasoningEffort`, the
chart value that renders `reasoning_effort` under every model alias. The
functional cases run the block cut out between its marker comments.
"""

import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CHART = _REPO_ROOT / "charts" / "kube-agents"

_BEGIN_MARKER = "# --- reasoning effort: begin"
_END_MARKER = "# --- reasoning effort: end"
_HELM_LINE = '--set-string "litellm.reasoningEffort=${EVAL_REASONING_EFFORT}"'
_MODEL_HELM_LINE = '--set-string "litellm.modelDefaultName=${MODEL_DEFAULT_NAME}"'
# The CI health digest quotes a FATAL line cut at this many characters.
_FATAL_LINE_MAX_CHARS = 200
_PROVIDER = "openai_compatible"
_MODEL = "served-model"
_API_BASE = "http://llm.inference.internal/served/v1"

_HARNESS = """set -euo pipefail
MODEL_PROVIDER={provider}
MODEL_DEFAULT_NAME={model}
eval "$(sed -n '/^{begin}/,/^{end}/p' "$CI_DEPLOY")"
printf 'EFFORT=[%s]\\n' "${{EVAL_REASONING_EFFORT}}"
"""


class CiDeployReasoningEffortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _CI_DEPLOY.read_text()

    def _run(self, effort: str | None) -> subprocess.CompletedProcess:
        harness = _HARNESS.format(provider=_PROVIDER, model=_MODEL, begin=_BEGIN_MARKER, end=_END_MARKER)
        overrides = {"CI_DEPLOY": str(_CI_DEPLOY)}
        absent: tuple[str, ...] = ()
        if effort is None:
            absent = ("EVAL_REASONING_EFFORT",)
        else:
            overrides["EVAL_REASONING_EFFORT"] = effort
        env = get_isolated_test_env(overrides=overrides, absent=absent)
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env)

    def test_the_block_sits_between_its_markers_once(self) -> None:
        self.assertEqual(self.text.count(_BEGIN_MARKER), 1)
        self.assertEqual(self.text.count(_END_MARKER), 1)
        self.assertLess(self.text.index(_BEGIN_MARKER), self.text.index(_END_MARKER))

    def test_the_helm_release_passes_the_effort_beside_the_model(self) -> None:
        self.assertIn(_HELM_LINE, self.text, "hack/ci-deploy.sh must hand EVAL_REASONING_EFFORT to litellm.reasoningEffort")
        self.assertLess(self.text.index(_MODEL_HELM_LINE), self.text.index(_HELM_LINE))

    def test_unset_and_empty_mean_the_model_default(self) -> None:
        for effort in (None, ""):
            with self.subTest(effort=effort):
                proc = self._run(effort)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("EFFORT=[]", proc.stdout)
                self.assertIn("reasoning effort: model default", proc.stdout)

    def test_low_medium_and_high_pass_and_are_echoed(self) -> None:
        for effort in ("low", "medium", "high"):
            with self.subTest(effort=effort):
                proc = self._run(effort)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn(f"EFFORT=[{effort}]", proc.stdout)
                self.assertIn(f"{_PROVIDER}/{_MODEL}, reasoning effort: {effort}", proc.stdout)

    def test_any_other_value_stops_before_the_deploy_with_a_short_fatal_line(self) -> None:
        for effort in ("xhigh", "max", "Low", "low medium", "minimal"):
            with self.subTest(effort=effort):
                proc = self._run(effort)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertNotIn("EFFORT=[", proc.stdout)
                fatal = [line for line in proc.stderr.splitlines() if line.startswith("FATAL:")]
                self.assertEqual(len(fatal), 1, proc.stderr)
                self.assertLessEqual(len(fatal[0]), _FATAL_LINE_MAX_CHARS)
                self.assertIn("EVAL_REASONING_EFFORT", fatal[0])
                self.assertNotIn(_API_BASE, proc.stderr)

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_the_chart_renders_the_effort_under_every_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = subprocess.run(
                [
                    "helm", "template", "kube-agents", str(_CHART),
                    "--namespace", "kubeagents-system",
                    "--set", "platformAgent.harness.projectId=my-proj",
                    "--set", "platformAgent.harness.clusterName=my-cluster",
                    "--set", "platformAgent.harness.location=us-central1",
                    "--set", "operator.enabled=false",
                    "--set-string", f"litellm.modelProvider={_PROVIDER}",
                    "--set-string", f"litellm.modelDefaultName={_MODEL}",
                    "--set-string", f"litellm.openaiCompatible.apiBase={_API_BASE}",
                    "--set-string", "litellm.openaiCompatible.egressCIDRs[0]=10.128.0.51/32",
                    "--set-string", "litellm.reasoningEffort=low",
                    "--show-only", "templates/litellm.yaml",
                ],
                capture_output=True, text=True, cwd=tmp,
            )
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        configmaps = [
            doc for doc in yaml.safe_load_all(rendered.stdout)
            if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "litellm-config"
        ]
        self.assertEqual(len(configmaps), 1, rendered.stdout[:500])
        config = yaml.safe_load(configmaps[0]["data"]["config.yaml"])
        aliases = config["model_list"]
        self.assertTrue(aliases)
        for entry in aliases:
            self.assertEqual(entry["litellm_params"].get("reasoning_effort"), "low", entry)


if __name__ == "__main__":
    unittest.main()
