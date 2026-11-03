"""Shadow-mode inference replay proxy: logs what replay would have keyed on, replays nothing.

Sits in front of LiteLLM (examples/inference-replay/eval-design.md, Rollout
step 1). Every request is forwarded upstream with its body unchanged and every
response is streamed back unchanged; no saved answer is ever served. For each
`/v1/chat/completions` request, once the response has gone back to the
client, the proxy:

- normalizes the request and computes its fingerprint (normalizer.py);
- writes one JSON line to stdout: the fingerprint, per-message hashes, token
  usage, model latency, a hash of the answer and LiteLLM's fallback headers.
  Nothing in the line is request text, but its hashes can be guessed
  against, so the log is private, like the stored text;
- scrubs the request text and keeps it under the store directory
  (request_store.py), or keeps nothing if the scrubbers cannot load.

`/metrics` is answered here and never forwarded: the proxy carries the
`app: litellm` label, and anything summing `/metrics` over those pods would
otherwise count one LiteLLM pod twice.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import normalizer
import request_store

COMPLETIONS_PATH = "/v1/chat/completions"
HEALTH_PATH = "/replay/healthz"
METRICS_PATH = "/metrics"
LOG_EVENT = "replay_shadow_request"
START_EVENT = "replay_shadow_start"
DEFAULT_BIND_HOST = "0.0.0.0"
BODY_METHODS = ("POST", "PUT", "PATCH")
HEAD_METHOD = "HEAD"
GET_METHOD = "GET"
POST_METHOD = "POST"
HOST_HEADER = "Host"
CONTENT_TYPE_HEADER = "Content-Type"
CONTENT_LENGTH_HEADER = "Content-Length"
TRANSFER_ENCODING_HEADER = "Transfer-Encoding"
# Why replay would not have saved an answer, in the line's `unstorable`.
UNSTORABLE_UPSTREAM_INCOMPLETE = "upstream_incomplete"
UNSTORABLE_CLIENT_DISCONNECTED = "client_disconnected"
LOG_SCHEMA_VERSION = 1

DEFAULT_LISTEN_PORT = 8080
DEFAULT_UPSTREAM_HOST = "inference-gateway-upstream"
DEFAULT_UPSTREAM_PORT = 80
# One model call in an agent turn can run for minutes; the prototype's 60 s
# cut real turns off. This bounds each socket read, not the whole response.
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 1800
DEFAULT_STORE_DIR = "/data/shadow"
DEFAULT_REDACTOR_PATH = "/app/scrubbers/redactor.py"
DEFAULT_TRANSCRIPT_RULES_PATH = "/app/scrubbers/worker_transcript.py"
UNSET = ""

# Request headers not forwarded: hop-by-hop ones, and Accept-Encoding so the
# upstream answers uncompressed and the usage block can be read.
DROPPED_REQUEST_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "keep-alive", "te", "trailer",
    "upgrade", "proxy-connection", "accept-encoding",
}
DROPPED_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection", "keep-alive", "te", "trailer", "upgrade"}
MODEL_GROUP_HEADER = "x-litellm-model-group"
ATTEMPTED_FALLBACKS_HEADER = "x-litellm-attempted-fallbacks"
NO_FALLBACKS = "0"

READ_CHUNK_BYTES = 65536
SSE_DATA_PREFIX = "data:"
SSE_DONE = "[DONE]"
JSON_CONTENT_TYPE = "application/json"
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4"
HTTP_OK = 200
HTTP_BAD_GATEWAY = 502
HTTP_METHOD_NOT_ALLOWED = 405
# Statuses that carry no body (RFC 9110): informational, 204 and 304.
HTTP_INFORMATIONAL_MIN = 100
HTTP_INFORMATIONAL_END = 200
HTTP_BODYLESS = (204, 304)
MS_PER_SECOND = 1000
CHUNKED = "chunked"
CRLF = b"\r\n"
HEX_BASE = 16
# What a broken upstream raises: a timeout or reset (OSError), or a response
# cut short (http.client.IncompleteRead and the other HTTPExceptions).
UPSTREAM_ERRORS = (OSError, http.client.HTTPException)
# Streamed fields concatenated across deltas.
CONCAT_KEYS = ("content", "reasoning_content", "arguments")


@dataclass(frozen=True)
class Config:
    listen_port: int = DEFAULT_LISTEN_PORT
    upstream_host: str = DEFAULT_UPSTREAM_HOST
    upstream_port: int = DEFAULT_UPSTREAM_PORT
    upstream_timeout_seconds: float = DEFAULT_UPSTREAM_TIMEOUT_SECONDS
    store_dir: str = DEFAULT_STORE_DIR
    redactor_path: str = DEFAULT_REDACTOR_PATH
    transcript_rules_path: str = DEFAULT_TRANSCRIPT_RULES_PATH
    # Set at deploy time from the LiteLLM Deployment's `checksum/config`
    # pod annotation and its container image.
    litellm_config_checksum: str = UNSET
    litellm_image: str = UNSET
    # Which job this proxy serves, stamped on every line for the analysis.
    job_kind: str = UNSET
    job_project: str = UNSET
    job_build: str = UNSET
    job_pr: str = UNSET

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "Config":
        return cls(
            listen_port=int(env.get("REPLAY_LISTEN_PORT", DEFAULT_LISTEN_PORT)),
            upstream_host=env.get("REPLAY_UPSTREAM_HOST", DEFAULT_UPSTREAM_HOST),
            upstream_port=int(env.get("REPLAY_UPSTREAM_PORT", DEFAULT_UPSTREAM_PORT)),
            upstream_timeout_seconds=float(env.get("REPLAY_UPSTREAM_TIMEOUT_SECONDS", DEFAULT_UPSTREAM_TIMEOUT_SECONDS)),
            store_dir=env.get("REPLAY_STORE_DIR", DEFAULT_STORE_DIR),
            redactor_path=env.get("REPLAY_REDACTOR_PATH", DEFAULT_REDACTOR_PATH),
            transcript_rules_path=env.get("REPLAY_TRANSCRIPT_RULES_PATH", DEFAULT_TRANSCRIPT_RULES_PATH),
            litellm_config_checksum=env.get("REPLAY_LITELLM_CONFIG_CHECKSUM", UNSET),
            litellm_image=env.get("REPLAY_LITELLM_IMAGE", UNSET),
            job_kind=env.get("REPLAY_JOB_KIND", UNSET),
            job_project=env.get("REPLAY_JOB_PROJECT", UNSET),
            job_build=env.get("REPLAY_JOB_BUILD", UNSET),
            job_pr=env.get("REPLAY_JOB_PR", UNSET),
        )


class Metrics:
    """The proxy's own counters, served on /metrics."""

    NAMES = {
        "requests": "replay_shadow_requests_total",
        "completions": "replay_shadow_completions_total",
        "upstream_errors": "replay_shadow_upstream_errors_total",
        "analysis_errors": "replay_shadow_analysis_errors_total",
        "stored": "replay_shadow_requests_stored_total",
        "store_skipped": "replay_shadow_requests_store_skipped_total",
        "prompt_tokens": "replay_shadow_prompt_tokens_total",
    }

    def __init__(self, scrubbers_loaded: bool):
        self._lock = threading.Lock()
        self._values = {name: 0 for name in self.NAMES}
        self.scrubbers_loaded = scrubbers_loaded

    def add(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._values[name]

    def render(self) -> str:
        with self._lock:
            lines = []
            for name, metric in self.NAMES.items():
                lines.append(f"# TYPE {metric} counter")
                lines.append(f"{metric} {self._values[name]}")
        lines.append("# TYPE replay_shadow_scrubbers_loaded gauge")
        lines.append(f"replay_shadow_scrubbers_loaded {1 if self.scrubbers_loaded else 0}")
        return "\n".join(lines) + "\n"


def merge_delta(acc: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """Fold one streamed delta into the assembled message."""
    for key, value in delta.items():
        if value is None:
            continue
        if key in CONCAT_KEYS and isinstance(value, str):
            acc[key] = (acc.get(key) or "") + value
        elif key == "tool_calls" and isinstance(value, list):
            calls = acc.setdefault("tool_calls", [])
            for call in value:
                index = call.get("index", len(calls)) if isinstance(call, dict) else len(calls)
                while len(calls) <= index:
                    calls.append({})
                if isinstance(call, dict):
                    merge_delta(calls[index], call)
        elif isinstance(value, dict):
            merge_delta(acc.setdefault(key, {}), value)
        elif key == "name" and isinstance(value, str) and acc.get(key) and acc[key] != value:
            acc[key] = acc[key] + value
        else:
            acc[key] = value
    return acc


class ResponseObserver:
    """Reads the answer and usage out of response bytes as they stream past."""

    def __init__(self, streaming: bool):
        self.streaming = streaming
        self.buffer = b""
        self.body = bytearray()
        self.message: Dict[str, Any] = {}
        self.usage: Optional[Dict[str, Any]] = None

    def feed(self, data: bytes) -> None:
        if not self.streaming:
            self.body.extend(data)
            return
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self._line(line)

    def _line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith(SSE_DATA_PREFIX):
            return
        payload = line[len(SSE_DATA_PREFIX):].strip()
        if payload == SSE_DONE:
            return
        try:
            chunk = json.loads(payload)
        except ValueError:
            return
        if not isinstance(chunk, dict):
            return
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict):
                merge_delta(self.message, choice.get("delta") or {})

    def finish(self) -> None:
        if self.streaming:
            if self.buffer:
                self._line(self.buffer)
                self.buffer = b""
            return
        try:
            out = json.loads(bytes(self.body))
        except ValueError:
            return
        if not isinstance(out, dict):
            return
        self.usage = out.get("usage")
        choices = out.get("choices") or []
        if choices and isinstance(choices[0], dict):
            self.message = choices[0].get("message") or {}


def usage_tokens(usage: Optional[Dict[str, Any]]) -> Dict[str, Optional[int]]:
    usage = usage or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = usage.get("cache_read_input_tokens")
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": cached,
        "completion_tokens": usage.get("completion_tokens"),
    }


def answer_view(message: Dict[str, Any]) -> Dict[str, Any]:
    """What the answer says: its text and tool calls, without ids or signatures."""
    calls = []
    for call in message.get("tool_calls") or []:
        function = (call.get("function") or {}) if isinstance(call, dict) else {}
        calls.append({"name": function.get("name"), "arguments": function.get("arguments")})
    return {"content": message.get("content"), "tool_calls": calls}


def answer_hash(message: Dict[str, Any], mapping: normalizer.Mapping) -> Optional[str]:
    """Hash of the answer with this request's identifiers swapped for placeholders."""
    if not message:
        return None
    view = answer_view(message)
    if view["content"] is not None:
        view["content"] = mapping.apply_to_answer(str(view["content"]))
    for call in view["tool_calls"]:
        if call["arguments"] is not None:
            call["arguments"] = mapping.apply_to_answer(str(call["arguments"]))
    return normalizer.stable_hash(view)


def unstorable_reasons(
    body: Dict[str, Any], status: int, message: Dict[str, Any], headers: Dict[str, str],
    mapping: normalizer.Mapping, scrubber: request_store.Scrubber,
    upstream_incomplete: bool = False, client_disconnected: bool = False,
) -> List[str]:
    """Why replay would not have saved this answer (eval-design.md); empty when it would."""
    reasons = []
    if upstream_incomplete:
        reasons.append(UNSTORABLE_UPSTREAM_INCOMPLETE)
        return reasons
    if client_disconnected:
        reasons.append(UNSTORABLE_CLIENT_DISCONNECTED)
        return reasons
    if status != HTTP_OK or not (message.get("content") or message.get("tool_calls")):
        reasons.append("no_answer")
        return reasons
    group = headers.get(MODEL_GROUP_HEADER)
    if group is None:
        reasons.append("no_model_group_header")
    elif group != body.get("model"):
        reasons.append("answered_by_other_model_group")
    if headers.get(ATTEMPTED_FALLBACKS_HEADER, NO_FALLBACKS) != NO_FALLBACKS:
        reasons.append("fallback_attempted")
    view = answer_view(message)
    texts = [str(view["content"] or "")] + [str(c["arguments"] or "") for c in view["tool_calls"]]
    if any(mapping.answer_repeats_bare_number(t) for t in texts):
        reasons.append("repeats_swapped_number")
    try:
        if scrubber.answer_changes(message):
            reasons.append("scrub_changes_answer" if scrubber.available else "scrubbers_unavailable")
    except Exception:
        reasons.append("scrub_failed")
    return reasons


class ShadowProxy:
    """Shared state for the handlers: configuration, scrubbers, store, metrics."""

    def __init__(self, config: Config, out=None):
        self.config = config
        self.key_context = normalizer.KeyContext(config.litellm_config_checksum, config.litellm_image)
        self.scrubber = request_store.Scrubber(config.redactor_path, config.transcript_rules_path)
        self.store = request_store.RequestStore(config.store_dir, self.scrubber)
        self.metrics = Metrics(self.scrubber.available)
        self.out = out if out is not None else sys.stdout
        self._log_lock = threading.Lock()

    def log(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        with self._log_lock:
            self.out.write(line + "\n")
            self.out.flush()

    def job(self) -> Dict[str, str]:
        return {
            "kind": self.config.job_kind,
            "project": self.config.job_project,
            "build": self.config.job_build,
            "pr": self.config.job_pr,
        }

    def startup_line(self) -> Dict[str, Any]:
        return {
            "event": START_EVENT,
            "schema": LOG_SCHEMA_VERSION,
            "job": self.job(),
            "normalizer_version": normalizer.NORMALIZER_VERSION,
            "litellm_config_checksum": self.config.litellm_config_checksum,
            "litellm_image": self.config.litellm_image,
            "scrubbers_loaded": self.scrubber.available,
            "scrubbers_error": self.scrubber.error,
        }

    def analyze(self, body: Dict[str, Any], received: float, upstream: Dict[str, Any]) -> Dict[str, Any]:
        """The log line for one completed chat request; stores its scrubbed text as a side effect."""
        request_id = uuid.uuid4().hex
        entry: Dict[str, Any] = {
            "event": LOG_EVENT,
            "schema": LOG_SCHEMA_VERSION,
            "request_id": request_id,
            "ts": received,
            "job": self.job(),
            "model": body.get("model"),
            "stream": bool(body.get("stream")),
            "normalizer_version": self.key_context.normalizer_version,
            "litellm_config_checksum": self.key_context.litellm_config_checksum,
            "litellm_image": self.key_context.litellm_image,
            "status": upstream["status"],
            "latency_ms": upstream["latency_ms"],
            "client_disconnected": upstream["client_disconnected"],
            "upstream_incomplete": upstream["upstream_incomplete"],
            "litellm_model_group": upstream["headers"].get(MODEL_GROUP_HEADER),
            "litellm_attempted_fallbacks": upstream["headers"].get(ATTEMPTED_FALLBACKS_HEADER),
        }
        entry.update(usage_tokens(upstream["usage"]))
        normalized = normalizer.normalize_request(body, received)
        fingerprint = normalizer.fingerprint(normalized, self.key_context)
        entry.update(
            fingerprint=fingerprint,
            settings_hash=normalizer.settings_hash(normalized, self.key_context),
            roles=normalized.roles(),
            unstorable=unstorable_reasons(
                body, upstream["status"], upstream["message"], upstream["headers"], normalized.mapping, self.scrubber,
                upstream_incomplete=upstream["upstream_incomplete"],
                client_disconnected=upstream["client_disconnected"],
            ),
        )
        # The per-message and answer hashes are of the normalized, unscrubbed
        # content, as the fingerprint is: the scrubber blanks the rest of a
        # line after a secret, so hashes of scrubbed text would hide real
        # state. That makes this log as private as the stored text: a hash of
        # request content can be guessed against, so both go to the private,
        # eval-only bucket, never to Prow artifacts.
        entry["message_hashes"] = normalized.message_hashes
        entry["answer_hash"] = answer_hash(upstream["message"], normalized.mapping)
        skipped = self.store.store(request_id, fingerprint, body)
        entry["stored"] = skipped is None
        entry["store_skipped"] = skipped
        self.metrics.add("stored" if skipped is None else "store_skipped")
        if entry["prompt_tokens"]:
            self.metrics.add("prompt_tokens", int(entry["prompt_tokens"]))
        return entry


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy: ShadowProxy  # set on the subclass make_server builds

    def log_message(self, *args: Any) -> None:  # the JSON lines are the log
        pass

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _read_body(self) -> bytes:
        if CHUNKED in (self.headers.get(TRANSFER_ENCODING_HEADER) or "").lower():
            data = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", HEX_BASE)
                if size == 0:
                    while self.rfile.readline() not in (CRLF, b"\n", b""):
                        pass
                    return bytes(data)
                data.extend(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get(CONTENT_LENGTH_HEADER) or 0)
        return self.rfile.read(length) if length else b""

    def _send_local(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header(CONTENT_TYPE_HEADER, content_type)
        self.send_header(CONTENT_LENGTH_HEADER, str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _local(self) -> bool:
        """Answer /metrics and the health check here; True when handled."""
        path = self._path()
        if path == METRICS_PATH:
            if self.command != GET_METHOD:
                self._send_local(HTTP_METHOD_NOT_ALLOWED, b"", METRICS_CONTENT_TYPE)
            else:
                self._send_local(HTTP_OK, self.proxy.metrics.render().encode(), METRICS_CONTENT_TYPE)
            return True
        if path == HEALTH_PATH:
            payload = json.dumps({"ok": True, "scrubbers_loaded": self.proxy.scrubber.available}).encode()
            self._send_local(HTTP_OK, payload, JSON_CONTENT_TYPE)
            return True
        return False

    def _handle(self) -> None:
        raw = self._read_body()
        if self._local():
            return
        self.proxy.metrics.add("requests")
        body = None
        if self.command == POST_METHOD and self._path() == COMPLETIONS_PATH:
            try:
                parsed = json.loads(raw)
                body = parsed if isinstance(parsed, dict) else None
            except ValueError:
                body = None
        received = time.time()
        upstream = self._forward(raw, observe=body is not None, streaming=bool(body and body.get("stream")))
        if body is None:
            return
        self.proxy.metrics.add("completions")
        try:
            entry = self.proxy.analyze(body, received, upstream)
        except Exception as exc:  # the analysis must never cost the agent its answer
            self.proxy.metrics.add("analysis_errors")
            # The type name only: an exception's message can quote request text.
            entry = {"event": LOG_EVENT, "schema": LOG_SCHEMA_VERSION, "ts": received, "job": self.proxy.job(),
                     "status": upstream["status"], "error": type(exc).__name__}
        self.proxy.log(entry)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    def _forward(self, raw: bytes, observe: bool, streaming: bool) -> Dict[str, Any]:
        """Send the request upstream and copy the response back byte for byte."""
        config = self.proxy.config
        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROPPED_REQUEST_HEADERS}
        headers[HOST_HEADER] = f"{config.upstream_host}:{config.upstream_port}"
        result: Dict[str, Any] = {"status": HTTP_BAD_GATEWAY, "headers": {}, "usage": None, "message": {},
                                  "latency_ms": None, "client_disconnected": False, "upstream_incomplete": False}
        started = time.monotonic()
        conn = http.client.HTTPConnection(config.upstream_host, config.upstream_port,
                                          timeout=config.upstream_timeout_seconds)
        try:
            conn.request(self.command, self.path, body=raw if raw or self.command in BODY_METHODS else None,
                         headers=headers)
            resp = conn.getresponse()
        except UPSTREAM_ERRORS as exc:
            conn.close()
            self.proxy.metrics.add("upstream_errors")
            message = json.dumps({"error": f"replay proxy: upstream unreachable: {type(exc).__name__}"}).encode()
            try:
                self._send_local(HTTP_BAD_GATEWAY, message, JSON_CONTENT_TYPE)
            except OSError:
                result["client_disconnected"] = True
            return result
        result["status"] = resp.status
        result["headers"] = {k.lower(): v for k, v in resp.getheaders()}
        observer = ResponseObserver(streaming and resp.status == HTTP_OK) if observe else None
        length = resp.getheader(CONTENT_LENGTH_HEADER)
        has_body = (
            self.command != HEAD_METHOD
            and resp.status not in HTTP_BODYLESS
            and not HTTP_INFORMATIONAL_MIN <= resp.status < HTTP_INFORMATIONAL_END
        )
        chunked = has_body and length is None
        try:
            if not self._send_upstream_headers(resp, chunked, length):
                result["client_disconnected"] = True
                return result
            while has_body:
                try:
                    data = resp.read1(READ_CHUNK_BYTES)
                except UPSTREAM_ERRORS:
                    # The upstream stalled past the timeout, reset, or closed a
                    # chunked stream early. The client's response is left
                    # unterminated so it cannot read as complete.
                    result["upstream_incomplete"] = True
                    self.proxy.metrics.add("upstream_errors")
                    self.close_connection = True
                    break
                if not data:
                    if not chunked and length is not None and resp.length:
                        # The upstream closed before the Content-Length it
                        # promised. Closing the client's connection is what
                        # tells it the body is short; otherwise it waits.
                        result["upstream_incomplete"] = True
                        self.proxy.metrics.add("upstream_errors")
                        self.close_connection = True
                    break
                if observer is not None:
                    observer.feed(data)
                if not self._send_client(chunked, data):
                    result["client_disconnected"] = True
                    break
            if chunked and not result["upstream_incomplete"] and not result["client_disconnected"]:
                if not self._send_client(chunked, b""):
                    result["client_disconnected"] = True
        finally:
            # Closed rather than drained when the client has gone: nobody reads
            # the rest, and the model may still be generating it.
            conn.close()
        result["latency_ms"] = int((time.monotonic() - started) * MS_PER_SECOND)
        if observer is not None and resp.status == HTTP_OK:
            observer.finish()
            result["usage"] = observer.usage
            result["message"] = observer.message if isinstance(observer.message, dict) else {}
        return result

    def _send_upstream_headers(self, resp: http.client.HTTPResponse, chunked: bool, length: Optional[str]) -> bool:
        """Copy the upstream status and headers to the client; False when the client has gone."""
        try:
            self.send_response_only(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in DROPPED_RESPONSE_HEADERS:
                    self.send_header(key, value)
            if chunked:
                self.send_header(TRANSFER_ENCODING_HEADER, CHUNKED)
            elif length is not None:
                self.send_header(CONTENT_LENGTH_HEADER, length)
            self.end_headers()
            return True
        except OSError:
            self.close_connection = True
            return False

    def _send_client(self, chunked: bool, data: bytes) -> bool:
        """Write body bytes to the client (an empty chunk ends a chunked body); False when it has gone."""
        try:
            if chunked:
                self.wfile.write(f"{len(data):x}".encode() + CRLF + data + CRLF)
            else:
                self.wfile.write(data)
            self.wfile.flush()
            return True
        except OSError:
            self.close_connection = True
            return False


def make_server(config: Config, host: str = DEFAULT_BIND_HOST, out=None) -> ThreadingHTTPServer:
    proxy = ShadowProxy(config, out=out)
    handler = type("BoundHandler", (Handler,), {"proxy": proxy})
    server = ThreadingHTTPServer((host, config.listen_port), handler)
    server.daemon_threads = True
    server.proxy = proxy  # type: ignore[attr-defined]
    return server


def main() -> None:
    config = Config.from_env(dict(os.environ))
    server = make_server(config)
    proxy = server.proxy  # type: ignore[attr-defined]
    proxy.log(proxy.startup_line())
    server.serve_forever()


if __name__ == "__main__":
    main()
