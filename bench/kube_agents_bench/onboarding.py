# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read the onboarding stages' files off the shell sandbox and the agent pod.

The prioritization card's worker runs ``inventory_findings.py`` through its
terminal, and with the shell sandbox on that terminal is the sandbox pod: the
files it writes land on the sandbox's data volume, which the agent pod does
not mount. ``harness._agent_shell`` execs into the agent's Service, so it
cannot see them. :func:`sandbox_shell` execs into the sandbox pod instead,
named the way ``hack/ci-eval-pr.sh`` names it. The delivery job runs in the
agent pod and writes its marker there, which :func:`agent_shell` reads, along
with the scheduler's record of the job's runs.

A reply without a sentinel is a failed read, never an empty one: both shells
return ``""`` on any kubectl failure.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from collections.abc import Callable, Collection
from typing import Any, Literal

from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

__all__ = [
    "COMPLETED_MARKER",
    "DELIVERED_FILE",
    "DELIVERY_JOB_ID",
    "EXECUTIONS_DB",
    "ITEMS_FILE",
    "REPORT_FILE",
    "agent_shell",
    "read_delivery_runs",
    "read_files",
    "read_items",
    "sandbox_pod",
    "sandbox_shell",
]

_log = logging.getLogger(__name__)

# How much of a failed exec's stderr the debug log keeps.
_STDERR_LOG_CHARS = 200

# The operator's StatefulSet for the agent is `<agent>-shell` with one replica
# (shellSandboxName in k8s-operator/internal/controller/shell_sandbox_manifests.go).
SANDBOX_POD_SUFFIX = "-shell-0"
SANDBOX_CONTAINER = "shell"
DEFAULT_AGENT_SERVICE = "platform-agent"
DEFAULT_AGENT_NAMESPACE = "kubeagents-system"
DEFAULT_AGENT_CONTAINER = "platform-agent"

# agents/platform/scripts/inventory_findings.py: DEFAULT_ITEMS_PATH.
ITEMS_FILE = f"{DATA_ROOT}/INVENTORY.items.json"
ITEMS_PRESENT = "__INVENTORY_ITEMS_PRESENT__"
ITEMS_ABSENT = "__INVENTORY_ITEMS_ABSENT__"
# Far above what a first scan extracts. A file past it fails the check rather
# than streaming an unbounded file through kubectl.
MAX_ITEMS_BYTES = 1 << 20

ItemsState = Literal["present", "absent", "error"]

# agents/chat/scripts/bootstrap_delivery.py: the marker it writes on the agent
# pod when it claims the report, and the sandbox names it reads and archives.
COMPLETED_MARKER = f"{DATA_ROOT}/.bootstrap_completed"
REPORT_FILE = f"{DATA_ROOT}/INVENTORY.md"
DELIVERED_FILE = f"{DATA_ROOT}/INVENTORY.delivered.md"
FILES_READ = "__ONBOARDING_FILES_READ__"
FILE_PRESENT = "present"
FILE_ABSENT = "absent"

# The scheduler's record of each run of the delivery job (bootstrap_delivery.py:
# DELIVERY_JOB_ID), which Hermes keeps in the agent pod's cron store. Read with
# the agent's own interpreter, falling back to python3 as the other reads do:
# the agent image ships no sqlite3 binary.
DELIVERY_JOB_ID = "bootstrap-inventory-delivery"
EXECUTIONS_DB = f"{DATA_ROOT}/cron/executions.db"
RUNS_READ = "__ONBOARDING_RUNS_READ__"
# The job ticks every minute, so this reaches back several hours from the
# newest run: far past the claim in any case that just ran.
MAX_RUNS = 500

# Prints the marker's mtime and every run of the job whose window, claimed_at
# to finished_at, holds it: the run that took the claim. A run still going has
# no finished_at. A stat, sqlite or timestamp failure is printed as "error"
# rather than raised, so the verdict names it instead of reading as an
# unreachable pod.
_RUNS_SCRIPT = """
import json, os, sqlite3, sys
from datetime import datetime
marker, db, job, limit, sentinel = sys.argv[1:6]
SQLITE_BUSY_TIMEOUT = 10
out = {"marker": None, "runs": [], "error": None}
try:
    out["marker"] = os.stat(marker).st_mtime
except FileNotFoundError:
    pass
except OSError as exc:
    out["error"] = str(exc)
rows = []
if out["marker"] is not None:
    try:
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)
        rows = con.execute(
            "SELECT status, claimed_at, finished_at, error, delivery_outcome FROM executions"
            " WHERE job_id = ? ORDER BY claimed_at DESC LIMIT ?", (job, int(limit))).fetchall()
    except sqlite3.Error as exc:
        out["error"] = "%s: %s" % (db, exc)
    for status, claimed, finished, error, outcome in rows:
        try:
            if not claimed or datetime.fromisoformat(claimed).timestamp() > out["marker"]:
                continue
            if finished and datetime.fromisoformat(finished).timestamp() < out["marker"]:
                continue
        except (TypeError, ValueError) as exc:
            out["error"] = "%s: run claimed_at %r, finished_at %r: %s" % (db, claimed, finished, exc)
            break
        out["runs"].append({"status": status, "claimed_at": claimed, "finished_at": finished,
                            "error": error, "delivery_outcome": outcome})
print(sentinel)
print(json.dumps(out))
"""


def sandbox_pod() -> str:
    """The sandbox pod: ``EVAL_SANDBOX_POD``, else ``<AGENT_SERVICE_NAME>-shell-0``."""
    agent = os.environ.get("AGENT_SERVICE_NAME", DEFAULT_AGENT_SERVICE)
    return os.environ.get("EVAL_SANDBOX_POD") or f"{agent}{SANDBOX_POD_SUFFIX}"


def _kubectl_exec(target: str, container: str, script: str, timeout: float) -> str:
    cmd = ["kubectl", "exec", target, "-n", os.environ.get("AGENT_NAMESPACE", DEFAULT_AGENT_NAMESPACE)]
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    cmd.extend(["-c", container, "--", "sh", "-c", script])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("kubectl exec into %s failed: %s", target, exc)
        return ""
    if proc.returncode != 0:
        _log.debug("kubectl exec into %s exited %d: %s", target, proc.returncode, proc.stderr.strip()[:_STDERR_LOG_CHARS])
        return ""
    return proc.stdout


def sandbox_shell(script: str, timeout: float) -> str:
    """Run ``script`` in the sandbox's shell container and return its stdout.

    Best effort, as ``harness._agent_shell`` is: a missing binary, an
    unreachable cluster or a non-zero exit all return ``""``.
    """
    return _kubectl_exec(f"pod/{sandbox_pod()}", SANDBOX_CONTAINER, script, timeout)


def agent_shell(script: str, timeout: float) -> str:
    """Run ``script`` in the agent container, as ``harness._agent_shell`` does.

    Through ``_kubectl_exec``, like ``sandbox_shell``, so the onboarding
    verifiers reach both pods the same way. Best effort: any failure returns
    ``""``.
    """
    agent = os.environ.get("AGENT_SERVICE_NAME", DEFAULT_AGENT_SERVICE)
    container = os.environ.get("AGENT_CONTAINER", DEFAULT_AGENT_CONTAINER)
    return _kubectl_exec(f"svc/{agent}", container, script, timeout)


def items_command() -> str:
    """The ``sh -c`` line that prints a sentinel, then the items file if there is one."""
    return (
        f'f={shlex.quote(ITEMS_FILE)}; if [ -f "$f" ]; then echo {ITEMS_PRESENT}; '
        f'head -c {MAX_ITEMS_BYTES + 1} "$f"; else echo {ITEMS_ABSENT}; fi'
    )


def read_items(shell: Callable[[str, float], str], timeout: float) -> tuple[ItemsState, str, str]:
    """What ``extract`` wrote on the sandbox, as ``(state, text, why)``.

    ``absent`` means the pod answered and has no items file; ``error`` means
    it could not be read. ``present`` returns the file's text unparsed, cut
    one byte past :data:`MAX_ITEMS_BYTES`: what the worker wrote is the
    verifier's to judge. ``shell`` is :func:`sandbox_shell`, a parameter so
    the tests can fake it.
    """
    reply = shell(items_command(), timeout)
    marker = reply.find(ITEMS_PRESENT)
    if marker < 0:
        if reply.strip() == ITEMS_ABSENT:
            return "absent", "", f"there is no {ITEMS_FILE} on {sandbox_pod()}"
        return "error", "", f"{sandbox_pod()} could not be read (kubectl exec failed or the command did not run)"
    return "present", reply[marker + len(ITEMS_PRESENT) :].lstrip("\n"), ""


def files_command(paths: list[str], links: Collection[str] = ()) -> str:
    """The ``sh -c`` line that prints ``present`` or ``absent`` and the path, per path.

    A path in ``links`` is also present as a dangling symlink.
    """
    tests = []
    for path in paths:
        quoted = shlex.quote(path)
        exists = f"[ -e {quoted} ] || [ -L {quoted} ]" if path in links else f"[ -e {quoted} ]"
        tests.append(f'if {exists}; then echo "{FILE_PRESENT}" {quoted}; else echo "{FILE_ABSENT}" {quoted}; fi')
    return "; ".join([*tests, f"echo {FILES_READ}"])


def read_files(
    shell: Callable[[str, float], str], paths: list[str], timeout: float, links: Collection[str] = ()
) -> dict[str, bool] | None:
    """Which of ``paths`` exist where ``shell`` runs, or ``None`` if the read failed.

    A path in ``links`` that is a dangling symlink exists. A reply missing the
    closing sentinel, or a path, is a failed read.
    """
    lines = shell(files_command(paths, links), timeout).splitlines()
    if not lines or lines[-1].strip() != FILES_READ:
        return None
    seen: dict[str, bool] = {}
    for line in lines[:-1]:
        state, _, path = line.partition(" ")
        if state in (FILE_PRESENT, FILE_ABSENT):
            seen[path] = state == FILE_PRESENT
    if set(seen) != set(paths):
        return None
    return seen


def runs_command() -> str:
    """The ``sh -c`` line that runs the executions read in the agent container."""
    args = " ".join(shlex.quote(a) for a in [COMPLETED_MARKER, EXECUTIONS_DB, DELIVERY_JOB_ID, str(MAX_RUNS), RUNS_READ])
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_RUNS_SCRIPT)} {args}'
    )


def read_delivery_runs(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    """The claim marker's mtime and the delivery runs that span it, or ``None`` if the read failed.

    A ``"marker"`` of ``None`` means there is no marker. An ``"error"``
    is a marker that could not be stat'd, the store's sqlite failure, or a
    run timestamp that does not parse, read from a pod that answered. ``shell`` is
    :func:`agent_shell`, a parameter so the tests can fake it.
    """
    reply = shell(runs_command(), timeout)
    marker = reply.rfind(RUNS_READ)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(RUNS_READ) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("runs"), list):
        return None
    return parsed
