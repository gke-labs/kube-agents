"""The answer cache: normalizing and restoring answers, write-once files, the prefix index, served bodies."""

import calendar
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime

import normalizer
import replay_cache

NOW = calendar.timegm(datetime(2026, 11, 3, 12, 0, 0).timetuple())


def mapping_for(text):
    return normalizer.normalize_request({"messages": [{"role": "user", "content": text}]}, NOW).mapping


ANSWER = {
    "role": "assistant",
    "content": "Checking t_11111111 on web-7d4b9c8f6d-x2k9p",
    "tool_calls": [{"id": "call_t_11111111__thought__x", "type": "function",
                    "function": {"name": "kanban_show", "arguments": json.dumps({"task_id": "t_11111111"})},
                    "provider_specific_fields": {"thought_signature": "t_11111111"}}],
}


class NormalizeRestoreTest(unittest.TestCase):
    def test_round_trip_puts_the_current_values_in(self):
        saved = replay_cache.normalize_answer(ANSWER, mapping_for("task t_11111111 pod web-7d4b9c8f6d-x2k9p"))
        self.assertEqual(saved["content"], "Checking ⟦TASK_1⟧ on web-⟦POD_1⟧")
        restored, missing = replay_cache.restore_answer(saved, mapping_for("task t_22222222 pod web-5f6c7d8b9c-q8rtz"))
        self.assertEqual(missing, [])
        self.assertEqual(restored["content"], "Checking t_22222222 on web-5f6c7d8b9c-q8rtz")
        self.assertEqual(json.loads(restored["tool_calls"][0]["function"]["arguments"]), {"task_id": "t_22222222"})

    def test_opaque_fields_are_never_rewritten(self):
        saved = replay_cache.normalize_answer(ANSWER, mapping_for("task t_11111111"))
        call = saved["tool_calls"][0]
        self.assertEqual(call["id"], "call_t_11111111__thought__x")
        self.assertEqual(call["provider_specific_fields"], {"thought_signature": "t_11111111"})

    def test_an_unbound_placeholder_is_reported(self):
        saved = replay_cache.normalize_answer(ANSWER, mapping_for("task t_11111111"))
        _, missing = replay_cache.restore_answer(saved, mapping_for("nothing to bind"))
        self.assertEqual(missing, ["⟦TASK_1⟧", "⟦TASK_1⟧"])


class AnswerCacheTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cache = replay_cache.AnswerCache(self.root)

    def test_first_write_wins_and_leaves_no_tmp_file(self):
        self.assertTrue(self.cache.save("fp", {"answer": {"content": "first"}}))
        self.assertFalse(self.cache.save("fp", {"answer": {"content": "second"}}))
        self.assertEqual(self.cache.load("fp")["answer"]["content"], "first")
        self.assertEqual(os.listdir(os.path.join(self.root, replay_cache.ANSWERS_DIR)), ["fp.json"])

    def test_concurrent_writers_leave_exactly_one_winner(self):
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.cache.save("fp", {"n": i})))
                   for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(True), 1)
        self.assertEqual(os.listdir(os.path.join(self.root, replay_cache.ANSWERS_DIR)), ["fp.json"])

    def test_missing_and_corrupt_entries_load_as_none(self):
        self.assertIsNone(self.cache.load("absent"))
        os.makedirs(os.path.join(self.root, replay_cache.ANSWERS_DIR))
        with open(os.path.join(self.root, replay_cache.ANSWERS_DIR, "bad.json"), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(self.cache.load("bad"))
        self.assertEqual(list(self.cache.entries()), [])

    def test_messages(self):
        self.assertTrue(self.cache.save_message("h1", "text \ud83d"))
        self.assertFalse(self.cache.save_message("h1", "other"))
        self.assertEqual(self.cache.load_message("h1"), "text \ud83d")
        self.assertIsNone(self.cache.load_message("h2"))


class PrefixIndexTest(unittest.TestCase):
    def test_closest_by_longest_prefix_settings_first(self):
        entries = [
            {"fingerprint": "a", "diag_settings_hash": "s", "diag_message_hashes": ["m1", "m2"]},
            {"fingerprint": "b", "diag_settings_hash": "s", "diag_message_hashes": ["m1", "m2", "m3", "m4"]},
        ]
        index = replay_cache.PrefixIndex(iter(entries))
        best, matched = index.closest("s", ["m1", "m2", "m3", "x"])
        self.assertEqual((best["fingerprint"], matched), ("b", 3))
        best, matched = index.closest("s", ["z"])
        self.assertEqual(matched, 0)
        self.assertIsNotNone(best)
        self.assertEqual(index.closest("other", ["m1"]), (None, 0))


class ServedBodyTest(unittest.TestCase):
    ENTRY = {"finish_reason": "tool_calls", "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

    def test_completion_body(self):
        body = json.loads(replay_cache.completion_body(ANSWER, self.ENTRY, "model-default", "f" * 64))
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "model-default")
        self.assertEqual(body["choices"][0]["message"], ANSWER)
        self.assertEqual(body["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(body["usage"]["prompt_tokens"], 10)
        self.assertTrue(body["id"].startswith("replay-"))

    def test_stream_events(self):
        events = replay_cache.stream_events(ANSWER, self.ENTRY, "model-default", "f" * 64)
        self.assertEqual(events[-1], b"data: [DONE]\n\n")
        first = json.loads(events[0].decode()[len("data: "):])
        last = json.loads(events[1].decode()[len("data: "):])
        self.assertEqual(first["object"], "chat.completion.chunk")
        self.assertEqual(first["choices"][0]["delta"]["tool_calls"][0]["index"], 0)
        self.assertEqual(first["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(last["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(last["usage"]["completion_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
