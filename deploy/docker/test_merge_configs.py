"""Tests for the build-time config merge that produces the platform profile.

Run: python3 -m unittest discover -s deploy/docker -p 'test_*.py'

`merge_configs.py` runs exactly once, inside `docker build`, and its output is
baked into /opt/platform-template/config.yaml — the file the Platform Agent
actually runs on. A wrong merge is therefore invisible until an agent boots
with the wrong tools enabled, and there is no runtime that would notice.

The list rule is the reason this file exists. Lists are *unioned*, not
replaced, so an entry deleted from agents/platform/config.yaml comes back if
deploy/shared/defaults/config.yaml still names it. That behaviour is load
bearing (it is what lets the overlay add a toolset without restating the base)
and it is documented in a comment in agents/platform/config.yaml, but nothing
enforced it.
"""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

MERGE_PY = Path(__file__).resolve().parent / "merge_configs.py"

_spec = importlib.util.spec_from_file_location("merge_configs", MERGE_PY)
merge_configs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_configs)
merge = merge_configs.merge


class MergeSemanticsTest(unittest.TestCase):
    def test_the_overlay_wins_for_a_scalar(self):
        self.assertEqual({"a": 2}, merge({"a": 1}, {"a": 2}))

    def test_a_key_only_the_base_has_survives(self):
        self.assertEqual({"a": 1, "b": 2}, merge({"a": 1}, {"b": 2}))

    def test_nested_dicts_merge_rather_than_replace(self):
        base = {"model": {"provider": "custom", "api_key": "none"}}
        overlay = {"model": {"provider": "vertex"}}
        # api_key is not restated by the overlay and must not be dropped: the
        # real overlay sets one key of `agent:` and expects the rest to stand.
        self.assertEqual(
            {"model": {"provider": "vertex", "api_key": "none"}},
            merge(base, overlay),
        )

    def test_lists_are_unioned_and_keep_base_order(self):
        # Not replaced. `platform_toolsets.cli` relies on this to add
        # mcp-platform_control without restating hermes-cli.
        self.assertEqual(
            ["hermes-cli", "mcp-gke", "mcp-platform_control"],
            merge(
                {"cli": ["hermes-cli", "mcp-gke"]},
                {"cli": ["hermes-cli", "mcp-platform_control"]},
            )["cli"],
        )

    def test_an_entry_the_overlay_dropped_comes_back_from_the_base(self):
        """The trap the comment in agents/platform/config.yaml warns about.

        Removing a tool means removing it from *both* files. Deleting it from
        the overlay alone silently re-enables it at image build, which is how
        `mcp-agent_common` outlived the decision to remove it.
        """
        merged = merge({"enabled": ["gone", "kept"]}, {"enabled": ["kept"]})
        self.assertIn("gone", merged["enabled"])

    def test_a_type_change_takes_the_overlay_whole(self):
        self.assertEqual({"a": {"b": 1}}, merge({"a": [1, 2]}, {"a": {"b": 1}}))
        self.assertEqual({"a": [1]}, merge({"a": {"b": 1}}, {"a": [1]}))

    def test_a_list_of_dicts_is_a_build_break_not_a_bad_merge(self):
        """Known limitation, pinned so it surfaces here and not in a build.

        The union goes through `dict.fromkeys`, so list *elements* must be
        hashable. No config merged today holds a list of mappings; the first
        one to do so fails `docker build` with this TypeError rather than
        producing a quietly wrong config, which is the safe direction — but
        the failure would otherwise arrive with no explanation attached.
        """
        with self.assertRaises(TypeError):
            merge({"x": [{"a": 1}]}, {"x": [{"b": 2}]})


class CommandLineTest(unittest.TestCase):
    """The Dockerfile's actual invocation: merge into `base`, in place."""

    def run_merge(self, base_text, overlay_text):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.yaml"
            overlay = Path(tmp) / "overlay.yaml"
            if base_text is not None:
                base.write_text(base_text)
            if overlay_text is not None:
                overlay.write_text(overlay_text)
            subprocess.run(
                [sys.executable, str(MERGE_PY), str(base), str(overlay)],
                check=True,
                capture_output=True,
            )
            return yaml.safe_load(base.read_text())

    def test_the_base_file_is_rewritten_in_place(self):
        merged = self.run_merge(
            "agent:\n  max_turns: 90\n", "agent:\n  max_turns: 250\n"
        )
        self.assertEqual({"agent": {"max_turns": 250}}, merged)

    def test_a_missing_file_reads_as_empty_rather_than_failing(self):
        # Both sides are optional on purpose — an agent may ship an overlay
        # with no base, or a base with no overlay.
        self.assertEqual({"a": 1}, self.run_merge(None, "a: 1\n"))
        self.assertEqual({"a": 1}, self.run_merge("a: 1\n", None))

    def test_an_empty_file_reads_as_empty_rather_than_none(self):
        # yaml.safe_load("") is None, and merge(None, {...}) would return the
        # overlay by the type-mismatch rule -- correct here, but only because
        # main() coerces both sides to {} first.
        self.assertEqual({"a": 1}, self.run_merge("", "a: 1\n"))
        self.assertEqual({"a": 1}, self.run_merge("a: 1\n", ""))


class RealConfigTest(unittest.TestCase):
    """The merge the Dockerfile performs, against the files it performs it on."""

    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        self.base = yaml.safe_load(
            (root / "deploy" / "shared" / "defaults" / "config.yaml").read_text()
        )
        self.overlay = yaml.safe_load(
            (root / "agents" / "platform" / "config.yaml").read_text()
        )
        self.merged = merge(self.base, self.overlay)

    def test_the_platform_profile_keeps_both_sides(self):
        # From the overlay only.
        self.assertIn("platform_control", self.merged["mcp_servers"])
        # From the base only -- the overlay names neither.
        self.assertIn("developer_knowledge", self.merged["mcp_servers"])
        self.assertEqual("ddgs", self.merged["web"]["backend"])

    def test_the_environment_probe_stays_off(self):
        # `agent:` is set by both files -- the base for environment_probe, the
        # overlay for max_turns. A replace-rather-than-merge would lose one.
        self.assertIs(False, self.merged["agent"]["environment_probe"])
        self.assertEqual(250, self.merged["agent"]["max_turns"])

    def test_no_agent_common_toolset_survives_the_merge(self):
        # Both copies had to be deleted; this is the assertion that says so.
        for surface in self.merged["platform_toolsets"].values():
            self.assertNotIn("mcp-agent_common", surface)
        self.assertNotIn("agent_common", self.merged["mcp_servers"])


if __name__ == "__main__":
    unittest.main()
