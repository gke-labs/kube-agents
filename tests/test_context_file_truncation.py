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
4. Truncation logic preserves full persona content when `context_file_max_chars` is read
   from shipped configs, and truncates when the config key is removed (sabotage verification).
   The in-CI simulation mirrors Hermes `_truncate_content` byte-for-byte.
5. Real Hermes prompt builder validation:
   - In CI (`requirements-test.txt` excludes `hermes-agent` by design): test 5 skips
     gracefully via `unittest.SkipTest`.
   - In Docker image build: `deploy/docker/Dockerfile` asserts via real Hermes that
     `/opt/platform-template`, `/opt/chat-template`, and `/opt/cluster-template`
     each evaluate `_get_context_file_max_chars() == 100000`.
   - In local / development environments: test 5 executes real Hermes
     `_get_context_file_max_chars` and `_truncate_content`, confirming warning emissions
     and asserting parity between `simulate_hermes_truncation` and real Hermes.
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
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


def get_merged_platform_config() -> dict:
    """Merge deploy/shared/defaults/config.yaml with agents/platform/config.yaml."""
    sys.path.insert(0, str(REPO_ROOT / "deploy" / "docker"))
    try:
        from merge_configs import merge
    finally:
        sys.path.pop(0)

    base = yaml.safe_load(SHARED_DEFAULTS.read_text(encoding="utf-8")) or {}
    overlay = yaml.safe_load(PLATFORM_OVERLAY.read_text(encoding="utf-8")) or {}
    return merge(base, overlay)


def load_configured_cap(profile: str) -> int | None:
    """Read context_file_max_chars from the profile's on-disk configuration."""
    if profile == "chat":
        doc = yaml.safe_load(CHAT_CONFIG.read_text(encoding="utf-8")) or {}
    elif profile == "cluster":
        doc = yaml.safe_load(CLUSTER_CONFIG.read_text(encoding="utf-8")) or {}
    elif profile == "platform":
        doc = get_merged_platform_config()
    else:
        raise ValueError(f"Unknown profile: {profile}")
    return doc.get("context_file_max_chars")


def simulate_hermes_truncation(
    content: str,
    filename: str = "SOUL.md",
    max_chars: int | None = None,
) -> tuple[str, bool]:
    """Mirror Hermes _truncate_content behavior when context_length is unknown."""
    limit = max_chars if (isinstance(max_chars, int) and max_chars > 0) else HERMES_FLOOR_CHARS
    if len(content) <= limit:
        return content, False
    head_chars = int(limit * HERMES_HEAD_RATIO)
    tail_chars = int(limit * HERMES_TAIL_RATIO)
    marker = (
        f"\n\n[...truncated {filename}: kept {head_chars}+{tail_chars} of "
        f"{len(content)} chars. The middle is omitted — if you need the full "
        f"instructions, read the complete file with the read_file tool: "
        f"{filename}]\n\n"
    )
    truncated = content[:head_chars] + marker + content[-tail_chars:]
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
        merged = get_merged_platform_config()
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
        """Prove that configs on disk prevent truncation, and revert causes truncation."""
        chat_soul = (REPO_ROOT / "agents" / "chat" / "SOUL.md").read_text(encoding="utf-8")
        plat_soul = (REPO_ROOT / "agents" / "platform" / "SOUL.md").read_text(encoding="utf-8")

        # Confirm both exceed 20,000 chars
        self.assertGreater(len(chat_soul), HERMES_FLOOR_CHARS)
        self.assertGreater(len(plat_soul), HERMES_FLOOR_CHARS)

        # Unset / default limit (pre-fix) triggers truncation
        _, chat_truncated = simulate_hermes_truncation(chat_soul, max_chars=None)
        _, plat_truncated = simulate_hermes_truncation(plat_soul, max_chars=None)
        self.assertTrue(chat_truncated, "chat SOUL.md must truncate under pre-fix floor")
        self.assertTrue(plat_truncated, "platform SOUL.md must truncate under pre-fix floor")

        # Read actual configured caps from shipped profile configs on disk
        chat_cap = load_configured_cap("chat")
        plat_cap = load_configured_cap("platform")

        self.assertIsNotNone(chat_cap, "chat config must declare context_file_max_chars")
        self.assertIsNotNone(plat_cap, "platform config must declare context_file_max_chars")

        # With the on-disk config applied, neither truncates
        res_chat, chat_trunc_pinned = simulate_hermes_truncation(chat_soul, max_chars=chat_cap)
        res_plat, plat_trunc_pinned = simulate_hermes_truncation(plat_soul, max_chars=plat_cap)

        self.assertFalse(chat_trunc_pinned, "chat SOUL.md must not truncate with configured cap")
        self.assertFalse(plat_trunc_pinned, "platform SOUL.md must not truncate with configured cap")
        self.assertEqual(res_chat, chat_soul)
        self.assertEqual(res_plat, plat_soul)

    def test_hermes_prompt_builder_honours_shipped_configs(self):
        """If Hermes is available, assert real prompt_builder honours context_file_max_chars."""
        try:
            import agent.prompt_builder as pb
        except ImportError:
            raise unittest.SkipTest("Hermes agent.prompt_builder not available in environment")

        chat_soul = (REPO_ROOT / "agents" / "chat" / "SOUL.md").read_text(encoding="utf-8")
        orig_hermes_home = os.environ.get("HERMES_HOME")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = pathlib.Path(tmp)

            # 1. Configured state (post-fix): context_file_max_chars: 100000
            shipped_chat = yaml.safe_load(CHAT_CONFIG.read_text(encoding="utf-8")) or {}
            (tmp_dir / "config.yaml").write_text(yaml.safe_dump(shipped_chat), encoding="utf-8")
            os.environ["HERMES_HOME"] = str(tmp_dir)

            cap = pb._get_context_file_max_chars()
            self.assertEqual(cap, 100_000)

            pb.drain_truncation_warnings()
            result = pb._truncate_content(chat_soul, "SOUL.md")
            warnings = pb.drain_truncation_warnings()

            self.assertEqual(result, chat_soul, "Hermes must not truncate when cap is pinned")
            self.assertEqual(len(warnings), 0, "No truncation warnings should be emitted")

            # 2. Unconfigured state (pre-fix / reverted sabotage): context_file_max_chars removed
            reverted_chat = dict(shipped_chat)
            reverted_chat.pop("context_file_max_chars", None)
            (tmp_dir / "config.yaml").write_text(yaml.safe_dump(reverted_chat), encoding="utf-8")

            cap_reverted = pb._get_context_file_max_chars()
            self.assertEqual(cap_reverted, HERMES_FLOOR_CHARS)

            pb.drain_truncation_warnings()
            result_reverted = pb._truncate_content(chat_soul, "SOUL.md")
            warnings_reverted = pb.drain_truncation_warnings()

            self.assertNotEqual(result_reverted, chat_soul, "Hermes must truncate under 20k floor")
            self.assertIn("[...truncated SOUL.md", result_reverted)
            self.assertEqual(len(warnings_reverted), 1, "Must emit exactly 1 truncation warning")
            self.assertIn("TRUNCATED", warnings_reverted[0])

            # 3. Assert in-CI simulation parity with real Hermes _truncate_content
            sim_reverted, sim_was_truncated = simulate_hermes_truncation(chat_soul, "SOUL.md", max_chars=HERMES_FLOOR_CHARS)
            self.assertTrue(sim_was_truncated, "simulate_hermes_truncation must mark content truncated")
            self.assertEqual(
                sim_reverted,
                result_reverted,
                "simulate_hermes_truncation must produce byte-for-byte identical output to Hermes _truncate_content",
            )

        if orig_hermes_home is not None:
            os.environ["HERMES_HOME"] = orig_hermes_home
        else:
            os.environ.pop("HERMES_HOME", None)


if __name__ == "__main__":
    unittest.main()
