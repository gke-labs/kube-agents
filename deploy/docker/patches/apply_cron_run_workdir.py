#!/usr/bin/env python3
"""Give each cron agent run a working directory of its own.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``cron/scheduler.py``.

The bug (gke-labs/kube-agents#2989)
-----------------------------------
A cron job with no ``workdir`` (all of ours) runs its agent turn with no
recorded terminal cwd.  On the shell sandbox every terminal command then starts
in the login home, ``/home/agent``, which is root's and not writable (observed
on ka-today-1, 2026-10-16: ``pwd`` printed ``/home/agent`` and a ``touch`` there
was refused).  So a run that needs scratch space finds its own somewhere on the
shared volume, and the profile home is where it lands and stays: nothing sweeps
a profile home.  #2989 found about 150 agent-written probe scripts, a ``.git``
and copies of the gitops-state ConfigMap in long-lived installs' profile homes,
written by audits standing in for configuration that wasn't there, and later
runs read them back as if they were real.

The fix
-------
One edit, in ``_CronRunScope.__init__``.  Upstream already binds a configured
``workdir`` to the run's task id with ``record_session_cwd``, which is the
terminal tool's cwd authority (``tools/terminal_tool.py`` reads
``get_session_cwd(task_id)`` before the config cwd).  When the job has none,
the patch mints ``<hermes home>/cron/runs/<job id>_<execution id>`` and records
that instead.  The directory is per run, so one run's files are not the next
run's inputs, and it is under one root, so a sweep can prune it
(``kanban_workspace_gc.py``).

Deliberately not changed:

- ``_SESSION_CWD`` (the ``cwd=`` passed to ``set_session_vars``).  Upstream
  makes it the prompt and context-file authority, and a non-empty workdir also
  turns on project context-file loading.  A minted directory must not change
  what the agent loads.
- ``_resolve_job_workdir``.  Script jobs still run with the scripts dir's
  parent as their cwd, as our image-owned scripts expect.  The residue comes
  from the model's terminal, not from them.

The cron home is per profile (``_get_hermes_home``), so the directory is inside
the profile home, and on the shell sandbox ``deploy/sandbox/session-command.sh``
both creates it and recovers ``HERMES_HOME`` from it.  A directory that cannot
be created is logged and the run goes on as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "cron/scheduler.py"

# Text only a patched file contains.
BUILD_MARKER = "kube-agents patch: cron run workdir"

# Where the per-run directories go, under the cron's Hermes home.
RUNS_PARTS = ("cron", "runs")

SCOPE = (
    "        if self.workdir:\n"
    "            record_session_cwd(self.task_id, self.workdir)\n"
)

SCOPE_PATCHED = (
    "        if self.workdir:\n"
    "            record_session_cwd(self.task_id, self.workdir)\n"
    "        else:\n"
    "            # kube-agents patch: cron run workdir (gke-labs/kube-agents#2989).\n"
    "            # No configured workdir: the terminal gets a directory of this\n"
    "            # run's own instead of the profile home, so what the model writes\n"
    "            # there is not the next run's input. Tool-layer cwd only; the\n"
    "            # session cwd above, and with it context-file loading, is unchanged.\n"
    "            _run_key = \"\".join(c if c.isascii() and (c.isalnum() or c in \"._-\") else \"_\"\n"
    "                               for c in f\"{job_id}_{self.task_id.rsplit(':', 1)[-1]}\")\n"
    f"            _run_dir = _get_hermes_home().joinpath(*{RUNS_PARTS!r}, _run_key)\n"
    "            try:\n"
    "                _run_dir.mkdir(parents=True, exist_ok=True)\n"
    "            except OSError as _err:\n"
    "                logger.warning(\"Job '%s': could not create run workdir %s (%s); running without one\",\n"
    "                               job_id, _run_dir, _err)\n"
    "            else:\n"
    "                record_session_cwd(self.task_id, str(_run_dir))\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="cron_run_workdir")
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(SCOPE, SCOPE_PATCHED, label="run scope cwd")
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
