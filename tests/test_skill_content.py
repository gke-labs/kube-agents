"""Pins skill text that `make skills-check` does not cover.

    python3 -m unittest tests/test_skill_content.py

`make skills-check` checks every byte of the mirrored platform `gke-*` skills
against their upstream copy and overlay. The Cluster Agent's skills are this
repository's own, not mirrored, so nothing else reads them. These assertions
were the cluster half of the skill-content tests that went with
scripts/sync-upstream-skills.py; the platform skills' routing descriptions
are pinned here too.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLUSTER_WORKLOAD_SECURITY = REPO_ROOT / "agents/cluster/skills/gke-workload-security/SKILL.md"
# gke-manifest-generation's routing patch sends Config Connector manifests here.
CONFIG_CONNECTOR_SKILL = REPO_ROOT / "agents/platform/skills/gcp-config-connector/SKILL.md"
# gcp-config-connector routes Terraform HCL repositories here.
TERRAFORM_SKILL = REPO_ROOT / "agents/platform/skills/gcp-terraform/SKILL.md"
# gke-manifest-generation's routing patch and gke-basics' cli-reference patch send
# third-party Helm charts here.
HELM_CHART_SKILL = REPO_ROOT / "agents/platform/skills/gitops-helm-chart/SKILL.md"


class ClusterWorkloadSecuritySkillTest(unittest.TestCase):
    def test_teaches_two_step_network_policy_enforcement(self):
        # GKE rejects node enforcement until the addon is on, so the skill checks the
        # dataplane and proposes the two changes in order (the Cluster Agent is read-only).
        content = CLUSTER_WORKLOAD_SECURITY.read_text(encoding="utf-8")
        for text in (
            "spec.addonsConfig.networkPolicyConfig.disabled: false",
            "spec.networkPolicy.enabled: true",
            "networkConfig.datapathProvider",
            "--location <location>",
            "node pools may be recreated, which can take several minutes",
        ):
            self.assertIn(text, content)


class RoutedSkillsExistTest(unittest.TestCase):
    def test_config_connector_skill_exists(self):
        self.assertTrue(CONFIG_CONNECTOR_SKILL.is_file())

    def test_terraform_skill_exists(self):
        self.assertTrue(TERRAFORM_SKILL.is_file())
    def test_helm_chart_skill_exists(self):
        self.assertTrue(HELM_CHART_SKILL.is_file())


def _description(skill):
    """The frontmatter `description:` of a platform skill, which the router reads."""
    text = (REPO_ROOT / "agents/platform/skills" / skill / "SKILL.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    for line in front.splitlines():
        if line.startswith("description:"):
            return line
    raise AssertionError(f"{skill} has no description line")


class DeleteRequestsRouteToTheDeletionSectionTest(unittest.TestCase):
    """A delete request on a cluster once routed to cluster-agent-lifecycle (its
    description says "deleted" and "single-cluster"), and the worker never
    opened the deletion section; it probed a `kubectl delete` instead (#3077,
    #3086's repro runs). Both descriptions name the split."""

    def test_submit_suggestion_claims_deletions_as_a_stop(self):
        # The section answers every delete with a stated stop (the declared
        # path is #3233), so the description must not promise a removal.
        description = _description("submit-suggestion")
        self.assertIn("delete a resource", description)
        self.assertIn("Deleting a resource", description)
        self.assertIn("stated stop, never a pull request", description)
        self.assertNotIn("removes the resource's manifest", description)

    def test_cluster_agent_lifecycle_disclaims_them(self):
        description = _description("cluster-agent-lifecycle")
        self.assertIn("Not for a request to delete or change a resource on a cluster", description)
        self.assertIn("submit-suggestion", description)


if __name__ == "__main__":
    unittest.main()
