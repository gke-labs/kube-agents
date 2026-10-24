"""autoscaler-zonal-stockout-cooldown-scope-probe reads the verdict line, not prose.

The objective passes when a line opens with the zonal verdict and no line
opens with the other one. ``report_contains`` lowercases the report and drops
emphasis and backticks before the patterns run, so a bolded, quoted or
bulleted verdict line counts, and the verdict named inside a sentence does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "autoscaler-zonal-stockout-cooldown-scope-probe" / "task.yaml"
CHECK = "the-verdict-is-the-zonal-one"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == CHECK]
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**check["check"]).verify(TIMEOUT_SEC).success


def test_the_zonal_verdict_line_passes():
    for report in (
        "VERDICT: STILL-TRIES-N2-SPOT\n\nFrom 1.36.3-gke.1244000 the cooldown is zonal.",
        "**VERDICT: STILL-TRIES-N2-SPOT**\n\nOnly us-central1-a backs off.",
        "`VERDICT: STILL-TRIES-N2-SPOT`\nOnly us-central1-a backs off.",
        "> VERDICT: STILL-TRIES-N2-SPOT\n",
        "1. VERDICT: STILL-TRIES-N2-SPOT\n",
        "VERDICT: \u201cSTILL-TRIES-N2-SPOT\u201d\n",
        "Here is the answer.\nVERDICT: STILL-TRIES-N2-SPOT\n",
    ):
        assert _passes(report), report


def test_the_global_verdict_or_a_verdict_in_prose_fails():
    for report in (
        "VERDICT: FALLS-BACK-TO-E2\n\nThe whole tier cools down for ~5 minutes.",
        "VERDICT: STILL-TRIES-N2-SPOT\nVERDICT: FALLS-BACK-TO-E2\n",
        "VERDICT: STILL-TRIES-N2-SPOT\n2. **VERDICT: FALLS-BACK-TO-E2**\n",
        "It would not be VERDICT: STILL-TRIES-N2-SPOT on older versions.",
        "The pod falls back to e2 on-demand; it never still-tries-n2-spot.",
    ):
        assert not _passes(report), report
