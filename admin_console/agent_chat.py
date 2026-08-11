"""Bounded interactive access to the in-cluster agent chat API."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from admin_console.project_config import (
    DeploymentTarget,
    is_valid_cluster_name,
    is_valid_location,
    is_valid_namespace,
    is_valid_project_id,
)
from admin_console.telemetry import redact_evidence

_K8S_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RUN_ID = re.compile(r"^run_[a-f0-9]{32}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_USER_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,253}$")
_MAX_PROMPT_BYTES = 32_000
MAX_HISTORY_MESSAGES = 100

# The script runs in the agent container and reads API_SERVER_KEY only inside
# that process. The credential is never returned over stdout, copied into the
# local portal process, or placed in kubectl arguments.
_RUN_SCRIPT = r'''
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "http://127.0.0.1:8642"
HEADERS = {
    "Authorization": "Bearer " + os.environ["API_SERVER_KEY"],
    "Content-Type": "application/json",
}


def emit(payload):
    print(json.dumps(payload), flush=True)


def request(method, path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        BASE + path, data=data, headers=HEADERS, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read(8192).decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"error": {"message": raw}}
        print(json.dumps({"transport_error": True, "status_code": exc.code, "detail": detail}))
        raise SystemExit(0)


def profile_path(profile, suffix):
    return suffix if profile == "default" else "/p/" + profile + suffix


def record_portal_identity(session_id, user_email):
    if not session_id.startswith("portal_") or not user_email:
        return
    metadata = {
        "session_id": session_id,
        "platform": "admin_portal",
        "user_id": user_email,
        "user_email": user_email,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with sqlite3.connect(
        "/var/lib/kube-agents/session/session_kv.db", timeout=5.0
    ) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS session_metadata (
                session_id TEXT PRIMARY KEY,
                metadata TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT OR REPLACE INTO session_metadata
                (session_id, metadata, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (session_id, json.dumps(metadata, sort_keys=True)),
        )


payload = json.load(sys.stdin)
action = payload["action"]
profile = payload.get("profile", "default")

if action == "run":
    record_portal_identity(payload["session_id"], payload.get("user_email", ""))
    body = {
        "input": payload["prompt"],
        "session_id": payload["session_id"],
        "conversation_history": payload.get("history", []),
    }
    started = request("POST", profile_path(profile, "/v1/runs"), body, 30)
    run_id = started["run_id"]
    emit({
        "checkpoint": True,
        "run_id": run_id,
        "session_id": payload["session_id"],
        "status": "running",
    })
    events = []
    path = profile_path(profile, "/v1/runs/" + run_id + "/events")
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=payload.get("timeout", 600)) as response:
            for raw in response:
                if not raw.startswith(b"data:"):
                    continue
                event = json.loads(raw[5:].strip().decode("utf-8"))
                if len(events) < 250:
                    events.append(event)
                if event.get("event") == "approval.request":
                    emit({
                        "checkpoint": True,
                        "run_id": run_id,
                        "session_id": payload["session_id"],
                        "status": "waiting_for_approval",
                        "approval": event,
                        "events": events,
                    })
                    events = []
                    continue
                if event.get("event") in {"run.completed", "run.failed", "run.cancelled"}:
                    emit({
                        "run_id": run_id,
                        "session_id": payload["session_id"],
                        "status": event["event"].removeprefix("run."),
                        "output": event.get("output", ""),
                        "error": event.get("error", ""),
                        "events": events,
                    })
                    raise SystemExit(0)
    except TimeoutError:
        pass
    status = request("GET", profile_path(profile, "/v1/runs/" + run_id))
    status.update({"run_id": run_id, "session_id": payload["session_id"], "events": events})
    emit(status)
elif action == "approve":
    run_id = payload["run_id"]
    request(
        "POST",
        profile_path(profile, "/v1/runs/" + run_id + "/approval"),
        {"choice": payload["choice"], "all": False},
    )
    emit({"run_id": run_id, "status": "running"})
elif action == "stop":
    run_id = payload["run_id"]
    result = request("POST", profile_path(profile, "/v1/runs/" + run_id + "/stop"), {})
    result["run_id"] = run_id
    print(json.dumps(result))
else:
    raise ValueError("unsupported action")
'''


@dataclass(frozen=True)
class ChatCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class ChatKubeRunner(Protocol):
    def run(
        self,
        arguments: list[str],
        *,
        input_text: str,
        timeout: int = 620,
        line_callback: Callable[[str], None] | None = None,
    ) -> ChatCommandResult: ...


class KubectlChatRunner:
    def run(
        self,
        arguments: list[str],
        *,
        input_text: str,
        timeout: int = 620,
        line_callback: Callable[[str], None] | None = None,
    ) -> ChatCommandResult:
        environment = os.environ.copy()
        account = os.environ.get("KUBE_AGENTS_ADMIN_USER", "").strip()
        if account:
            environment["CLOUDSDK_CORE_ACCOUNT"] = account
        if line_callback is None:
            try:
                completed = subprocess.run(
                    ["kubectl", *arguments],
                    input=input_text,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=environment,
                )
            except subprocess.TimeoutExpired:
                return ChatCommandResult(124, timed_out=True)
            except OSError as exc:
                return ChatCommandResult(127, stderr=type(exc).__name__)
            return ChatCommandResult(
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )

        try:
            process = subprocess.Popen(
                ["kubectl", *arguments],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
        except OSError as exc:
            return ChatCommandResult(127, stderr=type(exc).__name__)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def drain_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                stdout_lines.append(line)
                line_callback(line.rstrip("\r\n"))

        def drain_stderr() -> None:
            assert process.stderr is not None
            stderr_lines.extend(process.stderr)

        stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        assert process.stdin is not None
        try:
            process.stdin.write(input_text)
            process.stdin.close()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            return ChatCommandResult(
                124,
                "".join(stdout_lines),
                "".join(stderr_lines),
                timed_out=True,
            )
        stdout_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        return ChatCommandResult(
            process.returncode,
            "".join(stdout_lines),
            "".join(stderr_lines),
        )


class AgentChatError(RuntimeError):
    """Safe user-facing interactive agent failure."""

    def __init__(self, message: str, guidance: str = "") -> None:
        super().__init__(message)
        self.guidance = guidance


@dataclass(frozen=True)
class ChatRunResult:
    run_id: str
    session_id: str
    status: str
    output: str = ""
    error: str = ""
    approval: dict | None = None
    events: tuple[dict, ...] = ()


class AgentChatProvider:
    """Call the agent through a fixed in-pod loopback client."""

    def __init__(
        self,
        target: DeploymentTarget,
        *,
        runner: ChatKubeRunner | None = None,
    ) -> None:
        if not (
            is_valid_project_id(target.project_id)
            and is_valid_cluster_name(target.cluster_name)
            and is_valid_location(target.location)
            and is_valid_namespace(target.namespace)
        ):
            raise ValueError("invalid agent chat target")
        self.target = target
        self.runner = runner or KubectlChatRunner()
        self.context = f"gke_{target.project_id}_{target.location}_{target.cluster_name}"

    def _base(self) -> list[str]:
        return ["--context", self.context, "-n", self.target.namespace]

    def _gateway_pod(self, agent: str) -> str:
        if not _K8S_NAME.fullmatch(agent):
            raise ValueError("invalid PlatformAgent name")
        result = self.runner.run(
            [
                *self._base(),
                "get",
                "pods",
                "-l",
                f"app={agent}-gateway",
                "--field-selector=status.phase=Running",
                "-o",
                "json",
            ],
            input_text="",
            timeout=20,
        )
        payload = self._json(result, "Gateway discovery")
        pods = sorted(
            str((item.get("metadata") or {}).get("name") or "")
            for item in payload.get("items", [])
            if (item.get("metadata") or {}).get("name")
        )
        if not pods:
            raise AgentChatError(
                "No running gateway pod was found.",
                f"Check PlatformAgent {agent} in namespace {self.target.namespace}.",
            )
        return pods[0]

    @staticmethod
    def _json(result: ChatCommandResult, component: str) -> dict:
        if result.returncode != 0:
            if result.timed_out:
                guidance = "The run exceeded the portal timeout. Check Activity Explorer before retrying."
            elif "forbidden" in result.stderr.lower():
                guidance = "Request pods/exec access to the selected gateway namespace."
            else:
                guidance = "Check the selected gateway pod and agent API health."
            raise AgentChatError(f"{component} failed.", guidance)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AgentChatError(
                f"{component} returned invalid data.",
                "Inspect the gateway logs and retry.",
            ) from exc
        if not isinstance(payload, dict):
            raise AgentChatError(f"{component} returned invalid data.")
        if payload.get("transport_error"):
            detail = payload.get("detail") or {}
            error = detail.get("error") or {}
            message = redact_evidence(str(error.get("message") or "The agent rejected the request."))
            raise AgentChatError(message, "Inspect the selected agent's API and model-provider health.")
        return payload

    def _invoke(
        self,
        agent: str,
        payload: dict,
        *,
        timeout: int = 620,
        update_callback: Callable[[dict], None] | None = None,
    ) -> dict:
        pod = self._gateway_pod(agent)
        parsed_lines: list[dict] = []

        def parse_line(line: str) -> None:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return
            if not isinstance(parsed, dict):
                return
            parsed_lines.append(parsed)
            if parsed.get("checkpoint") and update_callback is not None:
                update_callback(parsed)

        result = self.runner.run(
            [
                *self._base(),
                "exec",
                "-i",
                pod,
                "-c",
                "platform-agent",
                "--",
                "/opt/hermes/.venv/bin/python3",
                "-c",
                _RUN_SCRIPT,
            ],
            input_text=json.dumps(payload),
            timeout=timeout,
            line_callback=parse_line if update_callback is not None else None,
        )
        if update_callback is not None:
            if result.returncode != 0:
                return self._json(result, "Agent chat")
            if not parsed_lines:
                raise AgentChatError(
                    "Agent chat returned invalid data.",
                    "Inspect the gateway logs and retry.",
                )
            return self._json(
                ChatCommandResult(0, json.dumps(parsed_lines[-1])),
                "Agent chat",
            )
        return self._json(result, "Agent chat")

    @staticmethod
    def _result(payload: dict, *, session_id: str = "") -> ChatRunResult:
        approval = payload.get("approval")
        if isinstance(approval, dict):
            approval = {
                key: redact_evidence(value) if isinstance(value, str) else value
                for key, value in approval.items()
            }
        events = []
        for raw in payload.get("events", []):
            if isinstance(raw, dict):
                events.append(
                    {
                        key: redact_evidence(value) if isinstance(value, str) else value
                        for key, value in raw.items()
                    }
                )
        return ChatRunResult(
            run_id=str(payload.get("run_id") or ""),
            session_id=str(payload.get("session_id") or session_id),
            status=str(payload.get("status") or "unknown"),
            output=redact_evidence(str(payload.get("output") or "")),
            error=redact_evidence(str(payload.get("error") or "")),
            approval=approval,
            events=tuple(events),
        )

    def run(
        self,
        agent: str,
        *,
        prompt: str,
        session_id: str,
        history: Sequence[dict[str, str]] = (),
        profile: str = "default",
        user_email: str = "",
        timeout: int = 600,
        on_update: Callable[[ChatRunResult], None] | None = None,
    ) -> ChatRunResult:
        prompt = prompt.strip()
        if not prompt or len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise ValueError("chat prompt must be between 1 and 32,000 bytes")
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid agent session ID")
        if not _PROFILE.fullmatch(profile):
            raise ValueError("invalid agent profile")
        if user_email and not _USER_IDENTITY.fullmatch(user_email):
            raise ValueError("invalid portal user identity")
        clean_history = []
        for message in list(history)[-MAX_HISTORY_MESSAGES:]:
            role = str(message.get("role") or "")
            content = str(message.get("content") or "")
            if role not in {"user", "assistant"}:
                continue
            clean_history.append({"role": role, "content": content[:32_000]})
        payload = self._invoke(
            agent,
            {
                "action": "run",
                "profile": profile,
                "prompt": prompt,
                "session_id": session_id,
                "user_email": user_email,
                "history": clean_history,
                "timeout": max(30, min(timeout, 900)),
            },
            timeout=max(40, min(timeout + 20, 920)),
            update_callback=(
                (lambda update: on_update(self._result(update, session_id=session_id)))
                if on_update is not None
                else None
            ),
        )
        return self._result(payload, session_id=session_id)

    def resolve_approval(
        self,
        agent: str,
        *,
        run_id: str,
        choice: str,
        profile: str = "default",
        timeout: int = 600,
    ) -> ChatRunResult:
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid agent run ID")
        if choice not in {"once", "deny"}:
            raise ValueError("portal approvals are limited to once or deny")
        if not _PROFILE.fullmatch(profile):
            raise ValueError("invalid agent profile")
        payload = self._invoke(
            agent,
            {
                "action": "approve",
                "profile": profile,
                "run_id": run_id,
                "choice": choice,
                "timeout": max(30, min(timeout, 900)),
            },
            timeout=max(40, min(timeout + 20, 920)),
        )
        return self._result(payload)

    def stop(self, agent: str, *, run_id: str, profile: str = "default") -> None:
        if not _RUN_ID.fullmatch(run_id) or not _PROFILE.fullmatch(profile):
            raise ValueError("invalid agent run selection")
        self._invoke(
            agent,
            {"action": "stop", "profile": profile, "run_id": run_id},
            timeout=40,
        )
