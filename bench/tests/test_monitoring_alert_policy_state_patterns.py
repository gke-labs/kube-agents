"""monitoring-alert-policy-read's state check, on the folded text the verifier grades.

A bullet list puts the policy's name on one line and its state on the next.
The "<name>: enabled" forbidden pattern must stay on the policy's own line: read
across the line break it took the next bullet's "Enabled:" label for an enabled
state, and failed answers that report the planted policy disabled.
"""

from __future__ import annotations

import pytest

from conftest import check_passes

CASE = "monitoring-alert-policy-read"
CHECK = "the-planted-policy-is-reported-disabled"
NAME = "ka-bench orbital-checkout restart storm"


@pytest.mark.parametrize(
    "report",
    [
        f"- **Display name:** {NAME}\n- **Enabled:** `\"enabled\": false`\n",
        f"- **Display name:** {NAME}\n- **Status:** enabled: false\n",
        f"- {NAME}\n  - Enabled: false\n",
        f"| Policy | Enabled |\n|---|---|\n| {NAME} | false |\n",
        f"{NAME} is disabled.\n",
    ],
)
def test_a_disabled_report_passes_whatever_its_layout(report):
    assert check_passes(CASE, CHECK, report)


@pytest.mark.parametrize(
    "report",
    [
        f"{NAME}: enabled\n",
        f"{NAME} is enabled.\n",
        f"- **Display name:** {NAME}\n- **Enabled:** true\n",
        f"| Policy | Enabled |\n|---|---|\n| {NAME} | true |\n",
    ],
)
def test_an_enabled_report_still_fails(report):
    assert not check_passes(CASE, CHECK, report)
