"""Record and replay modes end to end against the fake LiteLLM, and the guard on serving."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import replay_cache
import replay_proxy
from test_replay_proxy import TOKEN, FakeLiteLLM, ProxyTestCase, chat

HERE = Path(__file__).resolve().parent
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

    def test_unstorable_answer_is_not_saved(self):
        FakeLiteLLM.fallbacks = "1"
        self.request("POST", COMPLETIONS, chat("t_11111111", stream=False))
        line = self.lines(1)[0]
        self.assertFalse(line["recorded"])
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, replay_cache.ANSWERS_DIR)))


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
        self.assertFalse(self.lines(1, out)[0]["served"])


if __name__ == "__main__":
    unittest.main()
