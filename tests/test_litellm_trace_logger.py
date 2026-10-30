"""Tests for litellm.traceLogEnabled and PassThroughTrajectoryLogger.

Verifies:
1. Chart rendering invariance when traceLogEnabled is false (default):
   - ConfigMap data does not include llm_trace_logger.py or redactor.py.
   - litellm_settings.callbacks does not include the logger.
   - Deployment volumeMounts and env vars omit the logger and LITELLM_TRACE_LOG_PATH.
   - Checksum matches clean unset default.
2. Chart rendering when traceLogEnabled is true:
   - ConfigMap data includes llm_trace_logger.py and redactor.py identical to the chart source files.
   - litellm_settings.callbacks includes llm_trace_logger.proxy_handler_instance.
   - Deployment volumeMounts mounts /app/llm_trace_logger.py and /app/redactor.py without duplicating redactor.py when redaction.enabled is also true.
   - Deployment env includes LITELLM_TRACE_LOG_PATH with default "/tmp/litellm_traces.jsonl" and SESSION_KV_SALT.
   - Deployment checksum changes.
3. Custom litellm.traceLogPath under /tmp/ sets LITELLM_TRACE_LOG_PATH accordingly.
4. Schema and template validation reject invalid traceLogPath (relative paths, paths outside /tmp/, or bare /tmp/).
5. Unit tests for PassThroughTrajectoryLogger:
   - async_log_success_event writes valid JSONL record without mutating payload.
   - async_log_failure_event writes valid failure JSONL record with error string.
   - AuditRedactor scrubs credentials, emails, Secret data blocks, and IP literals from messages, tools, responses, and errors before writing.
   - Log rotation shifts .1-.3 backups when incoming bytes exceed max_bytes.
   - A nested trace path gets its parent directory created before the first write.
   - Operator rules from KUBE_AGENTS_REDACTION_CONFIG are read once, at import.
   - I/O errors emit a once-per-path warning to stderr and never raise exceptions.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib.util
import io
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "llm_trace_logger.py"
_REDACTOR_FILE = _CHART / "files" / "redactor.py"
_CALLBACK = "llm_trace_logger.proxy_handler_instance"
_KEY = "llm_trace_logger.py"
_REDACTOR_KEY = "redactor.py"
_MOUNT = "/app/llm_trace_logger.py"
_REDACTOR_MOUNT = "/app/redactor.py"
_DEFAULT_LOG_PATH = "/tmp/litellm_traces.jsonl"
_YA29_SHAPED_VALUE = "ya29." + "A" * 195
_HELM_BASE_ARGS = [
    "helm",
    "template",
    "test-release",
    str(_CHART),
    "--set-string",
    "platformAgent.harness.clusterName=test-cluster",
    "--set-string",
    "platformAgent.harness.location=us-central1",
    "--set-string",
    "platformAgent.harness.projectId=test-project",
    "-s",
    "templates/litellm.yaml",
]


def _load_logger_module():
    """Import the logger module with a fallback stand-in for CustomLogger if needed."""
    spec = importlib.util.spec_from_file_location("llm_trace_logger", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestTraceLoggerChartRender(unittest.TestCase):
    @staticmethod
    def _render(extra_args: list[str] | None = None, values_text: str | None = None):
        command = list(_HELM_BASE_ARGS)
        values_path = None
        if values_text is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
                handle.write(values_text)
                values_path = handle.name
            command += ["-f", values_path]
        command += extra_args or []
        try:
            return subprocess.run(command, capture_output=True, text=True, check=False)
        finally:
            if values_path and os.path.exists(values_path):
                os.unlink(values_path)

    def _documents(self, extra_args: list[str] | None = None, values_text: str | None = None):
        proc = self._render(extra_args, values_text)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        by_kind = {d["kind"]: d for d in documents if d["kind"] in ("ConfigMap", "Deployment")}
        return by_kind["ConfigMap"], by_kind["Deployment"]

    @staticmethod
    def _checksum(deployment: dict) -> str:
        return deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    @staticmethod
    def _callbacks(configmap: dict) -> list[str]:
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        return config["litellm_settings"]["callbacks"]

    def test_default_unset_and_explicit_false_are_identical(self) -> None:
        unset_cm, unset_deploy = self._documents()
        false_cm, false_deploy = self._documents(["--set", "litellm.traceLogEnabled=false"])

        self.assertEqual(unset_cm, false_cm)
        self.assertEqual(self._checksum(unset_deploy), self._checksum(false_deploy))
        self.assertNotIn(_KEY, unset_cm["data"])
        self.assertNotIn(_REDACTOR_KEY, unset_cm["data"])
        self.assertNotIn(_CALLBACK, self._callbacks(unset_cm))

        container = unset_deploy["spec"]["template"]["spec"]["containers"][0]
        mounts = container.get("volumeMounts", [])
        self.assertFalse(any(m.get("mountPath") == _MOUNT for m in mounts))
        self.assertFalse(any(m.get("mountPath") == _REDACTOR_MOUNT for m in mounts))
        envs = container.get("env", [])
        self.assertFalse(any(e.get("name") == "LITELLM_TRACE_LOG_PATH" for e in envs))

    def test_enabled_renders_callback_configmap_mount_and_env(self) -> None:
        set_cm, set_deploy = self._documents(["--set", "litellm.traceLogEnabled=true"])
        unset_cm, unset_deploy = self._documents()

        # ConfigMap data contains the source file and shared redactor verbatim
        self.assertIn(_KEY, set_cm["data"])
        self.assertEqual(set_cm["data"][_KEY], _CHART_FILE.read_text(encoding="utf-8"))
        self.assertIn(_REDACTOR_KEY, set_cm["data"])
        self.assertEqual(set_cm["data"][_REDACTOR_KEY], _REDACTOR_FILE.read_text(encoding="utf-8"))

        # ConfigMap litellm_settings.callbacks includes the callback
        callbacks = self._callbacks(set_cm)
        self.assertIn(_CALLBACK, callbacks)
        self.assertIn("litellm_thinking_replay_callback.proxy_handler_instance", callbacks)

        # Deployment rolls (checksum changes)
        self.assertNotEqual(self._checksum(unset_deploy), self._checksum(set_deploy))

        # Deployment volumeMounts contains the logger and redactor mounts
        container = set_deploy["spec"]["template"]["spec"]["containers"][0]
        mounts = container.get("volumeMounts", [])
        logger_mount = next((m for m in mounts if m.get("mountPath") == _MOUNT), None)
        self.assertIsNotNone(logger_mount)
        self.assertEqual(logger_mount["subPath"], _KEY)
        self.assertEqual(logger_mount["name"], "config-volume")
        redactor_mounts = [m for m in mounts if m.get("mountPath") == _REDACTOR_MOUNT]
        self.assertEqual(len(redactor_mounts), 1)
        self.assertEqual(redactor_mounts[0]["subPath"], _REDACTOR_KEY)

        # Deployment env contains default LITELLM_TRACE_LOG_PATH and SESSION_KV_SALT
        envs = container.get("env", [])
        path_env = next((e for e in envs if e.get("name") == "LITELLM_TRACE_LOG_PATH"), None)
        self.assertIsNotNone(path_env)
        self.assertEqual(path_env["value"], _DEFAULT_LOG_PATH)
        salt_envs = [e for e in envs if e.get("name") == "SESSION_KV_SALT"]
        self.assertEqual(len(salt_envs), 1)

    def test_enabled_alongside_redaction_does_not_duplicate_redactor_mount_or_salt(self) -> None:
        set_cm, set_deploy = self._documents(
            [
                "--set",
                "litellm.traceLogEnabled=true",
                "--set",
                "litellm.redaction.enabled=true",
            ]
        )
        container = set_deploy["spec"]["template"]["spec"]["containers"][0]
        mounts = container.get("volumeMounts", [])
        redactor_mounts = [m for m in mounts if m.get("mountPath") == _REDACTOR_MOUNT]
        self.assertEqual(len(redactor_mounts), 1)
        envs = container.get("env", [])
        salt_envs = [e for e in envs if e.get("name") == "SESSION_KV_SALT"]
        self.assertEqual(len(salt_envs), 1)

    def test_custom_trace_log_path(self) -> None:
        custom_path = "/tmp/litellm/audit.jsonl"
        set_cm, set_deploy = self._documents(
            [
                "--set",
                "litellm.traceLogEnabled=true",
                "--set-string",
                f"litellm.traceLogPath={custom_path}",
            ]
        )
        container = set_deploy["spec"]["template"]["spec"]["containers"][0]
        envs = container.get("env", [])
        path_env = next((e for e in envs if e.get("name") == "LITELLM_TRACE_LOG_PATH"), None)
        self.assertIsNotNone(path_env)
        self.assertEqual(path_env["value"], custom_path)

    def test_values_file_renders_the_same_as_flags(self) -> None:
        set_cm, set_deploy = self._documents(
            [
                "--set",
                "litellm.traceLogEnabled=true",
                "--set-string",
                "litellm.traceLogPath=/tmp/custom.jsonl",
            ]
        )
        file_cm, file_deploy = self._documents(
            values_text="litellm:\n  traceLogEnabled: true\n  traceLogPath: /tmp/custom.jsonl\n"
        )
        self.assertEqual(set_cm, file_cm)
        self.assertEqual(self._checksum(set_deploy), self._checksum(file_deploy))

    def test_schema_validates_trace_log_path(self) -> None:
        for bad_path in (
            "relative/path.jsonl",
            "just_filename.jsonl",
            "/var/log/litellm/audit.jsonl",
            "/tmp/",
            "/tmp",
            # Components clean rewrites: accepted as written they would
            # resolve outside /tmp or to /tmp itself.
            "/tmp/../app/traces.jsonl",
            "/tmp/.",
            "/tmp/./traces.jsonl",
            "/tmp/litellm/../traces.jsonl",
            "/tmp/litellm/",
            "/tmp//traces.jsonl",
        ):
            with self.subTest(path=bad_path):
                proc = self._render(
                    [
                        "--set",
                        "litellm.traceLogEnabled=true",
                        "--set-string",
                        f"litellm.traceLogPath={bad_path}",
                    ]
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("traceLogPath", proc.stderr)


class TestPassThroughTrajectoryLoggerUnit(unittest.TestCase):
    def setUp(self):
        self.mod = _load_logger_module()
        self.mod._warned_paths.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.temp_dir.name, "test_traces.jsonl")
        os.environ["LITELLM_TRACE_LOG_PATH"] = self.log_path
        os.environ["SESSION_KV_SALT"] = "unit-test-salt"

    def tearDown(self):
        os.environ.pop("LITELLM_TRACE_LOG_PATH", None)
        os.environ.pop("SESSION_KV_SALT", None)
        self.temp_dir.cleanup()

    def test_success_event_logging(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        start = dt.datetime(2026, 10, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 10, 18, 12, 0, 0, 450000, tzinfo=dt.timezone.utc)

        kwargs = {
            "model": "model-default",
            "litellm_params": {
                "custom_llm_provider": "hosted_vllm",
                "api_base": "http://vllm.bench-training.svc:8000/v1",
            },
            "optional_params": {
                "reasoning_effort": "high",
                "tools": [{"type": "function", "function": {"name": "kubectl_get"}}],
            },
            "messages": [{"role": "user", "content": "list pods"}],
        }
        response_obj = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking pods.",
                        "reasoning_content": "Need to check pods.",
                    }
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
        }

        asyncio.run(logger.async_log_success_event(kwargs, response_obj, start, end))

        self.assertTrue(os.path.exists(self.log_path))
        lines = pathlib.Path(self.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

        record = json.loads(lines[0])
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["model_alias"], "model-default")
        self.assertEqual(record["custom_llm_provider"], "hosted_vllm")
        self.assertEqual(record["api_base"], "http://vllm.bench-training.svc:8000/v1")
        self.assertEqual(record["reasoning_effort"], "high")
        self.assertEqual(record["latency_ms"], 450.0)
        self.assertEqual(record["messages"], [{"role": "user", "content": "list pods"}])
        self.assertEqual(record["tools"], [{"type": "function", "function": {"name": "kubectl_get"}}])
        self.assertEqual(record["response"]["usage"]["total_tokens"], 165)
        self.assertIsNone(record["error"])

    def test_failure_event_logging(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        start = dt.datetime(2026, 10, 18, 12, 0, 0, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 10, 18, 12, 0, 1, tzinfo=dt.timezone.utc)

        kwargs = {
            "model": "model-default",
            "litellm_params": {"custom_llm_provider": "vertex_ai"},
            "optional_params": {},
            "messages": [{"role": "user", "content": "hello"}],
            "exception": "ConnectionRefusedError: Port 8000 unreachable",
        }

        asyncio.run(logger.async_log_failure_event(kwargs, None, start, end))

        self.assertTrue(os.path.exists(self.log_path))
        lines = pathlib.Path(self.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)

        record = json.loads(lines[0])
        self.assertEqual(record["status"], "failure")
        self.assertEqual(record["model_alias"], "model-default")
        self.assertEqual(record["error"], "ConnectionRefusedError: Port 8000 unreachable")
        self.assertEqual(record["response"], {})
        self.assertEqual(record["latency_ms"], 1000.0)

    def test_redaction_strips_secrets_emails_and_ips_without_mutating_caller_payloads(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        raw_user_msg = f"token {_YA29_SHAPED_VALUE} ip 10.0.0.5 email alice@example.com"
        raw_assistant_msg = f"found secret {_YA29_SHAPED_VALUE} on 10.0.0.5 for bob@example.com"
        kwargs = {
            "model": "model-default",
            "litellm_params": {"custom_llm_provider": "gemini"},
            "optional_params": {},
            "messages": [{"role": "user", "content": raw_user_msg}],
        }
        response_obj = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": raw_assistant_msg,
                    }
                }
            ]
        }

        asyncio.run(logger.async_log_success_event(kwargs, response_obj, None, None))

        # Caller payloads remain untouched in memory
        self.assertEqual(kwargs["messages"][0]["content"], raw_user_msg)
        self.assertEqual(response_obj["choices"][0]["message"]["content"], raw_assistant_msg)

        record = json.loads(pathlib.Path(self.log_path).read_text(encoding="utf-8").strip())
        logged_user = record["messages"][0]["content"]
        logged_assistant = record["response"]["choices"][0]["message"]["content"]
        self.assertNotIn(_YA29_SHAPED_VALUE, logged_user)
        self.assertNotIn("10.0.0.5", logged_user)
        self.assertNotIn("alice@example.com", logged_user)
        self.assertIn("[REDACTED_SECRET]", logged_user)
        self.assertIn("[REDACTED_EMAIL]", logged_user)
        self.assertRegex(logged_user, r"\[ip:[0-9a-f]{12}\]")

        self.assertNotIn(_YA29_SHAPED_VALUE, logged_assistant)
        self.assertNotIn("10.0.0.5", logged_assistant)
        self.assertNotIn("bob@example.com", logged_assistant)
        self.assertIn("[REDACTED_SECRET]", logged_assistant)
        self.assertIn("[REDACTED_EMAIL]", logged_assistant)
        self.assertRegex(logged_assistant, r"\[ip:[0-9a-f]{12}\]")

    def test_rotation_caps_files_at_max_bytes_and_backup_count(self):
        # One record (about 370 bytes) fits the cap; a second does not, so
        # every record after the first rotates the file. The cap is above the
        # record because the append refuses a line over the file cap.
        logger = self.mod.PassThroughTrajectoryLogger(max_bytes=600, backup_count=3)
        for idx in range(6):
            kwargs = {
                "model": "model-default",
                "messages": [{"role": "user", "content": f"turn-{idx}-" + ("x" * 80)}],
            }
            asyncio.run(logger.async_log_success_event(kwargs, {}, None, None))

        base = pathlib.Path(self.log_path)
        self.assertTrue(base.exists())
        self.assertTrue(base.with_name(f"{base.name}.1").exists())
        self.assertTrue(base.with_name(f"{base.name}.2").exists())
        self.assertTrue(base.with_name(f"{base.name}.3").exists())
        self.assertFalse(base.with_name(f"{base.name}.4").exists())

        # Latest record is in the live file
        latest = json.loads(base.read_text(encoding="utf-8").strip())
        self.assertIn("turn-5-", latest["messages"][0]["content"])

    def test_hooks_run_the_write_off_the_event_loop(self):
        # LiteLLM awaits the hook on its loop: the record must be built and
        # written in another thread, where no loop is running.
        seen = {}
        original = self.mod._append_line

        def recording_append(line, path, **kw):
            seen["thread"] = threading.get_ident()
            try:
                asyncio.get_running_loop()
                seen["loop_running"] = True
            except RuntimeError:
                seen["loop_running"] = False
            return original(line, path, **kw)

        self.mod._append_line = recording_append
        try:
            logger = self.mod.PassThroughTrajectoryLogger()
            asyncio.run(logger.async_log_success_event({"model": "m", "messages": []}, {}, None, None))
            asyncio.run(logger.async_log_failure_event({"model": "m", "messages": []}, None, None, None))
        finally:
            self.mod._append_line = original
        self.assertNotEqual(seen["thread"], threading.get_ident())
        self.assertFalse(seen["loop_running"])
        lines = pathlib.Path(self.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual([json.loads(l)["status"] for l in lines], ["success", "failure"])

    def test_oversize_record_keeps_metadata_and_drops_payload(self):
        # Rotation cannot shrink a record, so the record cap is what keeps
        # every file under max_bytes: an oversize record lands as its metadata
        # plus a marker, and no file passes the cap.
        logger = self.mod.PassThroughTrajectoryLogger(max_bytes=600, backup_count=3, record_max_bytes=300)
        big = {"model": "model-default", "messages": [{"role": "user", "content": "x" * 2000}]}
        asyncio.run(logger.async_log_success_event(big, {"choices": ["y" * 500]}, None, None))
        base = pathlib.Path(self.log_path)
        latest = json.loads(base.read_text(encoding="utf-8").strip())
        self.assertEqual(latest["model_alias"], "model-default")
        self.assertEqual(latest["status"], "success")
        for field in ("messages", "tools", "response"):
            self.assertNotIn(field, latest)
        self.assertEqual(latest["truncated"]["fields"], ["messages", "tools", "response"])
        self.assertGreater(latest["truncated"]["bytes"], 2500)
        for _ in range(5):
            asyncio.run(logger.async_log_success_event(big, {}, None, None))
            asyncio.run(logger.async_log_success_event({"model": "m", "messages": [{"role": "user", "content": "short"}]}, {}, None, None))
        for candidate in [base] + [base.with_name(f"{base.name}.{i}") for i in range(1, 4)]:
            if candidate.exists():
                self.assertLessEqual(candidate.stat().st_size, 600, candidate)

    def test_caller_chosen_metadata_cannot_carry_a_record_past_the_file_cap(self):
        # A 6 MiB model name from any same-namespace pod used to land whole.
        # Now every caller-chosen string is clipped, and a line over the file
        # cap is refused rather than written.
        logger = self.mod.PassThroughTrajectoryLogger(max_bytes=4096, backup_count=3, record_max_bytes=2048)
        kwargs = {"model": "A" * 6_000_000, "messages": [], "optional_params": {"reasoning_effort": "B" * 100_000},
                  "exception": RuntimeError("no such model")}
        asyncio.run(logger.async_log_failure_event(kwargs, None, None, None))
        base = pathlib.Path(self.log_path)
        latest = json.loads(base.read_text(encoding="utf-8").strip())
        self.assertEqual(len(latest["model_alias"]), self.mod.METADATA_MAX_CHARS)
        self.assertEqual(len(latest["reasoning_effort"]), self.mod.METADATA_MAX_CHARS)
        self.assertLessEqual(base.stat().st_size, 4096)
        # And the append itself refuses a line over the file cap, whatever built it.
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        self.mod.logger.addHandler(handler)
        try:
            original = self.mod._truncate_record
            self.mod._truncate_record = lambda record, size: {"blob": "C" * 5000}
            try:
                asyncio.run(logger.async_log_failure_event(dict(kwargs, exception=RuntimeError("x" * 5000)), None, None, None))
            finally:
                self.mod._truncate_record = original
        finally:
            self.mod.logger.removeHandler(handler)
        self.assertIn("exceeds the 4096-byte file cap", stream.getvalue())
        for candidate in [base] + [base.with_name(f"{base.name}.{i}") for i in range(1, 4)]:
            if candidate.exists():
                self.assertLessEqual(candidate.stat().st_size, 4096, candidate)

    def test_error_is_redacted_before_it_is_clipped(self):
        # A token that begins just before the clip must be redacted whole,
        # not cut into a prefix the patterns no longer match.
        logger = self.mod.PassThroughTrajectoryLogger()
        token = _YA29_SHAPED_VALUE
        text = "x" * (self.mod.ERROR_MAX_CHARS - 10) + " " + token + " tail"
        kwargs = {"model": "m", "messages": [], "exception": RuntimeError(text)}
        asyncio.run(logger.async_log_failure_event(kwargs, None, None, None))
        latest = json.loads(pathlib.Path(self.log_path).read_text(encoding="utf-8").strip())
        self.assertNotIn("ya29.", latest["error"])
        self.assertIn("[REDACTED", latest["error"])
        self.assertLessEqual(len(latest["error"]), self.mod.ERROR_MAX_CHARS)

    def test_error_is_clipped_to_the_named_length(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        kwargs = {"model": "m", "messages": [], "exception": RuntimeError("e" * 10000)}
        asyncio.run(logger.async_log_failure_event(kwargs, None, None, None))
        latest = json.loads(pathlib.Path(self.log_path).read_text(encoding="utf-8").strip())
        self.assertEqual(len(latest["error"]), self.mod.ERROR_MAX_CHARS)

    def test_oversize_error_is_clipped_and_the_cap_holds_without_payload(self):
        # The cap must hold whatever field is large: a long error is clipped
        # at the hook, and a record still over after the payload drop loses
        # the error too.
        logger = self.mod.PassThroughTrajectoryLogger(max_bytes=600, backup_count=3, record_max_bytes=300)
        kwargs = {"model": "model-default", "messages": [], "exception": RuntimeError("e" * 10000)}
        asyncio.run(logger.async_log_failure_event(kwargs, None, None, None))
        base = pathlib.Path(self.log_path)
        self.assertLessEqual(base.stat().st_size, 300 + 1)
        latest = json.loads(base.read_text(encoding="utf-8").strip())
        self.assertEqual(latest["status"], "failure")
        self.assertIsNone(latest["error"])
        self.assertIn("truncated", latest)

    def test_the_writer_is_one_thread_and_records_keep_their_order(self):
        # Many hooks at once must not contend for the lock: everything goes
        # through one writer thread, in submission order.
        logger = self.mod.PassThroughTrajectoryLogger()
        threads = set()
        original = self.mod._append_line

        def recording_append(line, path, **kw):
            threads.add(threading.current_thread().name)
            return original(line, path, **kw)

        self.mod._append_line = recording_append
        try:
            async def burst():
                await asyncio.gather(*[
                    logger.async_log_success_event({"model": "m", "messages": [{"role": "user", "content": f"n{i}"}]}, {}, None, None)
                    for i in range(40)
                ])
            asyncio.run(burst())
        finally:
            self.mod._append_line = original
        self.assertEqual(len(threads), 1)
        self.assertTrue(next(iter(threads)).startswith(self.mod.WRITER_THREAD_NAME))
        lines = pathlib.Path(self.log_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 40)
        self.assertEqual([json.loads(l)["messages"][0]["content"] for l in lines], [f"n{i}" for i in range(40)])

    def test_reuses_the_redaction_hooks_redactor_module(self):
        # With litellm.redaction.enabled on, the hook has already loaded
        # redactor.py under its own name; the logger must share that module
        # (and so its pseudonym salt) rather than execute the file again.
        name = self.mod.REDACTION_HOOK_MODULE_NAME
        spec = importlib.util.spec_from_file_location(name, _CHART_FILE.with_name("redactor.py"))
        hook_module = importlib.util.module_from_spec(spec)
        sys.modules[name] = hook_module
        try:
            spec.loader.exec_module(hook_module)
            fresh = _load_logger_module()
            self.assertIs(fresh._redactor, hook_module)
            self.assertIs(fresh.AuditRedactor, hook_module.AuditRedactor)
        finally:
            sys.modules.pop(name, None)
        alone = _load_logger_module()
        self.assertIsNot(alone._redactor, hook_module)

    def test_nested_trace_log_path_creates_parent_directory(self):
        # The chart admits /tmp/<dir>/<file> (test_custom_trace_log_path), and
        # the gateway's /tmp emptyDir starts empty, so the logger has to create
        # <dir> itself.
        nested_path = os.path.join(self.temp_dir.name, "litellm", "audit.jsonl")
        os.environ["LITELLM_TRACE_LOG_PATH"] = nested_path
        logger = self.mod.PassThroughTrajectoryLogger()

        asyncio.run(logger.async_log_success_event({"model": "model-default"}, {}, None, None))

        lines = pathlib.Path(nested_path).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["model_alias"], "model-default")
        self.assertEqual(self.mod._warned_paths, set())

    def test_redaction_rules_are_read_once_at_import(self):
        rules_path = pathlib.Path(self.temp_dir.name) / "redaction.yaml"
        rules_path.write_text(
            "rules:\n  - name: cluster-name\n    literal: prod-eu-1\n    action: mask\n",
            encoding="utf-8",
        )
        os.environ["KUBE_AGENTS_REDACTION_CONFIG"] = str(rules_path)
        self.addCleanup(os.environ.pop, "KUBE_AGENTS_REDACTION_CONFIG", None)
        mod = _load_logger_module()
        # The pod's rule file never changes after start; a record written after
        # it is gone still carries the operator rule loaded at import.
        rules_path.unlink()
        logger = mod.PassThroughTrajectoryLogger()
        kwargs = {
            "model": "model-default",
            "messages": [{"role": "user", "content": "deploy to prod-eu-1"}],
        }

        asyncio.run(logger.async_log_success_event(kwargs, {}, None, None))

        record = json.loads(pathlib.Path(self.log_path).read_text(encoding="utf-8").strip())
        self.assertEqual(record["messages"][0]["content"], "deploy to [REDACTED_CLUSTER_NAME]")

    def test_io_failure_logs_warning_once_and_never_raises(self):
        logger = self.mod.PassThroughTrajectoryLogger()
        # A parent that is a regular file cannot be created as a directory by
        # any user, root included, so the write fails the same way everywhere.
        not_a_directory = pathlib.Path(self.temp_dir.name) / "not-a-directory"
        not_a_directory.write_text("", encoding="utf-8")
        bad_path = str(not_a_directory / "traces.jsonl")
        os.environ["LITELLM_TRACE_LOG_PATH"] = bad_path

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        self.mod.logger.addHandler(handler)
        try:
            kwargs = {"model": "model-default"}
            asyncio.run(logger.async_log_success_event(kwargs, {}, None, None))
            asyncio.run(logger.async_log_failure_event(kwargs, {}, None, None))
        except Exception as e:
            self.fail(f"Logger raised exception on I/O error: {e}")
        finally:
            self.mod.logger.removeHandler(handler)

        output = stream.getvalue().strip().splitlines()
        self.assertEqual(len(output), 1)
        self.assertIn(bad_path, output[0])

    def test_safe_serialize_handles_objects_and_pydantic_shapes(self):
        class DummyPydantic:
            def model_dump(self):
                return {"field": "value", "sub": [1, 2]}

        class DummyLegacy:
            def dict(self):
                return {"legacy": 42}

        class Unserializable:
            def __repr__(self):
                return "<CustomObj>"

        obj = {
            "pydantic": DummyPydantic(),
            "legacy": DummyLegacy(),
            "custom": Unserializable(),
            "list": [1, "two", None],
        }

        res = self.mod._safe_serialize(obj)
        self.assertEqual(res["pydantic"], {"field": "value", "sub": [1, 2]})
        self.assertEqual(res["legacy"], {"legacy": 42})
        self.assertEqual(res["custom"], "<CustomObj>")
        self.assertEqual(res["list"], [1, "two", None])


if __name__ == "__main__":
    unittest.main()
