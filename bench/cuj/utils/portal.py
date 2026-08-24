"""Reusable isolated admin portal lifecycle for live CUJ tests."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from kube_agents_bench.cuj import PortalTransport as Portal
from kube_agents_bench.cuj import PortalTransportError as PortalError

from cuj.utils.evidence import EvidenceLog

REPO_ROOT = Path(__file__).resolve().parents[3]
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}
CANONICAL_AGENT_ID = "platform-agent"
PORTAL_API_TOKEN_ENV = "KUBE_AGENTS_PORTAL_API_TOKEN"


def portal_token() -> str:
    """The per-launch API token shared with the isolated portal process."""

    return os.environ.get(PORTAL_API_TOKEN_ENV, "")


def configured_agent_profiles() -> tuple[tuple[str, str], ...]:
    """Return the canonical agent/profile pair shared by all collected CUJs."""

    profile = os.environ.get("CUJ_PROFILE", "default").strip() or "default"
    return ((CANONICAL_AGENT_ID, profile),)


def active_gcloud_account() -> str:
    try:
        result = subprocess.run(
            [
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=value(account)",
                "--limit=1",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PortalError(
            f"could not inspect the active gcloud account: {exc}"
        ) from exc
    account = result.stdout.strip()
    if not account:
        raise PortalError("no active gcloud account; run `gcloud auth login`")
    return account


def wait_for_portal(
    endpoint: str,
    process: subprocess.Popen,
    timeout: float = 30,
) -> None:
    ready_url = endpoint.removesuffix("/api/v1") + "/readyz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PortalError(
                "portal stopped during startup with exit code "
                f"{process.returncode}"
            )
        try:
            with urllib.request.urlopen(ready_url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise PortalError("portal did not become ready within 30 seconds")


def verify_agent(
    endpoint: str,
    agent_id: str,
    log: EvidenceLog,
    *,
    profile: str = "default",
    timeout: float = 120,
) -> Path:
    """Require a discovered agent to complete a minimal portal interaction."""

    portal = Portal(endpoint, token=portal_token())
    discovered = portal.get("agents").get("agents", [])
    log.record("prerequisite_agents", discovered)
    if agent_id not in discovered:
        raise PortalError(
            f"agent {agent_id!r} is not live in the admin portal; "
            f"discovered agents: {discovered!r}; evidence: {log.path}"
        )

    request = {
        "agentId": agent_id,
        "profile": profile,
        "sessionId": f"portal_prerequisite_{uuid.uuid4().hex}",
        "input": {
            "text": "Reply with exactly READY and nothing else. Do not use tools."
        },
        "history": [],
    }
    log.record("prerequisite_request", request)
    interaction = portal.post("interactions", request)
    log.record("prerequisite_interaction", interaction)
    interaction_id = str(interaction.get("interactionId") or "")
    if not interaction_id:
        raise PortalError(
            f"agent prerequisite response omitted interactionId; evidence: {log.path}"
        )

    deadline = time.monotonic() + timeout
    while str(interaction.get("status") or "") not in TERMINAL_STATUSES:
        if time.monotonic() >= deadline:
            raise PortalError(
                f"agent {agent_id!r} did not respond within {timeout:g} seconds; "
                f"evidence: {log.path}"
            )
        time.sleep(1)
        interaction = portal.get(
            f"interactions/{urllib.parse.quote(interaction_id, safe='')}"
        )
        log.record("prerequisite_interaction", interaction)

    status = str(interaction.get("status") or "unknown")
    response = str(interaction.get("output") or "").strip()
    if status != "completed" or response != "READY":
        error = str(interaction.get("error") or "no terminal error reported")
        raise PortalError(
            f"agent {agent_id!r} profile {profile!r} is not responsive through "
            "the admin portal: "
            f"status={status}, error={error}, output={response!r}; "
            f"evidence: {log.path}"
        )
    return log.path


@contextmanager
def isolated_portal(output: Path) -> Iterator[str]:
    """Run an API-only portal on an OS-assigned port for one CUJ test."""

    output.mkdir(parents=True, exist_ok=True)
    account = active_gcloud_account()
    token = secrets.token_urlsafe(32)
    os.environ[PORTAL_API_TOKEN_ENV] = token
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}/api/v1"
    environment = os.environ.copy()
    environment.update(
        {
            "KUBE_AGENTS_ADMIN_USER": account,
            "KUBE_AGENTS_ADMIN_INTERACTION_STATE": str(output / "portal.db"),
            PORTAL_API_TOKEN_ENV: token,
        }
    )
    log = (output / "portal.log").open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--factory",
                "admin_console.api.app:create_app",
                "--fd",
                str(listener.fileno()),
                "--workers=1",
                "--no-access-log",
            ],
            cwd=REPO_ROOT,
            env=environment,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    except BaseException:
        listener.close()
        log.close()
        raise
    listener.close()
    try:
        wait_for_portal(endpoint, process)
        yield endpoint
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()
