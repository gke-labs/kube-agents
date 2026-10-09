"""Stop one environment's teardown from closing the ssh master every environment shares (#2174).

Hermes derives the ssh ``ControlPath`` from ``sha256(user@host:port)`` and this install publishes
one of each, so the front door, every kanban worker and every cron turn in the pod ride one
master (a delegate child too, if it inherits the ssh backend). ``SSHEnvironment.cleanup()`` runs
``ssh -O exit`` on it. The callers that reach the shared environment include the process-exit
sweep (``cleanup_all_environments``, so a kanban worker subprocess that ran a command and exited
normally), the environment's ``__del__`` and the idle reaper; the per-turn ``cleanup_vm`` pops the
turn's task id (a fresh uuid in a worker, the session id in the gateway) while the terminal tool
registers the environment under ``session:<key>`` or ``default``, so it misses. A command another
environment is running when one of them fires dies as a mux client whose master went away: exit
255, nothing printed, no cwd marker. Hermes ``main`` still had the shared path on 2026-10-07; the
``cleanup()`` half of this patch goes when a Hermes bump derives ``ControlPath`` per environment,
and the hint stays useful either way.

Four anchored edits in two files:

- ``ssh.py``: ``cleanup()`` keeps ``sync_back`` and no longer closes the master; the exit loop
  becomes ``close_master()``. ``ControlPersist=300`` reaps an idle master and the far side is a
  StatefulSet pod, so nothing is lost. A prompt-time probe's master is private (its own socket)
  and ``cleanup()`` still closes that one.
- ``ssh.py``: ``_build_ssh_command()`` adds ``ServerAliveInterval=15`` and ``ServerAliveCountMax=3``.
  The process exit used to be what cleared a master whose peer died without a FIN or RST (an
  evicted sandbox pod); now three missed replies, about 60 s, drop it and the next command opens
  a fresh master. On the command line because that outranks every config file, so no file under
  the runtime home, and no upstream option placed after it, can shadow it; the verifier asks
  ``ssh -G`` what the argv resolves to, so a copy placed earlier with another value fails the
  build in any spelling ssh accepts. Same pair as
  ``agents/platform/scripts/sandbox_exec.py`` and ``deploy/shared/sandbox_mirror.py``; it also
  caps how long a multiplexed command rides out a silent sandbox, where the sandbox's sshd allows
  the client five minutes the other way.
- ``terminal_tool_result.py``: a foreground ssh result with exit 255 and no cwd marker gets a ``hint``, the
  way exit 124 has one, unless upstream already attached a hint to the output (``Permission
  denied``). The wrapper prints the marker after the command and exits with its code, so a
  command's own 255 carries the marker (unless the command text keeps the marker from being
  printed: a top-level ``exit`` or ``exec``, or a failing command under ``set -e``, ends the
  wrapper shell first, and a closed stdout drops the printf) and a cut connection, or one ssh
  never opened, does not.

The eviction path (``_evict_environment_for_task``) is left alone: at v2026.9.14 nothing reaches it
with a registered ssh environment, because a connection failure during construction fires before
registration and the sync, foreground and background-spawn paths catch their own errors.

The two files are substituted first and written last, so a moved anchor leaves none of them
changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

MARKER = "kube-agents patch: ssh_shared_master"
PREFIX = "ssh-shared-master"

SSH_RELATIVE = "tools/environments/ssh.py"
RESULT_RELATIVE = "tools/terminal_tool_result.py"

SSH_INIT_ANCHOR = (
    "        _socket_id = hashlib.sha256(socket_key.encode()).hexdigest()[:16]\n"
)
SSH_INIT_PATCHED = SSH_INIT_ANCHOR + (
    f"        self._shared_master = not probe_only  # {MARKER}\n"
)

SSH_ARGV_ANCHOR = (
    '        cmd.extend(["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"])\n'
)
SSH_ARGV_PATCHED = SSH_ARGV_ANCHOR + (
    f'        cmd.extend(["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"])  # {MARKER}\n'
)

SSH_CLEANUP_ANCHOR = (
    "    def cleanup(self):\n"
    "        if self._sync_manager:\n"
    '            logger.info("SSH: syncing files from sandbox...")\n'
    "            self._sync_manager.sync_back()\n"
    "        for socket in self._control_sockets():\n"
    "            if not socket.exists():\n"
    "                continue\n"
    "            with contextlib.suppress(OSError, subprocess.SubprocessError):\n"
    '                cmd = ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", f"{self.user}@{self.host}"]\n'
    "                subprocess.run(cmd, capture_output=True, timeout=5, stdin=subprocess.DEVNULL)\n"
    "            with contextlib.suppress(OSError):\n"
    "                socket.unlink()\n"
)
SSH_CLEANUP_PATCHED = (
    f"    def cleanup(self):  # {MARKER}\n"
    "        if self._sync_manager:\n"
    '            logger.info("SSH: syncing files from sandbox...")\n'
    "            self._sync_manager.sync_back()\n"
    "        # One master per user@host:port is shared by every environment in the pod, so closing\n"
    "        # it here kills a sibling's running command (#2174). ControlPersist reaps it once idle;\n"
    "        # a probe's master is its own and is closed as before.\n"
    '        if not getattr(self, "_shared_master", True):\n'
    "            self.close_master()\n"
    "\n"
    "    def close_master(self):\n"
    "        for socket in self._control_sockets():\n"
    "            if not socket.exists():\n"
    "                continue\n"
    "            with contextlib.suppress(OSError, subprocess.SubprocessError):\n"
    '                cmd = ["ssh", "-o", f"ControlPath={socket}", "-O", "exit", f"{self.user}@{self.host}"]\n'
    "                subprocess.run(cmd, capture_output=True, timeout=5, stdin=subprocess.DEVNULL)\n"
    "            with contextlib.suppress(OSError):\n"
    "                socket.unlink()\n"
)

HINT = (
    "Exit 255 with no exit marker: the sandbox ssh connection was closed under this command, or "
    "never opened. If the output is an ssh error (connection refused, timed out) the command did "
    "not run; otherwise it may have run to completion, so check its effect before retrying."
)
RESULT_ANCHOR = (
    "    failure_hint = _failure_hint(command, returncode, output, exit_note)\n"
)
RESULT_PATCHED = RESULT_ANCHOR + (
    f'    if env_type == "ssh" and returncode == 255 and failure_hint is None and not (result or {{}}).get("cwd_observed"):  # {MARKER}\n'
    f"        failure_hint = {HINT!r}\n"
)


def apply(root: Path) -> None:
    ssh = patchlib.Patch(root, SSH_RELATIVE, prefix=PREFIX)
    ssh.refuse_if_patched(MARKER)
    ssh.substitute(SSH_INIT_ANCHOR, SSH_INIT_PATCHED, label="shared-master mark in __init__")
    ssh.substitute(SSH_ARGV_ANCHOR, SSH_ARGV_PATCHED, label="keep-alive on the client argv")
    ssh.substitute(SSH_CLEANUP_ANCHOR, SSH_CLEANUP_PATCHED, label="cleanup() exit loop")

    result = patchlib.Patch(root, RESULT_RELATIVE, prefix=PREFIX)
    result.refuse_if_patched(MARKER)
    result.substitute(RESULT_ANCHOR, RESULT_PATCHED, label="failure hint assignment")

    ssh.commit("cleanup() leaves the shared master to ControlPersist and the client keeps it alive; close_master() closes it")
    result.commit("an ssh exit 255 without the cwd marker carries a hint")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
