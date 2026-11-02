#!/usr/bin/env python3
"""Let a cron job carry its own turn budget.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree at
``cron/scheduler.py``.

The gap (gke-labs/kube-agents#1947)
-----------------------------------
The scheduler resolves a run's iteration cap from the profile's
``agent.max_turns`` and nothing else; a job's own ``max_turns`` is ignored.
The Platform Agent's profile therefore carried 250, sized for the fleet
audits, and every kanban worker spawned under that profile (a delegated chat
request, an issue-resolver card) ran with the same 250, since the worker
reads the same ``config.yaml``. A task that goes wrong has 250 steps before
anything stops it.

The fix
-------
Two edits, both mirroring upstream NousResearch/hermes-agent#134059 so the
patch drops out on the tag bump that carries it: a helper,
``_resolve_cron_max_iterations``, that reads ``job["max_turns"]`` first and
falls back to the profile's ``agent.max_turns`` (then the legacy root key),
and the call to it where ``_resolve_cron_agent_setup`` used to read the
profile alone. A job with no ``max_turns`` behaves exactly as before. Two
departures from upstream. A job value Hermes would read as unlimited (0, a
negative number, a bool, a non-numeric string) is ignored and the profile's
value stands, so an entry the image does not ship (an operator's own job on
the volume, which the roster lint never sees) cannot run a cron turn with no
budget at all. And a job value never lowers the budget below the profile's:
the entry raises its job above the profile, so an operator who sets
``spec.harness.tuning.platform.maxTurns`` above the audits' 250 still raises
the audits, and an install already above 250 keeps that headroom on upgrade.

With this the audits declare 250 on their roster entries
(``agents/platform/cron/jobs.json``) and the profile default drops to 150,
which is what the workers get.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "cron/scheduler.py"

# Text only a patched file contains.
BUILD_MARKER = "kube-agents patch: per-job turn budget"

# The name the helper is defined under; the verify script and the unit test
# call it by this name.
HELPER_NAME = "_resolve_cron_max_iterations"

SETUP_DEF = (
    "def _resolve_cron_agent_setup(job: dict, job_id: str, job_name: str, jc) -> _CronAgentSetup:\n"
)

SETUP_DEF_PATCHED = (
    f"def {HELPER_NAME}(job: dict, cfg: dict):\n"
    '    """Resolve a narrow per-job turn cap, falling back to the global setting.\n'
    "\n"
    f"    {BUILD_MARKER} (gke-labs/kube-agents#1947), mirroring\n"
    "    NousResearch/hermes-agent#134059. A job's own ``max_turns`` bounds its run;\n"
    "    a job without one keeps the profile's ``agent.max_turns`` as before. Two\n"
    "    departures from upstream: a job value that would lift the cap altogether\n"
    "    (0, a negative number, a bool, a non-numeric string, which\n"
    "    ``resolve_turn_limit`` reads as unlimited) is ignored, and a job value\n"
    "    below the profile's does not lower it -- the entry raises its job above\n"
    "    the profile, never under it, so an operator's larger\n"
    "    ``tuning.platform.maxTurns`` still reaches every job.\n"
    '    """\n'
    "    from hermes_cli.config import TURN_LIMIT_UNLIMITED, resolve_turn_limit as _resolve_turn_limit\n"
    '    profile = cfg.get("agent", {}).get("max_turns")\n'
    "    if profile is None:\n"
    '        profile = cfg.get("max_turns")\n'
    "    profile_cap = _resolve_turn_limit(profile)\n"
    '    value = job.get("max_turns")\n'
    "    if value is None:\n"
    "        return profile_cap\n"
    "    cap = _resolve_turn_limit(value)\n"
    "    if cap >= TURN_LIMIT_UNLIMITED:\n"
    "        return profile_cap\n"
    "    return max(cap, profile_cap)\n"
    "\n"
    "\n" + SETUP_DEF
)

RESOLVE = (
    "    # resolve_turn_limit() honors none/unlimited (sys.maxsize) and explicit 0 / null.\n"
    "    from hermes_cli.config import resolve_turn_limit as _resolve_turn_limit\n"
    '    _mt = _cfg.get("agent", {}).get("max_turns")\n'
    "    if _mt is None:\n"
    '        _mt = _cfg.get("max_turns")\n'
    "    setup.max_iterations = _resolve_turn_limit(_mt)\n"
)

RESOLVE_PATCHED = (
    "    # Optional job-scoped cap; jobs without it retain the profile's budget.\n"
    f"    setup.max_iterations = {HELPER_NAME}(job, _cfg)\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="cron_job_max_turns")
    patch.refuse_if_patched(BUILD_MARKER)
    patch.substitute(SETUP_DEF, SETUP_DEF_PATCHED, label="setup def")
    patch.substitute(RESOLVE, RESOLVE_PATCHED, label="turn limit resolution")
    patch.commit("2 anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
