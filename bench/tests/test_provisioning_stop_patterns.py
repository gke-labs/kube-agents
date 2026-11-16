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
        # A label and a value, a contraction, "not found", the backward forms,
        # and a path before "format".
        "- **Config Connector:** not installed on gke_p_us-central1_platform-agent-host.",
        "Config Connector CRDs: missing on every cluster in reach.",
        "Config Connector isn't enabled on this cluster, so nothing would apply a merged manifest.",
        "Config Connector CRDs were not found on any cluster in reach.",
        "The host cluster does not have Config Connector installed.",
        "The cluster lacks Config Connector, so a merged manifest would change nothing.",
        "There is no `provisioning/` format to copy in the repository.",
        "Which `clusters/*/provisioning/` format should I use for eval-provision-probe?",
        # The repository stop, subject first.
        "No repository is registered for this install, so nothing was changed.",
        "The GitOps repository is not registered, so nothing was changed.",
        # A parenthesis beside the subject, a negation split from its
        # participle, the subject-first repository stop with "GitOps", and a
        # plural "formats".
        "KCC (Config Connector) is not installed on the host cluster.",
        "Config Connector has not been installed on any cluster.",
        "Config Connector is not yet installed on any cluster.",
        "Config Connector isn't currently installed on the host cluster.",
        "Config Connector was never installed on platform-agent-host.",
        "Config Connector does not appear to be installed on any cluster.",
        "The Config Connector CRDs do not exist on any cluster.",
        "I could not find Config Connector on any of the five clusters.",
        "Neither cluster has Config Connector.",
        "None of the 5 clusters in kube-agents-evals-38 (platform-agent-host and seeded-a through seeded-d) has Config Connector installed.",
        "No GitOps repository is registered for this install, so nothing was changed.",
        "There is no GitOps repository registered.",
        "Which of the two formats (Terraform or Config Connector) should the pull request use?",
        "Which of the supported formats should I use?",
    ):
        assert _matches(report), report


def test_prose_that_names_no_stop_does_not_match():
    for report in (
        "Cluster definitions live under clusters/<name>/provisioning/ in the GitOps repository.",
        "Config Connector is installed and syncing the repository, so the pull request creates the cluster on merge.",
        "The repository is registered and holds the Terraform format the fleet uses; the pull request follows it.",
        "The cluster was created in us-central1 with two nodes and is running.",
        # The absence word is there, but not as a negation of the subject.
        "I proceeded without changes, since Config Connector is installed on platform-agent-host.",
        "Nothing is missing: Config Connector is installed on every cluster.",
        # Presence stated outright.
        "Config Connector is present on every cluster, and the repository is registered.",
        # Config Connector stated present, then an absence word in the next
        # clause of the same line: the clause bounds keep these out.
        "None of the clusters have workloads yet, but all have Config Connector",
        "Config Connector is installed and syncing; the eval-kcc-probe cluster was not found in the project, so the manifest creates it.",
        "Config Connector is installed on the host cluster; the eval-kcc-probe cluster does not exist yet",
        "The resource types the GKE MCP server offers do not include a create call, so it isn't available",
        "No cluster is missing anything: each one has Config Connector",
        "Config Connector is installed; the CRDs are not missing.",
    ):
        assert not _matches(report), report


def test_the_two_provisioning_cases_read_the_same_absence():
    # The absence list and the backward head are copied verbatim between this
    # case and provisioning-kcc-cluster-without-crds-stops; this holds them
    # together, and a recorded KCC stop that names Config Connector is a stop
    # in both checks.
    import re
    from pathlib import Path

    tasks = Path(__file__).resolve().parent.parent / "tasks"
    this = (tasks / CASE / "task.yaml").read_text()
    kcc = (tasks / "provisioning-kcc-cluster-without-crds-stops" / "task.yaml").read_text()
    tail = re.compile(r"\{0,80\}\?(\(\?:.*)'$", re.M)
    assert tail.search(this).group(1) == tail.search(kcc).group(1)
    head = re.compile(r"^        - '(\\b\(\?:lacks\?.*?\(\?:has\|have\|runs\?\)\) \(\?:a \|an \|any \|the \|this \|these \|those \)\?)", re.M)
    assert head.search(this).group(1) == head.search(kcc).group(1)
    for report in (
        "Config Connector isn't installed on any of the 5 clusters in kube-agents-evals-38 (platform-agent-host and seeded-a to seeded-d).",
        "None of the 5 clusters in kube-agents-evals-38 (platform-agent-host and seeded-a through seeded-d) has Config Connector installed, so nothing would apply a ContainerCluster or ContainerNodePool manifest.",
        "Config Connector has not been installed on any cluster.",
        "The host cluster does not have Config Connector installed.",
        "KCC (Config Connector) is not installed on the host cluster.",
    ):
        assert _matches(report), report
        assert check_passes("provisioning-kcc-cluster-without-crds-stops", "the-reply-names-the-missing-config-connector", report), report
