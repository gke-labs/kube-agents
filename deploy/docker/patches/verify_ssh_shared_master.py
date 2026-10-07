#!/usr/bin/env python3
"""Build gate for the ssh shared-master patch (#2174).

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after ``apply_ssh_shared_master.py``.
The applier proves its anchors matched once; this imports the patched modules and proves they
behave: a shared environment's ``cleanup()`` runs no ``ssh -O exit`` and ``close_master()`` does,
a probe's ``cleanup()`` still closes its private master, eviction calls ``close_master()`` after
``cleanup()``, and the terminal result carries the hint only for an ssh exit 255 without the cwd
marker.

Usage::

    cd /opt/hermes && python3 verify_ssh_shared_master.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
FAILURES: list[str] = []
MARKER = "kube-agents patch: ssh_shared_master"


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _import(name: str):
    """Import *name* from HERMES, dropping any ``tools`` module already loaded from elsewhere."""
    if sys.path[:1] != [str(HERMES)]:
        sys.path.insert(0, str(HERMES))
        for loaded in [m for m in sys.modules if m == "tools" or m.startswith("tools.")]:
            del sys.modules[loaded]
    try:
        return __import__(name, fromlist=["_"])
    except Exception as exc:  # noqa: BLE001 - a build gate reports, never tracebacks
        fail(f"cannot import {name}: {exc}")
        return None


def _exit_calls(run_mock) -> list[list[str]]:
    return [c.args[0] for c in run_mock.call_args_list if "-O" in c.args[0] and "exit" in c.args[0]]


def check_ssh(ssh) -> None:
    cls = ssh.SSHEnvironment
    if not callable(getattr(cls, "close_master", None)):
        fail("SSHEnvironment has no close_master()")
        return
    with tempfile.TemporaryDirectory() as tmp:
        def env(shared: bool):
            e = cls.__new__(cls)
            e.user, e.host, e.port = "agent", "sandbox", 2222
            e._sync_manager = None
            e._shared_master = shared
            e.control_socket = Path(tmp) / "0123456789abcdef.sock"
            e.control_socket.touch()
            return e

        with mock.patch.object(ssh.subprocess, "run") as run:
            e = env(shared=True)
            e.cleanup()
            if _exit_calls(run) or not e.control_socket.exists():
                fail("a shared environment's cleanup() still closes the master")
            e.close_master()
            if len(_exit_calls(run)) != 1 or e.control_socket.exists():
                fail("close_master() did not run `ssh -O exit` once and unlink the socket")
        with mock.patch.object(ssh.subprocess, "run") as run:
            env(shared=False).cleanup()
            if len(_exit_calls(run)) != 1:
                fail("a probe's cleanup() no longer closes its private master")


def check_lifecycle(lifecycle, terminal_tool) -> None:
    calls: list[str] = []

    class Env:
        def cleanup(self):
            calls.append("cleanup")

        def close_master(self):
            calls.append("close_master")

    task = "verify-ssh-shared-master"
    with terminal_tool._env_lock:
        terminal_tool._active_environments[task] = Env()
    try:
        lifecycle._evict_environment_for_task(task)
    finally:
        with terminal_tool._env_lock:
            terminal_tool._active_environments.pop(task, None)
    if calls != ["cleanup", "close_master"]:
        fail(f"eviction called {calls}, expected cleanup then close_master")


def check_result(result_mod) -> None:
    def hint(res: dict, env_type: str = "ssh") -> str | None:
        out = result_mod.finalize_foreground_result(
            command="sleep 60", result=res, env=None, env_type=env_type, effective_task_id="t",
            task_id="t", session_id="s", session_key="verify-ssh-shared-master", workdir=None,
            command_cwd="/tmp", approval_note=None)
        return json.loads(out).get("hint")

    cut = hint({"output": "", "returncode": 255})
    if not cut or "closed under this command" not in cut:
        fail(f"ssh exit 255 without the marker carries no hint: {cut!r}")
    if hint({"output": "", "returncode": 255, "cwd_observed": True, "cwd": "/tmp"}) is not None:
        fail("a command's own exit 255 (marker present) got the hint")
    if hint({"output": "", "returncode": 255}, env_type="local") is not None:
        fail("a local exit 255 got the ssh hint")
    if hint({"output": "", "returncode": 0}) is not None:
        fail("exit 0 got a hint")
    timeout = hint({"output": "", "returncode": 124})
    if not timeout or "Exit 124" not in timeout:
        fail(f"the upstream exit 124 hint is gone: {timeout!r}")


def main() -> int:
    for rel in ("tools/environments/ssh.py", "tools/terminal_tool_lifecycle.py", "tools/terminal_tool_result.py"):
        path = HERMES / rel
        if not path.is_file() or MARKER not in path.read_text(encoding="utf-8"):
            fail(f"{rel} does not carry the patch marker")
    ssh = _import("tools.environments.ssh")
    lifecycle = _import("tools.terminal_tool_lifecycle")
    terminal_tool = _import("tools.terminal_tool")
    result_mod = _import("tools.terminal_tool_result")
    if ssh is not None:
        check_ssh(ssh)
    if lifecycle is not None and terminal_tool is not None:
        check_lifecycle(lifecycle, terminal_tool)
    if result_mod is not None:
        check_result(result_mod)
    if FAILURES:
        for msg in FAILURES:
            print(f"verify_ssh_shared_master: {msg}", file=sys.stderr)
        return 1
    print("verify_ssh_shared_master: ok (cleanup leaves the shared master; close_master, eviction and the 255 hint behave)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
