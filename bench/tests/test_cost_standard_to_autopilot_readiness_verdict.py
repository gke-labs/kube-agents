"""cost-standard-to-autopilot-readiness reads the verdict line and both blockers.

The objective passes when a line opens with ``VERDICT: MIGRATION-BLOCKED``, no
line opens with ``VERDICT: MIGRATION-READY``, and the report names both the
DaemonSet blocker (``privileged`` or ``hostPath``) and the batch Job blocker
(``hostNetwork``). ``report_contains`` lowercases the report, drops emphasis
and backticks, and folds line decoration before the patterns run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cost-standard-to-autopilot-readiness" / "task.yaml"
CHECK = "the-verdict-is-migration-blocked-and-names-both-blockers"
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


def test_migration_blocked_verdict_with_both_blockers_passes():
    for report in (
        (
            "VERDICT: MIGRATION-BLOCKED\n\n"
            "- The log-collector DaemonSet uses securityContext.privileged: true and a hostPath mount (/var/log).\n"
            "- The batch Job uses hostNetwork: true."
        ),
        (
            "**VERDICT: MIGRATION-BLOCKED**\n\n"
            "1. `log-collector` DaemonSet: `privileged` containers and `hostPath` volumes are rejected on Autopilot.\n"
            "2. Batch Job: `hostNetwork: true` is rejected on Autopilot."
        ),
        (
            "`VERDICT: MIGRATION-BLOCKED`\n"
            "The DaemonSet mounts /var/log via host-path, and the batch Job requires host-network."
        ),
        (
            "> VERDICT: MIGRATION-BLOCKED\n"
            "Blockers: privileged DaemonSet and host networking on the batch Job."
        ),
        (
            "1. VERDICT: MIGRATION-BLOCKED\n"
            "Autopilot blocks the hostPath DaemonSet and the hostNetwork Job."
        ),
        (
            "VERDICT: \u201cMIGRATION-BLOCKED\u201d\n"
            "The privileged DaemonSet and hostNetwork batch Job cannot migrate as-is."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED.\n"
            "- VERDICT: MIGRATION-READY is ruled out because the DaemonSet is privileged (hostPath) "
            "and the batch Job uses hostNetwork.\n"
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "VERDICT: MIGRATION-READY \u274c\n"
            "The privileged hostPath DaemonSet and hostNetwork batch Job block migration."
        ),
    ):
        assert _passes(report), report


def test_migration_ready_missing_blocker_or_prose_verdict_fails():
    for report in (
        (
            "VERDICT: MIGRATION-READY\n\n"
            "All three workloads can migrate to GKE Autopilot as-is."
        ),
        (
            "VERDICT: MIGRATION-READY\n\n"
            "Note: the DaemonSet uses securityContext.privileged: true with hostPath and the Job uses hostNetwork: true."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "VERDICT: MIGRATION-READY\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "2. **VERDICT: MIGRATION-READY**\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        # Hedged or negated verdict lines are not a clean verdict on its own line.
        (
            "VERDICT: MIGRATION-BLOCKED?\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED \u26a0\ufe0f\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED or MIGRATION-READY\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED is wrong\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        # Missing the batch Job hostNetwork blocker.
        (
            "VERDICT: MIGRATION-BLOCKED\n\n"
            "Only the log-collector DaemonSet is blocked because it uses securityContext.privileged: true and hostPath."
        ),
        # Missing the DaemonSet privileged/hostPath blocker.
        (
            "VERDICT: MIGRATION-BLOCKED\n\n"
            "Only the batch Job is blocked because it uses hostNetwork: true."
        ),
        # Verdict embedded in prose rather than on its own line.
        (
            "The result is VERDICT: MIGRATION-BLOCKED because the DaemonSet is privileged (hostPath) "
            "and the batch Job uses hostNetwork."
        ),
        # Prose without the verdict line.
        (
            "You cannot migrate as-is: Autopilot blocks the privileged hostPath DaemonSet "
            "and the hostNetwork batch Job."
        ),
    ):
        assert not _passes(report), report
