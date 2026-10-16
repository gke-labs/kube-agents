"""The agent-api-auth sidecar's default resources are declared twice: as the
agentAPIAuth* constants in the operator, and as the kube-agents.agentAPIAuthDefaults
helper the chart's quota preflight reads for its footprint delta. The operator is
the source of truth; this test fails if the chart's copy drifts from it, so the
footprint delta cannot silently subtract the wrong default.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
OPERATOR = REPO_ROOT / "k8s-operator/internal/controller/platformagent_manifests.go"
CHART_HELPERS = REPO_ROOT / "charts/kube-agents/templates/_helpers.tpl"

# (chart side, chart key, operator constant) for each default the chart merges over.
DEFAULTS = (
    ("requests", "cpu", "agentAPIAuthCPURequest"),
    ("requests", "memory", "agentAPIAuthMemoryRequest"),
    ("limits", "cpu", "agentAPIAuthCPULimit"),
    ("limits", "memory", "agentAPIAuthMemoryLimit"),
    ("limits", "ephemeral-storage", "agentAPIAuthEphemeralStorageLimit"),
)


def operator_defaults() -> dict[str, str]:
    text = OPERATOR.read_text(encoding="utf-8")
    out = {}
    for _, _, name in DEFAULTS:
        found = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if found is None:
            raise AssertionError(f"{OPERATOR.relative_to(REPO_ROOT)} declares no {name}")
        out[name] = found.group(1)
    return out


def chart_defaults() -> dict[tuple[str, str], str]:
    text = CHART_HELPERS.read_text(encoding="utf-8")
    block = re.search(
        r'\{\{- define "kube-agents\.agentAPIAuthDefaults" -\}\}(.*?)\{\{- end \}\}',
        text,
        re.DOTALL,
    )
    if block is None:
        raise AssertionError("_helpers.tpl declares no kube-agents.agentAPIAuthDefaults")
    out = {}
    for side in ("requests", "limits"):
        found = re.search(rf'"{side}" \(dict ([^)]*)\)', block.group(1))
        if found is None:
            raise AssertionError(f"agentAPIAuthDefaults carries no {side}")
        for key, value in re.findall(r'"([^"]+)" "([^"]+)"', found.group(1)):
            out[(side, key)] = value
    return out


class AgentAPIAuthSizingParity(unittest.TestCase):
    def test_chart_defaults_match_the_operator_constants(self) -> None:
        op = operator_defaults()
        chart = chart_defaults()
        for side, key, name in DEFAULTS:
            self.assertIn((side, key), chart, f"agentAPIAuthDefaults missing {side}.{key}")
            self.assertEqual(
                chart[(side, key)], op[name],
                f"{side}.{key}: chart {chart[(side, key)]!r} != operator {name} {op[name]!r}; "
                "the footprint delta would subtract the wrong default",
            )
        # No extra keys beyond the five the operator declares.
        self.assertEqual(len(chart), len(DEFAULTS), f"agentAPIAuthDefaults has extra keys: {chart}")


if __name__ == "__main__":
    unittest.main()
