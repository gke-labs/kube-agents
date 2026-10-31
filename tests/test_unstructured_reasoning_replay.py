"""hack/eval/unstructured-reasoning-replay/replay.py without a gateway: the two arms' request
bodies, and the exit code the red/green record depends on."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPLAY = Path(__file__).resolve().parents[1] / "hack" / "eval" / "unstructured-reasoning-replay" / "replay.py"
_spec = importlib.util.spec_from_file_location("urr_replay", _REPLAY)
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)

CALL = {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
FIXTURE = {
    "model": "model-default",
    "tools": [],
    "messages": [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "run the audit"},
        {"role": "assistant", "content": "thought\nPlan: read the SOP.", "tool_calls": [CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "1|# SOP"},
    ],
}


def reply(content):
    return {"choices": [{"message": {"content": content, "tool_calls": []}}]}


class RequestBodyTest(unittest.TestCase):
    def test_main_sends_the_leaked_reply_as_built(self):
        body = replay.request_body(FIXTURE, "main", "model-default")
        self.assertEqual(body["messages"], FIXTURE["messages"])

    def test_patched_sends_the_marker_and_keeps_the_tool_call(self):
        body = replay.request_body(FIXTURE, "patched", "alias")
        leaked = body["messages"][2]
        self.assertEqual(leaked["content"], replay.unstructured_reasoning.PLACEHOLDER)
        self.assertEqual(leaked["tool_calls"], [CALL])
        self.assertEqual(body["messages"][1:2] + body["messages"][3:], FIXTURE["messages"][1:2] + FIXTURE["messages"][3:])
        self.assertEqual(body["model"], "alias")
        self.assertTrue(FIXTURE["messages"][2]["content"].startswith("thought"), "the fixture was mutated")


class ExitCodeTest(unittest.TestCase):
    def run_arm(self, arm, contents, path):
        with mock.patch.object(replay, "call", side_effect=[reply(c) for c in contents]), \
             mock.patch("builtins.print"):
            return replay.main([str(path), "--arm", arm, "--runs", str(len(contents))])

    def test_main_passes_only_when_every_run_imitates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(json.dumps(FIXTURE))
            self.assertEqual(self.run_arm("main", ["thought\nx"] * 3, path), 0)
            self.assertEqual(self.run_arm("main", ["thought\nx", "", "thought: y"], path), 1)
            self.assertEqual(self.run_arm("patched", ["", "", "a plain answer"], path), 0)
            self.assertEqual(self.run_arm("patched", ["", "thought\nx", ""], path), 1)


if __name__ == "__main__":
    unittest.main()
