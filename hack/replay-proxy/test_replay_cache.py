"""The answer cache: normalizing and restoring answers, write-once files, the prefix index, served bodies."""

import ast
import calendar
import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

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


class LayoutTest(unittest.TestCase):
    def test_a_name_with_a_trailing_newline_is_outside_the_layout(self):
        for rel in (f"answers/{'a' * 64}.json", f"variants/{'a' * 16}/{'b' * 16}.json",
                    f"alts/{'a' * 64}/{'b' * 64}.json"):
            with self.subTest(rel=rel):
                self.assertTrue(replay_cache.CACHE_REL_RE.match(rel))
                self.assertIsNone(replay_cache.CACHE_REL_RE.match(rel + "\n"))


class WeightsTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.cache = replay_cache.AnswerCache(self.root)

    def test_a_weights_file_is_in_the_directory_layout_but_not_among_the_buckets_objects(self):
        import publish_cache
        rel = f"weights/{'a' * 64}.json"
        self.assertTrue(replay_cache.CACHE_REL_RE.match(rel), "the lane seeds it")
        self.assertIsNone(replay_cache.CACHE_REL_RE.match(rel + "\n"))
        self.assertIsNone(publish_cache.OBJECT_NAME_RE.match(rel), "no record may name one")

    def test_weights_keep_positive_integers_and_cap_them(self):
        a, b, c, d, e = ("%x" % n * 64 for n in range(10, 15))
        self.assertTrue(self.cache.save_weights("f" * 64, {a: 3, b: 0, c: True, d: "2", e: 10 ** 9}))
        self.assertEqual(self.cache.weights("f" * 64), {a: 3, e: replay_cache.MAX_ANSWER_WEIGHT})

    def test_no_file_or_a_file_that_is_not_a_map_gives_no_weights(self):
        self.assertEqual(self.cache.weights("f" * 64), {})
        path = Path(self.root, "weights", "e" * 64 + ".json")
        path.parent.mkdir()
        path.write_text("[1, 2]")
        self.assertEqual(self.cache.weights("e" * 64), {})


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

    def test_an_alternate_is_written_once_and_never_changes(self):
        fp, answer = "a" * 64, "b" * 64
        self.assertTrue(self.cache.save_alternate(fp, answer, {"answer": {"content": "first"}}))
        self.assertFalse(self.cache.save_alternate(fp, answer, {"answer": {"content": "second"}}))
        (name, entry), = self.cache.answers(fp)
        self.assertEqual((name, entry["answer"]["content"]), (answer, "first"))
        self.assertEqual(os.listdir(os.path.join(self.root, replay_cache.ALTS_DIR, fp)), [answer + ".json"])

    def test_concurrent_writers_of_one_alternate_leave_exactly_one_winner(self):
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(
            self.cache.save_alternate("a" * 64, "b" * 64, {"n": i}))) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(True), 1)

    def test_a_keys_answers_come_in_one_order_and_only_hex_names_count(self):
        fp = "a" * 64
        for answer in ("c" * 64, "b" * 64):
            self.cache.save_alternate(fp, answer, {"answer": {"content": answer[0]}})
        Path(self.root, replay_cache.ALTS_DIR, fp, "stray.json").write_text("{}")
        self.assertEqual([name for name, _ in self.cache.answers(fp)], ["b" * 64, "c" * 64])
        for bad in ("../" + fp, fp + "\n", "x"):
            with self.subTest(bad=bad):
                self.assertEqual(self.cache.answers(bad), [])
                with self.assertRaises(ValueError):
                    self.cache.save_alternate(bad, "b" * 64, {})

    def test_an_alternate_counts_as_the_keys_answer_for_slots_and_the_index(self):
        fp = "a" * 64
        self.cache.save_alternate(fp, "b" * 64, {"answer": {"content": "x"}, "fingerprint": fp})
        self.assertTrue(self.cache.has_answer(fp))
        self.assertFalse(self.cache.save_slot("d" * 64, fp) and self.cache.save_slot("d" * 64, "e" * 64))
        self.assertEqual(self.cache.load_slot("d" * 64), fp, "a pointer to a key with an alternate stays")
        self.assertEqual([entry["fingerprint"] for entry in self.cache.entries()], [fp])

    def test_messages(self):
        self.assertTrue(self.cache.save_message("h1", "text \ud83d"))
        self.assertFalse(self.cache.save_message("h1", "other"))
        self.assertEqual(self.cache.load_message("h1"), "text \ud83d")
        self.assertIsNone(self.cache.load_message("h2"))


class PrefixIndexTest(unittest.TestCase):
    def test_candidates_by_longest_prefix_settings_first(self):
        entries = [
            {"fingerprint": "a", "diag_settings_hash": "s", "diag_message_hashes": ["m1", "m2"]},
            {"fingerprint": "b", "diag_settings_hash": "s", "diag_message_hashes": ["m1", "m2", "m3", "m4"]},
        ]
        index = replay_cache.PrefixIndex(iter(entries))
        found, matched = index.candidates("s", ["m1", "m2", "m3", "x"])
        self.assertEqual(([e["fingerprint"] for e in found], matched), (["b"], 3))
        found, matched = index.candidates("s", ["z"])
        self.assertEqual(matched, 0)
        self.assertTrue(found)
        self.assertEqual(index.candidates("other", ["m1"]), ([], 0))

    def test_a_long_conversation_costs_one_fixed_size_key_per_prefix(self):
        hashes = [f"m{i}" for i in range(500)]
        index = replay_cache.PrefixIndex(iter([{"fingerprint": "a", "diag_settings_hash": "s",
                                                "diag_message_hashes": hashes}]))
        self.assertEqual(len(index.by_prefix), 501)
        self.assertEqual({len(key) for key in index.by_prefix}, {64})
        self.assertEqual(index.candidates("s", hashes[:200] + ["x"])[1], 200)


class ModuleLayoutTest(unittest.TestCase):
    def test_every_test_module_defines_its_tests_before_running_them(self):
        # Run as a script, a module's tests defined after its __main__ block never run.
        for path in Path(__file__).resolve().parent.glob("test_*.py"):
            tree = ast.parse(path.read_text())
            main = next((i for i, node in enumerate(tree.body) if isinstance(node, ast.If)
                         and "__main__" in ast.dump(node.test)), None)
            if main is not None:
                later = [n.name for n in tree.body[main + 1:] if isinstance(n, (ast.ClassDef, ast.FunctionDef))]
                self.assertEqual(later, [], path.name)


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




class SimilarityTest(unittest.TestCase):
    def test_json_messages_are_compared_by_their_words(self):
        # Same pods in another order: identical words, though raw JSON glues each line to the next.
        def tool(text):
            return json.dumps({"content": text, "role": "tool"})
        current = tool("web-a 1/1\nweb-b 2/2\nweb-c 1/1")
        reordered = tool("web-c 1/1\nweb-a 1/1\nweb-b 2/2")
        other = tool("web-a 1/1\nweb-b 2/2\napi-x 0/3")
        self.assertGreater(replay_cache.similarity(reordered, current), replay_cache.similarity(other, current))

    def test_tool_call_arguments_count(self):
        message = json.dumps({"role": "assistant", "tool_calls": [{"function": {"name": "kubectl", "arguments": "get pods"}}]})
        self.assertEqual(replay_cache.message_text(message), "kubectl\nget pods")


if __name__ == "__main__":
    unittest.main()
