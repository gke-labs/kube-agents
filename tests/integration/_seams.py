"""Shared fixtures for the integration seam tests.

The tier's contract (testing strategy §4.1b): real components wired together,
the agent replaced by a fake, no model calls, deterministic. Everything here
serves that — a real `session_kv_server` in a subprocess with a controlled
environment, fake executables that record their argv, and small stub HTTP
servers that stand in for the one component deliberately not under test.

The KV server runs as a subprocess rather than in-thread because its quota
table and platform detection are configured from environment variables read at
module import; a subprocess gives every test its own configuration without
module-reload games, and it is also exactly how the entrypoint runs it in the
pod (uvicorn, backgrounded).
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "agents" / "platform" / "scripts"

API_KEY = "integration-test-key"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=15.0, interval=0.1, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}")


def http_json(url, payload=None, token=API_KEY, method=None, timeout=10.0):
    """One JSON round-trip; returns (status, parsed-body-or-None)."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=body, headers=headers, method=method or ("POST" if body else "GET")
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except Exception:
            return error.code, None


class KVServer:
    """The real session_kv_server, on a loopback port, in a subprocess."""

    def __init__(self, tmp_path: Path, env: dict | None = None, path_prepend: str = ""):
        self.port = free_port()
        self.db_path = str(tmp_path / "session_kv.db")
        self.url = f"http://127.0.0.1:{self.port}"
        run_env = dict(os.environ)
        run_env.update(
            {
                "SESSION_KV_DB_PATH": self.db_path,
                "SESSION_KV_API_KEY": API_KEY,
                "PLATFORM_AGENT_CONFIG_PATH": str(tmp_path / "absent-config.yaml"),
                "PLATFORM_AGENT_DOTENV_PATH": str(tmp_path / "absent.env"),
                "PYTHONPATH": str(SCRIPTS_DIR),
            }
        )
        # An absent config file gets `get_active_platform` past the yaml branch
        # but not to a fixed answer: it then reads SLACK_BOT_TOKEN from the
        # environment and returns "slack" if it finds one
        # (`session_kv_server.py:431`). Dropping the chat credentials is what
        # makes the fallback deterministic, and it also means a server started
        # here holds no token to post with.
        for leaked in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "GOOGLE_CHAT_WEBHOOK"):
            run_env.pop(leaked, None)
        # And a `hermes` that cannot reach a workspace, shadowing any real one
        # on the runner's PATH. `_post_initial_alert` shells out to a bare
        # `hermes send --json --to <platform>` (`session_kv_server.py:439-445`)
        # on every Critical inject, resolved through this env's PATH, so a
        # maintainer running `make test-integration` on a configured
        # workstation would otherwise post an actual alert into the real
        # workspace. Every seam test that drives an inject gets this whether it
        # asked for it or not; a test that wants to observe the call installs
        # its own recording fake through `path_prepend`, which is searched
        # first.
        guard_dir = tmp_path / "_seam-path-guard"
        fake_executable(
            guard_dir,
            "hermes",
            """
            import sys
            sys.stderr.write(
                "hermes: refusing to send from a seam test -- this process was "
                "started by tests/integration/_seams.py:KVServer, which shadows "
                "the real hermes. A test that needs the call recorded should "
                "pass its own fake via path_prepend.\\n"
            )
            sys.exit(127)
            """,
        )
        if env:
            run_env.update(env)
        # PATH is assembled last, after the caller's `env`, so that a test
        # setting PATH explicitly still gets the guard in front of whatever it
        # set. Nothing here should be able to opt back into the real hermes by
        # accident.
        prepends = [p for p in (path_prepend, str(guard_dir)) if p]
        run_env["PATH"] = os.pathsep.join(prepends + [run_env.get("PATH", "")])
        self.log = open(tmp_path / "kv-server.log", "wb")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "session_kv_server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=str(SCRIPTS_DIR),
            env=run_env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_until(self._healthy, message=f"session_kv_server on :{self.port}")
        except AssertionError:
            self.stop()
            raise

    def _healthy(self) -> bool:
        if self.process.poll() is not None:
            raise AssertionError(
                "session_kv_server exited early; log:\n"
                + Path(self.log.name).read_text(errors="replace")
            )
        try:
            status, _ = http_json(f"{self.url}/healthz", token=None, timeout=2.0)
            return status == 200
        except Exception:
            return False

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        self.log.close()


def fake_executable(bin_dir: Path, name: str, script_body: str) -> Path:
    """Install an argv-recording fake executable onto a PATH directory.

    `script_body` is a Python program; sys.argv[1:] are the fake's arguments.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(script_body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class RecordingHTTPServer:
    """A stub HTTP server that records every request and replays canned replies.

    Stands in for the one component a seam test deliberately fakes (the hermes
    gateway, mostly). `responses` maps a path prefix to (status, body-dict).
    """

    def __init__(self, responses: dict[str, tuple[int, dict]] | None = None):
        self.requests: list[dict] = []
        self.responses = responses or {}
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                try:
                    parsed = json.loads(body) if body else None
                except Exception:
                    parsed = None
                recorder.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "body": parsed,
                        "headers": dict(self.headers),
                    }
                )
                status, reply = 200, {"status": "ok"}
                for prefix, canned in recorder.responses.items():
                    if self.path.startswith(prefix):
                        status, reply = canned
                        break
                payload = json.dumps(reply).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self, method=None):
        return [
            r["path"]
            for r in self.requests
            if method is None or r["method"] == method
        ]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
