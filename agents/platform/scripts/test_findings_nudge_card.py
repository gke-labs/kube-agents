"""Tests for findings_nudge_card: sidecar persistence, kube-agents.chat/v1, and Block Kit layout.

Run: python3 -m unittest agents/platform/scripts/test_findings_nudge_card.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import chat_cases
import findings_nudge_card as card_mod


def sample_card(**overrides) -> dict:
    base = {
        "version": 1,
        "text": (
            "2 critical issues will block cluster upgrades · 5 open across 2 clusters\n\n"
            "- critical · kube-system/istiod on prod-central — PodDisruptionBudget blocks drain. "
            "Allow at least 1 disruption.\n"
            "- critical · gke-pool-1 on prod-central — Orphan Node has no backing VM. "
            "Delete the orphan Node object.\n\n"
            "3 more issues are queued (1 critical, 2 lower priority)."
        ),
        "thread_context": (
            "2 critical issues will block cluster upgrades\n\n"
            "[Queued item metadata for thread replies]\n"
            "1. [reminder] id=urr-pdb.1 members=urr-pdb.1 location=proj/prod-central/kube-system/istiod covers=1 unreported=0\n"
            "2. [reminder] id=urr-node.1 members=urr-node.1 location=proj/prod-central/gke-pool-1 covers=1 unreported=0"
        ),
        "headline": "2 critical issues will block cluster upgrades",
        "note": "· 5 open across 2 clusters",
        "tone": "critical",
        "rows": [
            {
                "severity": "critical",
                "text": "**kube-system/istiod** on `prod-central` — PodDisruptionBudget blocks drain",
                "detail": "Allow at least 1 disruption.",
            },
            {
                "severity": "critical",
                "text": "**gke-pool-1** on `prod-central` — Orphan Node has no backing VM",
                "detail": "Delete the orphan Node object.",
            },
        ],
        "after": ["3 more issues are queued (1 critical, 2 lower priority)."],
        "fold": {
            "title": "Queue breakdown (3 more queued)",
            "table": {
                "columns": ["Cluster", "Critical", "Major", "Lower", "Next queued issue"],
                "rows": [
                    ["prod-central", "1", "1", "0", "billing/worker: mounts static key"],
                    ["staging-east", "0", "0", "1", "ledger: missing ResourceQuota"],
                ],
            },
        },
        "choices": ["Fix first issue", "See all 5 items"],
        "replies": {"Fix first issue": "Fix first issue: kube-system/istiod on prod-central — PodDisruptionBudget blocks drain"},
        "links": [["Open backlog", "https://github.com/acme/ops/issues/1"]],
    }
    base.update(overrides)
    return base


class FindingsNudgeCardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gateway = Path(tmp.name)
        self.profile = self.gateway / "profiles" / "platform"
        self.profile.mkdir(parents=True)

    def test_save_and_load_round_trip_and_remove_on_none(self):
        card = sample_card()
        path = card_mod.card_path(self.gateway)
        self.assertEqual(path, self.profile / card_mod.CARD_FILE)
        card_mod.save(path, card)
        loaded = card_mod.load(self.gateway, card["text"])
        self.assertEqual(loaded, card)

        card_mod.save(path, None)
        self.assertFalse(path.exists())
        self.assertIsNone(card_mod.load(self.gateway, card["text"]))

    def test_profile_home_resolves_platform_agent_home_and_override(self):
        self.assertEqual(
            card_mod.profile_home({"PLATFORM_AGENT_HOME": str(self.gateway)}),
            self.profile,
        )
        custom = self.gateway / "custom"
        self.assertEqual(
            card_mod.profile_home({"FINDINGS_NUDGE_HOME": str(custom)}),
            custom,
        )

    def test_load_rejects_stale_text_wrong_version_and_malformed_fields(self):
        card = sample_card()
        path = card_mod.card_path(self.profile)
        card_mod.save(path, card)
        self.assertIsNone(card_mod.load(self.profile, "different text"))

        for bad in (
            sample_card(version=2),
            sample_card(headline=""),
            sample_card(rows=[]),
            sample_card(rows=[{"severity": "critical", "text": ""}]),
            sample_card(tone="neon"),
            sample_card(fold={"title": "Queue", "table": {"columns": ["Cluster"], "rows": [["a", "b"]]}}),
            sample_card(links=[["only-one-element"]]),
        ):
            card_mod.save(path, bad)
            self.assertIsNone(card_mod.load(self.profile, bad["text"]))

    def test_chat_from_card_produces_valid_lint_clean_v1_message(self):
        card = sample_card()
        chat, text = card_mod.chat_from_card(card)
        self.assertEqual(text, card["text"])
        chat_cases.check_chat(chat, "nudge")
        self.assertEqual(chat_cases.lint(chat), [])
        self.assertEqual(
            list(chat.keys()),
            ["schema", "headline", "note", "tone", "rows", "after", "fold", "choices", "links"],
        )
        self.assertEqual(chat["tone"], "critical")
        self.assertEqual(chat["fold"]["title"], "Queue breakdown (3 more queued)")
        self.assertEqual(len(chat["fold"]["table"]["rows"]), 2)
        self.assertEqual(chat["choices"][0]["primary"], True)
        self.assertIn("kube-system/istiod", chat["choices"][0]["reply"])

    def test_blocks_from_card_places_collapsible_table_container_above_actions(self):
        card = sample_card()
        blocks, text = card_mod.blocks_from_card(card)
        self.assertEqual(text, card["text"])
        block_types = [b["type"] for b in blocks]
        self.assertIn("container", block_types)
        self.assertIn("actions", block_types)
        container_idx = block_types.index("container")
        actions_idx = block_types.index("actions")
        self.assertLess(container_idx, actions_idx)

        container = blocks[container_idx]
        self.assertTrue(container["is_collapsible"])
        self.assertTrue(container["default_collapsed"])
        table_block = container["child_blocks"][0]
        self.assertEqual(table_block["type"], "table")
        # Header row + 2 cluster rows.
        self.assertEqual(len(table_block["rows"]), 3)

    def test_thread_context_falls_back_when_empty(self):
        card = sample_card(thread_context="")
        self.assertEqual(card_mod.thread_context(card, "fallback"), "fallback")
        self.assertIn("id=urr-pdb.1", card_mod.thread_context(sample_card(), "fallback"))


if __name__ == "__main__":
    unittest.main()
