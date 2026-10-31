"""Pass-through trajectory logger callback for LiteLLM inside kube-agents.

Records every LLM call (model alias, upstream provider, redacted request
messages, tool schemas, redacted assistant response, reasoning_content, token
usage, and latency_ms) as one JSONL line in LITELLM_TRACE_LOG_PATH (default:
/tmp/litellm_traces.jsonl).

Before a record reaches disk, `AuditRedactor` from the sibling `redactor.py`
module strips credentials, Secret payloads, sensitive key/value pairs, email
addresses, and IP literals (plus any operator rules in
`KUBE_AGENTS_REDACTION_CONFIG` when `litellm.redaction.enabled` is also set).
IP literals are pseudonymised by default; with that rule file they follow its
`ip` block (`litellm.redaction.ip`) instead: `off` writes them verbatim, `mask`
masks them, and addresses in `allowCidrs` pass through.
The trace file rotates at `TRACE_FILE_MAX_BYTES` (5 MiB) across
`TRACE_FILE_BACKUP_COUNT` (3) backups under an `flock` on `<path>.lock`. A
record over `TRACE_RECORD_MAX_BYTES` (1 MiB) keeps its metadata and drops
`messages`, `tools` and `response` behind a `truncated` marker, so no file
grows past its cap and the trail on the gateway pod's `/tmp` `emptyDir` holds
at most 20 MiB. Each record is serialised, redacted and written on one writer
thread, off the proxy's event loop, so records never contend for the file.

Designed to be registered in `litellm_settings.callbacks` alongside
`litellm_thinking_replay_callback.proxy_handler_instance`. Never mutates the
live request or response payload in place, and logs a once-per-path warning to
stderr if a write fails rather than breaking inference.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import fcntl
import importlib.util
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, List, Optional

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

try:
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover
    class CustomLogger:  # type: ignore[no-redef]
        pass

logger = logging.getLogger("kube_agents.llm_trace_logger")

TRACE_LOG_PATH_ENV = "LITELLM_TRACE_LOG_PATH"
DEFAULT_TRACE_LOG_PATH = "/tmp/litellm_traces.jsonl"
TRACE_LOG_PATH = os.environ.get(TRACE_LOG_PATH_ENV, DEFAULT_TRACE_LOG_PATH)

REDACTION_CONFIG_PATH_ENV = "KUBE_AGENTS_REDACTION_CONFIG"
REDACTOR_FILE_NAME = "redactor.py"
REDACTOR_MODULE_NAME = "kube_agents_litellm_trace_redactor"
# The name litellm_redaction_callback.py loads redactor.py under. That hook
# precedes this one in litellm_settings.callbacks, so when both are on its
# module is already in sys.modules and is reused here: one module, one
# pseudonym salt, so a value the hook rewrote in the request and the same
# value in the response carry the same token in one record.
REDACTION_HOOK_MODULE_NAME = "kube_agents_litellm_redactor"

# Match agents/chat/defaults/plugins/common/audit_sink.py's 5 MiB x 3 rotation
# cap so the gateway pod's /tmp emptyDir holds at most 20 MiB of trace records.
TRACE_FILE_MAX_BYTES = 5 * 1024 * 1024
TRACE_FILE_BACKUP_COUNT = 3
# One record never exceeds this: past it the payload fields are dropped and a
# marker says so, which is what keeps every file under TRACE_FILE_MAX_BYTES
# (rotation only decides when to rename; it cannot shrink a record).
TRACE_RECORD_MAX_BYTES = 1 * 1024 * 1024
TRACE_RECORD_PAYLOAD_FIELDS = ("messages", "tools", "response")
TRUNCATED_FIELD = "truncated"
TRACE_FILE_MODE = 0o600
TRACE_LOCK_FILE_SUFFIX = ".lock"
_OPEN_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT
_ENCODING = "utf-8"
# Every free-form string the caller chooses (the model alias, the provider,
# the api_base, the reasoning effort, the error) is clipped before the record
# is measured, so no field the cap does not drop can carry a record past it.
ERROR_MAX_CHARS = 4096
METADATA_MAX_CHARS = 512
LATENCY_DECIMALS = 2
UNKNOWN_ERROR = "unknown"
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
MS_PER_SECOND = 1000.0
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
# One writer thread, as audit_sink.py has: records queue in order, the flock
# never contends inside the process (LiteLLM's logging worker awaits up to a
# hundred hooks at once, and threads redacting in parallel would starve the
# one holding the lock), and the event loop is never the thread that writes.
WRITER_THREADS = 1
WRITER_THREAD_NAME = "llm-trace-writer"

LOG_LEVEL = logging.WARNING
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

# Track paths that have already emitted an I/O warning so a misconfigured or
# full volume is visible in pod logs without flooding stderr on every call.
_warned_paths: set[str] = set()


def _configure_logger() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


def _load_redactor():
    """Reuse the redaction hook's redactor module when it is loaded; else import
    the sibling file by path, as that hook does."""
    existing = sys.modules.get(REDACTION_HOOK_MODULE_NAME)
    if existing is not None and hasattr(existing, "AuditRedactor"):
        return existing
    path = Path(__file__).resolve().with_name(REDACTOR_FILE_NAME)
    spec = importlib.util.spec_from_file_location(REDACTOR_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the gateway redactor from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[REDACTOR_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


_configure_logger()
_redactor = _load_redactor()
AuditRedactor = _redactor.AuditRedactor


def _resolve_redaction_rules() -> List[Any]:
    """Load operator rules from KUBE_AGENTS_REDACTION_CONFIG when set, else default IP rules."""
    config_path = os.environ.get(REDACTION_CONFIG_PATH_ENV, "").strip()
    if config_path and yaml is not None and os.path.isfile(config_path):
        with open(config_path, "r", encoding=_ENCODING) as handle:
            config = yaml.safe_load(handle) or {}
        if isinstance(config, dict):
            return AuditRedactor.rules_from_config(config)
    return AuditRedactor.ip_rules()


# Resolved once at import, as litellm_redaction_callback does: the rule file is a
# subPath ConfigMap mount that never changes in a running pod (an edit rolls the
# pod through checksum/config), so reading it per call only adds file I/O and
# YAML parsing on the proxy's event loop.
_redaction_rules = _resolve_redaction_rules()


def _safe_serialize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe_serialize(x) for x in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _safe_serialize(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return _safe_serialize(obj.dict())
        except Exception:
            pass
    return str(obj)


def _clip(value: Any, limit: int = METADATA_MAX_CHARS) -> Any:
    """A string cut to limit characters; anything else as it is."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


def _truncate_record(record: dict, size: int) -> dict:
    """The record's metadata with its payload fields dropped and a marker that
    names them and the size the whole record had."""
    slim = {k: v for k, v in record.items() if k not in TRACE_RECORD_PAYLOAD_FIELDS}
    slim[TRUNCATED_FIELD] = {"fields": list(TRACE_RECORD_PAYLOAD_FIELDS), "bytes": size}
    return slim


def _lock_file_path(path: Path) -> Path:
    return path.with_name(path.name + TRACE_LOCK_FILE_SUFFIX)


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


_writer: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _writer_thread() -> concurrent.futures.ThreadPoolExecutor:
    global _writer
    if _writer is None:
        _writer = concurrent.futures.ThreadPoolExecutor(
            max_workers=WRITER_THREADS, thread_name_prefix=WRITER_THREAD_NAME
        )
    return _writer


def _acquire_lock(lock_fd: int) -> None:
    # Blocking: the only other holder is another process on the same volume,
    # and this runs on the writer thread, never on the event loop.
    fcntl.flock(lock_fd, fcntl.LOCK_EX)


def _rotate_if_full(
    path: Path,
    incoming: int,
    max_bytes: int = TRACE_FILE_MAX_BYTES,
    backup_count: int = TRACE_FILE_BACKUP_COUNT,
) -> None:
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return
    if size == 0 or size + incoming <= max_bytes:
        return
    try:
        for index in range(backup_count, 1, -1):
            older = _rotated(path, index - 1)
            if older.exists():
                os.replace(older, _rotated(path, index))
        if backup_count >= 1:
            os.replace(path, _rotated(path, 1))
    except OSError:
        pass


def _write_whole(fd: int, data: bytes) -> None:
    start = os.fstat(fd).st_size
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except OSError:
        try:
            os.ftruncate(fd, start)
        except OSError:
            pass
        raise


def _append_line(
    line: str,
    path: Path,
    max_bytes: int = TRACE_FILE_MAX_BYTES,
    backup_count: int = TRACE_FILE_BACKUP_COUNT,
) -> None:
    data = (line + "\n").encode(_ENCODING)
    if len(data) > max_bytes:
        # The cause-level bound: rotation renames, it cannot shrink, so a line
        # that would carry a file past its cap is never written. The caller's
        # once-per-path warning records it.
        raise ValueError(f"a {len(data)}-byte record exceeds the {max_bytes}-byte file cap and was not written")
    lock_path = _lock_file_path(path)
    os.makedirs(lock_path.parent, exist_ok=True)
    lock_fd = os.open(lock_path, _OPEN_FLAGS, TRACE_FILE_MODE)
    try:
        _acquire_lock(lock_fd)
        _rotate_if_full(path, len(data), max_bytes=max_bytes, backup_count=backup_count)
        fd = os.open(path, _OPEN_FLAGS, TRACE_FILE_MODE)
        try:
            _write_whole(fd, data)
        finally:
            os.close(fd)
    finally:
        os.close(lock_fd)


class PassThroughTrajectoryLogger(CustomLogger):
    def __init__(
        self,
        max_bytes: int = TRACE_FILE_MAX_BYTES,
        backup_count: int = TRACE_FILE_BACKUP_COUNT,
        record_max_bytes: int = TRACE_RECORD_MAX_BYTES,
    ) -> None:
        super().__init__()
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.record_max_bytes = record_max_bytes

    # LiteLLM awaits these hooks on its event loop. Serialising, redacting and
    # writing a record takes tens to hundreds of milliseconds on an agent-sized
    # conversation, so the whole of it runs on the writer thread and the loop
    # keeps serving requests and probes meanwhile.
    async def async_log_success_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        await self._submit(STATUS_SUCCESS, kwargs, response_obj, start_time, end_time, None)

    async def async_log_failure_event(
        self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        err = kwargs.get("exception") or kwargs.get("traceback_exception")
        await self._submit(
            STATUS_FAILURE,
            kwargs,
            response_obj,
            start_time,
            end_time,
            # The whole text goes to the writer: it is redacted there first
            # and clipped after, so a credential at the clip is not cut into a
            # fragment the patterns do not match.
            str(err) if err else UNKNOWN_ERROR,
        )

    async def _submit(
        self,
        status: str,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        error: Optional[str],
    ) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            _writer_thread(), self._write_record, status, kwargs, response_obj, start_time, end_time, error
        )

    def _write_record(
        self,
        status: str,
        kwargs: dict,
        response_obj: Any,
        start_time: Any,
        end_time: Any,
        error: Optional[str],
    ) -> None:
        log_path = os.environ.get(TRACE_LOG_PATH_ENV, TRACE_LOG_PATH)
        try:
            litellm_params = kwargs.get("litellm_params") or {}
            optional_params = kwargs.get("optional_params") or {}
            latency_ms = None
            if start_time and end_time:
                try:
                    latency_ms = round((end_time - start_time).total_seconds() * MS_PER_SECOND, LATENCY_DECIMALS)
                except Exception:
                    latency_ms = None

            rules = _redaction_rules
            raw_messages = _safe_serialize(kwargs.get("messages"))
            raw_tools = _safe_serialize(optional_params.get("tools"))
            raw_resp = _safe_serialize(response_obj) if response_obj is not None else {}
            raw_api_base = _safe_serialize(litellm_params.get("api_base"))

            record = {
                "timestamp": dt.datetime.now(dt.timezone.utc).strftime(TIMESTAMP_FORMAT),
                "status": status,
                "model_alias": _clip(kwargs.get("model")),
                "custom_llm_provider": _clip(litellm_params.get("custom_llm_provider")),
                "api_base": _clip(AuditRedactor.redact(raw_api_base, rules)),
                "reasoning_effort": _clip(optional_params.get("reasoning_effort")),
                "latency_ms": latency_ms,
                "messages": AuditRedactor.redact(raw_messages, rules),
                "tools": AuditRedactor.redact(raw_tools, rules),
                "response": AuditRedactor.redact(raw_resp, rules),
                "error": _clip(AuditRedactor.redact(error, rules), ERROR_MAX_CHARS) if error is not None else None,
            }
            line = json.dumps(record, ensure_ascii=False)
            size = len(line.encode(_ENCODING))
            if size > self.record_max_bytes:
                slim = _truncate_record(record, size)
                line = json.dumps(slim, ensure_ascii=False)
                if len(line.encode(_ENCODING)) > self.record_max_bytes:
                    # Every free-form field is clipped, so only a tiny cap can
                    # still be over; the error is the largest and goes too.
                    slim["error"] = None
                    line = json.dumps(slim, ensure_ascii=False)
            _append_line(
                line,
                Path(log_path),
                max_bytes=self.max_bytes,
                backup_count=self.backup_count,
            )
        except Exception as exc:
            if log_path not in _warned_paths:
                _warned_paths.add(log_path)
                logger.warning("trace record not written to %s: %s", log_path, exc)


proxy_handler_instance = PassThroughTrajectoryLogger()
