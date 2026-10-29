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


class TerraformNewClusterDefaultsTest(unittest.TestCase):
    """#3691's lane runs: a new cluster on a Terraform repository came out with
    deletion_protection = false in 4 of 5 runs, and 3 of 4 re-declared the
    sibling's project-wide google_gke_hub_feature (#3727). Step 3 gives a new
    cluster its siblings' protection, or true, and only per-cluster resources."""

    def _step3(self):
        # Whitespace collapsed, so a rewrap of the prose does not fail the test.
        content = TERRAFORM_SKILL.read_text(encoding="utf-8")
        step3 = content[content.index("### Step 3: Write the change"):content.index("### Step 4")]
        return " ".join(step3.split())

    def test_a_new_clusters_deletion_protection_is_on(self):
        step3 = self._step3()
        self.assertIn("**A new cluster's deletion protection** is on", step3)
        self.assertIn("it is `deletion_protection = true`", step3)
        self.assertIn("A sibling that has it `false` is not copied", step3)
        self.assertIn("Never `false`, for a test, an evaluation or an easier teardown", step3)

    def test_a_new_cluster_declares_only_per_cluster_resources(self):
        step3 = self._step3()
        self.assertIn("Shape, not contents", step3)
        self.assertIn("never declared a second time", step3)
        self.assertIn("`google_gke_hub_feature`", step3)
        self.assertIn("copied with this cluster's own state prefix", step3)

    def test_the_pr_body_says_which_deletion_protection_rule_applied(self):
        content = TERRAFORM_SKILL.read_text(encoding="utf-8")
        self.assertIn("new cluster: `true`, the provider default", content)
        self.assertIn("has `false` and was not copied", content)

    def test_the_reference_lists_the_shared_resources(self):
        reference = (TERRAFORM_SKILL.parent / "references" / "container.md").read_text(encoding="utf-8")
        self.assertIn("## Shared resources beside a cluster", reference)
        # The reference agrees with Step 3: a sibling's false is never copied.
        flat = " ".join(reference.split())
        self.assertNotIn("the existing clusters' value", flat)
        self.assertIn("a sibling's `false` is not copied", flat)
        for resource in ("google_gke_hub_feature", "google_gke_hub_fleet", "google_project_service"):
            self.assertIn(f"`{resource}`", reference)

    def test_gke_basics_no_longer_turns_deletion_protection_off(self):
        """gke-basics' Terraform example set it false "for testing" (#3727)."""
        example = (REPO_ROOT / "agents/platform/skills/gke-basics/references/iac-usage.md").read_text(encoding="utf-8")
        self.assertNotRegex(example, r'deletion_protection\s*=\s*"?false')


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


class SubmitSuggestionHandsEditsToTheirAuthoringSkillTest(unittest.TestCase):
    """#2999's lane run: asked to raise a Terraform node pool's autoscaling
    maximum, the agent went straight to submit-suggestion, edited the .tf, and
    never loaded gcp-terraform, so tofu fmt never ran (#3565). Step 2 hands a
    Terraform, Config Connector or Helm-chart edit to its authoring skill."""

    def test_step_two_names_each_authoring_skill(self):
        content = (REPO_ROOT / "agents/platform/skills/submit-suggestion/SKILL.md").read_text(encoding="utf-8")
        step2 = content[content.index("### Step 2: Make the Changes"):content.index("### Deleting a resource")]
        for skill in ("gcp-terraform", "gcp-config-connector", "gitops-helm-chart"):
            self.assertIn(f"**{skill}**", step2)
        self.assertIn("follow it from its Step 1", step2)
        # The authoring skill submits its own branch; editing this skill's copy
        # would leave submit with nothing to find.
        self.assertIn("Leave the copy you prepared here unused", step2)
        # A review round or an open proposal already fixes the branch; the
        # authoring skill's own prepare and submit would open a second PR.
        self.assertIn("When the branch is already decided, stay on it.", step2)
        self.assertIn("not its Step 1 `prepare`, its branch name or\nits submit", step2)

    def test_review_rounds_apply_the_same_table(self):
        # Step 5 is where pr-conversation enters; a .tf edit in a review round
        # takes the authoring skill's rules too.
        content = (REPO_ROOT / "agents/platform/skills/submit-suggestion/SKILL.md").read_text(encoding="utf-8")
        step5 = content[content.index("### Step 5: Addressing Review Feedback"):]
        self.assertIn("authoring skill's rules here too (Step 2's table)", step5)
        self.assertIn("`tofu fmt` after any `.tf` edit", step5)
        self.assertIn("submit do not apply: stay on `<source>` in this copy", step5)

    def test_step_two_scopes_multi_option_reports_to_chosen_option_only(self):
        content = (REPO_ROOT / "agents/platform/skills/submit-suggestion/SKILL.md").read_text(encoding="utf-8")
        step2 = content[content.index("### Step 2: Make the Changes"):content.index("### Deleting a resource")]
        flat = " ".join(step2.split())
        self.assertIn("**Implement only the chosen option.**", flat)
        self.assertIn("edit only the file(s) that option calls for", flat)
        self.assertIn("edit only the synced workload manifest(s)", flat)
        self.assertIn("Never bundle unchosen options or optional hardening into the same pull request", flat)
        self.assertIn("never touch unrelated manifests or infrastructure definitions", flat)

if __name__ == "__main__":
    unittest.main()
