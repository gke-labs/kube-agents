"""observability-konnectivity-agent-saturation reads standalone verdict lines, not prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "observability-konnectivity-agent-saturation" / "task.yaml"
CHECK = "konnectivity-saturation-cause-and-component-stated"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    assert check["check"]["type"] == "all"
    transcript.set(report, [], final_message=report)
    return all(
        ReportContainsVerifier(**part).verify(TIMEOUT_SEC).success
        for part in check["check"]["checks"]
    )


def test_structured_verdict_lines_pass():
    for report in (
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KONNECTIVITY-AGENT\n\nShort explanation follows.",
        "**CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION**\n**COMPONENT: KONNECTIVITY-AGENT**\n",
        "`CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION`\n`COMPONENT: KONNECTIVITY-AGENT`\n",
        "1. CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\n2. COMPONENT: KONNECTIVITY-AGENT\n",
        "> CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\n> COMPONENT: KONNECTIVITY-AGENT\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION.\nCOMPONENT: KONNECTIVITY-AGENT.\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KONNECTIVITY-AGENT\n\nCAUSE: ETCD-STORAGE-CORRUPTION is ruled out by the cluster evidence.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        "CAUSE: ETCD-STORAGE-CORRUPTION\nCOMPONENT: KONNECTIVITY-AGENT\n",
        "CAUSE: CNI-POD-TO-POD-FAILURE\nCOMPONENT: KONNECTIVITY-AGENT\n",
        "CAUSE: WORKLOAD-OOM-CASCADE\nCOMPONENT: KONNECTIVITY-AGENT\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KUBE-DNS\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: METRICS-SERVER-ONLY\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: ETCD\n",
        "COMPONENT: KONNECTIVITY-AGENT\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KONNECTIVITY-AGENT\nCAUSE: ETCD-STORAGE-CORRUPTION\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KONNECTIVITY-AGENT\n1. **CAUSE: ETCD-STORAGE-CORRUPTION**\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KONNECTIVITY-AGENT\nCOMPONENT: KUBE-DNS\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KONNECTIVITY-AGENT\n2. **COMPONENT: KUBE-DNS**\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION?\nCOMPONENT: KONNECTIVITY-AGENT\n",
        "CAUSE: CONTROL-PLANE-EGRESS-TUNNEL-SATURATION\nCOMPONENT: KONNECTIVITY-AGENT (maybe not)\n",
        "The timeouts on logs, exec, webhooks, and aggregated API requests point to konnectivity-agent tunnel saturation, not etcd.",
    ):
        assert not _passes(report), report
