"""monitoring-alert-policy-read's state check, on the folded text the verifier grades.

A bullet list puts the policy's name on one line and its state on the next.
The forbidden "<name>: enabled" pattern reads an "Enabled" or "Active" state that
starts the next line too, but not one whose line goes on to say false or no:
that is the "Enabled: false" label of a disabled report, which it used to fail.
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
        f"{NAME}\n  enabled:\n    false\n",
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
        # The next line's state, with a neighbour named disabled nearby: main
        # caught these by reading across the line break, and still must.
        f"- {NAME}\n  - Enabled: \u2705\n- ka-bench other policy\n  - Enabled: \u274c disabled\n",
        f"- **Display name:** {NAME}\n- **Enabled:** \u2705\n- Another policy: disabled\n",
        f"- {NAME}\n  - Active\n- ka-bench other: disabled\n",
        f"**{NAME}**\nEnabled: on\nNotifications: disabled\n",
        f"{NAME}\n\nEnabled\n\nother: disabled\n",
    ],
)
def test_an_enabled_report_still_fails(report):
    assert not check_passes(CASE, CHECK, report)
