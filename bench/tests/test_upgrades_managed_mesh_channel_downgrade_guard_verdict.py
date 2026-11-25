"""upgrades-managed-mesh-channel-downgrade-guard reads the verdict line and skew reasoning.

The objective passes when a line is ``VERDICT: BLOCK-CHANNEL-DOWNGRADE`` on its
own, no line is ``VERDICT: SAFE-TO-SWITCH`` on its own, and the explanation
names the downgrade outside the verdict token along with at least one of the
``sidecar``, ``nack``, or ``xds`` skew/mismatch terms. ``report_contains``
lowercases the report, drops emphasis and backticks, and folds leading and
trailing line decoration before the patterns run. So a bolded, quoted,
bulleted, or period-terminated verdict line counts; a verdict inside a sentence,
or a verdict with a hedge or a cross after it, does not.
"""

from __future__ import annotations

from conftest import check_passes

CASE = "upgrades-managed-mesh-channel-downgrade-guard"
CHECK = "the-verdict-blocks-the-channel-downgrade-and-names-the-skew-risk"


def _passes(report: str) -> bool:
    return check_passes(CASE, CHECK, report)


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
        "**VERDICT: BLOCK-CHANNEL-DOWNGRADE.**\n\n"
        "Switching directly from rapid to stable downgrades the control plane while sidecars remain at 1.24.",
        'VERDICT: "BLOCK-CHANNEL-DOWNGRADE."\n\n'
        "Switching directly from rapid to stable downgrades the control plane while sidecars remain at 1.24.",
        # A sentence that opens with the rejected verdict is prose, not a standalone verdict line.
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "- VERDICT: SAFE-TO-SWITCH is ruled out because switching channels downgrades the control plane below 1.24 sidecars.",
        # A cross after the rejected verdict rejects it; the line is not that verdict.
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n"
        "VERDICT: SAFE-TO-SWITCH \u274c\n\n"
        "Switching channels downgrades the control plane and causes sidecar skew.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Do not recommend, endorse, or apply VERDICT: SAFE-TO-SWITCH because switching channels downgrades the control plane below 1.24 sidecars.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n\n"
        "Do not return e.g. VERDICT: SAFE-TO-SWITCH; switching channels downgrades the control plane and causes sidecar skew.",
    ):
        assert _passes(report), report


def test_safe_verdict_prose_only_or_missing_reasoning_fails():
    for report in (
        "VERDICT: SAFE-TO-SWITCH\n\n"
        "Managed ASM handles the channel downgrade and sidecar skew automatically.",
        'VERDICT: "SAFE-TO-SWITCH"\n\n'
        "Managed ASM handles the channel downgrade and sidecar skew automatically.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\nVERDICT: SAFE-TO-SWITCH\n\n"
        "This downgrade causes sidecar version skew.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE\n2. **VERDICT: SAFE-TO-SWITCH**\n\n"
        "This downgrade causes Envoy NACK loops.",
        # A verdict line with more words, a contradiction, or a hedging/negating mark after it is not a standalone verdict.
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE?\n\n"
        "Switching from rapid to stable downgrades the control plane while sidecars stay at 1.24.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE \u274c\n\n"
        "Switching from rapid to stable downgrades the control plane while sidecars stay at 1.24.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE: \u274c\n\n"
        "Switching from rapid to stable downgrades the control plane while sidecars stay at 1.24.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE \u26a0\ufe0f\n\n"
        "Switching from rapid to stable downgrades the control plane while sidecars stay at 1.24.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE; SAFE-TO-SWITCH\n\n"
        "Switching from rapid to stable downgrades the control plane while sidecars stay at 1.24.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE or SAFE-TO-SWITCH\n\n"
        "Switching from rapid to stable downgrades the control plane while sidecars stay at 1.24.",
        "VERDICT: BLOCK-CHANNEL-DOWNGRADE is wrong; it is safe to switch because the downgrade and sidecar skew are benign.",
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
