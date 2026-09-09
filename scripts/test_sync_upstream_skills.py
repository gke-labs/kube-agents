"""Unit tests for the Cluster Agent coupling footer re-injection in sync-upstream-skills.

Run: python3 -m unittest scripts.test_sync_upstream_skills

The invariant under test: after an upstream sync wipes a skill dir, inject_footer restores the
Cluster Agent coupling footer exactly once (idempotent), and only for skills that have one.
"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

# The module file name has hyphens, so load it by path rather than a plain import.
_SPEC = importlib.util.spec_from_file_location(
    "sync_upstream_skills", str(Path(__file__).resolve().parent / "sync-upstream-skills.py")
)
sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync)


class InjectFooterTest(unittest.TestCase):
    def _skill_dir(self, body="# Upstream skill\n\nSome content.\n"):
        d = Path(tempfile.mkdtemp())
        (d / "SKILL.md").write_text(body, encoding="utf-8")
        return d

    def _read(self, d):
        return (d / "SKILL.md").read_text(encoding="utf-8")

    def test_injects_footer_for_configured_skill(self):
        d = self._skill_dir()
        self.assertTrue(sync.inject_footer(str(d), "gke-cluster-creation"))
        text = self._read(d)
        self.assertIn(sync.FOOTER_MARKER, text)
        self.assertIn("provision the Cluster Agent profile", text)
        self.assertIn("cluster_agent_profile.py create", text)

    def test_creation_footer_covers_teardown(self):
        d = self._skill_dir()
        self.assertTrue(sync.inject_footer(str(d), "gke-cluster-creation"))
        text = self._read(d)
        self.assertIn("Cluster Agent Profile Teardown", text)
        self.assertIn("cluster_agent_profile.py delete", text)
        self.assertIn("cluster-agent-reconcile", text)

    def test_idempotent_no_duplicate(self):
        d = self._skill_dir()
        self.assertTrue(sync.inject_footer(str(d), "gke-cluster-creation"))
        # Second call must be a no-op (footer already present from this run's copy).
        self.assertFalse(sync.inject_footer(str(d), "gke-cluster-creation"))
        self.assertEqual(self._read(d).count(sync.FOOTER_MARKER), 1)

    def test_unconfigured_skill_untouched(self):
        d = self._skill_dir(body="original\n")
        self.assertFalse(sync.inject_footer(str(d), "gke-cost-analysis"))
        self.assertEqual(self._read(d), "original\n")

    def test_missing_skill_md_is_safe(self):
        d = Path(tempfile.mkdtemp())  # no SKILL.md
        self.assertFalse(sync.inject_footer(str(d), "gke-cluster-creation"))


class ApplySubstitutionsTest(unittest.TestCase):
    def _skill_dir(self, body=""):
        d = Path(tempfile.mkdtemp())
        (d / sync.SKILL_MD_FILENAME).write_text(body, encoding=sync.UTF_8_ENCODING)
        return d

    def _read(self, d):
        return (d / sync.SKILL_MD_FILENAME).read_text(encoding=sync.UTF_8_ENCODING)

    def test_applies_substitution_for_configured_skill(self):
        d = self._skill_dir(body=sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET + "\n")
        self.assertTrue(sync.apply_substitutions(str(d), "gke-workload-security"))
        text = self._read(d)
        self.assertNotIn(sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET, text)
        self.assertIn(sync.GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET, text)
        self.assertIn("--enable-network-policy", text)

    def test_idempotent_no_duplicate(self):
        d = self._skill_dir(body=sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET + "\n")
        self.assertTrue(sync.apply_substitutions(str(d), "gke-workload-security"))
        # Second call must be a no-op (replacement already present).
        self.assertFalse(sync.apply_substitutions(str(d), "gke-workload-security"))
        text = self._read(d)
        self.assertEqual(text.count(sync.GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET), 1)

    def test_unconfigured_skill_untouched(self):
        d = self._skill_dir(body="original\n")
        self.assertFalse(sync.apply_substitutions(str(d), "gke-cost-analysis"))
        self.assertEqual(self._read(d), "original\n")

    def test_missing_skill_md_is_safe(self):
        d = Path(tempfile.mkdtemp())  # no SKILL.md
        self.assertFalse(sync.apply_substitutions(str(d), "gke-workload-security"))

    def test_target_not_found_returns_false(self):
        d = self._skill_dir(body="other content\n")
        self.assertFalse(sync.apply_substitutions(str(d), "gke-workload-security"))
        self.assertEqual(self._read(d), "other content\n")

    def test_repo_workload_security_skills_have_enforcement_command(self):
        repo_root = Path(__file__).resolve().parent.parent
        for agent in ["platform", "cluster"]:
            skill_md = repo_root / "agents" / agent / "skills" / "gke-workload-security" / "SKILL.md"
            self.assertTrue(skill_md.is_file(), f"{skill_md} must exist")
            content = skill_md.read_text(encoding="utf-8")
            self.assertIn("--enable-network-policy", content)
            self.assertIn("--update-addons=NetworkPolicy=ENABLED", content)
            self.assertIn("networkConfig.datapathProvider", content)
            self.assertIn("--location <location>", content)
            self.assertIn("node pools may be recreated; this can take several minutes", content)
            self.assertNotIn(sync.GKE_WORKLOAD_SECURITY_OLD_NETPOL_SNIPPET, content)
            self.assertIn(sync.GKE_WORKLOAD_SECURITY_NEW_NETPOL_SNIPPET, content)


if __name__ == "__main__":
    unittest.main()
