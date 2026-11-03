"""Scrub request text with the install's scrubbers and keep it, one copy per message.

The proxy sees every request before anything is cleaned up, so it runs the
cleanup `bench/kube_agents_bench/worker_trajectory.py` runs, in the same order:
the install's `AuditRedactor` (agents/chat/defaults/plugins/common/redactor.py)
first, then the rules in agents/platform/scripts/worker_transcript.py, both
loaded by path. A tool result whose call reads a secret's value is not kept at
all, as the trajectory read withholds it.

Fail closed: when either module cannot be loaded, or scrubbing a request
raises, no text from that request is written. The fingerprints are logged
regardless; only the text is lost.

Layout under the store directory:
  blobs/<sha256>.json      one scrubbed message (or the tool list, or the
                           request settings), keyed by the hash of its
                           scrubbed content, so a system prompt or a tool
                           result repeated in every later request is kept once
  requests/<request_id>.json  the blob hashes that make up one request, in order
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

REDACTOR_MODULE_NAME = "replay_proxy_audit_redactor"
TRANSCRIPT_RULES_MODULE_NAME = "replay_proxy_worker_transcript"
REDACTOR_CLASS = "AuditRedactor"

# The step kinds worker_transcript.scrub_step_text takes. User and system text
# carries card views and pasted output, so it gets the tool-result rules (the
# rest-of-line mask), the stricter of the two.
KIND_TOOL_CALL = "tool_call"
KIND_TOOL_RESULT = "tool_result"
KIND_REPLY = "reply"
KIND_THINKING = "thinking"
TOOL_ROLE = "tool"
ROLE_KINDS = {TOOL_ROLE: KIND_TOOL_RESULT, "user": KIND_TOOL_RESULT, "system": KIND_TOOL_RESULT, "assistant": KIND_REPLY}
MESSAGES_KEY = "messages"
TOOLS_KEY = "tools"
FUNCTION_KEY = "function"
ARGUMENTS_KEY = "arguments"
THINKING_KEYS = ("reasoning_content", "thinking_blocks")
# Kept as written: they hold no request text.
PLAIN_KEYS = ("role", "name", "tool_call_id", "type", "index", "id")
CONTENT_KEY = "content"
TOOL_CALLS_KEY = "tool_calls"
TEXT_PART_TYPE = "text"

WITHHELD = "[WITHHELD: scrubber unavailable]"
WITHHELD_READ_RESULT = "[REDACTED_RESULT: the call read a credential; its output is not recorded]"

BLOBS_DIR = "blobs"
REQUESTS_DIR = "requests"
JSON_SUFFIX = ".json"


def _load_module(name: str, path: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered before the exec: the redactor's dataclass resolves its string
    # annotations through sys.modules, as worker_trajectory.load_module notes.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Scrubber:
    """The install's two scrubbers, loaded by path; `available` is False when either is missing."""

    def __init__(self, redactor_path: str, transcript_rules_path: str):
        self.redactor: Any = None
        self.rules: Any = None
        self.error: Optional[str] = None
        try:
            self.redactor = getattr(_load_module(REDACTOR_MODULE_NAME, redactor_path), REDACTOR_CLASS)
            self.rules = _load_module(TRANSCRIPT_RULES_MODULE_NAME, transcript_rules_path)
        except Exception as exc:  # any failure to load means no text is kept
            self.redactor = None
            self.rules = None
            self.error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self.redactor is not None and self.rules is not None

    def text(self, kind: str, value: Any) -> str:
        if not self.available:
            raise RuntimeError("scrubbers unavailable")
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return self.rules.scrub_step_text(self.redactor, kind, text, WITHHELD)

    def secret_read_call_ids(self, messages: List[Any]) -> set:
        """Ids of tool calls whose command prints a secret's value."""
        ids = set()
        for message in messages:
            if not isinstance(message, dict):
                continue
            for call in message.get(TOOL_CALLS_KEY) or []:
                if not isinstance(call, dict):
                    continue
                arguments = (call.get(FUNCTION_KEY) or {}).get(ARGUMENTS_KEY)
                if arguments is not None and self.rules.is_secret_read_tool_call(arguments):
                    ids.add(call.get("id"))
        return ids

    def content(self, kind: str, content: Any) -> Any:
        if isinstance(content, str):
            return self.text(kind, content)
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == TEXT_PART_TYPE:
                    parts.append({"type": TEXT_PART_TYPE, "text": self.text(kind, part.get("text", ""))})
                elif isinstance(part, dict):
                    parts.append({"type": part.get("type"), "omitted": True})
            return parts
        if content is None:
            return None
        return self.text(kind, content)

    def message(self, message: Any, secret_reads: set) -> Any:
        if not isinstance(message, dict):
            return self.text(KIND_TOOL_RESULT, message)
        role = message.get("role")
        kind = ROLE_KINDS.get(role, KIND_TOOL_RESULT)
        out: Dict[str, Any] = {}
        for key, value in message.items():
            if key in PLAIN_KEYS:
                out[key] = value
            elif key == CONTENT_KEY:
                if role == TOOL_ROLE and message.get("tool_call_id") in secret_reads and value is not None:
                    out[key] = WITHHELD_READ_RESULT
                else:
                    out[key] = self.content(kind, value)
            elif key == TOOL_CALLS_KEY and isinstance(value, list):
                out[key] = [self.tool_call(call) for call in value]
            elif key in THINKING_KEYS:
                out[key] = self.text(KIND_THINKING, value)
            else:
                out[key] = self.text(KIND_TOOL_RESULT, value)
        return out

    def tool_call(self, call: Any) -> Any:
        if not isinstance(call, dict):
            return self.text(KIND_TOOL_CALL, call)
        out = {k: v for k, v in call.items() if k in PLAIN_KEYS}
        function = call.get(FUNCTION_KEY)
        if isinstance(function, dict):
            out[FUNCTION_KEY] = {
                "name": function.get("name"),
                ARGUMENTS_KEY: self.text(KIND_TOOL_CALL, function.get(ARGUMENTS_KEY) or ""),
            }
        for key, value in call.items():
            if key not in out and key != FUNCTION_KEY:
                out[key] = self.text(KIND_TOOL_RESULT, value)
        return out

    def request(self, body: Dict[str, Any]) -> Tuple[List[Any], Any, Dict[str, Any]]:
        """Scrubbed (messages, tools, other settings) of one request body."""
        messages = body.get(MESSAGES_KEY) if isinstance(body.get(MESSAGES_KEY), list) else []
        secret_reads = self.secret_read_call_ids(messages)
        scrubbed = [self.message(m, secret_reads) for m in messages]
        tools = body.get(TOOLS_KEY)
        scrubbed_tools = json.loads(self.text(KIND_TOOL_RESULT, tools)) if tools is not None else None
        settings = {k: v for k, v in body.items() if k not in (MESSAGES_KEY, TOOLS_KEY)}
        scrubbed_settings = json.loads(self.text(KIND_TOOL_RESULT, settings))
        return scrubbed, scrubbed_tools, scrubbed_settings

    def answer_changes(self, answer: Dict[str, Any]) -> bool:
        """Whether scrubbing would change an answer (such an answer is never saved)."""
        if not self.available:
            return True
        if answer.get(CONTENT_KEY) and self.text(KIND_REPLY, answer[CONTENT_KEY]) != answer[CONTENT_KEY]:
            return True
        for call in answer.get(TOOL_CALLS_KEY) or []:
            arguments = (call.get(FUNCTION_KEY) or {}).get(ARGUMENTS_KEY) or ""
            if arguments and self.text(KIND_TOOL_CALL, arguments) != arguments:
                return True
        return False


class RequestStore:
    """Writes scrubbed requests under `root`, each distinct message once."""

    def __init__(self, root: str, scrubber: Scrubber):
        self.root = root
        self.scrubber = scrubber
        self._lock = threading.Lock()

    def _blob(self, value: Any) -> str:
        data = _canonical(value)
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        path = os.path.join(self.root, BLOBS_DIR, digest + JSON_SUFFIX)
        if not os.path.exists(path):
            self._write(path, data)
        return digest

    @staticmethod
    def _write(path: str, data: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp, path)

    def store(self, request_id: str, fingerprint: str, body: Dict[str, Any]) -> Optional[str]:
        """Scrub and write one request; returns None when stored, else why nothing was written."""
        if not self.scrubber.available:
            return "scrubbers_unavailable"
        try:
            messages, tools, settings = self.scrubber.request(body)
        except Exception as exc:  # scrubbing failed: keep nothing from this request
            return f"scrub_failed: {type(exc).__name__}"
        with self._lock:
            manifest = {
                "request_id": request_id,
                "fingerprint": fingerprint,
                "settings": self._blob(settings),
                "tools": self._blob(tools) if tools is not None else None,
                "messages": [self._blob(m) for m in messages],
            }
            self._write(os.path.join(self.root, REQUESTS_DIR, request_id + JSON_SUFFIX), _canonical(manifest))
        return None
