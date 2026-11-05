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
COMPLETIONS = "/v1/chat/completions"
SUBPROCESS_TIMEOUT_SECONDS = 30


def sse_message(payload):
    """The answer and usage a client assembles from an SSE body."""
    message, usage, finish = {}, None, None
    for line in payload.decode().split("\n"):
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[len("data: "):])
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            replay_proxy.merge_delta(message, choice.get("delta") or {})
            finish = choice.get("finish_reason") or finish
    return message, usage, finish


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

    def recorded(self, body, answer, at=None, build="x"):
        """Record mode's log line for `body` answered with `answer`."""
        return self.proxy("record", build).analyze(body, time.time() if at is None else at, self.upstream(answer))


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
        # Bob named his own pod in his yes; Alice's recorded yes named another.
        bob = self.body("Yes, web-7d4b9c8f6d-x2k9p.")
        alice_yes = self.body("Yes please, web-5f6c7d8b9c-m8n7q.")
        self.record(bob, {"role": "assistant", "content": "restart web-7d4b9c8f6d-x2k9p"}, others=[alice_yes])
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

    def test_a_failed_entry_write_leaves_no_slot(self):
        recorder = self.proxy("record", "x")
        recorder.cache.save = lambda *args: (_ for _ in ()).throw(OSError("disk full"))
        with self.assertRaises(OSError):
            recorder.analyze(self.BODY, time.time(), self.upstream(self.ANSWER))
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


class UnreadableEntryTest(SlotCacheCase):
    """A key that points at a missing or broken saved answer is a refused miss, never a served one."""

    BODY = {"model": "m", "messages": [{"role": "user", "content": "work kanban task t_bbbbbbbb"},
                                       {"role": "assistant", "content": "Yes."},
                                       {"role": "user", "content": "ticket Latte"}]}

    def test_a_broken_exact_entry(self):
        self.record(self.BODY, {"role": "assistant", "content": "done"})
        for path in Path(self.cache_dir, replay_cache.ANSWERS_DIR).iterdir():
            path.write_text(json.dumps({"answer": "not an answer"}))
        self.assertEqual(self.proxy().lookup(self.BODY, time.time()),
                         (None, {"via": "exact", "guard": "unreadable_entry"}))

    def test_a_slot_whose_entry_is_gone(self):
        self.record(self.BODY, {"role": "assistant", "content": "done"})
        for path in Path(self.cache_dir, replay_cache.ANSWERS_DIR).iterdir():
            path.unlink()
        self.assertEqual(self.proxy().lookup(self.BODY, time.time()),
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

    def fork(self, opening, result, settings=None, later=0):
        """Two recordings answer `opening` in other words; the second goes on to `result` `later` seconds on.

        A replay that was served the first recording's answer then asks for the
        second recording's next step. Returns the replay's lookup.
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

    def test_an_ip_is_not_found_inside_a_longer_one(self):
        line = self.recorded(self.body("node 10.1.2.3 is cordoned"),
                             {"role": "assistant", "content": "10.1.2.30 is the gateway"})
        self.assertEqual(line["unstorable"], [])


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
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=True))
        self.request("POST", COMPLETIONS, chat("t_22222222", stream=True))
        first, second = self.lines(2)
        self.assertEqual(first["mode"], "record")
        self.assertFalse(first["served"])
        self.assertTrue(first["recorded"])
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertFalse(second["recorded"], "the first write wins")
        self.assertEqual(len(FakeLiteLLM.calls), 2, "record mode never serves")
        entry = replay_cache.AnswerCache(self.cache_dir).load(first["fingerprint"])
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

    def test_unstorable_answer_is_not_saved(self):
        FakeLiteLLM.fallbacks = "1"
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        line = self.lines(1)[0]
        self.assertFalse(line["recorded"])
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, replay_cache.ANSWERS_DIR)))


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

    def test_a_secret_read_result_is_withheld_in_the_cache_and_in_a_miss(self):
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
        for body in bodies:
            self.request("POST", COMPLETIONS, body, port=self.record_port)
        self.lines(len(bodies), self.record_out)

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
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, "answers", line["fingerprint"] + ".json")))

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

    def test_an_unbound_placeholder_is_a_miss(self):
        self.record(chat("t_11111111", stream=False))
        fingerprint = self.lines(1, self.record_out)[0]["fingerprint"]
        path = Path(self.cache_dir, replay_cache.ANSWERS_DIR, fingerprint + ".json")
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
