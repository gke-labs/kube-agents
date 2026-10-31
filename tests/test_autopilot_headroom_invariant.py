#!/usr/bin/env python3
"""Verify the GKE Autopilot Headroom Invariant is enforced across personas and cost/rightsizing skills (#3141)."""

from __future__ import annotations

from pathlib import Path
import re
import unittest

REPO = Path(__file__).resolve().parent.parent

PERSONA_AND_SKILL_DOCS = (
    REPO / "agents/cluster/SOUL.md",
    REPO / "agents/cluster/persona/instructions.md",
    REPO / "agents/platform/SOUL.md",
    REPO / "agents/cluster/skills/gke-rightsizing-checks/SKILL.md",
    REPO / "agents/platform/skill-overlays/gke-cost-analysis/append.md",
    REPO / "agents/platform/skills/gke-cost-analysis/SKILL.md",
    REPO / "agents/platform/skill-overlays/gke-cost-optimization/append.md",
    REPO / "agents/platform/skills/gke-cost-optimization/SKILL.md",
    REPO / "agents/platform/governance/fleet_wide_cost_analysis_sop.md",
)

HEADROOM_TERMS_RE = re.compile(
    r"gke-system-balloon-pod[\s\S]*?(?:headroom|preempt)",
    re.IGNORECASE,
)
USER_NAMESPACE_SCOPE_DOCS = (
    REPO / "agents/cluster/SOUL.md",
    REPO / "agents/cluster/persona/instructions.md",
    REPO / "agents/platform/SOUL.md",
    REPO / "agents/cluster/skills/gke-rightsizing-checks/SKILL.md",
    REPO / "agents/platform/skill-overlays/gke-cost-analysis/append.md",
    REPO / "agents/platform/skills/gke-cost-analysis/SKILL.md",
    REPO / "agents/platform/skill-overlays/gke-cost-optimization/append.md",
    REPO / "agents/platform/skills/gke-cost-optimization/SKILL.md",
)
NO_SEARCH_DOCS = (
    REPO / "agents/cluster/SOUL.md",
    REPO / "agents/cluster/persona/instructions.md",
    REPO / "agents/platform/SOUL.md",
    REPO / "agents/platform/skill-overlays/gke-cost-analysis/append.md",
    REPO / "agents/platform/skills/gke-cost-analysis/SKILL.md",
    REPO / "agents/platform/skill-overlays/gke-cost-optimization/append.md",
    REPO / "agents/platform/skills/gke-cost-optimization/SKILL.md",
)


class AutopilotHeadroomInvariantTest(unittest.TestCase):
    def test_all_cost_and_persona_docs_name_balloon_pod_headroom_invariant(self) -> None:
        for doc in PERSONA_AND_SKILL_DOCS:
            with self.subTest(doc=str(doc.relative_to(REPO))):
                text = doc.read_text(encoding="utf-8")
                self.assertIn("gke-system-balloon-pod", text)
                self.assertIn("kube-system", text)
                self.assertRegex(text, HEADROOM_TERMS_RE)

    def test_persona_and_skill_docs_scope_optimization_reads_to_non_system_namespaces(self) -> None:
        for doc in USER_NAMESPACE_SCOPE_DOCS:
            with self.subTest(doc=str(doc.relative_to(REPO))):
                text = doc.read_text(encoding="utf-8")
                self.assertRegex(
                    text,
                    re.compile(r"non-system\s+user\s+namespaces", re.IGNORECASE),
                )
                self.assertIn("gke-managed-*", text)

    def test_personas_and_cost_skill_forbid_balloon_pod_lookup_rabbit_holes(self) -> None:
        for doc in NO_SEARCH_DOCS:
            with self.subTest(doc=str(doc.relative_to(REPO))):
                text = doc.read_text(encoding="utf-8")
                self.assertRegex(
                    text,
                    re.compile(
                        r"(?:do not call `(?:mcp-)?developer_knowledge` or `web_search`|never run per-pod `kubectl` queries or documentation/web searches)",
                        re.IGNORECASE,
                    ),
                )


if __name__ == "__main__":
    unittest.main()
