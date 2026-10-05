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

from kube_agents_bench.board import BOARD_FILE
from kube_agents_bench.discovery import CLUSTER_KEY_PREFIX, SCAN_MARKER
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

__all__ = [
    "COMPLETED_MARKER",
    "DELIVERED_FILE",
    "DELIVERY_JOB_ID",
    "EXECUTIONS_DB",
    "ITEMS_FILE",
    "PRIORITIZE_KEY",
    "RAW_FILE",
    "REPORT_FILE",
    "agent_shell",
    "read_delivery_runs",
    "read_files",
    "read_handoff_board",
    "read_items",
    "read_raw_block",
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


# The sweep's raw report and the parser the prioritization stage runs over it,
# as the sandbox stages them (agents/platform/scripts/inventory_findings.py:
# DEFAULT_RAW_PATH; the sandbox stages the image's scripts tree under the data
# root). The sandbox's own copy is the oracle, so the verdict matches what the
# next stage would make of the file on that install.
RAW_FILE = f"{DATA_ROOT}/INVENTORY.raw.md"
PARSER_DIR = f"{DATA_ROOT}/scripts"
PARSER_MODULE = "inventory_findings"
SANDBOX_PYTHON = "python3"
RAW_READ = "__ONBOARDING_RAW_BLOCK__"
# Parser errors carried into the verdict; the rest are counted.
MAX_PARSER_ERRORS = 5

# bootstrap_scan_gate.py: PRIORITIZE_IDEMPOTENCY_KEY and SCAN_ASSIGNEE, and the
# word every ranking card title the SOP prescribes carries.
PRIORITIZE_KEY = "bootstrap-inventory-prioritize"
PRIORITIZE_ASSIGNEE = "platform"
PRIORITIZE_TITLE_WORD = "Prioritize"
HANDOFF_READ = "__ONBOARDING_HANDOFF_BOARD__"

# Runs in the sandbox. Parses the raw report with the sandbox's own parser and
# prints the items' (cluster, check, object), or the parser's Failure code and
# errors. A raw file that cannot be read or decoded is reported as such; a
# parser that cannot be imported is an install fault, printed as "error".
_RAW_SCRIPT = """
import json, os, sys
raw, parser_dir, module, limit, sentinel = sys.argv[1:6]
out = {"raw": "absent", "items": None, "code": None, "errors": None, "error_count": 0, "error": None}
if os.path.lexists(raw):
    out["raw"] = "present"
    sys.path.insert(0, parser_dir)
    try:
        parser = __import__(module)
    except Exception as exc:
        out["error"] = "cannot import %s from %s: %s" % (module, parser_dir, exc)
    else:
        try:
            with open(raw, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            out["raw"] = "unreadable"
            out["errors"] = [str(exc)]
        else:
            try:
                items = parser.parse_block(text)
            except parser.Failure as exc:
                out["code"] = exc.code
                out["errors"] = exc.errors[: int(limit)]
                out["error_count"] = len(exc.errors)
            else:
                out["items"] = [{k: i.get(k) for k in ("cluster", "check", "object")} for i in items]
print(sentinel)
print(json.dumps(out))
"""

# Runs in the agent container against the board, read-only. Reads the sweep
# named by the gate's marker; every cluster card created at or after it, with
# the cluster and findings count from the metadata of its latest completed
# run; the keyed ranking card; and any ranking-titled platform card without
# the key, so a fail can say the card was filed unkeyed.
_HANDOFF_SCRIPT = """
import json, os, sqlite3, sys
root, board, sentinel, marker, prefix, key, assignee, word = sys.argv[1:9]
SQLITE_BUSY_TIMEOUT = 10
out = {"sweep": None, "clusters": [], "keyed": [], "unkeyed": [], "error": None}


def done(error=None):
    out["error"] = error
    print(sentinel)
    print(json.dumps(out))
    sys.exit(0)


try:
    with open(os.path.join(root, marker)) as fh:
        ids = [line.strip()[len("task_id="):] for line in fh if line.startswith("task_id=")]
except OSError as exc:
    done("no discovery sweep has been filed: %s" % exc)
if not ids or not ids[0]:
    done("%s names no task_id" % marker)
try:
    conn = sqlite3.connect("file:%s/%s?mode=ro" % (root, board), uri=True, timeout=SQLITE_BUSY_TIMEOUT)
    row = conn.execute("SELECT id, status, created_at FROM tasks WHERE id = ?", (ids[0],)).fetchone()
    if row is None:
        done("the sweep card %s named by %s is not on the board" % (ids[0], marker))
    out["sweep"] = {"id": row[0], "status": row[1]}
    since = row[2]
    cards = conn.execute(
        "SELECT id, status FROM tasks WHERE substr(idempotency_key, 1, ?) = ? AND created_at >= ? ORDER BY created_at, id",
        (len(prefix), prefix, since)).fetchall()
    for tid, status in cards:
        card = {"id": tid, "status": status, "cluster": None, "findings": None, "metadata": "none"}
        if status == "done":
            run = conn.execute(
                "SELECT metadata FROM task_runs WHERE task_id = ? AND outcome = 'completed' ORDER BY id DESC LIMIT 1",
                (tid,)).fetchone()
            try:
                meta = json.loads(run[0]) if run and run[0] else None
            except ValueError:
                meta = None
                card["metadata"] = "invalid"
            if isinstance(meta, dict):
                card["metadata"] = "present"
                cluster = meta.get("cluster")
                card["cluster"] = cluster if isinstance(cluster, str) and cluster else None
                findings = meta.get("findings")
                card["findings"] = len(findings) if isinstance(findings, list) else None
        out["clusters"].append(card)
    out["keyed"] = [
        {"id": tid, "status": status}
        for tid, status in conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? AND created_at >= ? ORDER BY created_at, id",
            (key, since))
    ]
    out["unkeyed"] = [
        {"id": tid, "status": status, "title": title, "key": ikey}
        for tid, status, title, ikey in conn.execute(
            "SELECT id, status, title, idempotency_key FROM tasks WHERE assignee = ? AND created_at >= ? "
            "AND instr(title, ?) > 0 AND (idempotency_key IS NULL OR idempotency_key != ?) ORDER BY created_at, id",
            (assignee, since, word, key))
    ]
    conn.close()
except sqlite3.Error as exc:
    done("kanban board: %s" % exc)
done()
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


def raw_command() -> str:
    """The ``sh -c`` line that parses the raw report in the sandbox with the sandbox's parser."""
    args = " ".join(
        shlex.quote(a) for a in [RAW_FILE, PARSER_DIR, PARSER_MODULE, str(MAX_PARSER_ERRORS), RAW_READ]
    )
    return f"{SANDBOX_PYTHON} -c {shlex.quote(_RAW_SCRIPT)} {args}"


def _payload(reply: str, sentinel: str) -> dict[str, Any] | None:
    marker = reply.rfind(sentinel)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(sentinel) :])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def read_raw_block(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    """What the sandbox's parser makes of the raw report, or ``None`` if the read failed.

    ``"raw"`` is ``absent``, ``unreadable`` or ``present``; a present file
    carries ``"items"`` or the parser's ``"code"`` and ``"errors"``. An
    ``"error"`` is a parser that could not be imported. ``shell`` is
    :func:`sandbox_shell`, a parameter so the tests can run it locally.
    """
    return _payload(shell(raw_command(), timeout), RAW_READ)


def handoff_command() -> str:
    """The ``sh -c`` line that reads the sweep's hand-off off the agent's board."""
    args = " ".join(
        shlex.quote(a)
        for a in [
            DATA_ROOT,
            BOARD_FILE,
            HANDOFF_READ,
            SCAN_MARKER,
            CLUSTER_KEY_PREFIX,
            PRIORITIZE_KEY,
            PRIORITIZE_ASSIGNEE,
            PRIORITIZE_TITLE_WORD,
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_HANDOFF_SCRIPT)} {args}'
    )


def read_handoff_board(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    """The sweep, its cluster cards and the ranking cards, or ``None`` if the read failed.

    An ``"error"`` is no sweep marker, a sweep the board does not know, or a
    board that cannot be queried, read from a pod that answered. ``shell`` is
    :func:`agent_shell`, a parameter so the tests can run it locally.
    """
    parsed = _payload(shell(handoff_command(), timeout), HANDOFF_READ)
    if parsed is None or not isinstance(parsed.get("clusters"), list):
        return None
    return parsed
