"""Ensure context file character caps prevent SOUL.md truncation (issue #1957).

    python3 -m unittest discover -s tests -p 'test_context_file_truncation.py'

Hermes dynamically scales context file budgets (SOUL.md, AGENTS.md, etc.) as
6% of the model's context window (clamped between [20_000, 500_000]), but falls
back to a flat 20,000-char floor whenever model context length is unknown
(e.g., custom LiteLLM endpoints with logical model aliases like `model-default`).

Both shipped personas exceed the 20,000-char floor:
  - `agents/chat/SOUL.md`: ~36k chars
  - `agents/platform/SOUL.md`: ~50k chars

Without an explicit `context_file_max_chars` in the profile configuration,
prompt construction truncates the middle chunk (70% head, 20% tail) of SOUL.md,
dropping hand-off templates, delegation protocols, and safety rules.

These tests enforce:
1. Every shipped profile config (`deploy/shared/defaults/config.yaml`,
   `agents/chat/config.yaml`, `agents/cluster/config.yaml`) sets
   `context_file_max_chars` to an integer >= 100,000.
2. The image build merge (`merge_configs.py`) preserves `context_file_max_chars`
   in the platform profile template.
3. Every shipped context file (SOUL.md, AGENTS.md) across all profiles is strictly
   smaller than `context_file_max_chars`.
4. Truncation logic preserves the full content when `context_file_max_chars` is set,
   and demonstrably truncates when unset (sabotage verification).
"""

from __future__ import annotations

import pathlib
import sys
import unittest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SHARED_DEFAULTS = REPO_ROOT / "deploy" / "shared" / "defaults" / "config.yaml"
CHAT_CONFIG = REPO_ROOT / "agents" / "chat" / "config.yaml"
CLUSTER_CONFIG = REPO_ROOT / "agents" / "cluster" / "config.yaml"
PLATFORM_OVERLAY = REPO_ROOT / "agents" / "platform" / "config.yaml"

SHIPPED_CONFIGS = (SHARED_DEFAULTS, CHAT_CONFIG, CLUSTER_CONFIG)

# Hermes upstream constants from agent/prompt_builder.py
HERMES_FLOOR_CHARS = 20_000
HERMES_HEAD_RATIO = 0.70
HERMES_TAIL_RATIO = 0.20
MINIMUM_PINNED_CAP = 100_000


def simulate_hermes_truncation(content: str, max_chars: int | None = None) -> tuple[str, bool]:
    """Mirror Hermes _truncate_content behavior when context_length is unknown."""
    limit = max_chars if (isinstance(max_chars, int) and max_chars > 0) else HERMES_FLOOR_CHARS
    if len(content) <= limit:
        return content, False
    head_chars = int(limit * HERMES_HEAD_RATIO)
    tail_chars = int(limit * HERMES_TAIL_RATIO)
    truncated = content[:head_chars] + "\n...[TRUNCATED]...\n" + content[-tail_chars:]
    return truncated, True


class ContextFileTruncationTest(unittest.TestCase):
    def test_shipped_configs_declare_context_file_max_chars(self):
        """Every shipped profile config must explicitly pin context_file_max_chars."""
        for path in SHIPPED_CONFIGS:
            rel = path.relative_to(REPO_ROOT)
            self.assertTrue(path.exists(), f"{rel} does not exist")
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            val = doc.get("context_file_max_chars")
            self.assertIsNotNone(
                val,
                f"{rel} must declare top-level 'context_file_max_chars' to prevent "
                f"falling back to Hermes's {HERMES_FLOOR_CHARS}-char floor",
            )
            self.assertIsInstance(
                val,
                int,
                f"{rel} context_file_max_chars must be an integer, got {type(val)}",
            )
            assert isinstance(val, int)
            self.assertGreaterEqual(
                val,
                MINIMUM_PINNED_CAP,
                f"{rel} context_file_max_chars={val} is below minimum {MINIMUM_PINNED_CAP}",
            )

    def test_platform_merged_config_preserves_context_file_max_chars(self):
        """The Dockerfile build-time merge preserves context_file_max_chars for platform."""
        sys.path.insert(0, str(REPO_ROOT / "deploy" / "docker"))
        try:
            from merge_configs import merge
        finally:
            sys.path.pop(0)

        base = yaml.safe_load(SHARED_DEFAULTS.read_text(encoding="utf-8")) or {}
        overlay = yaml.safe_load(PLATFORM_OVERLAY.read_text(encoding="utf-8")) or {}
        merged = merge(base, overlay)

        val = merged.get("context_file_max_chars")
        self.assertIsNotNone(
            val,
            "merged platform config lost 'context_file_max_chars'",
        )
        self.assertIsInstance(val, int)
        assert isinstance(val, int)
        self.assertGreaterEqual(
            val,
            MINIMUM_PINNED_CAP,
            f"merged platform config context_file_max_chars={val} is below {MINIMUM_PINNED_CAP}",
        )

    def test_every_shipped_soul_and_agents_fits_within_pinned_cap(self):
        """All shipped personas and workspace instructions must fit in context_file_max_chars."""
        for profile in ("chat", "platform", "cluster"):
            profile_dir = REPO_ROOT / "agents" / profile
            for filename in ("SOUL.md", "AGENTS.md"):
                doc_path = profile_dir / filename
                if not doc_path.exists():
                    continue
                content = doc_path.read_text(encoding="utf-8")
                length = len(content)
                self.assertLess(
                    length,
                    MINIMUM_PINNED_CAP,
                    f"{doc_path.relative_to(REPO_ROOT)} is {length} chars, which exceeds "
                    f"the pinned context_file_max_chars ({MINIMUM_PINNED_CAP})",
                )

    def test_sabotage_proof_pre_fix_behavior_truncates_souls(self):
        """Prove that without the pin (pre-fix code), both shipped SOULs are truncated."""
        chat_soul = (REPO_ROOT / "agents" / "chat" / "SOUL.md").read_text(encoding="utf-8")
        plat_soul = (REPO_ROOT / "agents" / "platform" / "SOUL.md").read_text(encoding="utf-8")

        # Confirm both exceed 20,000 chars
        self.assertGreater(len(chat_soul), HERMES_FLOOR_CHARS)
        self.assertGreater(len(plat_soul), HERMES_FLOOR_CHARS)

        # Unset / default limit triggers truncation
        _, chat_truncated = simulate_hermes_truncation(chat_soul, max_chars=None)
        _, plat_truncated = simulate_hermes_truncation(plat_soul, max_chars=None)

        self.assertTrue(chat_truncated, "chat SOUL.md must truncate under pre-fix floor")
        self.assertTrue(plat_truncated, "platform SOUL.md must truncate under pre-fix floor")

        # With pinned limit (post-fix), neither truncates
        res_chat, chat_trunc_pinned = simulate_hermes_truncation(chat_soul, max_chars=MINIMUM_PINNED_CAP)
        res_plat, plat_trunc_pinned = simulate_hermes_truncation(plat_soul, max_chars=MINIMUM_PINNED_CAP)

        self.assertFalse(chat_trunc_pinned)
        self.assertFalse(plat_trunc_pinned)
        self.assertEqual(res_chat, chat_soul)
        self.assertEqual(res_plat, plat_soul)


if __name__ == "__main__":
    unittest.main()
