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

    def test_submit_suggestion_claims_deletions(self):
        # The section removes a declared object's manifest in a pull request
        # and stops on anything else (#3233), and the description says both.
        description = _description("submit-suggestion")
        self.assertIn("delete a resource", description)
        self.assertIn("Deleting a resource", description)
        self.assertIn("pull request that removes the declaring manifest", description)
        self.assertIn("stated stop when the repository does not declare it", description)

    def test_cluster_agent_lifecycle_disclaims_them(self):
        description = _description("cluster-agent-lifecycle")
        self.assertIn("Not for a request to delete or change a resource on a cluster", description)
        self.assertIn("submit-suggestion", description)



class InstallGuidesAreReadNotSearchedTest(unittest.TestCase):
    """The #2935 done-when e2e (2026-10-21) went 0/3: web_extract cannot fetch
    on an install whose web backend is search-only, so the worker built
    Agent Substrate from search snippets (a fork chart, Autopilot, no bucket
    or IAM). The guide is read through tools that return its text, a search
    result is never a source, and a workload's requirements beat a template
    default."""

    def test_gitops_helm_chart_reads_the_guide_through_tools_that_fetch(self):
        content = HELM_CHART_SKILL.read_text(encoding="utf-8")
        self.assertIn("mcp__developer_knowledge__search_documents", content)
        # Deferred tools: the worker reaches them only through the dispatcher,
        # and read the page (get_documents), not a chunk (#3678's e2e runs).
        self.assertIn('tool_search("developer_knowledge")', content)
        self.assertIn("mcp__developer_knowledge__get_documents", content)
        self.assertIn("inspect_repository.py clone", content)
        # The version source is readable too: never the default branch's
        # Chart.yaml, and the cert-manager reference reads its release branch,
        # not GitHub's releases page or a raw URL (#3678 review round 3).
        self.assertIn("Never the default branch's `Chart.yaml`", content)
        cert_manager = (HELM_CHART_SKILL.parent / "references" / "cert-manager.md").read_text(encoding="utf-8")
        self.assertNotIn("raw.githubusercontent.com", cert_manager)
        self.assertNotIn("/releases`", cert_manager)
        self.assertIn("inspect-repository", content)
        self.assertIn("A search result is never a source", content)
        self.assertNotIn("web_extract", content)

    def test_gke_cluster_creation_lets_the_workload_override_the_template(self):
        content = (REPO_ROOT / "agents/platform/skills/gke-cluster-creation/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("a template default applies only where the workload", content)
        self.assertIn("Search-result snippets are not requirements", content)

if __name__ == "__main__":
    unittest.main()
