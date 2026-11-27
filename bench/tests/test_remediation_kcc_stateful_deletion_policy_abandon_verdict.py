"""remediation-kcc-stateful-deletion-policy-abandon reads the verdict line and required fixes.

The objective passes when a line opens with ``VERDICT: MISSING-SAFEGUARDS``, no
line opens with ``VERDICT: SAFE-TO-MERGE``, and the report names both
``cnrm.cloud.google.com/deletion-policy: abandon`` and disabling public IPv4
(``ipv4Enabled: false`` or private-only IP guidance). ``report_contains``
lowercases the report, drops emphasis and backticks, and folds leading list or
quote markers when ``fold_decoration: true`` is set.
"""

from __future__ import annotations

from conftest import check_passes

CASE = "remediation-kcc-stateful-deletion-policy-abandon"
CHECK = "the-review-flags-missing-deletion-policy-and-public-ip"


def _passes(report: str) -> bool:
    return check_passes(CASE, CHECK, report)


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
        # Prose phrasings for deletion-policy (gap > 40 chars or across a newline) and ipv4Enabled to false.
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add the `cnrm.cloud.google.com/deletion-policy` annotation to `metadata.annotations` with value `abandon`.\n"
            "Set `spec.settings.ipConfiguration.ipv4Enabled` to `false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "- Annotation: `cnrm.cloud.google.com/deletion-policy`\n"
            "  Value: `abandon`\n"
            "- Set `ipv4Enabled` to `false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon` and disable `ipv4Enabled` in `ipConfiguration`."
        ),
        # Problem explanations describing what the manifest lacks, plus change ipv4Enabled: true to false and privateNetworkRef.
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Without `cnrm.cloud.google.com/deletion-policy`, deleting the manifest does not abandon the instance; "
            "add `cnrm.cloud.google.com/deletion-policy: abandon`.\n"
            "The manifest does not disable the public IPv4 address; set `ipv4Enabled: false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "- `VERDICT: SAFE-TO-MERGE` does not apply to this production database.\n"
            "- Add `cnrm.cloud.google.com/deletion-policy: abandon`.\n"
            "- Change `ipv4Enabled: true` to `false` and configure `privateNetworkRef`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon` and configure `spec.settings.ipConfiguration.privateNetworkRef`."
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
        # Negated deletion-policy: abandon (same line, >40-char prose gap, multi-line, or leading negation).
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Set `cnrm.cloud.google.com/deletion-policy` to `none` (not `abandon`) and `ipv4Enabled: false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Set the `cnrm.cloud.google.com/deletion-policy` annotation on `metadata.annotations` to `none` (not `abandon`) and `ipv4Enabled: false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Do not set `cnrm.cloud.google.com/deletion-policy: abandon`; set `ipv4Enabled: false`."
        ),
        # Contradictory or negated network advice.
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon`. A private network is not needed; keep `ipv4Enabled: true`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon`, and do not set `ipv4Enabled: false`."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon`, and don't disable the public IP."
        ),
        (
            "VERDICT: MISSING-SAFEGUARDS\n\n"
            "Add `cnrm.cloud.google.com/deletion-policy: abandon`, and do not configure a private network."
        ),
    ):
        assert not _passes(report), report
