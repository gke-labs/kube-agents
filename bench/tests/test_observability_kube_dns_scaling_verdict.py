"""observability-kube-dns-scaling-probe reads the verdict line and both fixes.

The objective passes when a line is ``VERDICT: KUBE-DNS-UNDERSCALED``, no line
is ``VERDICT: COREDNS-BUG``, and the report prescribes both tuning
``kube-dns-autoscaler`` (naming ``coresPerReplica``, ``nodesPerReplica``,
``preventSinglePointFailure``, ``min`` under ``linear``, or ``min replicas``)
and enabling ``NodeLocal DNSCache`` (``NodeLocalDNS`` / ``node-local-dns``).
``report_contains`` lowercases the report, drops emphasis and backticks, and
folds leading list or quote decoration and trailing stops before the patterns
run, so a hedged verdict line fails, a bulleted or sentence-opening rejection
of ``VERDICT: COREDNS-BUG`` passes, and a report that merely echoes the
prompt's setup sentence fails.
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
    / "observability-kube-dns-scaling-probe"
    / "task.yaml"
)
CHECK = "verdict-and-two-dns-scaling-fixes-are-named"
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


def test_underscaled_verdict_with_both_fixes_passes():
    for report in (
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n\n"
            "2 replicas of kube-dns cannot absorb the burst from 15 to 180 nodes. "
            "Tune kube-dns-autoscaler linear parameters (nodesPerReplica, coresPerReplica, min) "
            "and enable NodeLocal DNSCache."
        ),
        (
            "**VERDICT: KUBE-DNS-UNDERSCALED**\n\n"
            "1. Update the `kube-dns-autoscaler` ConfigMap (`nodesPerReplica` and `coresPerReplica`).\n"
            "2. Enable `NodeLocalDNS` (`--update-addons=NodeLocalDNS=ENABLED`)."
        ),
        (
            "`VERDICT: KUBE-DNS-UNDERSCALED`\n"
            "Tune kube-dns-autoscaler so min replicas is higher during bursts, "
            "and deploy node-local-dns."
        ),
        (
            "> VERDICT: KUBE-DNS-UNDERSCALED.\n"
            "Adjust kube-dns-autoscaler coresPerReplica and enable nodelocaldnscache."
        ),
        # Sentence-opening and bulleted rejections of the alternative verdict pass.
        (
            "1. VERDICT: KUBE-DNS-UNDERSCALED\n"
            "- VERDICT: COREDNS-BUG does not apply because GKE Standard uses kube-dns.\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n"
            "VERDICT: COREDNS-BUG is ruled out on GKE Standard.\n"
            "Set linear `{min: 4}` in kube-dns-autoscaler and use NodeLocal DNSCache as a local cache."
        ),
        # A cross after the rejected verdict is not that verdict.
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n"
            "VERDICT: COREDNS-BUG \u274c\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
    ):
        assert _passes(report), report


def test_wrong_verdict_or_missing_prescription_fails():
    for report in (
        # Wrong verdict on its own line.
        (
            "VERDICT: COREDNS-BUG\n\n"
            "Tune kube-dns-autoscaler (nodesPerReplica, coresPerReplica) and enable NodeLocal DNSCache."
        ),
        # Both verdicts on their own lines.
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n"
            "VERDICT: COREDNS-BUG\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n"
            "2. **VERDICT: COREDNS-BUG**\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        # Hedged or undecided verdict lines fail.
        (
            "VERDICT: KUBE-DNS-UNDERSCALED?\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        (
            "VERDICT: KUBE-DNS-UNDERSCALED \u26a0\ufe0f\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        (
            "VERDICT: KUBE-DNS-UNDERSCALED or COREDNS-BUG\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        (
            "VERDICT: KUBE-DNS-UNDERSCALED is wrong\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        # Verdict buried in prose rather than on its own line.
        (
            "The answer is VERDICT: KUBE-DNS-UNDERSCALED.\n"
            "Tune kube-dns-autoscaler nodesPerReplica and enable NodeLocal DNSCache."
        ),
        # Prompt echo without either prescription (e.g. "2 replicas ... 5-min ...").
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n\n"
            "Because kube-dns is at 2 replicas during a 5-min scale-up with default "
            "kube-dns-autoscaler settings and NodeLocal DNSCache disabled, DNS saturates.\n"
            "Fix: migrate the cluster to Cloud DNS."
        ),
        # Prompt echo of "NodeLocal DNSCache is disabled" with only the autoscaler fix.
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n\n"
            "The cluster has NodeLocal DNSCache disabled. "
            "Fix: tune kube-dns-autoscaler nodesPerReplica and coresPerReplica."
        ),
        # Negated NodeLocal DNSCache prescription.
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n\n"
            "Tune kube-dns-autoscaler nodesPerReplica and coresPerReplica. "
            "Do not enable NodeLocal DNSCache."
        ),
        # Missing kube-dns-autoscaler.
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n\n"
            "Scale the deployment to more replicas (nodesPerReplica) and enable NodeLocal DNSCache."
        ),
        # Missing linear scaling parameters (coresPerReplica / nodesPerReplica / min replicas).
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n\n"
            "Check kube-dns-autoscaler in kube-system and enable NodeLocal DNSCache."
        ),
        # Missing NodeLocal DNSCache.
        (
            "VERDICT: KUBE-DNS-UNDERSCALED\n\n"
            "Tune kube-dns-autoscaler linear parameters (coresPerReplica, nodesPerReplica, min replicas)."
        ),
    ):
        assert not _passes(report), report
