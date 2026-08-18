"""Reusable admin portal lifecycle and HTTP client for live CUJ tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPO_ROOT = Path(__file__).resolve().parents[2]


class PortalError(RuntimeError):
    pass


class Portal:
    def __init__(self, endpoint: str) -> None:
        parsed = urllib.parse.urlsplit(endpoint.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("endpoint must be an absolute HTTP(S) URL")
        self.endpoint = endpoint.rstrip("/")

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, payload)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.endpoint}/{path.lstrip('/')}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read(8192).decode(errors="replace")
            raise PortalError(f"portal returned HTTP {exc.code}: {detail}") from exc
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise PortalError(f"portal request failed: {exc}") from exc
        if not isinstance(result, dict):
            raise PortalError("portal returned a non-object response")
        return result


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
