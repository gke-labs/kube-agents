"""remediation-kcc-stateful-deletion-policy-abandon reads the verdict line and required fixes.

The objective passes when a line opens with ``VERDICT: MISSING-SAFEGUARDS``, no
line opens with ``VERDICT: SAFE-TO-MERGE``, and the report names both
``cnrm.cloud.google.com/deletion-policy: abandon`` and disabling public IPv4
(``ipv4Enabled: false`` or private-only IP guidance). ``report_contains``
lowercases the report, drops emphasis and backticks, and folds leading list or
quote markers when ``fold_decoration: true`` is set.
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
CHECK = "the-review-flags-missing-deletion-policy-and-public-ip"
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


def test_missing_safeguards_verdict_with_annotation_and_network_fixes_passes():
    for report in (
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Required fixes:\n"
            "1. Add `metadata.annotations` with `cnrm.cloud.google.com/deletion-policy: abandon`.\n"
            "2. Under `spec.settings.ipConfiguration`, set `ipv4Enabled: false` and configure `privateNetworkRef`."
        ),
        (
            "**VERDICT: MISSING-SAFEGUARDS**\n\n"
            "- Set `cnrm.cloud.google.com/deletion-policy: \"abandon\"` on the SQLInstance.\n"
            "- Disable the public IPv4 address (`ipv4Enabled: false`)."
        ),
        (
            "`VERDICT: MISSING-SAFEGUARDS`\n"
            "Add annotation `cnrm.cloud.google.com/deletion-policy: abandon` and turn off public IP by using a private network."
        ),
        (
            "> VERDICT: MISSING-SAFEGUARDS\n\n"
            "Set `cnrm.cloud.google.com/deletion-policy` to `abandon` and use a private-only IP instead of `ipv4Enabled: true`."
        ),
        (
            "1. **VERDICT: MISSING-SAFEGUARDS** — This production SQLInstance is not safe to merge.\n"
            "2. Add `cnrm.cloud.google.com/deletion-policy: abandon`.\n"
            "3. Set `ipv4Enabled: false` under `spec.settings.ipConfiguration`."
        ),
    ):
        assert _passes(report), report


def test_safe_to_merge_missing_fixes_or_inline_verdict_fails():
    for report in (
        # Wrong verdict line.
        (
            "VERDICT: SAFE-TO-MERGE\n\n"
            "Even though `cnrm.cloud.google.com/deletion-policy: abandon` and `ipv4Enabled: false` are optional, this is fine."
        ),
        # Both verdict lines present on their own lines.
        (
            "VERDICT: MISSING-SAFEGUARDS\n"
            "VERDICT: SAFE-TO-MERGE\n"
            "Set `cnrm.cloud.google.com/deletion-policy: abandon` and `ipv4Enabled: false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n"
            "2. **VERDICT: SAFE-TO-MERGE**\n"
            "Set `cnrm.cloud.google.com/deletion-policy: abandon` and `ipv4Enabled: false`."
        ),
        # Verdict only mentioned inline inside a negated sentence, not at the start of a line.
        (
            "I will not say VERDICT: MISSING-SAFEGUARDS here.\n"
            "Set `cnrm.cloud.google.com/deletion-policy: abandon` and `ipv4Enabled: false`."
        ),
        # Missing deletion-policy: abandon fix.
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Set `spec.settings.ipConfiguration.ipv4Enabled: false` to disable public IP."
        ),
        # Deletion policy mentioned without abandon.
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Set `cnrm.cloud.google.com/deletion-policy: none` and `ipv4Enabled: false`."
        ),
        # Missing network / public IP fix (only quotes ipv4Enabled: true without fix).
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon` to `metadata.annotations`."
        ),
        # Hedged or negated verdict line.
        (
            "VERDICT: MISSING-SAFEGUARDS or SAFE-TO-MERGE\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon` and `ipv4Enabled: false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS?\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon` and `ipv4Enabled: false`."
        ),
        # Negated deletion-policy: abandon on the same line.
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Set `cnrm.cloud.google.com/deletion-policy` to `none` (not `abandon`) and `ipv4Enabled: false`."
        ),
    ):
        assert not _passes(report), report
