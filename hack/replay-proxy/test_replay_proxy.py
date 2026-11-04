"""The shadow proxy end to end against a fake LiteLLM: pass-through, the log line, /metrics, never replaying."""

import http.client
import io
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import replay_proxy

REPO_ROOT = Path(__file__).resolve().parents[2]
REDACTOR = str(REPO_ROOT / "agents/chat/defaults/plugins/common/redactor.py")
RULES = str(REPO_ROOT / "agents/platform/scripts/worker_transcript.py")
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
LOG_WAIT_SECONDS = 5
POLL_SECONDS = 0.02
STALL_SECONDS = 0.3
# A stall far longer than the proxy's timeout, which is far longer than the
# fake's header latency, so the timeout test has no narrow window.
LONG_STALL_SECONDS = 1.0
SHORT_TIMEOUT_SECONDS = 0.25
DEEP_NESTING = 100_000
SLOW_CHUNKS = 100
SLOW_CHUNK_SECONDS = 0.05


class FakeLiteLLM(BaseHTTPRequestHandler):
    """Answers every call with a fresh answer numbered by call, streamed when asked."""

    protocol_version = "HTTP/1.1"
    calls = []
    fallbacks = "0"
    model_group = "model-default"
    # "normal", "stall_reset" (one chunk, a pause, then a TCP reset),
    # "early_close" (two chunks, then the connection closes without the last
    # chunk), "short_body" (a non-streamed answer cut short of its
    # Content-Length), or "slow" (many chunks, slowly; `aborted` is set when a
    # write fails).
    mode = "normal"
    # When set, the whole body: one SSE chunk for a stream, the JSON otherwise.
    custom_body = None
    aborted = threading.Event()
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with self.lock:
            self.calls.append({"method": self.command, "path": self.path, "body": raw, "headers": dict(self.headers)})
            return raw, len(self.calls)

    def do_GET(self):
        self._record()
        data = b'{"data": [{"id": "model-default"}]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw, number = self._record()
        body = json.loads(raw)
        task = body["messages"][-1]["content"].split()[-1]
        # A fixed value, not the request's `model`: a response header is never
        # built from request data, even in this fake.
        headers = {"x-litellm-model-group": self.model_group, "x-litellm-attempted-fallbacks": self.fallbacks}
        if not body.get("stream") and self.custom_body is not None:
            data = self.custom_body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not body.get("stream"):
            data = json.dumps({
                "id": f"resp-{number}", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": f"answer {number} for {task}"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 7,
                          "prompt_tokens_details": {"cached_tokens": 600}},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if self.mode == "short_body":
                self.wfile.write(data[: len(data) // 2])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        chunks = self.stream_chunks(number, task) if self.custom_body is None else [self.custom_body]
        if self.mode == "slow":
            chunks = [b": keep-alive\n\n"] * SLOW_CHUNKS + chunks
        for index, chunk in enumerate(chunks):
            try:
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            except OSError:
                FakeLiteLLM.aborted.set()
                self.close_connection = True
                return
            if self.mode == "slow":
                time.sleep(SLOW_CHUNK_SECONDS)
            if self.mode == "stall" and index == 0:
                time.sleep(LONG_STALL_SECONDS)
                self.close_connection = True
                return
            if self.mode == "stall_reset" and index == 0:
                time.sleep(STALL_SECONDS)
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close()
                self.close_connection = True
                return
            if self.mode == "early_close" and index == 1:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
        self.wfile.write(b"0\r\n\r\n")

    @staticmethod
    def stream_chunks(number, task):
        events = [
            {"id": f"resp-{number}", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Checking "}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": f"call_{number}__thought__c2ln",
                          "type": "function", "function": {"name": "kanban_show", "arguments": ""}}]}}]},
            {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {
                "arguments": json.dumps({"task_id": task})}}]}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 2000, "completion_tokens": 12, "prompt_tokens_details": {"cached_tokens": 1500}}},
        ]
        out = [f"data: {json.dumps(e)}\n\n".encode() for e in events]
        # One event split across two chunks, as a real stream can.
        out[1:2] = [out[1][:10], out[1][10:]]
        return out + [b"data: [DONE]\n\n"]

    @classmethod
    def expected_stream_body(cls, number, task):
        return b"".join(cls.stream_chunks(number, task))


def chat(task, stream, model="model-default", extra_messages=()):
    return {
        "model": model,
        "stream": stream,
        "messages": [
            {"role": "system", "content": "You are the platform agent."},
            *extra_messages,
            {"role": "user", "content": f"token {TOKEN} pod web-7d4b9c8f6d-x2k9p work kanban task {task}"},
        ],
    }


class ProxyTestCase(unittest.TestCase):
    redactor_path = REDACTOR
    upstream_timeout = replay_proxy.DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    store_is_file = False

    def setUp(self):
        FakeLiteLLM.calls = []
        FakeLiteLLM.fallbacks = "0"
        FakeLiteLLM.mode = "normal"
        FakeLiteLLM.custom_body = None
        FakeLiteLLM.aborted = threading.Event()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeLiteLLM)
        threading.Thread(target=self.upstream.serve_forever, args=(POLL_SECONDS,), daemon=True).start()
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp, ignore_errors=True)
        self.store = temp
        if self.store_is_file:
            self.store = os.path.join(temp, "a-file")  # no directory can be made under a file
            with open(self.store, "w"):
                pass
        self.out = io.StringIO()
        config = replay_proxy.Config(
            listen_port=0, upstream_host="127.0.0.1", upstream_port=self.upstream.server_address[1],
            store_dir=self.store, redactor_path=self.redactor_path, transcript_rules_path=RULES,
            upstream_timeout_seconds=self.upstream_timeout,
            litellm_config_checksum="abc123", litellm_image="ghcr.io/berriai/litellm:v1",
            job_kind="presubmit", job_project="pool-1", job_build="42", job_pr="4700",
        )
        self.server = replay_proxy.make_server(config, host="127.0.0.1", out=self.out)
        self.proxy = self.server.proxy
        # Registered first, so it runs last: after a test's own cleanups have
        # released anything holding the worker.
        self.addCleanup(self.proxy.close)
        threading.Thread(target=self.server.serve_forever, args=(POLL_SECONDS,), daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.upstream.shutdown()
        self.server.server_close()
        self.upstream.server_close()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=LOG_WAIT_SECONDS)
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        resp = conn.getresponse()
        try:
            payload = resp.read()
        except http.client.IncompleteRead as exc:  # an upstream cut short shows as a cut-short response
            payload = exc.partial
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, headers, payload

    def lines(self, count):
        deadline = time.monotonic() + LOG_WAIT_SECONDS
        while time.monotonic() < deadline:
            lines = [json.loads(l) for l in self.out.getvalue().splitlines() if l]
            if len(lines) >= count:
                return lines
            time.sleep(POLL_SECONDS)
        self.fail(f"expected {count} log lines, got: {self.out.getvalue()!r}")


class PassThroughTest(ProxyTestCase):
    def test_non_streaming_bytes_and_request_pass_through_unchanged(self):
        body = chat("t_11111111", stream=False)
        raw = json.dumps(body).encode()
        status, headers, payload = self.request("POST", "/v1/chat/completions", raw)
        self.assertEqual(status, 200)
        self.assertEqual(FakeLiteLLM.calls[0]["body"], raw)
        expected = json.loads(payload)
        self.assertEqual(expected["choices"][0]["message"]["content"], "answer 1 for t_11111111")
        self.assertEqual(headers["x-litellm-model-group"], "model-default")
        self.assertEqual(headers["content-length"], str(len(payload)))

    def test_streaming_bytes_pass_through_unchanged(self):
        status, headers, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=True))
        self.assertEqual(status, 200)
        self.assertEqual(payload, FakeLiteLLM.expected_stream_body(1, "t_11111111"))
        self.assertEqual(headers["content-type"], "text/event-stream")

    def test_nothing_is_ever_replayed(self):
        for stream in (False, True):
            FakeLiteLLM.calls = []
            body = chat("t_11111111", stream=stream)
            first = self.request("POST", "/v1/chat/completions", body)[2]
            second = self.request("POST", "/v1/chat/completions", body)[2]
            self.assertEqual(len(FakeLiteLLM.calls), 2, "every request reaches the model")
            self.assertNotEqual(first, second, "the second answer is the model's fresh one")
            if stream:
                self.assertEqual(second, FakeLiteLLM.expected_stream_body(2, "t_11111111"))
            else:
                self.assertIn(b"answer 2 for", second)
        lines = self.lines(4)
        self.assertEqual(lines[0]["fingerprint"], lines[1]["fingerprint"])

    def test_other_paths_pass_through(self):
        status, _, payload = self.request("GET", "/v1/models")
        self.assertEqual(status, 200)
        self.assertIn(b"model-default", payload)
        self.assertEqual(FakeLiteLLM.calls[0]["path"], "/v1/models")
        self.assertEqual(self.out.getvalue(), "", "only chat requests are logged")

    def test_upstream_down_is_a_502(self):
        self.upstream.shutdown()
        self.upstream.server_close()
        status, _, _ = self.request("POST", "/v1/chat/completions", chat("t_1", stream=False))
        self.assertEqual(status, 502)
        self.assertEqual(self.proxy.metrics.get("upstream_errors"), 1)
        self.assertEqual(self.lines(1)[0]["status"], 502)

    def test_analysis_failure_never_costs_the_answer(self):
        def explode(*_args):
            raise RuntimeError(f"bug near {TOKEN}")

        self.proxy.analyze = explode
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.assertEqual(status, 200)
        self.assertIn(b"answer 1", payload)
        line = self.lines(1)[0]
        self.assertEqual(line["error"], "RuntimeError")
        self.assertEqual(self.proxy.metrics.get("analysis_errors"), 1)


class UpstreamFailureTest(ProxyTestCase):
    def assert_incomplete_line(self):
        line = self.lines(1)[0]
        self.assertTrue(line["upstream_incomplete"])
        self.assertEqual(line["unstorable"], ["upstream_incomplete"])
        self.assertTrue(line["fingerprint"])
        self.assertEqual(self.proxy.metrics.get("upstream_errors"), 1)

    def test_stall_then_reset(self):
        FakeLiteLLM.mode = "stall_reset"
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=True))
        self.assertEqual(status, 200)
        self.assertNotIn(b"[DONE]", payload)
        self.assert_incomplete_line()

    def test_chunked_stream_closed_early(self):
        FakeLiteLLM.mode = "early_close"
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=True))
        self.assertEqual(status, 200)
        self.assertNotIn(b"[DONE]", payload)
        self.assert_incomplete_line()


    def test_body_cut_short_of_its_content_length(self):
        FakeLiteLLM.mode = "short_body"
        started = time.monotonic()
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, LOG_WAIT_SECONDS, "the client is not left waiting")
        self.assertNotIn(b"usage", payload)
        self.assert_incomplete_line()


class UpstreamTimeoutTest(ProxyTestCase):
    upstream_timeout = SHORT_TIMEOUT_SECONDS

    def test_stall_past_the_timeout(self):
        FakeLiteLLM.mode = "stall"
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=True))
        line = self.lines(1)[0]
        self.assertEqual(line["unstorable"], ["upstream_incomplete"])
        self.assertEqual(self.proxy.metrics.get("upstream_errors"), 1)


def sse(event):
    return f"data: {event}\n\n".encode()


MALFORMED_STREAMS = {
    "tool_call index null": sse(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": None, "id": "c"}]}}]})),
    "tool_call index string": sse(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": "0", "id": "c"}]}}]})),
    "tool_call index negative": sse(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": -1, "id": "c"}]}}]})),
    "tool_call index a bool": sse(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": True, "id": "c"}]}}]})),
    "tool_call index at the cap": sse(json.dumps({"choices": [{"delta": {"tool_calls": [{"index": 256, "id": "c"}]}}]})),
    "data that is not JSON": sse("not json at all"),
    "delta not a dict": sse(json.dumps({"choices": [{"delta": ["x"]}]})),
    "choices a number": sse(json.dumps({"choices": 5})),
    "nested too deep": sse('{"choices": ' + "[" * DEEP_NESTING + "]" * DEEP_NESTING + "}"),
}


class ObserveFailureTest(ProxyTestCase):
    def check(self, body, stream):
        FakeLiteLLM.custom_body = body
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=stream))
        self.assertEqual(status, 200)
        self.assertEqual(payload, body, "the client gets every byte")
        line = self.lines(1)[0]
        self.assertEqual(line["unstorable"], ["observe_failed"])
        self.assertTrue(line["observe_failed"])
        self.assertTrue(line["tokens_estimated"], "no usage was read, so the tokens are estimated")
        self.assertIsNone(line["answer_hash"])
        self.assertTrue(line["fingerprint"])
        self.assertGreaterEqual(self.proxy.metrics.get("analysis_errors"), 1)

    def test_malformed_streamed_chunks(self):
        for name, body in MALFORMED_STREAMS.items():
            with self.subTest(name):
                self.out.seek(0)
                self.out.truncate()
                self.check(body, stream=True)

    def test_malformed_non_streamed_body(self):
        self.check(json.dumps({"choices": 5}).encode(), stream=False)

    def test_non_streamed_body_that_is_not_json(self):
        self.check(b"<html>upstream error page</html>", stream=False)


class MergeDeltaTest(unittest.TestCase):
    def test_tool_call_index_must_be_a_small_int(self):
        # 256 first: code without the bound accepts it and fails here, before
        # reaching 10**12, which it would try to allocate.
        for index in (256, 10**12, -1, True, "0", None, 1.0):
            with self.assertRaises((TypeError, ValueError), msg=repr(index)):
                replay_proxy.merge_delta({}, {"tool_calls": [{"index": index, "id": "c"}]})
        merged = replay_proxy.merge_delta({}, {"tool_calls": [{"index": 1, "id": "c"}]})
        self.assertEqual(merged["tool_calls"][1]["id"], "c")


class SseGrammarTest(ProxyTestCase):
    def test_multi_line_data_event_is_joined(self):
        event = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "joined answer"}}],
                 "usage": {"prompt_tokens": 321, "completion_tokens": 4}}
        text = json.dumps(event, indent=1)  # several lines
        body = "".join(f"data: {line}\n" for line in text.split("\n")).encode() + b"\n" + b"data: [DONE]\n\n"
        FakeLiteLLM.custom_body = body
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=True))
        self.assertEqual(payload, body)
        line = self.lines(1)[0]
        self.assertFalse(line["observe_failed"])
        self.assertEqual(line["prompt_tokens"], 321)
        self.assertTrue(line["answer_hash"])


class ObserveCapTest(ProxyTestCase):
    CAP = 4096

    def setUp(self):
        super().setUp()
        original = getattr(replay_proxy, "MAX_OBSERVED_BYTES", None)
        replay_proxy.MAX_OBSERVED_BYTES = self.CAP
        self.addCleanup(setattr, replay_proxy, "MAX_OBSERVED_BYTES", original)

    def check(self, body, stream):
        FakeLiteLLM.custom_body = body
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=stream))
        self.assertEqual(status, 200)
        self.assertEqual(payload, body, "the client gets every byte")
        line = self.lines(1)[0]
        self.assertEqual(line["unstorable"], ["observe_too_large"])
        self.assertTrue(line["observe_failed"])

    def test_oversized_streamed_answer(self):
        chunk = sse(json.dumps({"choices": [{"index": 0, "delta": {"content": "x" * 1000}}]}))
        self.check(chunk * 10 + b"data: [DONE]\n\n", stream=True)

    def test_oversized_non_streamed_body(self):
        body = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "x" * 10_000}}]})
        self.check(body.encode(), stream=False)


class RequestCapTest(ProxyTestCase):
    CAP = 4096

    def setUp(self):
        super().setUp()
        original = getattr(replay_proxy, "MAX_REQUEST_BYTES", None)
        replay_proxy.MAX_REQUEST_BYTES = self.CAP
        self.addCleanup(setattr, replay_proxy, "MAX_REQUEST_BYTES", original)

    def test_oversized_request_is_a_413_and_not_forwarded(self):
        body = chat("t_11111111", stream=False)
        body["messages"][-1]["content"] += " " + "y" * 10_000
        status, _, _ = self.request("POST", "/v1/chat/completions", body)
        self.assertEqual(status, 413)
        self.assertEqual(FakeLiteLLM.calls, [])


class FramingTest(ProxyTestCase):
    def raw(self, request):
        client = socket.create_connection(("127.0.0.1", self.port), timeout=LOG_WAIT_SECONDS)
        client.sendall(request)
        data = b""
        while True:
            part = client.recv(4096)
            if not part:
                break
            data += part
        client.close()
        return data

    def test_bad_content_length_is_a_400(self):
        reply = self.raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n{}")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply)
        self.assertEqual(FakeLiteLLM.calls, [])

    def test_bad_chunk_size_is_a_400(self):
        reply = self.raw(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n{}\r\n0\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply)
        self.assertEqual(FakeLiteLLM.calls, [])


class SurrogateTest(ProxyTestCase):
    def test_lone_surrogate_still_logs_and_stores(self):
        body = chat("t_11111111", stream=False)
        body["messages"].insert(1, {"role": "user", "content": "half an emoji \ud83d here"})
        status, _, _ = self.request("POST", "/v1/chat/completions", body)
        self.assertEqual(status, 200)
        line = self.lines(1)[0]
        self.assertNotIn("error", line)
        self.assertTrue(line["fingerprint"])
        self.assertTrue(line["stored"], line["store_skipped"])


class ClientDisconnectTest(ProxyTestCase):
    def test_upstream_is_closed_when_the_client_goes(self):
        FakeLiteLLM.mode = "slow"
        data = json.dumps(chat("t_11111111", stream=True)).encode()
        client = socket.create_connection(("127.0.0.1", self.port))
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                       + f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        client.recv(1024)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        line = self.lines(1)[0]
        self.assertTrue(line["client_disconnected"])
        self.assertEqual(line["unstorable"], ["client_disconnected"])
        # Closed, not read to the end: the upstream's next write fails long
        # before its stream would have finished.
        self.assertTrue(FakeLiteLLM.aborted.wait(SLOW_CHUNKS * SLOW_CHUNK_SECONDS / 2))


class StoreWriteFailureTest(ProxyTestCase):
    store_is_file = True

    def test_write_failure_keeps_the_line(self):
        status, _, _ = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.assertEqual(status, 200)
        line = self.lines(1)[0]
        self.assertFalse(line["stored"])
        self.assertTrue(line["store_skipped"].startswith("write_failed"), line["store_skipped"])
        self.assertTrue(line["fingerprint"])
        self.assertTrue(line["message_hashes"])


class DiagnosisHashTest(ProxyTestCase):
    def test_line_carries_diagnosis_hashes_beside_unchanged_message_hashes(self):
        import normalizer

        body = chat("t_11111111", stream=False)
        self.request("POST", "/v1/chat/completions", body)
        line = self.lines(1)[0]
        expected = normalizer.normalize_request(body, line["ts"]).message_hashes
        self.assertEqual(line["message_hashes"], expected)
        self.assertEqual(len(line["diag_message_hashes"]), len(body["messages"]))


class DiagSettingsTest(ProxyTestCase):
    def test_diag_settings_hash_holds_while_settings_hash_drifts(self):
        tools = [{"type": "function", "function": {"name": "ping", "description": "pings 10.0.0.1"}}]
        first = chat("t_11111111", stream=False)
        first["tools"] = tools
        second = json.loads(json.dumps(first))
        second["messages"].append({"role": "user", "content": "the pod is on 10.9.9.9 in 10.9.0.0/16"})
        self.request("POST", "/v1/chat/completions", first)
        self.request("POST", "/v1/chat/completions", second)
        a, b = self.lines(2)
        self.assertNotEqual(a["settings_hash"], b["settings_hash"], "the matching hash drifts, as before")
        self.assertEqual(a["diag_settings_hash"], b["diag_settings_hash"])


class EstimatedTokensTest(ProxyTestCase):
    def test_missing_usage_is_estimated_from_the_request_size(self):
        FakeLiteLLM.custom_body = sse("not json")
        body = chat("t_11111111", stream=True)
        self.request("POST", "/v1/chat/completions", body)
        line = self.lines(1)[0]
        self.assertTrue(line["tokens_estimated"])
        self.assertEqual(line["prompt_tokens"], len(json.dumps(body).encode()) // replay_proxy.BYTES_PER_TOKEN)

    def test_reported_usage_is_not_estimated(self):
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        line = self.lines(1)[0]
        self.assertFalse(line["tokens_estimated"])
        self.assertEqual(line["prompt_tokens"], 1000)


class BackgroundAnalysisTest(ProxyTestCase):
    def test_slow_analysis_does_not_delay_the_next_request_on_the_connection(self):
        original = self.proxy.analyze

        def slow(*args):
            time.sleep(1.0)
            return original(*args)

        self.proxy.analyze = slow
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=LOG_WAIT_SECONDS)
        self.addCleanup(conn.close)
        started = time.monotonic()
        for task in ("t_11111111", "t_22222222"):
            conn.request("POST", "/v1/chat/completions", body=json.dumps(chat(task, stream=False)),
                         headers={"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().read()[:1], b"{")
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertEqual(len(self.lines(2)), 2)


class AnalysisQueueFullTest(ProxyTestCase):
    def setUp(self):
        original = getattr(replay_proxy, "ANALYSIS_QUEUE_SIZE", None)
        replay_proxy.ANALYSIS_QUEUE_SIZE = 1
        self.addCleanup(setattr, replay_proxy, "ANALYSIS_QUEUE_SIZE", original)
        super().setUp()

    def test_full_queue_drops_and_counts(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.proxy.analyze

        def blocked(*args):
            started.set()
            release.wait(LOG_WAIT_SECONDS)
            return original(*args)

        self.proxy.analyze = blocked
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.assertTrue(started.wait(LOG_WAIT_SECONDS))
        self.request("POST", "/v1/chat/completions", chat("t_22222222", stream=False))  # queued
        status, _, _ = self.request("POST", "/v1/chat/completions", chat("t_33333333", stream=False))  # dropped
        self.assertEqual(status, 200)
        # The handler hands a request to the queue only after its response is
        # sent, so the client can see the response before the drop is counted.
        deadline = time.monotonic() + LOG_WAIT_SECONDS
        while self.proxy.metrics.get("analysis_dropped") < 1 and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        self.assertEqual(self.proxy.metrics.get("analysis_dropped"), 1)
        release.set()
        self.proxy.drain()
        lines = self.lines(3)
        self.assertEqual(sum(1 for l in lines if l.get("error") == "analysis_dropped"), 1)
        self.assertEqual(sum(1 for l in lines if l.get("fingerprint")), 2)


class LocalEndpointTest(ProxyTestCase):
    def test_metrics_is_answered_locally_and_never_forwarded(self):
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.lines(1)
        status, headers, payload = self.request("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        text = payload.decode()
        self.assertIn("replay_shadow_completions_total 1", text)
        self.assertIn("replay_shadow_scrubbers_loaded 1", text)
        self.assertEqual(self.request("POST", "/metrics", b"x")[0], 405)
        self.assertEqual(self.request("GET", "/metrics?name=x")[0], 200)
        self.assertEqual([c["path"] for c in FakeLiteLLM.calls], ["/v1/chat/completions"])

    def test_health(self):
        status, _, payload = self.request("GET", "/replay/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"ok": True, "scrubbers_loaded": True})
        self.assertEqual(FakeLiteLLM.calls, [])


class LogLineTest(ProxyTestCase):
    def test_non_streaming_line(self):
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        line = self.lines(1)[0]
        self.assertEqual(line["event"], "replay_shadow_request")
        self.assertEqual(line["job"], {"kind": "presubmit", "project": "pool-1", "build": "42", "pr": "4700"})
        self.assertEqual((line["prompt_tokens"], line["cached_tokens"], line["completion_tokens"]), (1000, 600, 7))
        self.assertEqual(len(line["fingerprint"]), 64)
        self.assertEqual(len(line["message_hashes"]), 2)
        self.assertEqual(line["roles"], ["system", "user"])
        self.assertEqual(line["litellm_model_group"], "model-default")
        self.assertEqual(line["litellm_attempted_fallbacks"], "0")
        self.assertEqual(line["litellm_config_checksum"], "abc123")
        self.assertEqual(line["litellm_image"], "ghcr.io/berriai/litellm:v1")
        self.assertIsInstance(line["latency_ms"], int)
        self.assertTrue(line["answer_hash"])
        self.assertEqual(line["unstorable"], [])
        self.assertTrue(line["stored"])

    def test_streaming_line_reads_usage_and_answer(self):
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=True))
        self.request("POST", "/v1/chat/completions", chat("t_22222222", stream=True))
        a, b = self.lines(2)
        self.assertEqual((a["prompt_tokens"], a["cached_tokens"], a["completion_tokens"]), (2000, 1500, 12))
        self.assertTrue(a["stream"])
        # Same request but for the ids: same fingerprint, and the answers name
        # each run's own card, so their normalized hashes match too.
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        self.assertEqual(a["answer_hash"], b["answer_hash"])

    def test_line_holds_no_request_text(self):
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.lines(1)
        log = self.out.getvalue()
        for text in (TOKEN, "t_11111111", "web-7d4b9c8f6d", "platform agent", "answer 1"):
            self.assertNotIn(text, log)

    def test_message_hashes_are_of_unscrubbed_content(self):
        # The scrubber blanks the rest of a line after a secret; the hashes
        # must still tell replicas=3 from replicas=7.
        a, b = chat("t_11111111", stream=False), chat("t_11111111", stream=False)
        a["messages"][-1]["content"] = "DB_PASSWORD=hunter2 replicas=3"
        b["messages"][-1]["content"] = "DB_PASSWORD=hunter2 replicas=7"
        self.request("POST", "/v1/chat/completions", a)
        self.request("POST", "/v1/chat/completions", b)
        first, second = self.lines(2)
        self.assertNotEqual(first["message_hashes"][-1], second["message_hashes"][-1])
        self.assertEqual(first["message_hashes"][0], second["message_hashes"][0])

    def test_message_hashes_are_prefix_stable(self):
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        extra = ({"role": "user", "content": "earlier turn t_99999999"},)
        self.request("POST", "/v1/chat/completions", chat("t_22222222", stream=False, extra_messages=extra))
        a, b = self.lines(2)
        self.assertEqual(a["message_hashes"][:1], b["message_hashes"][:1])
        self.assertEqual(len(b["message_hashes"]), 3)

    def test_fallback_answer_is_marked_unstorable(self):
        FakeLiteLLM.fallbacks = "1"
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        line = self.lines(1)[0]
        self.assertEqual(line["litellm_attempted_fallbacks"], "1")
        self.assertIn("fallback_attempted", line["unstorable"])

    def test_scrubbed_text_is_stored(self):
        self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        line = self.lines(1)[0]
        manifest = json.loads(Path(self.store, "requests", line["request_id"] + ".json").read_text())
        self.assertEqual(manifest["fingerprint"], line["fingerprint"])
        stored = "".join(Path(self.store, "blobs", h + ".json").read_text() for h in manifest["messages"])
        self.assertIn("t_11111111", stored)
        self.assertNotIn(TOKEN, stored)


class FailClosedProxyTest(ProxyTestCase):
    redactor_path = "/nonexistent/redactor.py"

    def test_no_text_is_stored_but_fingerprints_are_logged(self):
        status, _, _ = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.assertEqual(status, 200)
        line = self.lines(1)[0]
        self.assertFalse(line["stored"])
        self.assertEqual(line["store_skipped"], "scrubbers_unavailable")
        self.assertTrue(line["fingerprint"])
        self.assertEqual(os.listdir(self.store), [])
        self.assertIn("replay_shadow_scrubbers_loaded 0", self.request("GET", "/metrics")[2].decode())
        self.assertIn("scrubbers_unavailable", line["unstorable"])
        # Hashes do not depend on the scrubbers; only the stored text does.
        self.assertEqual(len(line["message_hashes"]), 2)
        self.assertTrue(line["answer_hash"])


class ConfigTest(unittest.TestCase):
    def test_from_env(self):
        config = replay_proxy.Config.from_env({
            "REPLAY_UPSTREAM_HOST": "up", "REPLAY_UPSTREAM_PORT": "4000", "REPLAY_LITELLM_CONFIG_CHECKSUM": "c",
            "REPLAY_LITELLM_IMAGE": "img", "REPLAY_STORE_DIR": "/s", "REPLAY_JOB_KIND": "nightly",
        })
        self.assertEqual((config.upstream_host, config.upstream_port), ("up", 4000))
        self.assertEqual((config.litellm_config_checksum, config.litellm_image, config.store_dir), ("c", "img", "/s"))
        self.assertEqual(config.job_kind, "nightly")
        self.assertGreaterEqual(replay_proxy.Config.from_env({}).upstream_timeout_seconds, 600)


if __name__ == "__main__":
    unittest.main()
