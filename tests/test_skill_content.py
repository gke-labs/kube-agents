"""Pins skill text that `make skills-check` does not cover.

    python3 -m unittest tests/test_skill_content.py

`make skills-check` checks every byte of the mirrored platform `gke-*` skills
against their upstream copy and overlay. The Cluster Agent's skills are this
repository's own, not mirrored, so nothing else reads them. These assertions
were the cluster half of the skill-content tests that went with
scripts/sync-upstream-skills.py.
"""

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLUSTER_WORKLOAD_SECURITY = REPO_ROOT / "agents/cluster/skills/gke-workload-security/SKILL.md"
# gke-manifest-generation's routing patch sends Config Connector manifests here.
CONFIG_CONNECTOR_SKILL = REPO_ROOT / "agents/platform/skills/gcp-config-connector/SKILL.md"


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


class SessionShippedWorkloadSkillsTest(unittest.TestCase):
    # a2a/cmd/session-persona replaces any section that names a kanban tool
    # with a one-line note, so a kanban call written into a workflow step would
    # silently remove that whole workflow from the session copy. The reporting
    # contract lives in workload-triage-scan/references/reporting.md instead.
    SKILLS = (
        "workload-triage-scan",
        "pod-crashloop-oom",
        "pod-pending-scheduling",
        "pod-container-creating",
        "service-connectivity",
        "gke-workload-troubleshooting",
    )

    def test_workflow_skills_name_no_kanban_tool(self):
        for name in self.SKILLS:
            body = (REPO_ROOT / "agents/cluster/skills" / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertNotIn("kanban_", body, name)

    def test_the_shared_reporting_contract_names_the_kanban_tools(self):
        reference = REPO_ROOT / "agents/cluster/skills/workload-triage-scan/references/reporting.md"
        text = reference.read_text(encoding="utf-8")
        self.assertIn("kanban_complete(", text)
        self.assertIn('kanban_block(kind="needs_input"', text)


class RoutedSkillsExistTest(unittest.TestCase):
    def test_config_connector_skill_exists(self):
        self.assertTrue(CONFIG_CONNECTOR_SKILL.is_file())


if __name__ == "__main__":
    unittest.main()
