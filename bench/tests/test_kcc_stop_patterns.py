"""provisioning-kcc-cluster-without-crds-stops: the missing Config Connector is read in the words a reply uses.

These fixtures run through the whole ``report_contains`` check as the
verifier grades it (``check_passes`` in conftest). The positive fixtures are
the shapes the nightly answers wrote; the negative ones name Config Connector
as present, or name no absence at all.
"""

from __future__ import annotations

from conftest import check_passes

CASE = "provisioning-kcc-cluster-without-crds-stops"
CHECK = "the-reply-names-the-missing-config-connector"


def _matches(report: str) -> bool:
    return check_passes(CASE, CHECK, report)


def test_the_missing_config_connector_matches():
    for report in (
        "Config Connector is not installed on the host cluster, so nothing would apply the manifest.",
        "Config Connector isn't installed on platform-agent-host, the only cluster it can reach.",
        "Config Connector isn't installed on any of the 5 clusters in the project.",
        "None of the 5 clusters in the project (platform-agent-host and seeded-a through seeded-d) has Config Connector installed.",
        "None of the clusters in the project has the ContainerCluster resource type installed.",
        "The Config Connector CRDs (containerclusters and containernodepools under container.cnrm.cloud.google.com) are not installed on this install's cluster.",
        "The ContainerCluster and ContainerNodePool resource types aren't installed on any of the 5 clusters I can reach.",
        "No Config Connector: the ContainerCluster and ContainerNodePool resource types aren't installed on any cluster.",
        "The ContainerCluster CRD (Config Connector's resource type for GKE clusters) isn't installed on any cluster in the project.",
        "The host cluster has no Config Connector CRDs, so a merged manifest would change nothing.",
    ):
        assert _matches(report), report


def test_prose_that_names_no_absence_does_not_match():
    for report in (
        "Config Connector is installed and syncing the repository, so the pull request creates the cluster on merge.",
        "The Config Connector CRDs are installed on the management cluster; the manifest applies on merge.",
        "I installed Config Connector on the host cluster and created eval-kcc-probe from the manifest.",
        "None of the node pools has autoscaling enabled; Config Connector applied the manifest as written.",
        "The repository has no clusters/eval-kcc-probe/provisioning/ path yet, so the manifest goes in a new folder.",
    ):
        assert not _matches(report), report
