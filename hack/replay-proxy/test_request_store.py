"""Scrubbing and storing request text: the repository's scrubbers, de-duplication, fail-closed."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import request_store

REPO_ROOT = Path(__file__).resolve().parents[2]
REDACTOR = str(REPO_ROOT / "agents/chat/defaults/plugins/common/redactor.py")
RULES = str(REPO_ROOT / "agents/platform/scripts/worker_transcript.py")
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SECRET_YAML = "apiVersion: v1\nkind: Secret\ndata:\n  password: aHVudGVyMg==\n"


def body(*extra_messages):
    return {
        "model": "model-default",
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are the platform agent."},
            {"role": "user", "content": f"work kanban task t_11111111, token {TOKEN}"},
            *extra_messages,
        ],
        "tools": [{"type": "function", "function": {"name": "terminal"}}],
    }


def read_all(root):
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            out.append(Path(dirpath, name).read_text(encoding="utf-8"))
    return "\n".join(out)


class ScrubberTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.scrubber = request_store.Scrubber(REDACTOR, RULES)
        self.store = request_store.RequestStore(self.root, self.scrubber)

    def test_repository_scrubbers_load(self):
        self.assertTrue(self.scrubber.available, self.scrubber.error)

    def test_credentials_never_reach_disk(self):
        tool_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "terminal", "arguments": json.dumps({"command": "kubectl get pods"})}}],
        }
        result = {"role": "tool", "tool_call_id": "call_1", "content": SECRET_YAML}
        self.assertIsNone(self.store.store("r1", "fp", body(tool_call, result)))
        written = read_all(self.root)
        self.assertNotIn(TOKEN, written)
        self.assertNotIn("aHVudGVyMg==", written)
        self.assertIn("work kanban task t_11111111", written)

    def test_secret_read_result_is_withheld(self):
        tool_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_s", "type": "function", "function": {
                "name": "terminal", "arguments": json.dumps({"command": "kubectl get secret db -o yaml"})}}],
        }
        result = {"role": "tool", "tool_call_id": "call_s", "content": "plain looking value hunter2"}
        self.assertIsNone(self.store.store("r1", "fp", body(tool_call, result)))
        written = read_all(self.root)
        self.assertNotIn("hunter2", written)
        self.assertIn(request_store.WITHHELD_READ_RESULT, written)

    def test_each_message_is_stored_once(self):
        self.store.store("r1", "fp1", body())
        self.store.store("r2", "fp2", body({"role": "assistant", "content": "done"}))
        blobs = os.listdir(os.path.join(self.root, request_store.BLOBS_DIR))
        # system, user, tools, settings shared; one new assistant message.
        self.assertEqual(len(blobs), 5)
        manifests = sorted(os.listdir(os.path.join(self.root, request_store.REQUESTS_DIR)))
        self.assertEqual(manifests, ["r1.json", "r2.json"])
        r2 = json.loads(Path(self.root, request_store.REQUESTS_DIR, "r2.json").read_text())
        self.assertEqual(r2["fingerprint"], "fp2")
        self.assertEqual(len(r2["messages"]), 3)
        r1 = json.loads(Path(self.root, request_store.REQUESTS_DIR, "r1.json").read_text())
        self.assertEqual(r1["messages"], r2["messages"][:2])

    def test_answer_change_detection(self):
        self.assertFalse(self.scrubber.answer_changes({"content": "the pod is healthy"}))
        self.assertTrue(self.scrubber.answer_changes({"content": f"use {TOKEN}"}))


class FailClosedTest(unittest.TestCase):
    def test_missing_redactor_stores_nothing(self):
        root = tempfile.mkdtemp()
        scrubber = request_store.Scrubber(os.path.join(root, "missing.py"), RULES)
        self.assertFalse(scrubber.available)
        self.assertIsNotNone(scrubber.error)
        store = request_store.RequestStore(root, scrubber)
        self.assertEqual(store.store("r1", "fp", body()), "scrubbers_unavailable")
        self.assertEqual(os.listdir(root), [])

    def test_missing_transcript_rules_stores_nothing(self):
        root = tempfile.mkdtemp()
        scrubber = request_store.Scrubber(REDACTOR, os.path.join(root, "missing.py"))
        self.assertFalse(scrubber.available)
        self.assertEqual(request_store.RequestStore(root, scrubber).store("r1", "fp", body()), "scrubbers_unavailable")
        self.assertEqual(os.listdir(root), [])

    def test_redactor_without_the_class_stores_nothing(self):
        root = tempfile.mkdtemp()
        broken = os.path.join(root, "redactor.py")
        Path(broken).write_text("x = 1\n")
        scrubber = request_store.Scrubber(broken, RULES)
        self.assertFalse(scrubber.available)

    def test_scrubber_that_raises_stores_nothing(self):
        root = tempfile.mkdtemp()
        scrubber = request_store.Scrubber(REDACTOR, RULES)

        class Exploding:
            @staticmethod
            def redact(_value):
                raise ValueError("boom")

        scrubber.redactor = Exploding
        reason = request_store.RequestStore(root, scrubber).store("r1", "fp", body())
        self.assertTrue(reason.startswith("scrub_failed"), reason)
        self.assertEqual(os.listdir(root), [])

    def test_unavailable_scrubber_counts_every_answer_as_changed(self):
        scrubber = request_store.Scrubber("/nonexistent/redactor.py", RULES)
        self.assertTrue(scrubber.answer_changes({"content": "fine"}))


if __name__ == "__main__":
    unittest.main()
