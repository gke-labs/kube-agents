"""Reusable isolated admin portal lifecycle for live CUJ tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from kube_agents_bench.cuj import PortalTransport as Portal
from kube_agents_bench.cuj import PortalTransportError as PortalError

REPO_ROOT = Path(__file__).resolve().parents[3]


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


@contextmanager
def isolated_portal(output: Path) -> Iterator[str]:
    """Run an API-only portal on an OS-assigned port for one CUJ test."""

    output.mkdir(parents=True, exist_ok=True)
    account = active_gcloud_account()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}/api/v1"
    environment = os.environ.copy()
    environment.update(
        {
            "KUBE_AGENTS_ADMIN_USER": account,
            "KUBE_AGENTS_ADMIN_INTERACTION_STATE": str(output / "portal.db"),
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
