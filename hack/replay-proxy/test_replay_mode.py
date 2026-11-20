"""Record and replay modes end to end against the fake LiteLLM, and the guard on serving."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import normalizer
import replay_cache
import replay_proxy
from test_replay_proxy import REDACTOR, RULES, TOKEN, FakeLiteLLM, ProxyTestCase, chat

HERE = Path(__file__).resolve().parent
# A fixed clock for the tests that move it: 2027-01-15T08:00:00Z.
CLOCK = 1_800_000_000
MINUTE = 60
DAY = 86400
# How late the first request's analysis is queued in the ordering test.
LATE_SUBMIT_SECONDS = 0.3
COMPLETIONS = "/v1/chat/completions"
SUBPROCESS_TIMEOUT_SECONDS = 30


def sse_message(payload):
    """The answer, usage and finish reason a client assembles from an SSE body, read as the proxy reads one."""
    observer = replay_proxy.ResponseObserver(streaming=True)
    observer.feed(payload)
    observer.finish()
    return observer.message, observer.usage, observer.finish_reason


class GuardTest(unittest.TestCase):
    def test_replay_refuses_without_allow_serve(self):
        with self.assertRaises(ValueError) as caught:
            replay_proxy.ShadowProxy(replay_proxy.Config(mode="replay"))
        self.assertIn("REPLAY_ALLOW_SERVE=yes", str(caught.exception))

    def test_unknown_mode_refuses(self):
        with self.assertRaises(ValueError):
            replay_proxy.ShadowProxy(replay_proxy.Config(mode="serve"))

    def test_main_exits_non_zero_with_a_clear_message(self):
        env = dict(os.environ, REPLAY_MODE="replay", REPLAY_LISTEN_PORT="0")
        env.pop("REPLAY_ALLOW_SERVE", None)
        done = subprocess.run([sys.executable, str(HERE / "replay_proxy.py")], env=env, capture_output=True,
                              text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)
        self.assertEqual(done.returncode, replay_proxy.EXIT_BAD_CONFIG)
        self.assertIn("REPLAY_ALLOW_SERVE=yes", done.stderr)

    def test_allow_serve_reads_only_yes(self):
        for value, allowed in (("yes", True), ("true", False), ("1", False), ("", False)):
            config = replay_proxy.Config.from_env({"REPLAY_MODE": "replay", "REPLAY_ALLOW_SERVE": value})
            self.assertEqual(config.allow_serve, allowed, value)
        config = replay_proxy.Config.from_env({"REPLAY_CACHE_DIR": "/c"})
        self.assertEqual((config.mode, config.cache_dir), ("shadow", "/c"))


class AnalyzeCase(unittest.TestCase):
    """Record and replay proxies over one cache, driven through ShadowProxy.analyze and lookup."""

    def setUp(self):
        self.cache_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache_dir, ignore_errors=True)

    def proxy(self, mode="replay", build="r"):
        config = replay_proxy.Config(mode=mode, allow_serve=mode == "replay", cache_dir=self.cache_dir,
                                     store_dir=os.path.join(self.cache_dir, "store-" + build),
                                     redactor_path=REDACTOR, transcript_rules_path=RULES,
                                     litellm_config_checksum="abc", litellm_image="img", job_build=build)
        proxy = replay_proxy.ShadowProxy(config, out=io.StringIO())
        self.addCleanup(proxy.close)
        return proxy

    @staticmethod
    def upstream(answer):
        return {"status": 200, "latency_ms": 1, "client_disconnected": False, "upstream_incomplete": False,
                "observe_failed": False, "observe_too_large": False, "usage": {"prompt_tokens": 10},
                "headers": {"x-litellm-model-group": "m", "x-litellm-attempted-fallbacks": "0"},
                "message": answer, "finish_reason": "stop"}

    def recorded(self, body, answer, at=None):
        """Record mode's log line for `body` answered with `answer`."""
        return self.proxy("record", "x").analyze(body, time.time() if at is None else at, self.upstream(answer))


class SlotCacheCase(AnalyzeCase):
    """A replay proxy over a cache filled by record mode."""

    def record(self, body, answer, others=()):
        """Record `answer` to `body`, and each assistant message in `body` and `others` as the answer to what preceded it."""
        for other in (body, *others):
            for index, message in enumerate(other["messages"]):
                if message["role"] == "assistant":
                    asked = dict(other, messages=other["messages"][:index])
                    self.assertEqual(self.recorded(asked, message)["unstorable"], [], message)
        self.assertEqual(self.recorded(body, answer)["unstorable"], [])


class AnswerSlotGuardTest(SlotCacheCase):
    """A saved answer is served through a slot only if every value it names is bound outside the answers."""

    @staticmethod
    def body(said, ticket="ticket Latte"):
        return {"model": "m", "messages": [
            {"role": "user", "content": "work kanban task t_bbbbbbbb"},
            {"role": "assistant", "content": said},
            {"role": "user", "content": ticket},
        ]}

    def test_a_value_only_an_answer_named_is_not_served_through_a_slot(self):
        # Bob named his own pod in his yes; Alice's yes named another. Neither yes
        # can be a recorded answer (each names a pod its request never bound),
        # so record mode refuses them; the slot guard is the second line, met by
        # noting them by hand.
        bob = self.body("Yes, web-7d4b9c8f6d-x2k9p.")
        alice_yes = self.body("Yes please, web-5f6c7d8b9c-m8n7q.")
        opening = dict(bob, messages=bob["messages"][:1])
        self.assertEqual(self.recorded(opening, bob["messages"][1])["unstorable"], ["unbound_identifier"])
        self.recorded(bob, {"role": "assistant", "content": "restart web-7d4b9c8f6d-x2k9p"})
        context = normalizer.KeyContext("abc", "img")
        for said in (bob["messages"][1], alice_yes["messages"][1]):
            replay_cache.AnswerCache(self.cache_dir).add_variant(*normalizer.answer_variant(
                normalizer.normalize_request(opening, time.time()), said, context))
        hit, refused = self.proxy().lookup(alice_yes, time.time())
        self.assertIsNone(hit, "Bob's answer would restart the pod Alice's yes named")
        self.assertEqual(refused, {"via": "answer_slots", "guard": "value_only_in_answers"})

    def test_a_value_bound_by_the_tool_output_is_served(self):
        bob = self.body("Yes.", ticket="ticket Latte for web-7d4b9c8f6d-x2k9p")
        alice_yes = self.body("Yes please.", ticket="ticket Latte for web-5f6c7d8b9c-m8n7q")
        self.record(bob, {"role": "assistant", "content": "restart web-7d4b9c8f6d-x2k9p"}, others=[alice_yes])
        hit, _ = self.proxy().lookup(alice_yes, time.time())
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0]["content"], "restart web-5f6c7d8b9c-m8n7q")
        self.assertEqual(hit[3], "answer_slots")


class SlotPointerTest(SlotCacheCase):
    """A slot pointer is written after its entry, and one left dangling heals."""

    BODY = {"model": "m", "messages": [{"role": "user", "content": "work kanban task t_bbbbbbbb"},
                                       {"role": "assistant", "content": "Yes."},
                                       {"role": "user", "content": "ticket Latte"}]}
    ANSWER = {"role": "assistant", "content": "done"}

    def test_a_failed_entry_write_leaves_no_slot_and_still_logs(self):
        recorder = self.proxy("record", "x")
        recorder.cache.save_alternate = lambda *args: (_ for _ in ()).throw(OSError("disk full"))
        line = recorder.analyze(self.BODY, time.time(), self.upstream(self.ANSWER))
        self.assertEqual((line["recorded"], line["record_error"], line["stored"]), (False, "OSError", True))
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, replay_cache.SLOTS_DIR)))

    def test_a_dangling_slot_heals(self):
        slot_key, _ = normalizer.answer_slots(normalizer.normalize_request(self.BODY, time.time()),
                                              normalizer.KeyContext("abc", "img"))
        replay_cache.AnswerCache(self.cache_dir).save_slot(slot_key, "0" * 64)  # an entry never written
        self.record(self.BODY, self.ANSWER)
        changed = dict(self.BODY, messages=self.BODY["messages"][:1] + [{"role": "assistant", "content": "Sure."}]
                       + self.BODY["messages"][2:])
        self.record(dict(changed, messages=changed["messages"][:1]), {"role": "assistant", "content": "Sure."})
        hit, refused = self.proxy().lookup(changed, time.time())
        self.assertIsNone(refused)
        self.assertEqual((hit[0]["content"], hit[3]), ("done", "answer_slots"))


class SeveralAnswersTest(SlotCacheCase):
    """Every distinct answer to a key is kept, and replay hands a key's answers out in turn."""

    BODY = {"model": "m", "messages": [{"role": "user", "content": "work kanban task t_bbbbbbbb"}]}
    LOOKING = {"role": "assistant", "content": "Looking at the card."}
    ON_IT = {"role": "assistant", "content": "On it, reading the board."}

    def saved(self, fingerprint):
        return replay_cache.AnswerCache(self.cache_dir).answers(fingerprint)

    def contents(self, fingerprint):
        """The key's answers' texts in the order replay hands them out."""
        return [entry["answer"]["content"] for _, entry in self.saved(fingerprint)]

    def test_a_second_run_with_a_different_answer_is_kept_beside_the_first(self):
        first = self.recorded(self.BODY, self.LOOKING)
        second = self.recorded(self.BODY, self.ON_IT)
        again = self.recorded(self.BODY, self.LOOKING)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual((first["recorded"], second["recorded"], again["recorded"]), (True, True, False),
                         "a different answer is saved; the same answer again is not")
        self.assertEqual((again["answer_variant"], again["stored_answer_hash"]), (first["answer_variant"], first["stored_answer_hash"]),
                         "an unsaved line still logs which answer it got, so the publisher can count its run")
        names = [name for name, _ in self.saved(first["fingerprint"])]
        self.assertEqual(names, sorted([first["stored_answer_hash"], second["stored_answer_hash"]]))
        self.assertEqual(sorted(self.contents(first["fingerprint"])), sorted([self.LOOKING["content"], self.ON_IT["content"]]))

    def test_answers_whose_identity_collides_but_whose_stored_text_differs_get_files_of_their_own(self):
        # answer_variant's identity renames values the request never held (a
        # keyed number, a pid), so these pairs share it; what is stored and
        # served differs, so each must be its own alternate.
        card = {"model": "m", "messages": [{"role": "user", "content": "Show the latest card."}]}
        pod = {"model": "m", "messages": [{"role": "user", "content": "Why is pod web-1 crashing?"}]}
        shows = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "kanban_show", "arguments": json.dumps({"id": n})}}]} for n in (12, 13)]
        pids = [{"role": "assistant", "content": f"Process pid {n} was killed."} for n in (4821, 9999)]
        for body, (one, other) in ((card, shows), (pod, pids)):
            with self.subTest(body=body["messages"][0]["content"]):
                first, second = self.recorded(body, one), self.recorded(body, other)
                self.assertEqual(first["answer_variant"], second["answer_variant"], "the coarse identity collides")
                self.assertNotEqual(first["stored_answer_hash"], second["stored_answer_hash"])
                self.assertEqual((first["recorded"], second["recorded"]), (True, True))
                stored = [entry["answer"] for _, entry in self.saved(first["fingerprint"])]
                self.assertEqual(len(stored), 2, "the second answer is not dropped onto the first's file")
                self.assertEqual(sorted(json.dumps(a, sort_keys=True) for a in stored),
                                 sorted(json.dumps(a, sort_keys=True) for a in (one, other)))

    def test_answers_differing_only_in_thinking_text_get_files_of_their_own(self):
        # The stored file keeps thinking blocks and is served whole, so their
        # text is part of the name; signatures are not.
        said = [{"role": "assistant", "content": "Looking at the card.",
                 "thinking_blocks": [{"type": "thinking", "thinking": thought, "signature": sig}]}
                for thought, sig in (("check the card", "c2ln"), ("delete the card", "c2lo"))]
        first, second = self.recorded(self.BODY, said[0]), self.recorded(self.BODY, said[1])
        self.assertNotEqual(first["stored_answer_hash"], second["stored_answer_hash"])
        self.assertEqual(len(self.saved(first["fingerprint"])), 2)
        again = dict(said[0], thinking_blocks=[dict(said[0]["thinking_blocks"][0], signature="b3RoZXI=")])
        self.assertEqual(self.recorded(self.BODY, again)["stored_answer_hash"], first["stored_answer_hash"],
                         "a signature is opaque: the same answer delivered again shares the name")

    def test_replay_hands_the_answers_out_in_turn_and_wraps(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        self.recorded(self.BODY, self.ON_IT)
        order = self.contents(fingerprint)
        names = [name for name, _ in self.saved(fingerprint)]
        replay = self.proxy()
        hits = [replay.lookup(self.BODY, time.time()) for _ in range(5)]
        self.assertEqual([refused for _, refused in hits], [None] * 5)
        self.assertEqual([hit[0]["content"] for hit, _ in hits], [order[0], order[1], order[0], order[1], order[0]])
        self.assertEqual([hit[4] for hit, _ in hits], [names[0], names[1], names[0], names[1], names[0]])
        self.assertEqual({hit[5] for hit, _ in hits}, {2})

    def test_a_key_with_one_answer_serves_it_every_time(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        replay = self.proxy()
        hits = [replay.lookup(self.BODY, time.time()) for _ in range(3)]
        self.assertEqual([(hit[0]["content"], hit[5], refused) for hit, refused in hits],
                         [(self.LOOKING["content"], 1, None)] * 3)
        self.assertEqual({hit[4] for hit, _ in hits}, {name for name, _ in self.saved(fingerprint)})

    def break_answer(self, fingerprint, content):
        """Make the saved answer with `content` name a placeholder no request binds."""
        for path in Path(self.cache_dir, replay_cache.ALTS_DIR, fingerprint).iterdir():
            entry = json.loads(path.read_text())
            if entry["answer"]["content"] == content:
                entry["answer"]["content"] = "look at ⟦TASK_9⟧"
                path.write_text(json.dumps(entry))

    def test_a_refused_answer_passes_to_the_next(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        self.recorded(self.BODY, self.ON_IT)
        self.break_answer(fingerprint, self.LOOKING["content"])
        replay = self.proxy()
        for _ in range(3):
            hit, refused = replay.lookup(self.BODY, time.time())
            self.assertIsNone(refused)
            self.assertEqual(hit[0]["content"], self.ON_IT["content"], "whatever the turn, the servable one")

    def test_a_key_whose_every_answer_is_refused_goes_live(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        self.recorded(self.BODY, self.ON_IT)
        self.break_answer(fingerprint, self.LOOKING["content"])
        self.break_answer(fingerprint, self.ON_IT["content"])
        self.assertEqual(self.proxy().lookup(self.BODY, time.time()),
                         (None, {"via": "exact", "guard": "unbound_placeholder", "alternates": 2}))

    def test_an_alternate_filed_under_another_key_is_never_served(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        path, = Path(self.cache_dir, replay_cache.ALTS_DIR, fingerprint).iterdir()
        entry = json.loads(path.read_text())
        entry["fingerprint"] = "0" * 64
        path.write_text(json.dumps(entry))
        self.assertEqual(self.proxy().lookup(self.BODY, time.time()),
                         (None, {"via": "exact", "guard": "unreadable_entry"}))

    def test_the_older_layout_still_serves_as_one_answer(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        (_, entry), = self.saved(fingerprint)
        shutil.rmtree(Path(self.cache_dir, replay_cache.ALTS_DIR))
        cache = replay_cache.AnswerCache(self.cache_dir)
        self.assertTrue(cache.save(fingerprint, entry))
        hit, refused = self.proxy().lookup(self.BODY, time.time())
        self.assertIsNone(refused)
        self.assertEqual((hit[0]["content"], hit[4], hit[5]), (self.LOOKING["content"], replay_cache.LEGACY_ANSWER, 1))

    def test_alternates_win_over_the_older_layout(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        (_, entry), = self.saved(fingerprint)
        old = dict(entry, answer={"role": "assistant", "content": "an old night's answer"})
        replay_cache.AnswerCache(self.cache_dir).save(fingerprint, old)
        replay = self.proxy()
        for _ in range(2):
            hit, _ = replay.lookup(self.BODY, time.time())
            self.assertEqual(hit[0]["content"], self.LOOKING["content"])

    DONE = {"role": "assistant", "content": "Done already."}

    def weigh(self, fingerprint, *weights):
        """Write the key's weights file, giving its answers (in their saved order) these weights."""
        names = [name for name, _ in self.saved(fingerprint)]
        replay_cache.AnswerCache(self.cache_dir).save_weights(fingerprint, dict(zip(names, weights)))
        return names

    def test_answers_are_served_in_proportion_to_their_weights_in_a_fixed_order(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        self.recorded(self.BODY, self.ON_IT)
        names = self.weigh(fingerprint, 9, 1)
        runs = []
        for _ in range(2):  # a second pod turns the same way
            replay = self.proxy()
            runs.append([replay.lookup(self.BODY, time.time())[0] for _ in range(20)])
        served = [hit[4] for hit in runs[0]]
        self.assertEqual(served, [hit[4] for hit in runs[1]], "the order is reproducible")
        for cycle in (served[:10], served[10:]):
            self.assertEqual((cycle.count(names[0]), cycle.count(names[1])), (9, 1), "9 of every 10 turns")
        self.assertEqual({(hit[4], hit[6], hit[7]) for hit in runs[0]}, {(names[0], 9, 10), (names[1], 1, 10)})

    def test_weights_two_to_one_interleave(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        self.recorded(self.BODY, self.ON_IT)
        names = self.weigh(fingerprint, 2, 1)
        replay = self.proxy()
        self.assertEqual([replay.lookup(self.BODY, time.time())[0][4] for _ in range(6)],
                         [names[0], names[1], names[0]] * 2, "the rarer answer is spread, not bunched")

    def test_an_answer_the_weights_do_not_name_weighs_one(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        self.recorded(self.BODY, self.ON_IT)
        names = [name for name, _ in self.saved(fingerprint)]
        replay_cache.AnswerCache(self.cache_dir).save_weights(fingerprint, {names[0]: 3})
        replay = self.proxy()
        served = [replay.lookup(self.BODY, time.time())[0][4] for _ in range(4)]
        self.assertEqual((served.count(names[0]), served.count(names[1])), (3, 1))

    def served_with_one_refused(self, weights, refused, turns):
        """What `turns` lookups serve when the key's answers weigh `weights` and answer `refused` is always refused."""
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        self.recorded(self.BODY, self.ON_IT)
        self.recorded(self.BODY, self.DONE)
        names = [name for name, _ in self.saved(fingerprint)]
        replay_cache.AnswerCache(self.cache_dir).save_weights(fingerprint, dict(zip(names, weights)))
        self.break_answer(fingerprint, dict(self.saved(fingerprint))[names[refused]]["answer"]["content"])
        replay = self.proxy()
        served = [replay.lookup(self.BODY, time.time())[0][4] for _ in range(turns)]
        return names, served

    def test_a_refused_answers_turns_are_shared_by_the_remaining_weights(self):
        names, served = self.served_with_one_refused((1, 1, 8), 2, 10)
        self.assertEqual((served.count(names[0]), served.count(names[1])), (5, 5), "1:1, not by cycle position")
        self.assertEqual(served, [names[i] for i in (0, 1, 0, 0, 1, 0, 1, 1, 0, 1)], "in a fixed order")

    def test_a_refused_answers_turns_follow_uneven_remaining_weights(self):
        names, served = self.served_with_one_refused((3, 1, 6), 2, 40)
        self.assertEqual((served.count(names[0]), served.count(names[1])), (30, 10))

    def test_the_older_layout_weighs_one(self):
        fingerprint = self.recorded(self.BODY, self.LOOKING)["fingerprint"]
        (_, entry), = self.saved(fingerprint)
        shutil.rmtree(Path(self.cache_dir, replay_cache.ALTS_DIR))
        replay_cache.AnswerCache(self.cache_dir).save(fingerprint, entry)
        hit, _ = self.proxy().lookup(self.BODY, time.time())
        self.assertEqual((hit[4], hit[5], hit[6], hit[7]), (replay_cache.LEGACY_ANSWER, 1, 1, 1))

    def test_a_slot_hands_its_keys_answers_out_in_turn(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "work kanban task t_bbbbbbbb"},
                                           {"role": "assistant", "content": "Yes."},
                                           {"role": "user", "content": "ticket Latte"}]}
        self.record(body, {"role": "assistant", "content": "done"})
        fingerprint = self.recorded(body, {"role": "assistant", "content": "finished"})["fingerprint"]
        order = self.contents(fingerprint)
        changed = dict(body, messages=body["messages"][:1] + [{"role": "assistant", "content": "Sure."}]
                       + body["messages"][2:])
        self.record(dict(changed, messages=changed["messages"][:1]), {"role": "assistant", "content": "Sure."})
        replay = self.proxy()
        hits = [replay.lookup(changed, time.time()) for _ in range(3)]
        self.assertEqual([(hit[0]["content"], hit[3]) for hit, _ in hits],
                         [(order[0], "answer_slots"), (order[1], "answer_slots"), (order[0], "answer_slots")])


class SlotCheckCostTest(SlotCacheCase):
    """Each turn's recorded-answer check normalizes only its new answer's prefix."""

    def test_a_second_turn_does_not_renormalize_earlier_prefixes(self):
        turn = [{"role": "user", "content": "work kanban task t_bbbbbbbb"}]
        for said, shown in (("Looking.", "card open"), ("Commenting.", "comment added")):
            asked = {"model": "m", "messages": list(turn)}
            self.recorded(asked, {"role": "assistant", "content": said})
            turn += [{"role": "assistant", "content": said}, {"role": "user", "content": shown}]
        replay = self.proxy()
        calls = []
        original = normalizer.normalize_request
        normalizer.normalize_request = lambda *args: calls.append(args) or original(*args)
        self.addCleanup(setattr, normalizer, "normalize_request", original)
        for length, expected in ((3, 1), (5, 1)):
            calls.clear()
            body = {"model": "m", "messages": turn[:length]}
            digests, _ = replay.diagnosis.first_seen_with_digests(body["messages"], time.time())
            seen = replay.prefix_first_seen(body, digests, time.time())
            self.assertTrue(replay.answers_recorded(body, body["messages"], digests, seen, time.time()))
            self.assertEqual(len(calls), expected, f"{length} messages")


class UnreadableEntryTest(SlotCacheCase):
    """A key that points at a missing or broken saved answer is a refused miss, never a served one."""

    BODY = {"model": "m", "messages": [{"role": "user", "content": "work kanban task t_bbbbbbbb"},
                                       {"role": "assistant", "content": "Yes."},
                                       {"role": "user", "content": "ticket Latte"}]}

    def test_a_broken_exact_entry(self):
        self.record(self.BODY, {"role": "assistant", "content": "done"})
        for path in Path(self.cache_dir, replay_cache.ALTS_DIR).rglob("*.json"):
            path.write_text(json.dumps({"answer": "not an answer"}))
        self.assertEqual(self.proxy().lookup(self.BODY, time.time()),
                         (None, {"via": "exact", "guard": "unreadable_entry"}))

    def test_a_slot_whose_entry_is_gone(self):
        self.record(self.BODY, {"role": "assistant", "content": "done"})
        shutil.rmtree(Path(self.cache_dir, replay_cache.ALTS_DIR))
        self.assertEqual(self.proxy().lookup(self.BODY, time.time()),
                         (None, {"via": "answer_slots", "guard": "unreadable_entry"}))

    def test_a_slot_whose_entry_is_gone_is_refused_before_checking_its_answers(self):
        # Checking the answers is the costly step; a dangling slot skips it.
        self.recorded(self.BODY, {"role": "assistant", "content": "done"})  # its answers never recorded
        shutil.rmtree(Path(self.cache_dir, replay_cache.ALTS_DIR))
        replay = self.proxy()
        replay.answers_recorded = lambda *args: self.fail("checked a slot whose entry is gone")
        self.assertEqual(replay.lookup(self.BODY, time.time()),
                         (None, {"via": "answer_slots", "guard": "unreadable_entry"}))


class AnswerSlotActionsTest(SlotCacheCase):
    """A slot blanks an answer's words, never its tool calls."""

    OPENING = [{"role": "system", "content": "You are the platform agent."},
               {"role": "user", "content": "work kanban task t_bbbbbbbb"}]

    @staticmethod
    def act(name, arguments, said="", call_id="call_1__thought__c2ln"):
        return {"role": "assistant", "content": said, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}

    @staticmethod
    def result(content, call_id="call_1__thought__c2ln"):
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    def conversation(self, *messages):
        return {"model": "m", "messages": self.OPENING + list(messages)}

    def test_another_tool_call_followed_by_the_same_result_misses(self):
        # Recording 1 commented and got {"ok": true}, then kept working; recording
        # 2 completed the card. A replay that completed it and also got {"ok": true}
        # must not be served recording 1's "keep working".
        comment = self.act("kanban_comment", {"body": "still checking"})
        complete = self.act("kanban_complete", {"summary": "done"})
        self.record(self.conversation(comment, self.result('{"ok": true}')),
                    self.act("kanban_comment", {"body": "found it"}, call_id="call_2"),
                    others=[self.conversation(complete, self.result('{"ok": true, "status": "done"}'))])
        replayed = self.conversation(self.act("kanban_complete", {"summary": "done"}, call_id="call_9__thought__eHl6"),
                                     self.result('{"ok": true}', call_id="call_9__thought__eHl6"))
        self.assertEqual(self.proxy().lookup(replayed, time.time()), (None, None))

    def lookups(self, replayed_arguments, replayed_result, name="kanban_show"):
        """Two recordings looked the card up with and without its id; replay `name` with other arguments."""
        self.record(self.conversation(self.act(name, {"task_id": "t_bbbbbbbb"}), self.result('{"task": "running"}')),
                    self.act("kanban_comment", {"body": "started"}, call_id="call_2"),
                    others=[self.conversation(self.act(name, {}), self.result('{"task": "done"}'))])
        replayed = self.conversation(self.act(name, replayed_arguments, call_id="call_5__thought__cXJz"),
                                     self.result(replayed_result, call_id="call_5__thought__cXJz"))
        return self.proxy().lookup(replayed, time.time())

    def test_a_read_only_lookup_with_other_arguments_and_the_same_result_is_served(self):
        hit, _ = self.lookups({}, '{"task": "running"}')
        self.assertIsNotNone(hit)
        self.assertEqual(hit[3], "answer_slots")

    def test_a_read_only_lookup_with_another_result_misses(self):
        self.assertEqual(self.lookups({}, '{"task": "blocked"}'), (None, None))

    def test_another_tool_with_other_arguments_misses(self):
        self.assertEqual(self.lookups({}, '{"task": "running"}', name="kanban_heartbeat"), (None, None))

    def test_the_same_tool_call_in_other_words_is_served(self):
        self.record(self.conversation(self.act("kanban_comment", {"body": "x"}, said="Leaving a note."),
                                      self.result('{"ok": true}')),
                    {"role": "assistant", "content": "Noted on ticket t_bbbbbbbb."},
                    others=[self.conversation(self.act("kanban_comment", {"body": "x"}, said="Commenting."),
                                              self.result('{"ok": true, "other": 1}'))])
        replayed = self.conversation(self.act("kanban_comment", {"body": "x"}, said="Commenting.",
                                              call_id="call_7__thought__bmV3"),
                                     self.result('{"ok": true}', call_id="call_7__thought__bmV3"))
        hit, _ = self.proxy().lookup(replayed, time.time())
        self.assertIsNotNone(hit)
        self.assertEqual((hit[0]["content"], hit[3]), ("Noted on ticket t_bbbbbbbb.", "answer_slots"))


class VariantContextTest(AnalyzeCase):
    """A recorded answer still fills its slot when the later request normalizes what came before it differently.

    Record mode notes an answer when it records the request the answer
    replied to; replay checks it from a later request. Whole-request choices
    (how old a timestamp is, whether any CIDR keeps IPs literal, how settings
    placeholders are numbered) can differ between the two.
    """

    @staticmethod
    def act(said, call_id):
        return {"role": "assistant", "content": said, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": "terminal", "arguments": '{"command": "ls"}'}}]}

    def fork(self, opening, result, settings=None, later=0, seen_before=()):
        """Two recordings answer `opening` in other words; the second goes on to `result` `later` seconds on.

        A replay that was served the first recording's answer then asks for the
        second recording's next step. Returns the replay's lookup. The replay
        proxy first looks up each (body, time) of `seen_before`, another
        conversation it served earlier.
        """
        settings = settings or {}
        asked = dict(settings, model="m", messages=opening)
        first, second = self.act("Listing.", "call_1"), self.act("Let me list.", "call_2")
        self.proxy("record", "x").analyze(asked, CLOCK, self.upstream(first))
        recorder = self.proxy("record", "b")
        recorder.analyze(asked, CLOCK, self.upstream(second))
        went_on = dict(asked, messages=opening + [second, {"role": "tool", "tool_call_id": "call_2", "content": result}])
        recorder.analyze(went_on, CLOCK + later, self.upstream({"role": "assistant", "content": "All listed."}))
        replay = self.proxy("replay", "r")
        for body, at in seen_before:
            replay.lookup(body, at)
        replay.lookup(asked, CLOCK)  # the replay's own earlier request, served the first answer
        replayed = dict(asked, messages=opening + [first, {"role": "tool", "tool_call_id": "call_1", "content": result}])
        return replay.lookup(replayed, CLOCK + later)

    def assert_served(self, looked_up):
        hit, refused = looked_up
        self.assertIsNone(refused)
        self.assertIsNotNone(hit)
        self.assertEqual((hit[0]["content"], hit[3]), ("All listed.", "answer_slots"))

    def test_a_timestamp_that_crosses_the_hour_before_the_later_request(self):
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(CLOCK - 50 * MINUTE))
        opening = [{"role": "user", "content": f"card t_bbbbbbbb created {created}"}]
        self.assert_served(self.fork(opening, "a.txt", later=20 * MINUTE))

    def test_a_message_another_conversation_sent_earlier(self):
        # A skill's text is the same in every conversation. Normalized as of when
        # another conversation sent it ten minutes before, this card's
        # timestamps read as future ones and the answer as never recorded.
        skill = {"role": "user", "content": "skill cluster-agent-lifecycle: create and tear down profiles"}
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(CLOCK - MINUTE))
        opening = [{"role": "user", "content": f"card t_bbbbbbbb created {created}"}, skill]
        other = {"model": "m", "messages": [{"role": "user", "content": "card t_cccccccc opened"}, skill]}
        self.assert_served(self.fork(opening, "a.txt", later=MINUTE, seen_before=[(other, CLOCK - 10 * MINUTE)]))

    def test_the_same_messages_under_other_settings_earlier(self):
        # Another conversation sent these very messages ten minutes before, with
        # another model. A prefix is the settings and the messages, so its time
        # is this conversation's own; keyed by the messages alone, the card's
        # timestamp would read as a future one and the answer as never recorded.
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(CLOCK - MINUTE))
        opening = [{"role": "user", "content": f"card t_bbbbbbbb created {created}"}]
        other = {"model": "another-model", "messages": opening}
        self.assert_served(self.fork(opening, "a.txt", later=MINUTE, seen_before=[(other, CLOCK - 10 * MINUTE)]))

    def test_a_cidr_only_in_a_later_message(self):
        opening = [{"role": "user", "content": "node 10.1.2.3 is cordoned"}]
        self.assert_served(self.fork(opening, "allowed 10.0.0.0/8"))

    def test_a_settings_placeholder_shifted_by_a_later_id(self):
        settings = {"tools": [{"type": "function", "function": {"name": "terminal",
                                                                "description": "runs on web-7d4b9c8f6d-x2k9p"}}]}
        opening = [{"role": "user", "content": "list the files"}]
        self.assert_served(self.fork(opening, "api-5f6c7d8b9c-m8n7q.log", settings=settings))


class BoundValueTest(AnalyzeCase):
    """An identifier the key swapped is swapped in the stored answer too, wherever the key found it."""

    CASES = (
        # (what the request shows, recorded value, replayed value, the answer naming it)
        ("work kanban task {}", "t_abcdef12", "t_99999999", "hermes kanban comment --id={} done"),
        ("session {} started", "20261102_002640_791a68", "20261103_101010_abcdef", "ls ~/.hermes/sessions/{}/"),
        ("message {} arrived", "inj-4821", "inj-77", "cat /tmp/{}/body"),
        ("work kanban task {}", "t_abcdef12", "t_99999999", "label+{} added"),
        ("pod {} is pending", "web-7d4b9c8f6d-x2k9p", "web-5f6c7d8b9c-m8n7q", "kubectl logs pod/{}"),
    )

    @staticmethod
    def body(shown):
        return {"model": "m", "messages": [{"role": "user", "content": shown}]}

    def test_the_answer_is_served_with_this_runs_value(self):
        for shown, recorded, replayed, said in self.CASES:
            with self.subTest(said=said):
                self.setUp()
                line = self.recorded(self.body(shown.format(recorded)), {"role": "assistant", "content": said.format(recorded)})
                self.assertEqual(line["unstorable"], [])
                hit, _ = self.proxy().lookup(self.body(shown.format(replayed)), time.time())
                self.assertIsNotNone(hit)
                self.assertEqual(hit[0]["content"], said.format(replayed))

    def test_an_identifier_after_a_json_escape_in_tool_arguments(self):
        # Escaped, a tab before the uid is `\\t`, and no pattern finds the uid after
        # its `t`; parsed, as the key reads the arguments, it is found.
        def answer(uid):
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "kanban_comment", "arguments": json.dumps({"body": f"pending\t{uid}"})}}]}

        old, new = "c7fa9f5e-12ba-46a4-9fbb-16d1991ecff5", "6f79db96-15b3-4308-83df-7c4f203b6015"
        self.assertEqual(self.recorded(self.body(f"pod uid {old}"), answer(old))["unstorable"], [])
        hit, _ = self.proxy().lookup(self.body(f"pod uid {new}"), time.time())
        self.assertEqual(json.loads(hit[0]["tool_calls"][0]["function"]["arguments"]), {"body": f"pending\t{new}"})

    def test_an_answer_that_keeps_a_bound_identifier_literal_is_not_stored(self):
        # `_` borders the id on both sides, so no pattern finds it there; the key
        # swapped it in the request, so a stored answer would carry the recording's.
        line = self.recorded(self.body("work kanban task t_abcdef12"),
                             {"role": "assistant", "content": "archive card_t_abcdef12_old"})
        self.assertEqual(line["unstorable"], ["bound_value_left_literal"])

    def test_an_identifier_the_key_never_bound_is_not_stored(self):
        # The recent-work digest is dropped from the key before identifiers are
        # bound, so a card named only there has no placeholder to restore.
        card = "card t_bbbbbbbb\n## Recent work by @platform\n- t_deadbeef: audited the fleet\n\nstatus: running"
        answer = {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {
            "name": "kanban_show", "arguments": json.dumps({"task_id": "t_deadbeef"})}}]}
        self.assertEqual(self.recorded(self.body(card), answer)["unstorable"], ["unbound_identifier"])

    def test_an_ip_is_not_found_inside_a_longer_one(self):
        line = self.recorded(self.body("node 10.1.2.3 is cordoned"),
                             {"role": "assistant", "content": "10.1.2.30 is the gateway"})
        self.assertEqual(line["unstorable"], [])


class SkillViewOrderTest(AnalyzeCase):
    """A skill_view result listing the skill's files in another directory order still matches its recording."""

    @staticmethod
    def body(references):
        call = {"id": "call_1__thought__c2ln", "type": "function",
                "function": {"name": "skill_view", "arguments": json.dumps({"name": "fleet-audit"})}}
        result = json.dumps({"success": True, "name": "fleet-audit", "content": "# fleet-audit",
                             "linked_files": {"references": references}})
        return {"model": "m", "messages": [
            {"role": "user", "content": "audit the fleet"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1__thought__c2ln", "content": result},
        ]}

    def test_the_turn_after_skill_view_is_served_from_another_pods_order(self):
        answer = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_2__thought__eHl6", "type": "function", "function": {
                "name": "skill_view", "arguments": json.dumps({"name": "fleet-audit", "file_path": "references/ledger.md"})}}]}
        recorded = self.body(["references/ledger.md", "references/finish.md"])
        self.assertEqual(self.recorded(recorded, answer)["unstorable"], [])
        hit, refused = self.proxy().lookup(self.body(["references/finish.md", "references/ledger.md"]), time.time())
        self.assertIsNone(refused)
        self.assertEqual(hit[3], "exact")
        self.assertEqual(json.loads(hit[0]["tool_calls"][0]["function"]["arguments"])["file_path"],
                         "references/ledger.md")


class RunNameTest(AnalyzeCase):
    """The eval pool project, a repository commit, a clone directory and a GKE operation are bound like ids."""

    CASES = (
        # (what the request shows, recorded value, replayed value, the answer naming it)
        ("You are cluster-{}-seeded-a-us-central1-a.", "kube-agents-evals-31", "kube-agents-evals-29",
         "gcloud container clusters describe seeded-a --project={} --zone us-central1-a"),
        # The nightly's project has no number; the presubmit's does.
        ("project {}", "kube-agents-evals", "kube-agents-evals-7",
         "I opened a PR on gke-agentic/{}-infra for cluster-{}-seeded-a-us-central1-a."),
        ("searched gke-agentic/kube-agents-evals-3-infra@{} (whole tree)", "c99b1ab", "4f1e414",
         "git -C repo checkout {}"),
        ("cloned into /opt/data/scratch/declared-intent-{}", "a2_elobt", "j77z8q6q",
         "ls /opt/data/scratch/declared-intent-{}/"),
        ("newest operation operation-{} (UPGRADE_NODES)", "1793806478493-11b21f18-4eef-47ea-8ce1-fbd3c40047b3",
         "1792900340178-5482a62f-d7e3-4c54-bb09-236983a9dfb7", "gcloud container operations describe operation-{}"),
    )

    @staticmethod
    def body(shown):
        return {"model": "m", "messages": [{"role": "user", "content": shown}]}

    def test_the_answer_is_served_with_this_runs_value(self):
        for shown, recorded, replayed, said in self.CASES:
            with self.subTest(said=said):
                self.setUp()
                line = self.recorded(self.body(shown.format(recorded)),
                                     {"role": "assistant", "content": said.format(recorded, recorded)})
                self.assertEqual(line["unstorable"], [])
                hit, _ = self.proxy().lookup(self.body(shown.format(replayed)), time.time())
                self.assertIsNotNone(hit)
                self.assertEqual(hit[0]["content"], said.format(replayed, replayed))

    def test_the_next_request_holding_the_served_answer_still_matches(self):
        # The answer names the value outside the shape that bound it, so only the
        # whole-token swap finds it; turn 2 sends each run's own text back.
        for shown, recorded, replayed, said in (
            ("searched gke-agentic/kube-agents-evals-3-infra@{}", "c99b1ab", "4f1e414", "git -C repo checkout {}"),
            ("cloned into /opt/data/scratch/declared-intent-{}", "a2_elobt", "j77z8q6q", "mv clone-{} done"),
        ):
            with self.subTest(said=said):
                self.setUp()
                first = self.body(shown.format(recorded))
                answer = {"role": "assistant", "content": said.format(recorded)}
                self.assertEqual(self.recorded(first, answer)["unstorable"], [])
                first["messages"] += [answer, {"role": "user", "content": "thanks"}]
                self.recorded(first, {"role": "assistant", "content": "Done."})

                replay = self.proxy()
                echoed = self.body(shown.format(replayed))
                hit, _ = replay.lookup(echoed, time.time())
                self.assertEqual(hit[0]["content"], said.format(replayed))
                echoed["messages"] += [hit[0], {"role": "user", "content": "thanks"}]
                hit, refused = replay.lookup(echoed, time.time())
                self.assertIsNone(refused)
                self.assertEqual((hit[0]["content"], hit[3]), ("Done.", "exact"))

    def test_the_project_in_tool_arguments_is_served_with_this_runs_value(self):
        def answer(project):
            return {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "terminal", "arguments": json.dumps({"command": f"gcloud projects describe {project}"})}}]}

        def shown(project):
            return json.dumps({"output": f"project: {project}"})

        self.assertEqual(self.recorded(self.body(shown("kube-agents-evals-31")),
                                       answer("kube-agents-evals-31"))["unstorable"], [])
        hit, _ = self.proxy().lookup(self.body(shown("kube-agents-evals-12")), time.time())
        self.assertEqual(json.loads(hit[0]["tool_calls"][0]["function"]["arguments"]),
                         {"command": "gcloud projects describe kube-agents-evals-12"})

    def test_a_bound_commit_anywhere_in_the_answer_is_served_with_this_runs_commit(self):
        # The request binds the commit after `@`; the answer quotes it inside a forge URL.
        old, new = "0ffe0cbd6b463ad4509311e5ea7eaf34afd746f3", "4f1e4141f06dd20a6d36af538e51dab2f5af93a6"
        shown = "searched gke-agentic/kube-agents-evals-29-infra@{}"
        said = "[README.md](https://github.com/gke-agentic/kube-agents-evals-29-infra/blob/{}/README.md) is empty."
        self.assertEqual(self.recorded(self.body(shown.format(old)),
                                       {"role": "assistant", "content": said.format(old)})["unstorable"], [])
        hit, _ = self.proxy().lookup(self.body(shown.format(new)), time.time())
        self.assertEqual(hit[0]["content"], said.format(new))
        # After `=`, which no pattern and no whole-token swap reads as a boundary.
        self.setUp()
        said = "git fetch origin --negotiation-tip={}"
        self.assertEqual(self.recorded(self.body(shown.format(old[:7])),
                                       {"role": "assistant", "content": said.format(old[:7])})["unstorable"], [])
        hit, _ = self.proxy().lookup(self.body(shown.format(new[:7])), time.time())
        self.assertEqual(hit[0]["content"], said.format(new[:7]))

    def test_a_percent_escaped_project_is_served_with_this_runs_project(self):
        shown = "project {}"
        said = "[Logs](https://console.cloud.google.com/logs/query;query=project_id%3D%22{}%22?project={})"
        self.assertEqual(self.recorded(self.body(shown.format("kube-agents-evals-22")), {
            "role": "assistant", "content": said.format("kube-agents-evals-22", "kube-agents-evals-22")})["unstorable"], [])
        hit, _ = self.proxy().lookup(self.body(shown.format("kube-agents-evals-5")), time.time())
        self.assertEqual(hit[0]["content"], said.format("kube-agents-evals-5", "kube-agents-evals-5"))

    def test_a_pool_project_the_request_never_named_is_not_stored(self):
        line = self.recorded(self.body("list the clusters"),
                             {"role": "assistant", "content": "gcloud container clusters list --project=kube-agents-evals-31"})
        self.assertEqual(line["unstorable"], ["unbound_identifier"])

    def test_a_shortened_commit_is_not_stored(self):
        # The request holds the full commit; the answer abbreviates it, which no pattern binds.
        full = "c99b1ab1fee849df074d039fe5d4d4ad4301d1c9"
        line = self.recorded(self.body(f"searched gke-agentic/kube-agents-evals-3-infra@{full}"),
                             {"role": "assistant", "content": "the repository is at c99b1ab"})
        self.assertEqual(line["unstorable"], ["bound_value_left_literal"])

    def test_names_that_are_no_pool_project_are_served_as_recorded(self):
        said = "Opened by kube-agents-evals-token-minter[bot] against gke-agentic/kube-agents-evals-large-gitops."
        self.assertEqual(self.recorded(self.body("project kube-agents-evals-31"),
                                       {"role": "assistant", "content": said})["unstorable"], [])
        hit, _ = self.proxy().lookup(self.body("project kube-agents-evals-29"), time.time())
        self.assertEqual(hit[0]["content"], said)

    def test_a_node_name_that_holds_the_pool_project_is_served_with_this_runs_node(self):
        shown = "node {} is NotReady"
        old, new = "gke-kube-agents-evals-31-default-pool-abcdef12-x1y2", "gke-kube-agents-evals-7-default-pool-98765432-q9r8"
        said = "kubectl drain {} --ignore-daemonsets"
        self.assertEqual(self.recorded(self.body(shown.format(old)),
                                       {"role": "assistant", "content": said.format(old)})["unstorable"], [])
        hit, _ = self.proxy().lookup(self.body(shown.format(new)), time.time())
        self.assertEqual(hit[0]["content"], said.format(new))

    def test_an_answer_quoting_a_queued_events_count_is_not_stored(self):
        card = json.dumps({"task": {"id": "t_bbbbbbbb"}, "events": [
            {"kind": "queued", "payload": {"note": "Queued: the system is busy.", "running": 5, "limit": 5, "ahead": 6}}]})
        line = self.recorded(self.body(card), {"role": "assistant", "content": "It waited behind 6 other tasks."})
        self.assertEqual(line["unstorable"], ["repeats_swapped_number"])

    def test_an_answer_quoting_a_commit_only_the_digest_names_is_not_stored(self):
        card = "card t_bbbbbbbb\n\n## Recent work by @platform\n- t_deadbeef — synced the repo at 0ffe0cb: done"
        line = self.recorded(self.body(card), {"role": "assistant", "content": "The repository is at 0ffe0cb."})
        self.assertEqual(line["unstorable"], ["unbound_identifier"])

    def test_an_answer_repeating_a_dropped_queued_events_time_is_not_stored(self):
        card = json.dumps({"task": {"id": "t_bbbbbbbb"}, "events": [
            {"kind": "queued", "created_at": 1793700100, "payload": {"note": "Queued: the system is busy."}}]})
        line = self.recorded(self.body(card), {"role": "assistant", "content": "It waited from 1793700100."})
        self.assertEqual(line["unstorable"], ["repeats_swapped_number"])


class SimilarFilesTest(AnalyzeCase):
    """A read_file not-found result listing other similar files still matches its recording."""

    @staticmethod
    def body(similar):
        call = {"id": "call_1__thought__cmVh", "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "/opt/data/AGENTS.md"})}}
        result = {"content": "", "total_lines": 0, "file_size": 0, "truncated": False, "is_binary": False,
                  "is_image": False, "error": "File not found: /opt/data/AGENTS.md"}
        if similar:
            result["similar_files"] = similar
        return {"model": "m", "messages": [
            {"role": "user", "content": "look at the inventory"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1__thought__cmVh", "content": json.dumps(result)},
        ]}

    @staticmethod
    def answer(tool, arguments):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_2__thought__eHl6", "type": "function", "function": {"name": tool, "arguments": json.dumps(arguments)}}]}

    def test_the_next_turn_is_served_whatever_the_list_held(self):
        answer = self.answer("skill_view", {"name": "support-escalation-bundle"})
        recorded = self.body(["/opt/data/INVENTORY.md", "/opt/data/SETTINGS.md"])
        self.assertEqual(self.recorded(recorded, answer)["unstorable"], [])
        for similar in (["/opt/data/SETTINGS.md"], None):
            with self.subTest(similar=similar):
                hit, refused = self.proxy().lookup(self.body(similar), time.time())
                self.assertIsNone(refused)
                self.assertIsNotNone(hit)
                self.assertEqual(hit[3], "exact")
                self.assertEqual(json.loads(hit[0]["tool_calls"][0]["function"]["arguments"]),
                                 {"name": "support-escalation-bundle"})

    def test_an_answer_reading_a_listed_file_is_not_stored(self):
        # Served to a run whose list lacked INVENTORY.md, it would read a file that run was never shown.
        for arguments in ({"path": "/opt/data/INVENTORY.md"}, {"command": "cd /opt/data && cat INVENTORY.md"}):
            with self.subTest(arguments=arguments):
                self.setUp()
                tool = "read_file" if "path" in arguments else "terminal"
                line = self.recorded(self.body(["/opt/data/INVENTORY.md", "/opt/data/SETTINGS.md"]),
                                     self.answer(tool, arguments))
                self.assertEqual(line["unstorable"], ["names_dropped_similar_file"])

    def test_a_listed_name_holding_a_bound_identifier_is_not_stored(self):
        body = self.body(["/opt/data/kube-agents-evals-22-notes.md"])
        body["messages"][0]["content"] = "look at the inventory for kube-agents-evals-22"
        line = self.recorded(body, self.answer("read_file", {"path": "/opt/data/kube-agents-evals-22-notes.md"}))
        self.assertEqual(line["unstorable"], ["names_dropped_similar_file"])

    def test_an_answer_naming_a_file_only_this_runs_list_holds_is_not_served(self):
        # Recorded where INVENTORY.md did not exist yet; this run's list shows another agent wrote it.
        answer = self.answer("write_file", {"path": "/opt/data/INVENTORY.md", "content": "# fresh"})
        self.assertEqual(self.recorded(self.body(None), answer)["unstorable"], [])
        hit, refused = self.proxy().lookup(self.body(["/opt/data/INVENTORY.md"]), time.time())
        self.assertIsNone(hit)
        self.assertEqual(refused, {"via": "exact", "guard": "names_dropped_similar_file"})
        # A run whose list does not name it is served the write.
        hit, refused = self.proxy().lookup(self.body(["/opt/data/SETTINGS.md"]), time.time())
        self.assertIsNone(refused)
        self.assertIsNotNone(hit)

    def test_this_runs_listed_name_is_read_in_the_answer_as_served(self):
        # Saved, the name is `⟦EVALPROJECT_1⟧-notes.md`; restored with this run's project, it is the listed file.
        def body(project, similar):
            asked = self.body(similar)
            asked["messages"][0]["content"] = f"look at the inventory for {project}"
            return asked

        answer = self.answer("write_file", {"path": "/opt/data/kube-agents-evals-22-notes.md", "content": "# fresh"})
        self.assertEqual(self.recorded(body("kube-agents-evals-22", None), answer)["unstorable"], [])
        hit, refused = self.proxy().lookup(
            body("kube-agents-evals-23", ["/opt/data/kube-agents-evals-23-notes.md"]), time.time())
        self.assertIsNone(hit)
        self.assertEqual(refused, {"via": "exact", "guard": "names_dropped_similar_file"})


class KanbanCreateQueueTest(AnalyzeCase):
    """A front door's kanban_create result matches its recording whether its card waited for a slot or not."""

    @staticmethod
    def body(queued, running=5):
        call = {"id": "call_1__thought__Y3Jl", "type": "function",
                "function": {"name": "kanban_create", "arguments": json.dumps({"title": "triage seeded-a"})}}
        result = {"ok": True, "task_id": "t_d07508c5", "status": "ready", "workspace_kind": "scratch",
                  "workspace_path": None, "project_id": None, "subscribed": True}
        if queued is not None:
            result["queued"] = queued
        if queued:
            result["queue"] = {"running": running, "limit": 5}
        return {"model": "m", "messages": [
            {"role": "user", "content": "triage seeded-a"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1__thought__Y3Jl", "content": json.dumps(result)},
        ]}

    TALKS = ("It's queued and will start when a worker frees up.", "waiting for a slot (5 of 5 in use)",
             "queued behind 5 running")

    def test_the_ack_is_served_whether_or_not_the_card_waited(self):
        for recorded, replayed in ((True, None), (None, True), (True, False)):
            with self.subTest(recorded=recorded, replayed=replayed):
                self.setUp()
                answer = {"role": "assistant", "content": "I'm checking seeded-a."}
                self.assertEqual(self.recorded(self.body(recorded), answer)["unstorable"], [])
                hit, refused = self.proxy().lookup(self.body(replayed), time.time())
                self.assertIsNone(refused)
                self.assertIsNotNone(hit)
                self.assertEqual(hit[3], "exact")
                self.assertEqual(hit[0]["content"], "I'm checking seeded-a.")

    def test_an_answer_talking_about_the_wait_is_not_stored_after_a_queued_create(self):
        # Served to a run whose card had a free slot, it would announce a wait that run does not have.
        for said in self.TALKS:
            with self.subTest(said=said):
                self.setUp()
                line = self.recorded(self.body(True), {"role": "assistant", "content": said})
                self.assertIn("talks_about_dropped_queue", line["unstorable"])
                self.assertIsNone(self.proxy().lookup(self.body(None), time.time())[0])

    def test_the_count_guard_still_refuses_a_quoted_count(self):
        line = self.recorded(self.body(True), {"role": "assistant", "content": "queued behind 5 running"})
        self.assertEqual(line["unstorable"], ["repeats_swapped_number", "talks_about_dropped_queue"])

    def test_the_same_words_are_stored_after_a_create_that_did_not_wait(self):
        for queued in (False, None):
            for said in self.TALKS:
                with self.subTest(queued=queued, said=said):
                    self.setUp()
                    self.assertEqual(self.recorded(self.body(queued), {"role": "assistant", "content": said})["unstorable"], [])


class SpentProposalTest(AnalyzeCase):
    """An earlier run's proposal number and revision in submit_suggestion's notice don't fork the key."""

    @staticmethod
    def body(number, revision):
        call = {"id": "call_1__thought__cHJl", "type": "function", "function": {
            "name": "terminal", "arguments": json.dumps({"command": "python3 submit_suggestion.py prepare --branch b"})}}
        notice = (f"[2026-11-12 02:08:09] [SRE-AUTH] 'platform-agent/b' was the source of "
                  f"https://github.com/gke-agentic/kube-agents-evals-22-infra/pull/{number}, which is closed. That "
                  f"proposal's last revision, {revision}, is not in 'main', so it was squash-merged or closed rather "
                  "than merged whole. The repository no longer holds the branch, so the name is free.")
        return {"model": "m", "messages": [
            {"role": "user", "content": "propose the fix"},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1__thought__cHJl",
             "content": json.dumps({"output": notice, "exit_code": 0, "error": None})},
        ]}

    def test_the_next_turn_is_served_with_this_runs_revision(self):
        said = "The spent branch was last at {}; writing the change on a fresh copy."
        self.assertEqual(self.recorded(self.body(1399, "3ef98c4e29cf"),
                                       {"role": "assistant", "content": said.format("3ef98c4e29cf")})["unstorable"], [])
        replay = self.proxy()
        echoed = self.body(1398, "141a5bb53467")
        hit, refused = replay.lookup(echoed, time.time())
        self.assertIsNone(refused)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0]["content"], said.format("141a5bb53467"))

    def test_the_turn_after_a_served_revision_still_matches(self):
        said = "Last at {}."
        first = self.body(1399, "3ef98c4e29cf")
        answer = {"role": "assistant", "content": said.format("3ef98c4e29cf")}
        self.assertEqual(self.recorded(first, answer)["unstorable"], [])
        first["messages"] += [answer, {"role": "user", "content": "thanks"}]
        self.assertEqual(self.recorded(first, {"role": "assistant", "content": "Done."})["unstorable"], [])
        replay = self.proxy()
        echoed = self.body(1398, "141a5bb53467")
        hit, _ = replay.lookup(echoed, time.time())
        self.assertIsNotNone(hit)
        echoed["messages"] += [hit[0], {"role": "user", "content": "thanks"}]
        hit, refused = replay.lookup(echoed, time.time())
        self.assertIsNone(refused)
        self.assertEqual((hit[0]["content"], hit[3]), ("Done.", "exact"))

    def test_an_answer_naming_the_proposal_number_is_not_stored(self):
        for said in ("vcs.py proposal view 1399 --repo gke-agentic/kube-agents-evals-22-infra",
                     "First PR (closed): https://github.com/gke-agentic/kube-agents-evals-22-infra/pull/1399"):
            with self.subTest(said=said):
                self.setUp()
                line = self.recorded(self.body(1399, "3ef98c4e29cf"), {"role": "assistant", "content": said})
                self.assertEqual(line["unstorable"], ["repeats_swapped_number"])


class ReasoningTest(AnalyzeCase):
    """An answer's reasoning is checked for credentials, and never stored."""

    BODY = {"model": "m", "messages": [{"role": "user", "content": "check the registry login"}]}

    def test_a_credential_only_in_the_reasoning_is_never_written(self):
        for answer in (
            {"role": "assistant", "content": "The login works.", "reasoning_content": f"the token is {TOKEN}"},
            {"role": "assistant", "content": "The login works.",
             "thinking_blocks": [{"type": "thinking", "thinking": f"the token is {TOKEN}", "signature": "c2ln"}]},
        ):
            with self.subTest(answer=list(answer)):
                self.assertIn("scrub_masks_credential", self.recorded(self.BODY, answer)["unstorable"])
        written = "".join(p.read_text() for p in Path(self.cache_dir).rglob("*.json"))
        self.assertNotIn(TOKEN, written)

    def test_reasoning_is_not_stored_and_thinking_blocks_are(self):
        answer = {"role": "assistant", "content": "The login works.", "reasoning_content": "checked the secret's age",
                  "thinking_blocks": [{"type": "thinking", "thinking": "checked it", "signature": "c2ln"}]}
        line = self.recorded(self.BODY, answer)
        self.assertTrue(line["recorded"])
        (_, entry), = replay_cache.AnswerCache(self.cache_dir).answers(line["fingerprint"])
        saved = entry["answer"]
        self.assertNotIn("reasoning_content", saved)
        self.assertEqual(saved["thinking_blocks"][0]["signature"], "c2ln")


class TimestampTest(AnalyzeCase):
    """A timestamp an answer copied from the request is restored to this run's; one the model made is never stored."""

    @staticmethod
    def stamp(when):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))

    def card(self, at):
        return {"model": "m", "messages": [
            {"role": "user", "content": "work kanban task t_bbbbbbbb"},
            {"role": "tool", "tool_call_id": "call_1", "content": f"card created {self.stamp(at - 30 * MINUTE)}"}]}

    def said(self, at):
        return {"role": "assistant", "content": f"As of {self.stamp(at - 30 * MINUTE)} the card is open."}

    def test_a_copied_timestamp_is_restored_a_day_later_and_the_next_request_still_matches(self):
        self.assertEqual(self.recorded(self.card(CLOCK), self.said(CLOCK), at=CLOCK)["unstorable"], [])
        went_on = self.card(CLOCK)
        went_on["messages"] += [self.said(CLOCK), {"role": "user", "content": "thanks"}]
        self.recorded(went_on, {"role": "assistant", "content": "Done."}, at=CLOCK + MINUTE)

        replay = self.proxy()
        later = CLOCK + DAY
        hit, _ = replay.lookup(self.card(later), later)
        self.assertEqual(hit[0]["content"], self.said(later)["content"], "this run's time, not the recording's")
        echoed = self.card(later)
        echoed["messages"] += [hit[0], {"role": "user", "content": "thanks"}]
        hit, refused = replay.lookup(echoed, later + MINUTE)
        self.assertIsNone(refused)
        self.assertEqual((hit[0]["content"], hit[3]), ("Done.", "exact"))

    def job(self, start, transition, deadline):
        shown = (f"Start Time: {self.stamp(start)}\nLast Transition: {self.stamp(transition)}\n"
                 f"Deadline: {self.stamp(deadline)}")
        return {"model": "m", "messages": [{"role": "user", "content": "describe job backup"},
                                           {"role": "tool", "tool_call_id": "call_1", "content": shown}]}

    def test_a_quoted_timestamp_is_restored_from_the_same_field(self):
        # Recorded: the start time equals the last transition. A replay whose
        # transition is a second later keys the same. Numbered by distinct value,
        # the deadline (recorded as the second distinct timestamp) would be
        # served as the replay's transition; by position it is the deadline.
        start, deadline = CLOCK - 40 * MINUTE, CLOCK + 20 * MINUTE
        self.recorded(self.job(start, start, deadline),
                      {"role": "assistant", "content": f"Deadline is {self.stamp(deadline)}."}, at=CLOCK)
        hit, _ = self.proxy().lookup(self.job(start, start + 1, deadline + 5), CLOCK)
        self.assertEqual(hit[0]["content"], f"Deadline is {self.stamp(deadline + 5)}.")

    def test_a_quoted_timestamp_whose_fields_disagree_in_the_replay_is_not_served(self):
        # Recorded, the start time was also the last transition, so the answer's
        # timestamp names both fields; in the replay they differ, so neither is safe.
        start, deadline = CLOCK - 40 * MINUTE, CLOCK + 20 * MINUTE
        self.recorded(self.job(start, start, deadline),
                      {"role": "assistant", "content": f"Started {self.stamp(start)}."}, at=CLOCK)
        self.assertEqual(self.proxy().lookup(self.job(start, start + 1, deadline), CLOCK),
                         (None, {"via": "exact", "guard": "unbound_placeholder"}))

    def test_a_timestamp_the_model_made_is_not_stored(self):
        made = {"role": "assistant", "content": f"Checked at {self.stamp(CLOCK + 5)}."}
        self.assertEqual(self.recorded(self.card(CLOCK), made, at=CLOCK)["unstorable"], ["unbound_timestamp"])


class LostAnalysisLinesTest(unittest.TestCase):
    """A request line whose analysis was dropped or failed still says whether it was served."""

    def proxy(self):
        out = io.StringIO()
        cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        proxy = replay_proxy.ShadowProxy(replay_proxy.Config(mode="replay", allow_serve=True, cache_dir=cache), out=out)
        self.addCleanup(proxy.close)
        return proxy, out

    def test_a_failed_analysis_line_carries_mode_and_served(self):
        proxy, out = self.proxy()
        proxy.analyze = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
        proxy.submit({"messages": []}, time.time(), {"status": 200}, 0, served={"recorded_by": {"build": "7"}})
        proxy.drain()
        line = [json.loads(l) for l in out.getvalue().splitlines() if '"error"' in l][-1]
        self.assertEqual((line["mode"], line["served"], line["served_from"]), ("replay", True, {"build": "7"}))

    def test_a_dropped_analysis_line_carries_mode_and_served(self):
        proxy, out = self.proxy()
        # The real queue, refusing new work as a full one does; close() still stops the worker.
        proxy._queue.put_nowait = lambda item: (_ for _ in ()).throw(replay_proxy.queue.Full())
        proxy.submit({"messages": []}, time.time(), {"status": 200}, 0, served=None)
        line = [json.loads(l) for l in out.getvalue().splitlines() if '"error"' in l][-1]
        self.assertEqual((line["mode"], line["served"]), ("replay", False))


class RecordTest(ProxyTestCase):
    mode = "record"

    def test_storable_answer_is_saved_once_in_normalized_form(self):
        # The first request's analysis is queued late, as a slow handler thread
        # could; waiting for its line keeps the second from being analysed first.
        submit, delayed = self.proxy.submit, []

        def late_first(*args, **kwargs):
            if not delayed:
                delayed.append(True)
                time.sleep(LATE_SUBMIT_SECONDS)
            submit(*args, **kwargs)

        self.proxy.submit = late_first
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=True))
        self.lines(1)
        self.request("POST", COMPLETIONS, chat("t_22222222", stream=True))
        first, second = self.lines(2)
        self.assertEqual(first["mode"], "record")
        self.assertFalse(first["served"])
        self.assertTrue(first["recorded"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertFalse(second["recorded"], "the same answer is saved once")
        self.assertEqual(len(FakeLiteLLM.calls), 2, "record mode never serves")
        (name, entry), = replay_cache.AnswerCache(self.cache_dir).answers(first["fingerprint"])
        self.assertEqual(name, first["stored_answer_hash"], "an alternate is named by the hash of the answer as stored")
        arguments = json.loads(entry["answer"]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(arguments, {"task_id": "⟦TASK_1⟧"})
        self.assertEqual(entry["answer"]["tool_calls"][0]["id"], "call_1__thought__c2ln", "opaque ids are kept")
        self.assertEqual(entry["finish_reason"], "tool_calls")
        self.assertEqual(entry["usage"]["prompt_tokens"], 2000)
        self.assertEqual(entry["diag_message_hashes"], first["diag_message_hashes"])
        self.assertEqual(entry["diag_settings_hash"], first["diag_settings_hash"])
        self.assertEqual(entry["message_hashes"], first["message_hashes"])
        self.assertEqual(entry["recorded_by"]["build"], "42")
        messages = os.listdir(os.path.join(self.cache_dir, replay_cache.MESSAGES_DIR))
        self.assertEqual(len(messages), 2)
        stored = "".join(Path(self.cache_dir, replay_cache.MESSAGES_DIR, m).read_text() for m in messages)
        self.assertNotIn(TOKEN, stored, "saved messages are scrubbed")

    def test_only_messages_not_yet_saved_are_scrubbed(self):
        scrubbed = []
        original = self.proxy._scrubbed_message
        self.proxy._scrubbed_message = lambda walked, reads: scrubbed.append(walked) or original(walked, reads)
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        self.lines(1)
        self.assertEqual(len(scrubbed), 2)
        extra = ({"role": "user", "content": "one more turn"},)
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False, extra_messages=extra))
        self.lines(2)
        self.assertEqual(len(scrubbed), 3, "only the new turn is scrubbed; the rest were saved already")

    def test_a_recorded_request_is_normalized_once(self):
        calls = []
        original = normalizer.normalize_request
        normalizer.normalize_request = lambda *args: calls.append(args) or original(*args)
        self.addCleanup(setattr, normalizer, "normalize_request", original)
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        self.assertTrue(self.lines(1)[0]["recorded"])
        self.assertEqual(len(calls), 1, "the answer is noted from the analysis's normalization")

    def test_a_recorded_answer_is_normalized_once(self):
        calls = []
        original = replay_cache.normalize_answer
        replay_cache.normalize_answer = lambda *args: calls.append(args) or original(*args)
        self.addCleanup(setattr, replay_cache, "normalize_answer", original)
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        self.assertTrue(self.lines(1)[0]["recorded"])
        self.assertEqual(len(calls), 1, "the stored form is checked and saved from one normalization")

    def test_unstorable_answer_is_not_saved(self):
        FakeLiteLLM.fallbacks = "1"
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        line = self.lines(1)[0]
        self.assertFalse(line["recorded"])
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, replay_cache.ALTS_DIR)))


class RecordContinuationTest(ProxyTestCase):
    """A record line names its answer, and the next request names the answer it went on from."""

    mode = "record"

    def test_the_next_requests_prior_answer_is_the_recorded_answer_variant(self):
        self.request("POST", COMPLETIONS, ReplayTest.opening("t_11111111"))
        first = self.lines(1)[0]
        self.assertTrue(first["recorded"])
        self.assertNotIn("prior_answer", first, "an opening holds no answer yet")
        self.request("POST", COMPLETIONS, ReplayTest.pickup("t_11111111", "answer 1 for t_11111111", "Latte"))
        second = self.lines(2)[1]
        self.assertEqual(second["prior_answer"], first["answer_variant"])
        self.request("POST", COMPLETIONS, ReplayTest.pickup("t_11111111", "a different answer", "Latte"))
        third = self.lines(3)[2]
        self.assertNotEqual(third["prior_answer"], first["answer_variant"],
                            "a conversation that went on from another answer is not evidence for this one")


class RecordBudgetTest(ProxyTestCase):
    """The cache counts against the data budget in record mode: spent, nothing is saved and forwarding goes on."""

    mode = "record"
    data_max_bytes = 1

    def test_a_spent_budget_saves_nothing_and_still_answers(self):
        self.proxy.budget._used = 10  # past the one-byte budget, as a full volume would be
        status, _, _ = self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        self.assertEqual(status, 200)
        line = self.lines(1)[0]
        self.assertEqual((line["recorded"], line["record_error"]), (False, "budget_spent"))
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, replay_cache.ALTS_DIR)))
        self.assertEqual(self.proxy.metrics.get("record_budget_spent"), 1)

    def test_the_cache_directory_is_one_of_the_budgets_roots(self):
        self.assertIn(self.cache_dir, self.proxy.budget.roots)


class SecretReadTest(ProxyTestCase):
    """A tool result whose call printed a secret reaches neither the saved messages nor a miss line."""

    @staticmethod
    def body(value, task):
        return {"model": "model-default", "stream": False, "messages": [
            {"role": "system", "content": "You are the platform agent."},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call_s", "type": "function", "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": "kubectl get secret db -o jsonpath='{.data.password}' | base64 -d"})}}]},
            {"role": "tool", "tool_call_id": "call_s", "content": f"plain looking value {value}"},
            {"role": "user", "content": f"work kanban task {task}"},
        ]}

    def test_an_answer_after_a_secret_read_is_not_stored(self):
        # The answer repeats the decoded value, which no scrubber can recognise.
        FakeLiteLLM.custom_body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "The password is plain looking value hunter2."}}]}).encode()
        _, record_port, record_out = self.start_proxy(mode="record")
        self.request("POST", COMPLETIONS, self.body("hunter2", "t_11111111"), port=record_port)
        line = self.lines(1, record_out)[0]
        self.assertIn("follows_secret_read", line["unstorable"])
        self.assertFalse(line["recorded"])
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, replay_cache.ALTS_DIR)))

    def test_a_secret_read_result_is_withheld_in_the_cache_and_in_a_miss(self):
        # Record mode refuses such a request (follows_secret_read); the scrubbing
        # below is the second line, met by lifting that refusal for this test.
        original = replay_proxy.unstorable_reasons
        replay_proxy.unstorable_reasons = lambda *args, **kwargs: [
            r for r in original(*args, **kwargs) if r != "follows_secret_read"]
        self.addCleanup(setattr, replay_proxy, "unstorable_reasons", original)
        _, record_port, record_out = self.start_proxy(mode="record")
        self.request("POST", COMPLETIONS, self.body("hunter2", "t_11111111"), port=record_port)
        self.lines(1, record_out)
        _, replay_port, replay_out = self.start_proxy(mode="replay", allow_serve=True)
        self.request("POST", COMPLETIONS, self.body("swordfish", "t_11111111"), port=replay_port)
        miss = self.lines(1, replay_out)[0]["miss"]
        self.assertEqual((miss["index"], miss["role"]), (2, "tool"))
        saved = "".join(p.read_text() for p in Path(self.cache_dir, replay_cache.MESSAGES_DIR).iterdir())
        logged = replay_out.getvalue()
        for value in ("hunter2", "swordfish"):
            self.assertNotIn(value, saved)
            self.assertNotIn(value, logged)
        self.assertIn("REDACTED_RESULT", saved)
        self.assertEqual(miss["saved_snippet"], miss["current_snippet"], "both sides are withheld alike")


class ShadowNeverServesTest(ProxyTestCase):
    def test_shadow_with_a_full_cache_still_calls_the_model(self):
        _, record_port, record_out = self.start_proxy(mode="record")
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False), port=record_port)
        self.lines(1, record_out)
        status, headers, _ = self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        self.assertEqual(status, 200)
        self.assertEqual(len(FakeLiteLLM.calls), 2)
        self.assertNotIn("x-replay-served", headers)
        line = self.lines(1)[0]
        self.assertEqual(line["mode"], "shadow")
        self.assertFalse(line["served"])
        self.assertNotIn("miss", line)


class ReplayTest(ProxyTestCase):
    def setUp(self):
        super().setUp()
        _, self.record_port, self.record_out = self.start_proxy(mode="record", job_kind="nightly", job_build="7")

    def record(self, *bodies):
        """Record each body in turn, waiting for its line, so the first write is the first body's."""
        done = len(self.record_out.getvalue().splitlines())
        for body in bodies:
            self.request("POST", COMPLETIONS, body, port=self.record_port)
            done += 1
            self.lines(done, self.record_out)

    def replay(self):
        proxy, port, out = self.start_proxy(mode="replay", allow_serve=True, job_build="99")
        return proxy, port, out

    def test_a_hit_is_served_with_the_current_values_and_no_model_call(self):
        self.record(chat("t_11111111", stream=True))
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        status, headers, payload = self.request("POST", COMPLETIONS, chat("t_22222222", stream=True), port=port)
        self.assertEqual(status, 200)
        self.assertEqual(len(FakeLiteLLM.calls), calls, "the model is not called on a hit")
        self.assertEqual(headers["x-replay-served"], "true")
        self.assertEqual(headers["content-type"], "text/event-stream")
        message, usage, finish = sse_message(payload)
        self.assertEqual(json.loads(message["tool_calls"][0]["function"]["arguments"]), {"task_id": "t_22222222"})
        self.assertEqual(message["tool_calls"][0]["id"], "call_1__thought__c2ln")
        self.assertEqual(usage["prompt_tokens"], 2000)
        self.assertEqual(finish, "tool_calls")
        line = self.lines(1, out)[0]
        self.assertEqual(line["mode"], "replay")
        self.assertTrue(line["served"])
        self.assertEqual(line["served_from"]["build"], "7")
        self.assertNotIn("miss", line)

    def test_reps_are_served_the_keys_answers_in_turn_and_the_line_says_which(self):
        self.record(chat("t_11111111", stream=False))
        fingerprint = self.lines(1, self.record_out)[0]["fingerprint"]
        recorded, = Path(self.cache_dir, replay_cache.ALTS_DIR, fingerprint).iterdir()
        other = dict(json.loads(recorded.read_text()), answer={"role": "assistant", "content": "Nothing to do."})
        self.assertTrue(replay_cache.AnswerCache(self.cache_dir).save_alternate(fingerprint, "f" * 64, other))
        names = sorted([recorded.stem, "f" * 64])
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        bodies = []
        for _ in range(3):
            status, headers, payload = self.request("POST", COMPLETIONS, chat("t_22222222", stream=False), port=port)
            self.assertEqual((status, headers["x-replay-served"]), (200, "true"))
            bodies.append(json.loads(payload)["choices"][0]["message"])
        self.assertEqual(len(FakeLiteLLM.calls), calls, "every rep is served")
        self.assertEqual(bodies[0], bodies[2])
        self.assertNotEqual(bodies[0], bodies[1])
        lines = self.lines(3, out)
        self.assertEqual([line["served_alternate"] for line in lines], [names[0], names[1], names[0]])
        self.assertEqual({line["served_key"] for line in lines}, {fingerprint}, "an exact hit: its own key")
        self.assertEqual({line["alternates"] for line in lines}, {2})
        self.assertEqual({(line["served_weight"], line["alternates_weight"]) for line in lines}, {(1, 2)},
                         "no weights file: each answer weighs 1")

    def test_a_replay_request_is_normalized_and_fingerprinted_once(self):
        self.record(chat("t_11111111", stream=False))
        _, port, out = self.replay()
        for body in (chat("t_22222222", stream=False), chat("t_33333333", stream=False, extra_messages=(
                {"role": "user", "content": "a turn never recorded"},))):
            calls = {"normalize_request": [], "fingerprint": []}
            originals = {name: getattr(normalizer, name) for name in calls}
            for name, original in originals.items():
                setattr(normalizer, name, lambda *args, n=name, o=original: calls[n].append(args) or o(*args))
            try:
                self.request("POST", COMPLETIONS, body, port=port)
                self.lines(1, out)  # the analysis has run before the originals come back
            finally:
                for name, original in originals.items():
                    setattr(normalizer, name, original)
            self.assertEqual({n: len(c) for n, c in calls.items()}, {"normalize_request": 1, "fingerprint": 1},
                             "a hit and a miss are each normalized and fingerprinted once")
            out.truncate(0)
            out.seek(0)

    def test_a_served_line_is_not_judged_as_a_new_answer(self):
        # Served, the model was not called: no LiteLLM headers, and nothing to save.
        self.record(chat("t_11111111", stream=False))
        _, port, out = self.replay()
        self.request("POST", COMPLETIONS, chat("t_22222222", stream=False), port=port)
        line = self.lines(1, out)[0]
        self.assertTrue(line["served"])
        self.assertEqual(line["unstorable"], [])
        self.assertEqual((line["litellm_model_group"], line["litellm_attempted_fallbacks"]), (None, None))

    def test_a_served_line_carries_no_litellm_accounting(self):
        # The recording's usage block keeps LiteLLM's cost and reasoning count;
        # a served answer never reached LiteLLM, so its line says none of them.
        FakeLiteLLM.extra_headers = {"x-litellm-model-group": "model-default", "x-litellm-attempted-fallbacks": "0",
                                     "x-litellm-model-name": "vertex_ai/gemini-x", "x-litellm-attempted-retries": "1",
                                     "x-litellm-response-cost": "0.5"}
        self.addCleanup(setattr, FakeLiteLLM, "extra_headers", {})
        FakeLiteLLM.custom_body = json.dumps({
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 600, "completion_tokens": 59, "cost": 0.25, "cache_creation_input_tokens": 40,
                      "completion_tokens_details": {"reasoning_tokens": 57}},
        }).encode()
        self.record(chat("t_11111111", stream=False))
        recorded = json.loads(self.record_out.getvalue().splitlines()[-1])
        self.assertEqual((recorded["litellm_cost_usd"], recorded["litellm_model_name"], recorded["litellm_attempted_retries"],
                          recorded["reasoning_tokens"], recorded["cache_write_tokens"]),
                         (0.25, "vertex_ai/gemini-x", 1, 57, 40))
        _, port, out = self.replay()
        self.request("POST", COMPLETIONS, chat("t_22222222", stream=False), port=port)
        line = self.lines(1, out)[0]
        self.assertTrue(line["served"])
        for name in ("litellm_cost_usd", "litellm_model_name", "litellm_attempted_retries", "reasoning_tokens",
                     "cache_write_tokens"):
            self.assertIsNone(line[name], name)

    def test_a_non_streamed_hit(self):
        self.record(chat("t_11111111", stream=False))
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        status, _, payload = self.request("POST", COMPLETIONS, chat("t_22222222", stream=False), port=port)
        self.assertEqual(len(FakeLiteLLM.calls), calls)
        body = json.loads(payload)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "answer 1 for t_22222222")
        self.assertEqual(body["usage"]["prompt_tokens"], 1000)
        self.assertTrue(self.lines(1, out)[0]["served"])

    @staticmethod
    def opening(task):
        """A worker's first request."""
        return {"model": "model-default", "stream": False, "messages": [
            {"role": "system", "content": "You are the platform agent."},
            {"role": "user", "content": f"work kanban task {task}"},
        ]}

    @classmethod
    def pickup(cls, task, said, ticket):
        """A worker's second request: the opening, its answer to it, and what the tool showed."""
        body = cls.opening(task)
        body["messages"] += [{"role": "assistant", "content": said}, {"role": "user", "content": f"ticket {ticket}"}]
        return body

    def record_two_pickups(self):
        # Two recordings got different answers to the same opening (the fake
        # numbers its answers by call); the second went on to the Latte ticket.
        _, other_port, other_out = self.start_proxy(mode="record", job_kind="nightly", job_build="8")
        self.request("POST", COMPLETIONS, self.opening("t_aaaaaaaa"), port=other_port)  # "answer 1 for t_aaaaaaaa"
        self.lines(1, other_out)
        self.record(self.opening("t_bbbbbbbb"), self.pickup("t_bbbbbbbb", "answer 2 for t_bbbbbbbb", "Latte"))

    def test_another_recorded_answer_to_the_same_request_still_matches(self):
        self.record_two_pickups()
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        # Bob again, but served the other recording's answer at the fork.
        status, headers, payload = self.request(
            "POST", COMPLETIONS, self.pickup("t_cccccccc", "answer 1 for t_cccccccc", "Latte"), port=port)
        self.assertEqual(status, 200)
        self.assertEqual(len(FakeLiteLLM.calls), calls, "served without calling the model")
        self.assertEqual(headers["x-replay-served"], "true")
        self.assertEqual(json.loads(payload)["choices"][0]["message"]["content"], "answer 3 for Latte")
        line = self.lines(1, out)[0]
        self.assertEqual((line["served"], line["served_via"]), (True, "answer_slots"))

    def test_an_answer_never_recorded_for_that_request_misses(self):
        self.record_two_pickups()
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        self.request("POST", COMPLETIONS, self.pickup("t_cccccccc", "No, cancel it.", "Latte"), port=port)
        self.assertEqual(len(FakeLiteLLM.calls), calls + 1)
        line = self.lines(1, out)[0]
        self.assertFalse(line["served"])
        self.assertEqual(line["miss"]["refused"], {"via": "answer_slots", "guard": "answer_not_recorded"})

    def test_only_answers_the_proxy_recorded_fill_a_slot(self):
        # The first opening's answer came from a fallback, and the client went on
        # with it: the answer is in a recorded history, but no recording stored it.
        FakeLiteLLM.fallbacks = "1"
        self.record(self.opening("t_aaaaaaaa"))  # "answer 1 for t_aaaaaaaa", unstorable
        FakeLiteLLM.fallbacks = "0"
        self.record(self.pickup("t_aaaaaaaa", "answer 1 for t_aaaaaaaa", "Tea"),
                    self.opening("t_bbbbbbbb"), self.pickup("t_bbbbbbbb", "answer 3 for t_bbbbbbbb", "Latte"))
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        self.request("POST", COMPLETIONS, self.pickup("t_cccccccc", "answer 1 for t_cccccccc", "Latte"), port=port)
        self.assertEqual(len(FakeLiteLLM.calls), calls + 1, "a fallback's answer is not a recorded one")
        self.assertFalse(self.lines(1, out)[0]["served"])

    def test_what_the_tool_showed_still_has_to_match(self):
        self.record_two_pickups()
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        self.request("POST", COMPLETIONS, self.pickup("t_cccccccc", "answer 1 for t_cccccccc", "Mocha"), port=port)
        self.assertEqual(len(FakeLiteLLM.calls), calls + 1)
        line = self.lines(1, out)[0]
        self.assertFalse(line["served"])
        self.assertNotIn("refused", line["miss"], "no key matched: an ordinary fork")

    def test_an_exact_match_says_so(self):
        self.record_two_pickups()
        _, port, out = self.replay()
        self.request("POST", COMPLETIONS, self.pickup("t_cccccccc", "answer 2 for t_cccccccc", "Latte"), port=port)
        self.assertEqual(self.lines(1, out)[0]["served_via"], "exact")

    def test_a_miss_is_compared_with_the_most_alike_of_conversations_sharing_its_opening(self):
        # Every worker conversation opens the same way, so the miss must be shown
        # against the sibling whose next message is most like this one.
        cards = (
            "card: list the namespaces on cluster alpha",
            "card: check kube-system pods on cluster beta and report restarts with reasons",
            "card: design a training cluster with eight GPUs in us-central1",
            "card: open a pull request bumping the node pool version",
        )
        self.record(*(chat("t_11111111", stream=False, extra_messages=({"role": "user", "content": c},)) for c in cards))
        recorded = {line["fingerprint"] for line in self.lines(len(cards), self.record_out)}
        _, port, out = self.replay()
        changed = ({"role": "user", "content": "card: check kube-system pods on cluster beta and report restart counts"},)
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False, extra_messages=changed), port=port)
        miss = self.lines(1, out)[0]["miss"]
        self.assertIn(miss["closest_fingerprint"], recorded)
        self.assertEqual((miss["matched_messages"], miss["index"]), (1, 1))
        self.assertIn("cluster beta", miss["saved_snippet"])

    def test_a_miss_goes_live_is_not_recorded_and_says_where_it_differed(self):
        extra = ({"role": "user", "content": "first turn"},)
        self.record(chat("t_11111111", stream=False, extra_messages=extra))
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        changed = ({"role": "user", "content": "first turn, but the pod restarted 3 times"},)
        status, headers, payload = self.request(
            "POST", COMPLETIONS, chat("t_11111111", stream=False, extra_messages=changed), port=port)
        self.assertEqual(status, 200)
        self.assertEqual(len(FakeLiteLLM.calls), calls + 1, "a miss calls the model")
        self.assertNotIn("x-replay-served", headers)
        line = self.lines(1, out)[0]
        self.assertFalse(line["served"])
        self.assertNotIn("recorded", line)
        miss = line["miss"]
        recorded = self.lines(1, self.record_out)[0]
        self.assertEqual(miss["closest_fingerprint"], recorded["fingerprint"])
        self.assertEqual(miss["matched_messages"], 1)
        self.assertEqual((miss["index"], miss["role"]), (1, "user"))
        self.assertIn("first turn", miss["saved_snippet"])
        self.assertIn("restarted", miss["current_snippet"])
        self.assertLessEqual(len(miss["current_snippet"]), replay_proxy.MISS_SNIPPET_CHARS)
        self.assertIn(" -> ", miss["signature"])
        self.assertEqual(replay_cache.AnswerCache(self.cache_dir).answers(line["fingerprint"]), [])

    def test_a_miss_with_different_settings(self):
        self.record(chat("t_11111111", stream=False))
        _, port, out = self.replay()
        body = chat("t_11111111", stream=False)
        body["tools"] = [{"type": "function", "function": {"name": "new_tool"}}]
        self.request("POST", COMPLETIONS, body, port=port)
        self.assertEqual(self.lines(1, out)[0]["miss"]["role"], "settings")

    def test_an_empty_cache_says_so(self):
        _, port, out = self.replay()
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False), port=port)
        self.assertEqual(self.lines(1, out)[0]["miss"]["role"], "no_saved_request")

    def test_a_cache_seeded_after_start_rebuilds_the_miss_index_once_marked(self):
        # The replay lane seeds the cache into a running proxy (collect_shadow.py).
        _, port, out = self.replay()
        extra = ({"role": "user", "content": "first turn"},)
        self.record(chat("t_11111111", stream=False, extra_messages=extra))
        changed = ({"role": "user", "content": "first turn, but the pod restarted 3 times"},)
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False, extra_messages=changed), port=port)
        self.assertEqual(self.lines(1, out)[0]["miss"]["role"], "no_saved_request", "not marked yet")
        Path(self.cache_dir, replay_cache.SEED_MARKER).write_text("1")
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False, extra_messages=changed), port=port)
        miss = self.lines(2, out)[1]["miss"]
        self.assertEqual(miss["closest_fingerprint"], self.lines(1, self.record_out)[0]["fingerprint"])
        self.assertEqual((miss["index"], miss["role"]), (1, "user"))

    def test_an_unbound_placeholder_is_a_miss(self):
        self.record(chat("t_11111111", stream=False))
        fingerprint = self.lines(1, self.record_out)[0]["fingerprint"]
        path, = Path(self.cache_dir, replay_cache.ALTS_DIR, fingerprint).iterdir()
        entry = json.loads(path.read_text())
        entry["answer"]["content"] = "look at ⟦TASK_9⟧"
        path.write_text(json.dumps(entry))
        _, port, out = self.replay()
        calls = len(FakeLiteLLM.calls)
        status, headers, _ = self.request("POST", COMPLETIONS, chat("t_22222222", stream=False), port=port)
        self.assertEqual(len(FakeLiteLLM.calls), calls + 1)
        self.assertNotIn("x-replay-served", headers)
        line = self.lines(1, out)[0]
        self.assertFalse(line["served"])
        self.assertEqual(line["miss"]["refused"], {"via": "exact", "guard": "unbound_placeholder"})


if __name__ == "__main__":
    unittest.main()
