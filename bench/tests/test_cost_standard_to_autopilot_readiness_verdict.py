"""cost-standard-to-autopilot-readiness reads the verdict line and both workload-tied blockers.

The objective passes when a line opens with ``VERDICT: MIGRATION-BLOCKED``, no
line opens with ``VERDICT: MIGRATION-READY``, and the report ties both the
DaemonSet blocker (``privileged`` or ``hostPath``) and the batch Job blocker
(``hostNetwork``) to their workloads without claiming either is allowed on
Autopilot or merely repeating the prompt. ``report_contains`` lowercases the
report, drops emphasis and backticks, and folds line decoration before the
patterns run.
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
    doc = yaml.safe_load(TASK.read_text())
    for report in (
        doc["expected_output"],
        (
            "VERDICT: MIGRATION-BLOCKED\n\n"
            "- The log-collector DaemonSet uses securityContext.privileged: true and a hostPath mount (/var/log).\n"
            "- The batch Job uses hostNetwork: true."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED \u274c\n\n"
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
            "VERDICT: MIGRATION-BLOCKED\n"
            "The DaemonSet (privileged, hostPath) and batch Job (hostNetwork) are not allowed on Autopilot, "
            "while the stateless web API Deployment is allowed on Autopilot as-is."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The log-collector DaemonSet and the batch Job block migration because they require "
            "securityContext.privileged: true / hostPath and hostNetwork: true, respectively."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The batch Job and the log-collector DaemonSet block migration because they require "
            "hostNetwork: true and securityContext.privileged: true / hostPath, respectively."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Unlike the privileged hostPath DaemonSet and the hostNetwork batch Job, "
            "the stateless web API Deployment is allowed on Autopilot."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Unlike the privileged hostPath DaemonSet and the hostNetwork batch Job, "
            "the Deployment with CPU and memory requests is allowed on Autopilot."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Unlike the privileged hostPath DaemonSet and the hostNetwork batch Job, "
            "the stateless Deployment with CPU and memory requests can run on GKE Autopilot as-is."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "1. log-collector DaemonSet: privileged and hostPath are blocked.\n"
            "2. batch Job: hostNetwork is blocked.\n"
            "Remove hostNetwork so the Job is allowed on Autopilot."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "1. log-collector DaemonSet: privileged and hostPath are blocked.\n"
            "2. batch Job: hostNetwork is blocked.\n"
            "Fix: drop hostNetwork from the Job and it can run on Autopilot."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "1. log-collector DaemonSet: privileged and hostPath are blocked.\n"
            "2. batch Job: hostNetwork is blocked.\n"
            "Once the Job stops using hostNetwork, it is compatible."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "1. log-collector DaemonSet: privileged and hostPath are blocked.\n"
            "2. batch Job: hostNetwork is blocked.\n"
            "Without hostNetwork, the batch Job is allowed on Autopilot."
        ),
    ):
        assert _passes(report), report


def test_migration_ready_missing_blocker_wrong_claim_or_prompt_echo_fails():
    doc = yaml.safe_load(TASK.read_text())
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
        (
            "VERDICT: MIGRATION-BLOCKED or MIGRATION-READY\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED is wrong\n"
            "The DaemonSet is privileged with hostPath and the Job uses hostNetwork."
        ),
        # Wrong answer claiming only one workload blocks and the other is allowed.
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Only the DaemonSet blocks (privileged). The Job with hostNetwork is allowed on Autopilot."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Only the batch Job blocks (hostNetwork). The DaemonSet with privileged and hostPath is allowed on Autopilot."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The DaemonSet (privileged, hostPath) is rejected, and the batch Job with hostNetwork is supported on Autopilot."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The DaemonSet (privileged, hostPath) is rejected, and the batch Job with hostNetwork does not block migration."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The batch Job with hostNetwork and the stateless web API Deployment are allowed on Autopilot, "
            "whereas the privileged hostPath DaemonSet is blocked."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The privileged hostPath DaemonSet and the stateless web API Deployment are allowed on Autopilot, "
            "whereas the batch Job with hostNetwork is blocked."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The batch Job with hostNetwork, like the stateless web API Deployment, is allowed on Autopilot, "
            "whereas the privileged hostPath DaemonSet is blocked."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The Service, the batch Job with hostNetwork, the stateless web API Deployment, and the Ingress can run on Autopilot, "
            "whereas the privileged hostPath DaemonSet is blocked."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The Service, the batch Job with hostNetwork, and the stateless web API Deployment can run on Autopilot, "
            "whereas the privileged hostPath DaemonSet is blocked."
        ),
        # Verdict line followed by the prompt itself.
        f"VERDICT: MIGRATION-BLOCKED\n{doc['prompt']}",
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The cluster runs three workloads: (1) a stateless web API Deployment with CPU/memory requests, "
            "(2) a log-collector DaemonSet mounting /var/log via hostPath with securityContext.privileged: true, "
            "and (3) a batch Job using hostNetwork: true."
        ),
        # Constraint words present without tying each to its workload on the same line, or swapped.
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Autopilot rejects privileged containers, hostPath volumes, and hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The DaemonSet uses hostNetwork and the batch Job uses privileged."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "hostNetwork is used by the DaemonSet and privileged is used by the batch Job."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The log-collector DaemonSet and the batch Job block migration because they require "
            "hostNetwork: true and securityContext.privileged: true, respectively."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The batch Job and the log-collector DaemonSet block migration because they require "
            "securityContext.privileged: true and hostNetwork: true, respectively."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Between the log-collector DaemonSet and the batch Job, the batch Job uses privileged and the DaemonSet uses hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Two workloads fail (the batch Job and the log-collector DaemonSet): the DaemonSet uses hostNetwork and the batch Job uses privileged."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "Neither the Deployment nor the log-collector DaemonSet blocks migration; the batch Job blocks migration because of privileged and hostNetwork."
        ),
        (
            "VERDICT: MIGRATION-BLOCKED\n"
            "The log-collector DaemonSet has no Autopilot issues. The batch Job is blocked because it uses privileged and hostNetwork."
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
        # Verdict embedded in prose rather than opening a line.
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
