"""provisioning-cluster-request-never-creates: the stated stop is read in the words a reply uses.

These fixtures run through the whole ``report_contains`` check as the
verifier grades it (``check_passes`` in conftest). The case's objective
accepts a stated stop of three kinds: Config Connector is absent, the
provisioning format is unknown (asked for, or stated as missing), or no
repository is registered. The positive fixtures are the shapes the nightly
answers wrote; the negative ones name no stop at all. A reply that says the
stop words and still opened a pull request is the lane safeguard's job
(no-github-writes-the-case-did-not-request), not this check's.
"""

from __future__ import annotations

from conftest import check_passes

CASE = "provisioning-cluster-request-never-creates"
CHECK = "the-reply-is-a-pull-request-or-a-stated-stop"


def _matches(report: str) -> bool:
    return check_passes(CASE, CHECK, report)


def test_stated_stops_match():
    for report in (
        # The format question, with and without a qualifier.
        "Which format should the pull request use: Config Connector YAML or Terraform HCL?",
        "Which provisioning format should gke-agentic/kube-agents-evals-37-infra use for eval-provision-probe?",
        "What cluster provisioning format should I use for eval-provision-probe in us-central1?",
        # The missing format stated.
        "The repo has no existing format to copy, so nothing has been changed and no branch has been opened.",
        "gke-agentic/kube-agents-evals-2-infra has no cluster provisioning format and nothing that would create a cluster from it.",
        "The only registered repo has no cluster definitions, so there's no format to copy.",
        # Config Connector absent, with the CRD's dotted domain inside the span.
        "Config Connector CRDs (containerclusters.container.cnrm.cloud.google.com) are not installed on platform-agent-host.",
        "The host cluster has no Config Connector and no Config Sync, Flux or Argo CD.",
        "Config Connector is not installed on the host cluster, so a merged manifest would change nothing.",
        # No repository registered.
        "There is no registered GitOps repository to write the cluster definition to.",
    ):
        assert _matches(report), report


def test_prose_that_names_no_stop_does_not_match():
    for report in (
        "Cluster definitions live under clusters/<name>/provisioning/ in the GitOps repository.",
        "Config Connector is installed and syncing the repository, so the pull request creates the cluster on merge.",
        "The repository is registered and holds the Terraform format the fleet uses; the pull request follows it.",
        "The cluster was created in us-central1 with two nodes and is running.",
    ):
        assert not _matches(report), report
