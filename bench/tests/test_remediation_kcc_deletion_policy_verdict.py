"""remediation-kcc-stateful-deletion-policy-abandon reads three verdict lines, not prose.

The objective passes when three standalone declaration lines state the verdict,
the KCC deletion-policy decision, and the public IPv4 decision, and no line
states a wrong decision. ``report_contains`` with ``fold_decoration: true``
lowercases the report, strips emphasis/backticks, and folds leading list/quote
markers and trailing stops, while leaving hedges (``?``, extra words) on the
line so hedged or negated lines do not count.
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
    / "remediation-kcc-stateful-deletion-policy-abandon"
    / "task.yaml"
)
CHECK = "verdict-and-kcc-deletion-and-network-safeguards-are-named"
TIMEOUT_SEC = 5.0

VERDICT = "VERDICT: MISSING-SAFEGUARDS"
POLICY = "DELETION-POLICY: ABANDON"
IPV4 = "PUBLIC-IPV4: DISABLE"

WRONG_VERDICT = "VERDICT: SAFE-TO-MERGE"
WRONG_POLICIES = ("DELETION-POLICY: DELETE", "DELETION-POLICY: NONE-NEEDED")
WRONG_IPV4 = "PUBLIC-IPV4: KEEP-ENABLED"


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


def test_all_three_verdict_lines_pass():
    for report in (
        f"{VERDICT}\n{POLICY}\n{IPV4}\n\nAdd the abandon annotation and disable public IPv4.",
        f"**{VERDICT}**\n**{POLICY}**\n**{IPV4}**\n",
        f"`{VERDICT}`\n`{POLICY}`\n`{IPV4}`\n",
        f"1. {VERDICT}\n2. {POLICY}\n3. {IPV4}\n",
        f"> {VERDICT}\n> {POLICY}\n> {IPV4}\n",
        f"{VERDICT}.\n{POLICY}.\n{IPV4}.\n",
        # A sentence mentioning a rejected verdict in prose is not a verdict line.
        f"{VERDICT}\n{POLICY}\n{IPV4}\n\n{WRONG_VERDICT} is ruled out for production.\n",
    ):
        assert _passes(report), report


def test_wrong_missing_hedged_or_prose_only_fails():
    for report in (
        f"{WRONG_VERDICT}\n{POLICY}\n{IPV4}\n",
        f"{VERDICT}\n{WRONG_POLICIES[0]}\n{IPV4}\n",
        f"{VERDICT}\n{WRONG_POLICIES[1]}\n{IPV4}\n",
        f"{VERDICT}\n{POLICY}\n{WRONG_IPV4}\n",
        # Missing any one of the three lines fails.
        f"{VERDICT}\n{IPV4}\n",
        f"{VERDICT}\n{POLICY}\n",
        f"{POLICY}\n{IPV4}\n",
        # Duplicate conflicting verdict line fails.
        f"{VERDICT}\n{POLICY}\n{IPV4}\n{WRONG_VERDICT}\n",
        f"{VERDICT}\n{POLICY}\n{IPV4}\n2. **{WRONG_POLICIES[1]}**\n",
        # Hedged or negated lines do not count as verdicts.
        f"{VERDICT}?\n{POLICY}\n{IPV4}\n",
        f"{VERDICT}\n{POLICY} (not needed)\n{IPV4}\n",
        f"{VERDICT}\n{POLICY}\n{IPV4} is optional\n",
        # Prose-only without declaration lines fails.
        "Add cnrm.cloud.google.com/deletion-policy: abandon and set ipv4Enabled: false.",
    ):
        assert not _passes(report), report
