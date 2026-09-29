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

"""Read the onboarding prioritization stage's files off the shell sandbox.

The prioritization card's worker runs ``inventory_findings.py`` through its
terminal, and with the shell sandbox on that terminal is the sandbox pod: the
files it writes land on the sandbox's data volume, which the agent pod does
not mount. ``harness._agent_shell`` execs into the agent's Service, so it
cannot see them. :func:`sandbox_shell` execs into the sandbox pod instead,
named the way ``hack/ci-eval-pr.sh`` names it.

A reply without a sentinel is a failed read, never an empty one:
:func:`sandbox_shell` returns ``""`` on any kubectl failure.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Callable
from typing import Literal

__all__ = ["ITEMS_FILE", "read_items", "sandbox_pod", "sandbox_shell"]

_log = logging.getLogger(__name__)

# The operator's StatefulSet for the agent is `<agent>-shell` with one replica
# (shellSandboxName in k8s-operator/internal/controller/shell_sandbox_manifests.go).
SANDBOX_POD_SUFFIX = "-shell-0"
SANDBOX_CONTAINER = "shell"
DEFAULT_AGENT_SERVICE = "platform-agent"
DEFAULT_AGENT_NAMESPACE = "kubeagents-system"

# agents/platform/scripts/inventory_findings.py: DEFAULT_ITEMS_PATH.
ITEMS_FILE = "/opt/data/INVENTORY.items.json"
ITEMS_PRESENT = "__INVENTORY_ITEMS_PRESENT__"
ITEMS_ABSENT = "__INVENTORY_ITEMS_ABSENT__"
# Far above what a first scan extracts. A file past it fails the check rather
# than streaming an unbounded file through kubectl.
MAX_ITEMS_BYTES = 1 << 20

ItemsState = Literal["present", "absent", "error"]


def sandbox_pod() -> str:
    """The sandbox pod: ``EVAL_SANDBOX_POD``, else ``<AGENT_SERVICE_NAME>-shell-0``."""
    agent = os.environ.get("AGENT_SERVICE_NAME", DEFAULT_AGENT_SERVICE)
    return os.environ.get("EVAL_SANDBOX_POD") or f"{agent}{SANDBOX_POD_SUFFIX}"


def sandbox_shell(script: str, timeout: float) -> str:
    """Run ``script`` in the sandbox's shell container and return its stdout.

    Best effort, as ``harness._agent_shell`` is: a missing binary, an
    unreachable cluster or a non-zero exit all return ``""``.
    """
    cmd = [
        "kubectl",
        "exec",
        f"pod/{sandbox_pod()}",
        "-n",
        os.environ.get("AGENT_NAMESPACE", DEFAULT_AGENT_NAMESPACE),
    ]
    context = os.environ.get("AGENT_CLUSTER_CONTEXT")
    if context:
        cmd.extend(["--context", context])
    cmd.extend(["-c", SANDBOX_CONTAINER, "--", "sh", "-c", script])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("kubectl exec into the sandbox failed: %s", exc)
        return ""
    if proc.returncode != 0:
        _log.debug("kubectl exec into the sandbox exited %d: %s", proc.returncode, proc.stderr.strip()[:200])
        return ""
    return proc.stdout


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
