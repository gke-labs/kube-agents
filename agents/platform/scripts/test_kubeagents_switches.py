"""Tests for kubeagents_switches, the one reader of the earned-autonomy switches."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kubeagents_switches


class TestSwitch(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        patcher = mock.patch.object(kubeagents_switches, "SWITCH_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unset_is_off(self) -> None:
        with mock.patch.dict(os.environ, clear=True):
            for name in ("POST_MERGE_CHECK", "AUTOMERGE", "DIAL"):
                self.assertEqual(kubeagents_switches.switch(name), "off")

    def test_known_values_from_the_variable(self) -> None:
        env = {
            "KUBEAGENTS_POST_MERGE_CHECK": " Report ",
            "KUBEAGENTS_AUTOMERGE": "practice",
            "KUBEAGENTS_DIAL": "on",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(kubeagents_switches.switch("POST_MERGE_CHECK"), "report")
            self.assertEqual(kubeagents_switches.switch("AUTOMERGE"), "practice")
            self.assertEqual(kubeagents_switches.switch("DIAL"), "on")

    def test_unknown_value_is_off(self) -> None:
        # A value from another switch, or a boolean, does not turn one on.
        env = {"KUBEAGENTS_POST_MERGE_CHECK": "on", "KUBEAGENTS_DIAL": "true", "KUBEAGENTS_AUTOMERGE": "silent"}
        with mock.patch.dict(os.environ, env, clear=True):
            for name in ("POST_MERGE_CHECK", "AUTOMERGE", "DIAL"):
                self.assertEqual(kubeagents_switches.switch(name), "off")

    def test_unknown_switch_is_off(self) -> None:
        with mock.patch.dict(os.environ, {"KUBEAGENTS_AUDIT_AUTO_PROMOTE": "true"}, clear=True):
            self.assertEqual(kubeagents_switches.switch("AUDIT_AUTO_PROMOTE"), "off")
            self.assertEqual(kubeagents_switches.switch("post_merge_check"), "off")

    def test_the_root_owned_file_wins_over_the_variable(self) -> None:
        # A session that exports the variable cannot turn on what the operator left off.
        (self.dir / "automerge").write_text("off\n", encoding="utf-8")
        (self.dir / "post-merge-check").write_text("silent\n", encoding="utf-8")
        env = {"KUBEAGENTS_AUTOMERGE": "on", "KUBEAGENTS_POST_MERGE_CHECK": "report"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(kubeagents_switches.switch("AUTOMERGE"), "off")
            self.assertEqual(kubeagents_switches.switch("POST_MERGE_CHECK"), "silent")

    def test_the_paths_are_the_ones_the_sandbox_entrypoint_writes(self) -> None:
        entrypoint = Path(__file__).resolve().parents[3] / "deploy" / "sandbox" / "entrypoint.sh"
        content = entrypoint.read_text(encoding="utf-8")
        with mock.patch.object(kubeagents_switches, "SWITCH_DIR", Path("/run/kube-agents-sandbox")):
            for name in kubeagents_switches.VALUES:
                self.assertIn(f'SANDBOX_{name}_FILE="{kubeagents_switches._file(name)}"', content)

    def test_main_prints_every_switch(self) -> None:
        with (
            mock.patch.dict(os.environ, {"KUBEAGENTS_DIAL": "on"}, clear=True),
            mock.patch("builtins.print") as printed,
        ):
            self.assertEqual(kubeagents_switches.main([]), 0)
        lines = [call.args[0] for call in printed.call_args_list]
        self.assertEqual(
            lines,
            ["KUBEAGENTS_AUTOMERGE=off", "KUBEAGENTS_DIAL=on", "KUBEAGENTS_POST_MERGE_CHECK=off"],
        )


if __name__ == "__main__":
    unittest.main()
