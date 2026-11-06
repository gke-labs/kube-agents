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
- adds `diag_message_hashes` and `diag_settings_hash` (diagnosis.py), used
  only to find the closest earlier request for a miss;
- scrubs the request text and keeps it under the store directory
  (request_store.py), or keeps nothing if the scrubbers cannot load.

With `REPLAY_LOG_DIR` set, every line also goes to closed segment files there
(spool.py), which is what a CI job copies out while it runs; with
`REPLAY_DATA_MAX_BYTES` set, the store and those files stop growing at that
size.

All of that runs on one background thread fed by a bounded queue
(ANALYSIS_QUEUE_SIZE); the handler only enqueues, and drops the analysis (an
`analysis_dropped` line) rather than wait when the queue is full. Memory is
bounded: a request body over MAX_REQUEST_BYTES gets a 413, and the proxy stops
observing a response after MAX_OBSERVED_BYTES (its bytes still flow).

`/metrics` is answered here and never forwarded: the proxy carries the
`app: litellm` label, and anything summing `/metrics` over those pods would
otherwise count one LiteLLM pod twice.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

import diagnosis
import normalizer
import request_store
import schema as F
import spool

COMPLETIONS_PATH = "/v1/chat/completions"
HEALTH_PATH = "/replay/healthz"
METRICS_PATH = "/metrics"
HTTP_BAD_REQUEST = 400
HTTP_PAYLOAD_TOO_LARGE = 413
# Memory bounds. A request body above MAX_REQUEST_BYTES is refused with 413
# rather than read into memory. The observer stops reading a response (the
# bytes still flow) once it has seen MAX_OBSERVED_BYTES of it, which bounds the
# buffered SSE text, the merged answer and a non-streamed body together. A
# tool_calls index must be below MAX_TOOL_CALLS, so a hostile or broken index
# cannot make the merge allocate.
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_OBSERVED_BYTES = 8 * 1024 * 1024
MAX_TOOL_CALLS = 256
# Requests waiting for the background analysis; past this, analysis is dropped.
ANALYSIS_QUEUE_SIZE = 1024
# A rough prompt-token estimate for a line whose response carried no usage.
BYTES_PER_TOKEN = 4
DEFAULT_BIND_HOST = "0.0.0.0"
BODY_METHODS = ("POST", "PUT", "PATCH")
HEAD_METHOD = "HEAD"
GET_METHOD = "GET"
POST_METHOD = "POST"
HOST_HEADER = "Host"
CONTENT_TYPE_HEADER = "Content-Type"
CONTENT_LENGTH_HEADER = "Content-Length"
TRANSFER_ENCODING_HEADER = "Transfer-Encoding"

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
# A segment is closed after this long open, so a collector copying closed
# segments is never more than this far behind the proxy.
DEFAULT_LOG_SEGMENT_SECONDS = 60.0
# 0: no budget.
DEFAULT_DATA_MAX_BYTES = 0
# How often the budget re-measures the data volume, and how often the open
# segment is checked for its age.
DATA_BUDGET_INTERVAL_SECONDS = 30.0
SEGMENT_ROLL_CHECK_SECONDS = 1.0
# How long a TERM'd proxy spends logging what is queued before it closes the
# open segment and exits. The chart gives the pod 90 s of grace of which the
# preStop hook spends 75 (templates/litellm.yaml), so this has to fit in the
# remaining 15 with room for the interpreter to exit.
SHUTDOWN_CLOSE_SECONDS = 10.0

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
SSE_LINE_END = b"\n"
SSE_CR = "\r"
SSE_FIELD_SPACE = " "
SSE_DATA_JOIN = "\n"
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
    # Where each line is also written, in closed segments (spool.py); unset
    # writes stdout only.
    log_dir: str = UNSET
    log_segment_seconds: float = DEFAULT_LOG_SEGMENT_SECONDS
    # The most the store and the segments may hold together; 0 is no limit.
    data_max_bytes: int = DEFAULT_DATA_MAX_BYTES

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
            log_dir=env.get("REPLAY_LOG_DIR", UNSET),
            log_segment_seconds=float(env.get("REPLAY_LOG_SEGMENT_SECONDS", DEFAULT_LOG_SEGMENT_SECONDS)),
            data_max_bytes=int(env.get("REPLAY_DATA_MAX_BYTES", DEFAULT_DATA_MAX_BYTES)),
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
        "analysis_dropped": "replay_shadow_analysis_dropped_total",
        "log_file_errors": "replay_shadow_log_file_errors_total",
        "log_file_dropped": "replay_shadow_log_file_dropped_total",
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


class ObserveTooLarge(Exception):
    """The observer has seen more of a response than it keeps."""


class RequestTooLarge(Exception):
    """A request body is larger than the proxy reads into memory."""


def tool_call_index(call: Any, default: int) -> int:
    """A streamed tool call's index: an int (not a bool) in [0, MAX_TOOL_CALLS), else ValueError."""
    if not isinstance(call, dict):
        raise TypeError("tool call delta is not an object")
    index = call.get("index", default)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < MAX_TOOL_CALLS:
        raise ValueError("tool call index out of range")
    return index


def merge_delta(acc: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """Fold one streamed delta into the assembled message; raises on a malformed one."""
    if not isinstance(delta, dict):
        raise TypeError("delta is not an object")
    for key, value in delta.items():
        if value is None:
            continue
        if key in CONCAT_KEYS and isinstance(value, str):
            acc[key] = (acc.get(key) or "") + value
        elif key == "tool_calls" and isinstance(value, list):
            calls = acc.setdefault("tool_calls", [])
            for call in value:
                index = tool_call_index(call, len(calls))
                while len(calls) <= index:
                    calls.append({})
                merge_delta(calls[index], call)
        elif isinstance(value, dict):
            merge_delta(acc.setdefault(key, {}), value)
        elif key == "name" and isinstance(value, str) and acc.get(key) and acc[key] != value:
            acc[key] = acc[key] + value
        else:
            acc[key] = value
    return acc


class ResponseObserver:
    """Reads the answer and usage out of response bytes as they stream past.

    Raises on anything it cannot read (the caller then stops observing and
    marks the line), and ObserveTooLarge past MAX_OBSERVED_BYTES.
    """

    def __init__(self, streaming: bool):
        self.streaming = streaming
        self.seen = 0
        self.buffer = b""
        self.data_lines: List[str] = []
        self.body = bytearray()
        self.message: Dict[str, Any] = {}
        self.usage: Optional[Dict[str, Any]] = None

    def feed(self, data: bytes) -> None:
        self.seen += len(data)
        if self.seen > MAX_OBSERVED_BYTES:
            raise ObserveTooLarge()
        if not self.streaming:
            self.body.extend(data)
            return
        self.buffer += data
        while SSE_LINE_END in self.buffer:
            line, self.buffer = self.buffer.split(SSE_LINE_END, 1)
            self._line(line.decode("utf-8", "replace").rstrip(SSE_CR))

    def _line(self, line: str) -> None:
        """One SSE line: a blank line ends an event; `data:` lines of an event are joined."""
        if not line:
            self._dispatch()
        elif line.startswith(SSE_DATA_PREFIX):
            value = line[len(SSE_DATA_PREFIX):]
            self.data_lines.append(value[1:] if value.startswith(SSE_FIELD_SPACE) else value)

    def _dispatch(self) -> None:
        if not self.data_lines:
            return
        payload = SSE_DATA_JOIN.join(self.data_lines)
        self.data_lines = []
        if payload.strip() == SSE_DONE:
            return
        chunk = json.loads(payload)  # ValueError (or RecursionError) on a payload that is not JSON
        if not isinstance(chunk, dict):
            raise TypeError("event is not an object")
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not isinstance(choices, list):
            raise TypeError("choices is not a list")
        for choice in choices:
            if isinstance(choice, dict):
                merge_delta(self.message, choice.get("delta") or {})

    def finish(self) -> None:
        if self.streaming:
            if self.buffer:
                self._line(self.buffer.decode("utf-8", "replace").rstrip(SSE_CR))
                self.buffer = b""
            self._dispatch()
            return
        out = json.loads(bytes(self.body))  # ValueError on a 200 body that is not JSON
        if not isinstance(out, dict):
            raise TypeError("response is not an object")
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
        F.PROMPT_TOKENS: usage.get(F.PROMPT_TOKENS),
        F.CACHED_TOKENS: cached,
        F.COMPLETION_TOKENS: usage.get(F.COMPLETION_TOKENS),
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
    upstream_incomplete: bool = False, client_disconnected: bool = False, observe_failed: bool = False,
    observe_too_large: bool = False,
) -> List[str]:
    """Why replay would not have saved this answer (eval-design.md); empty when it would."""
    for flag, reason in (
        (upstream_incomplete, F.UNSTORABLE_UPSTREAM_INCOMPLETE),
        (client_disconnected, F.UNSTORABLE_CLIENT_DISCONNECTED),
        (observe_too_large, F.UNSTORABLE_OBSERVE_TOO_LARGE),
        (observe_failed, F.UNSTORABLE_OBSERVE_FAILED),
    ):
        if flag:
            return [reason]
    if status != HTTP_OK or not (message.get("content") or message.get("tool_calls")):
        return [F.UNSTORABLE_NO_ANSWER]
    reasons = []
    group = headers.get(MODEL_GROUP_HEADER)
    if group is None:
        reasons.append(F.UNSTORABLE_NO_MODEL_GROUP)
    elif group != body.get(F.MODEL):
        reasons.append(F.UNSTORABLE_OTHER_MODEL_GROUP)
    if headers.get(ATTEMPTED_FALLBACKS_HEADER, NO_FALLBACKS) != NO_FALLBACKS:
        reasons.append(F.UNSTORABLE_FALLBACK)
    view = answer_view(message)
    texts = [str(view["content"] or "")] + [str(c["arguments"] or "") for c in view["tool_calls"]]
    if any(mapping.answer_repeats_bare_number(t) for t in texts):
        reasons.append(F.UNSTORABLE_REPEATS_NUMBER)
    if any(mapping.answer_repeats_date(t) for t in texts):
        reasons.append(F.UNSTORABLE_REPEATS_DATE)
    if not scrubber.available:
        reasons.append(F.UNSTORABLE_SCRUBBERS_UNAVAILABLE)
        return reasons
    try:
        if scrubber.answer_masks_credential(message):
            reasons.append(F.UNSTORABLE_SCRUB_MASKS_CREDENTIAL)
    except Exception:
        reasons.append(F.UNSTORABLE_SCRUB_FAILED)
    return reasons


# Tells the analysis worker to stop.
_STOP = object()


class ShadowProxy:
    """Shared state for the handlers: configuration, scrubbers, store, metrics."""

    def __init__(self, config: Config, out=None):
        self.config = config
        self.key_context = normalizer.KeyContext(config.litellm_config_checksum, config.litellm_image)
        self.scrubber = request_store.Scrubber(config.redactor_path, config.transcript_rules_path)
        self.budget: Optional[spool.DataBudget] = None
        if config.data_max_bytes > 0:
            self.budget = spool.DataBudget(
                [config.store_dir, config.log_dir], config.data_max_bytes, DATA_BUDGET_INTERVAL_SECONDS
            )
        self.store = request_store.RequestStore(config.store_dir, self.scrubber, self.budget)
        self.metrics = Metrics(self.scrubber.available)
        self.segments: Optional[spool.SegmentLog] = None
        self.segments_error: Optional[str] = None
        if config.log_dir:
            try:
                self.segments = spool.SegmentLog(
                    config.log_dir, spool.segment_prefix(), config.log_segment_seconds, self.budget
                )
            except OSError as exc:  # stdout still carries every line
                self.segments_error = type(exc).__name__
        self._stopping = threading.Event()
        # Set, under the log lock, once the open segment is closed for good:
        # a line logged after that goes to stdout only, so a worker still busy
        # past close()'s deadline cannot open a segment nobody will close.
        self._segments_closed = False
        self._roller: Optional[threading.Thread] = None
        if self.segments is not None:
            self._roller = threading.Thread(target=self._roll, name="replay-shadow-segments", daemon=True)
            self._roller.start()
        self.diagnosis = diagnosis.DiagnosisHasher()
        self.out = out if out is not None else sys.stdout
        self._log_lock = threading.Lock()
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=ANALYSIS_QUEUE_SIZE)
        self._worker = threading.Thread(target=self._work, name="replay-shadow-analysis", daemon=True)
        self._worker.start()

    def submit(self, body: Dict[str, Any], received: float, upstream: Dict[str, Any], request_bytes: int) -> None:
        """Queue one request's analysis; never waits. A full queue drops it and logs that."""
        try:
            self._queue.put_nowait((body, received, upstream, request_bytes))
        except queue.Full:
            self.metrics.add("analysis_dropped")
            self.log({F.EVENT: F.REQUEST_EVENT, F.SCHEMA: F.LOG_SCHEMA_VERSION, F.TS: received,
                      F.JOB: self.job(), F.STATUS: upstream["status"], F.ERROR: F.ERROR_ANALYSIS_DROPPED})

    def drain(self) -> None:
        """Wait until every queued analysis has been logged (tests, shutdown)."""
        self._queue.join()

    def close(self, timeout: Optional[float] = None) -> None:
        """Log what is queued, stop the worker, and close the open segment.

        With `timeout`, gives the queued analyses at most that long; what is
        still queued then is not logged, and the segment is closed anyway.
        """
        deadline = None if timeout is None else time.monotonic() + timeout

        def left() -> Optional[float]:
            return None if deadline is None else max(0.0, deadline - time.monotonic())

        try:
            self._queue.put(_STOP, timeout=left())
        except queue.Full:
            pass
        self._worker.join(left())
        self._stopping.set()
        if self._roller is not None:
            self._roller.join(left())
        with self._log_lock:
            self._segments_closed = True
            if self.segments is not None:
                try:
                    self.segments.close()
                except OSError:
                    self.metrics.add("log_file_errors")
        if self.budget is not None:
            self.budget.close()

    def _roll(self) -> None:
        while not self._stopping.wait(SEGMENT_ROLL_CHECK_SECONDS):
            try:
                self.segments.roll_if_due()  # type: ignore[union-attr]
            except OSError:
                self.metrics.add("log_file_errors")

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                self._queue.task_done()
                return
            body, received, upstream, request_bytes = item
            try:
                try:
                    entry = self.analyze(body, received, upstream, request_bytes)
                except Exception as exc:  # one bad request never stops the worker
                    self.metrics.add("analysis_errors")
                    # The type name only: an exception's message can quote request text.
                    entry = {F.EVENT: F.REQUEST_EVENT, F.SCHEMA: F.LOG_SCHEMA_VERSION, F.TS: received,
                             F.JOB: self.job(), F.STATUS: upstream["status"], F.ERROR: type(exc).__name__}
                self.log(entry)
            except Exception:  # a failed write to stdout loses this line, not the worker
                self.metrics.add("analysis_errors")
            finally:
                self._queue.task_done()

    def log(self, entry: Dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        with self._log_lock:
            self.out.write(line + "\n")
            self.out.flush()
            if self.segments is not None and not self._segments_closed:
                # The segment is a copy for the collector; failing to write it
                # never costs the stdout line or the worker.
                try:
                    if not self.segments.write(line):
                        self.metrics.add("log_file_dropped")
                except OSError:
                    self.metrics.add("log_file_errors")

    def job(self) -> Dict[str, str]:
        return {
            F.JOB_KIND: self.config.job_kind,
            F.JOB_PROJECT: self.config.job_project,
            F.JOB_BUILD: self.config.job_build,
            F.JOB_PR: self.config.job_pr,
        }

    def startup_line(self) -> Dict[str, Any]:
        return {
            F.EVENT: F.START_EVENT,
            F.SCHEMA: F.LOG_SCHEMA_VERSION,
            F.JOB: self.job(),
            F.NORMALIZER_VERSION: normalizer.NORMALIZER_VERSION,
            F.LITELLM_CONFIG_CHECKSUM: self.config.litellm_config_checksum,
            F.LITELLM_IMAGE: self.config.litellm_image,
            F.SCRUBBERS_LOADED: self.scrubber.available,
            F.SCRUBBERS_ERROR: self.scrubber.error,
            F.LOG_FILE: self.segments is not None,
            F.LOG_FILE_ERROR: self.segments_error,
            F.LOG_SEGMENTS_RECOVERED: self.segments.recovered if self.segments is not None else 0,
            F.DATA_MAX_BYTES: self.config.data_max_bytes,
        }

    def analyze(
        self, body: Dict[str, Any], received: float, upstream: Dict[str, Any], request_bytes: int = 0
    ) -> Dict[str, Any]:
        """The log line for one completed chat request; stores its scrubbed text as a side effect."""
        request_id = uuid.uuid4().hex
        entry: Dict[str, Any] = {
            F.EVENT: F.REQUEST_EVENT,
            F.SCHEMA: F.LOG_SCHEMA_VERSION,
            F.REQUEST_ID: request_id,
            F.TS: received,
            F.JOB: self.job(),
            F.MODEL: body.get(F.MODEL),
            F.STREAM: bool(body.get(F.STREAM)),
            F.NORMALIZER_VERSION: self.key_context.normalizer_version,
            F.LITELLM_CONFIG_CHECKSUM: self.key_context.litellm_config_checksum,
            F.LITELLM_IMAGE: self.key_context.litellm_image,
            F.STATUS: upstream["status"],
            F.LATENCY_MS: upstream["latency_ms"],
            F.CLIENT_DISCONNECTED: upstream["client_disconnected"],
            F.UPSTREAM_INCOMPLETE: upstream["upstream_incomplete"],
            F.OBSERVE_FAILED: upstream["observe_failed"],
            F.LITELLM_MODEL_GROUP: upstream["headers"].get(MODEL_GROUP_HEADER),
            F.LITELLM_ATTEMPTED_FALLBACKS: upstream["headers"].get(ATTEMPTED_FALLBACKS_HEADER),
        }
        entry.update(usage_tokens(upstream["usage"]))
        entry[F.TOKENS_ESTIMATED] = entry[F.PROMPT_TOKENS] is None
        if entry[F.TOKENS_ESTIMATED]:
            entry[F.PROMPT_TOKENS] = request_bytes // BYTES_PER_TOKEN
        normalized = normalizer.normalize_request(body, received)
        fingerprint = normalizer.fingerprint(normalized, self.key_context)
        entry[F.FINGERPRINT] = fingerprint
        entry[F.SETTINGS_HASH] = normalizer.settings_hash(normalized, self.key_context)
        entry[F.ROLES] = normalized.roles()
        entry[F.UNSTORABLE] = unstorable_reasons(
            body, upstream["status"], upstream["message"], upstream["headers"], normalized.mapping, self.scrubber,
            upstream_incomplete=upstream["upstream_incomplete"],
            client_disconnected=upstream["client_disconnected"],
            observe_failed=upstream["observe_failed"],
            observe_too_large=upstream["observe_too_large"],
        )
        # The per-message and answer hashes are of the normalized, unscrubbed
        # content, as the fingerprint is: the scrubber blanks the rest of a
        # line after a secret, so hashes of scrubbed text would hide real
        # state. That makes this log as private as the stored text: a hash of
        # request content can be guessed against, so both go to the private,
        # eval-only bucket, never to Prow artifacts.
        entry[F.MESSAGE_HASHES] = normalized.message_hashes
        messages = body.get(request_store.MESSAGES_KEY)
        entry[F.DIAG_MESSAGE_HASHES] = self.diagnosis.hashes(messages if isinstance(messages, list) else [], received)
        entry[F.DIAG_SETTINGS_HASH] = self.diagnosis.settings_hash(body, self.key_context.as_dict(), received)
        entry[F.ANSWER_HASH] = answer_hash(upstream["message"], normalized.mapping)
        skipped = self.store.store(request_id, fingerprint, body)
        entry[F.STORED] = skipped is None
        entry[F.STORE_SKIPPED] = skipped
        self.metrics.add("stored" if skipped is None else "store_skipped")
        if entry[F.PROMPT_TOKENS]:
            self.metrics.add("prompt_tokens", int(entry[F.PROMPT_TOKENS]))
        return entry


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    proxy: ShadowProxy  # set on the subclass make_server builds

    def log_message(self, *args: Any) -> None:  # the JSON lines are the log
        pass

    def _path(self) -> str:
        return self.path.split("?", 1)[0]

    def _read_body(self) -> bytes:
        """The request body; raises ValueError on a malformed length or chunk size."""
        if CHUNKED in (self.headers.get(TRANSFER_ENCODING_HEADER) or "").lower():
            data = bytearray()
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", HEX_BASE)
                if size < 0:
                    raise ValueError("negative chunk size")
                if len(data) + size > MAX_REQUEST_BYTES:
                    raise RequestTooLarge()
                if size == 0:
                    while self.rfile.readline() not in (CRLF, b"\n", b""):
                        pass
                    return bytes(data)
                data.extend(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get(CONTENT_LENGTH_HEADER) or 0)
        if length < 0:
            raise ValueError("negative Content-Length")
        if length > MAX_REQUEST_BYTES:
            raise RequestTooLarge()
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
        try:
            raw = self._read_body()
        except ValueError:
            # The rest of the stream cannot be framed, so the connection ends here.
            self.close_connection = True
            self._send_local(HTTP_BAD_REQUEST, b'{"error": "replay proxy: malformed request framing"}', JSON_CONTENT_TYPE)
            return
        except RequestTooLarge:
            # The body is left unread, so the connection ends here too.
            self.close_connection = True
            self._send_local(HTTP_PAYLOAD_TOO_LARGE, b'{"error": "replay proxy: request too large"}', JSON_CONTENT_TYPE)
            return
        if self._local():
            return
        self.proxy.metrics.add("requests")
        body = None
        if self.command == POST_METHOD and self._path() == COMPLETIONS_PATH:
            try:
                parsed = json.loads(raw)
                body = parsed if isinstance(parsed, dict) else None
            except (ValueError, RecursionError):
                body = None
        received = time.time()
        upstream = self._forward(raw, observe=body is not None, streaming=bool(body and body.get("stream")))
        if body is None:
            return
        self.proxy.metrics.add("completions")
        self.proxy.submit(body, received, upstream, len(raw))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    def _forward(self, raw: bytes, observe: bool, streaming: bool) -> Dict[str, Any]:
        """Send the request upstream and copy the response back byte for byte."""
        config = self.proxy.config
        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROPPED_REQUEST_HEADERS}
        headers[HOST_HEADER] = f"{config.upstream_host}:{config.upstream_port}"
        result: Dict[str, Any] = {"status": HTTP_BAD_GATEWAY, "headers": {}, "usage": None, "message": {},
                                  "latency_ms": None, "client_disconnected": False, "upstream_incomplete": False,
                                  "observe_failed": False, "observe_too_large": False}
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
                    observer = self._observe(observer, result, data)
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
        cut_short = result["upstream_incomplete"] or result["client_disconnected"]
        if observer is not None and resp.status == HTTP_OK and not cut_short:
            # A cut-short response is not parsed to its end: its last event is partial.
            observer = self._observe(observer, result, None)
        if observer is not None and resp.status == HTTP_OK and not cut_short:
            result["usage"] = observer.usage if isinstance(observer.usage, dict) else None
            result["message"] = observer.message if isinstance(observer.message, dict) else {}
        return result

    def _observe(self, observer: "ResponseObserver", result: Dict[str, Any], data: Optional[bytes]):
        """Feed (or, with None, finish) the observer; on any failure stop observing, never the stream."""
        try:
            if data is None:
                observer.finish()
            else:
                observer.feed(data)
            return observer
        except ObserveTooLarge:
            result["observe_failed"] = True
            result["observe_too_large"] = True
            return None
        except Exception:  # an unusual chunk costs the line its usage and answer, not the client its bytes
            result["observe_failed"] = True
            self.proxy.metrics.add("analysis_errors")
            return None

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
    """Serve until TERM or INT, then log what is queued and close the open segment.

    The handlers go in before anything else, so a TERM at any point after the
    first line ends in the same orderly close: a segment left `.open` when the
    pod goes is lost with its emptyDir.
    """
    state: Dict[str, Any] = {"server": None, "stopping": False}

    def on_signal(_signum: int, _frame: Any) -> None:
        state["stopping"] = True
        server = state["server"]
        if server is not None:
            # shutdown() waits for serve_forever to return, which runs on this
            # thread, so it is called from another one.
            threading.Thread(target=server.shutdown, name="replay-shadow-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    config = Config.from_env(dict(os.environ))
    server = make_server(config)
    state["server"] = server
    proxy = server.proxy  # type: ignore[attr-defined]
    try:
        proxy.log(proxy.startup_line())
        if not state["stopping"]:
            server.serve_forever()
    finally:
        server.server_close()
        proxy.close(SHUTDOWN_CLOSE_SECONDS)


if __name__ == "__main__":
    main()
