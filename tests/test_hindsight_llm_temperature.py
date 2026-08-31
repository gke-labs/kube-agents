"""The hindsight-api LLM temperature contract, in both manifest copies.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

Hindsight sends a `temperature` on every LLM call it makes. A model that
refuses an explicit one answers 400, which reaches the agent as
`500 Fact extraction failed` — a retain that stores nothing while reporting a
server error, so memory is inert for the whole install rather than degraded.
`HINDSIGHT_API_LLM_TEMPERATURE=none` is Hindsight's own sentinel for omitting
the parameter; the rationale, and what the omission costs, is in `api.yaml`'s
comment beside the variable.

The Deployment exists twice on purpose: `k8s-operator/config/integrations/`
is the dev path and `charts/kube-agents/templates/` is what an install renders.
They are kept in step by hand, and `make chart-check` does not cover them — it
syncs CRDs and RBAC only. So losing the variable from either copy is a silent
edit whose symptom appears only on whichever install path lost it, which is
what this pins.

The chart copy is read as text rather than parsed: it is a Helm template, and
its `{{ }}` actions are not YAML.
"""

import pathlib
import re
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_API_YAML = _ROOT / "k8s-operator" / "config" / "integrations" / "hindsight" / "api.yaml"
_CHART_TEMPLATE = _ROOT / "charts" / "kube-agents" / "templates" / "hindsight.yaml"

_VAR = "HINDSIGHT_API_LLM_TEMPERATURE"

# Hindsight's own set of values that mean "send no temperature at all"
# (hindsight_api/config.py, _TEMPERATURE_OMIT_VALUES). Anything outside it is
# parsed as a float, and an unparseable value raises at startup rather than
# falling back — so a typo here is a CrashLoop, not a degraded install.
_OMIT_VALUES = {"", "none", "default", "off", "unset"}


def _api_container():
    docs = [d for d in yaml.safe_load_all(_API_YAML.read_text()) if d]
    deployments = [
        d
        for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "hindsight-api"
    ]
    assert len(deployments) == 1, f"expected one hindsight-api Deployment, got {len(deployments)}"
    containers = deployments[0]["spec"]["template"]["spec"]["containers"]
    by_name = {c["name"]: c for c in containers}
    assert "api" in by_name, f"no container named 'api' in {sorted(by_name)}"
    return by_name["api"]


class HindsightLLMTemperature(unittest.TestCase):
    def test_the_integration_manifest_omits_the_temperature(self):
        env = {e["name"]: e.get("value") for e in _api_container().get("env", [])}
        self.assertIn(
            _VAR,
            env,
            f"{_VAR} is missing from api.yaml; Hindsight will send a temperature "
            "and a model that refuses one answers 400, which surfaces as "
            "'500 Fact extraction failed' with nothing stored",
        )
        self.assertIn(
            str(env[_VAR]),
            _OMIT_VALUES,
            f"{_VAR}={env[_VAR]!r} is not one of Hindsight's omit sentinels "
            f"{sorted(_OMIT_VALUES)}; anything else is parsed as a float and "
            "sends a temperature after all",
        )

    def test_the_chart_template_carries_the_same_variable(self):
        # Both copies or neither: an install renders the chart, so a variable
        # present only in the dev manifest fixes nothing for a user.
        text = _CHART_TEMPLATE.read_text()
        match = re.search(
            rf"-\s+name:\s+{_VAR}\s*\n\s+value:\s*(\S+)",
            text,
        )
        self.assertIsNotNone(
            match,
            f"{_VAR} is missing from the chart template; the dev path would be "
            "fixed and every real install still broken",
        )
        self.assertIn(match.group(1).strip("\"'"), _OMIT_VALUES)


if __name__ == "__main__":
    unittest.main()
