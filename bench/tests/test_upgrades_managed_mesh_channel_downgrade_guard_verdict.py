"""upgrades-managed-mesh-channel-downgrade-guard reads the verdict line and skew reasoning.

The objective passes when a line opens with ``VERDICT: BLOCK-CHANNEL-DOWNGRADE``,
no line opens with ``VERDICT: SAFE-TO-SWITCH`` or hedges both verdicts, and the
explanation names the downgrade outside the verdict token along with at least
one of the ``sidecar``, ``nack``, or ``xds`` skew/mismatch terms.
``report_contains`` lowercases the report, drops emphasis and backticks, and
folds leading and trailing line decoration before the patterns run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "upgrades-managed-mesh-channel-downgrade-guard"
    / "task.yaml"
)
CHECK = "the-verdict-blocks-the-channel-downgrade-and-names-the-skew-risk"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**check["check"]).verify(TIMEOUT_SEC).success


def test_blocking_verdict_with_downgrade_and_skew_terms_passes():
    for report in (
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Switching from rapid (1.24) to stable (1.22) causes a control-plane downgrade while sidecar proxies stay at 1.24.",
        "**VERDICT: BLOCK-CHANNEL-DOWNGRADE**\n\n"
        "Direct channel changes to an older minor downgrade istiod and trigger Envoy NACK loops.",
        "`VERDICT: BLOCK-CHANNEL-DOWNGRADE`\n"
        "This downgrades the managed mesh control plane and risks xDS configuration rejections.",
        "> VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Avoid a direct control-plane downgrade when 1.24 sidecars are already injected.",
        "1. VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "A control-plane downgrade below 1.24 sidecars is unsupported.",
        "VERDICT: \u201cBLOCK-CHANNEL-DOWNGRADE\u201d\n\n"
        "The channel switch is a control-plane downgrade that causes sidecar skew.",
        "**VERDICT: BLOCK-CHANNEL-DOWNGRADE.** Switching directly from rapid to stable downgrades the control plane while sidecars remain at 1.24.",
    ):
        assert _passes(report), report


def test_safe_verdict_prose_only_or_missing_reasoning_fails():
    for report in (
        "VERDICT: SAFE-TO-SWITCH\n\n"
        "Managed ASM handles the channel downgrade and sidecar skew automatically.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\nVERDICT: SAFE-TO-SWITCH\n\n"
        "This downgrade causes sidecar version skew.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n2. **VERDICT: SAFE-TO-SWITCH**\n\n"
        "This downgrade causes Envoy NACK loops.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE or SAFE-TO-SWITCH\n\n"
        "This downgrade causes sidecar version skew.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE?\n\n"
        "This downgrade causes sidecar version skew.",
        "It would not be VERDICT: BLOCK-CHANNEL-DOWNGRADE if both channels served 1.24, avoiding a downgrade and sidecar skew.",
        "Do not apply this change: switching channels downgrades the control plane and causes sidecar skew and Envoy NACK loops.",
        # Missing downgrade explanation outside the verdict token itself.
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Workload sidecars are on 1.24 while stable serves 1.22.",
        # Missing sidecar / nack / xds skew or mismatch terms.
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Switching from rapid to stable is an unsupported control-plane downgrade from 1.24 to 1.22.",
    ):
        assert not _passes(report), report
