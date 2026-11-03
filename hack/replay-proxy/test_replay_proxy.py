"""The shadow proxy end to end against a fake LiteLLM: pass-through, the log line, /metrics, never replaying."""

import http.client
import io
import json
import os
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


class FakeLiteLLM(BaseHTTPRequestHandler):
    """Answers every call with a fresh answer numbered by call, streamed when asked."""

    protocol_version = "HTTP/1.1"
    calls = []
    fallbacks = "0"
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
        headers = {"x-litellm-model-group": body["model"], "x-litellm-attempted-fallbacks": self.fallbacks}
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
            self.wfile.write(data)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        for chunk in self.stream_chunks(number, task):
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
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

    def setUp(self):
        FakeLiteLLM.calls = []
        FakeLiteLLM.fallbacks = "0"
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeLiteLLM)
        threading.Thread(target=self.upstream.serve_forever, args=(POLL_SECONDS,), daemon=True).start()
        self.store = tempfile.mkdtemp()
        self.out = io.StringIO()
        config = replay_proxy.Config(
            listen_port=0, upstream_host="127.0.0.1", upstream_port=self.upstream.server_address[1],
            store_dir=self.store, redactor_path=self.redactor_path, transcript_rules_path=RULES,
            litellm_config_checksum="abc123", litellm_image="ghcr.io/berriai/litellm:v1",
            job_kind="presubmit", job_project="pool-1", job_build="42",
        )
        self.server = replay_proxy.make_server(config, host="127.0.0.1", out=self.out)
        self.proxy = self.server.proxy
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
        payload = resp.read()
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
            raise RuntimeError("bug")

        self.proxy.analyze = explode
        status, _, payload = self.request("POST", "/v1/chat/completions", chat("t_11111111", stream=False))
        self.assertEqual(status, 200)
        self.assertIn(b"answer 1", payload)
        line = self.lines(1)[0]
        self.assertIn("RuntimeError", line["error"])
        self.assertEqual(self.proxy.metrics.get("analysis_errors"), 1)


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
        self.assertEqual(line["job"], {"kind": "presubmit", "project": "pool-1", "build": "42"})
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
