#!/usr/bin/env python3
"""Tests for INSTALL.md troubleshooting and uninstall documentation consistency."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_MD = REPO_ROOT / "INSTALL.md"
UNINSTALL_MD = REPO_ROOT / "docs" / "site" / "src" / "content" / "docs" / "install" / "uninstall.md"


class InstallTroubleshootingDocsTest(unittest.TestCase):
    """Verifies that INSTALL.md covers Slack troubleshooting and mode next."""

    def test_install_md_slack_troubleshooting_section_present(self):
        content = INSTALL_MD.read_text(encoding="utf-8")
        self.assertIn("### 5. Slack Bot Doesn't Answer", content)

    def test_install_md_troubleshooting_checklist_items(self):
        content = INSTALL_MD.read_text(encoding="utf-8")
        # Check required checklist items
        self.assertIn("Socket Mode", content)
        self.assertIn("SLACK_APP_TOKEN", content)
        self.assertIn("SLACK_BOT_TOKEN", content)
        self.assertIn("*:history", content)
        self.assertIn("im:history", content)
        self.assertIn("files:write", content)
        self.assertIn("reactions:write", content)
        self.assertIn("Event Subscriptions", content)
        self.assertIn("app_mention", content)
        self.assertIn("allowedUsers", content)
        self.assertIn("SLACK_ALLOWED_USERS", content)
        self.assertIn("Single-Workspace vs Multi-Workspace", content)
        self.assertIn("platformagent-crd.md#specintegration", content)
        self.assertNotIn("platformagent-crd.md#slack", content)

    def test_install_md_troubleshooting_kubectl_logs_commands(self):
        content = INSTALL_MD.read_text(encoding="utf-8")
        # Today mode log commands
        self.assertIn("kubectl logs -n kubeagents-system deploy/platform-agent-credential-proxy", content)
        self.assertIn("kubectl logs -n kubeagents-system deploy/platform-agent-gateway -c platform-agent", content)
        # Next mode log commands
        self.assertIn("kubectl logs -n kubeagents-system deploy/platform-agent-a2a-gateway", content)
        self.assertIn("A2AGateway", content)
        self.assertIn("NoChatBackend", content)
        self.assertIn("WaitingForReplica", content)
        self.assertNotIn("Look for the `A2AGateway` condition", content)


class UninstallDocsTest(unittest.TestCase):
    """Verifies that docs/site/.../uninstall.md is accurate and not stale."""

    def test_uninstall_page_does_not_contain_stale_harness_references(self):
        content = UNINSTALL_MD.read_text(encoding="utf-8")
        self.assertNotIn("recurring 1-minute cron in your agent harness", content)
        self.assertNotIn("delete the `agents/platform` directory from your harness workspace", content)

    def test_uninstall_page_documents_mode_next_residue_and_commands(self):
        content = UNINSTALL_MD.read_text(encoding="utf-8")
        # Check that spec.mode: next section exists
        self.assertIn("What `spec.mode: next` leaves behind", content)
        self.assertIn("platform-agent-a2a-nats-creds", content)
        self.assertIn("data-platform-agent-a2a-nats-0", content)
        self.assertIn("a2a-slack-principal-map", content)
        self.assertIn("kubectl delete secret platform-agent-a2a-nats-creds a2a-slack-principal-map", content)
        self.assertIn("kubectl delete pvc data-platform-agent-a2a-nats-0", content)

    def test_uninstall_page_documents_cluster_scoped_and_secret_cleanup(self):
        content = UNINSTALL_MD.read_text(encoding="utf-8")
        self.assertIn("kubeagents:minimal:kubeagents-system:platform-agent", content)
        self.assertIn("kubeagents:tokenreview:kubeagents-system:platform-agent", content)
        self.assertIn("kubeagents:a2a-callout-tokenreview:kubeagents-system:platform-agent", content)
        self.assertIn("platform-agent-secrets", content)


if __name__ == "__main__":
    unittest.main()
