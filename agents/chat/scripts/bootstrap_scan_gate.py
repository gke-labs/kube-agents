#!/usr/bin/env python3
"""Dispatcher for the ``bootstrap-inventory-scan`` cron job.

First-time onboarding needs a full GKE discovery sweep (control plane options,
node pools, Workload Identity, running workloads) written up as a
presentation-ready report. That is LLM work AND privileged work, and the
profile this cron runs on can do neither:

- Only the ``default`` (Chat Agent) profile's cron ticks — a job placed on the
  ``platform`` profile never fires at all.
- The Chat Agent's toolsets are deliberately stripped to ``mcp-router`` +
  ``kanban`` (no terminal, no gcloud, no kubectl), so it cannot run the sweep
  itself even as an LLM job.

So this runs as a ``no_agent`` script — a plain subprocess, not bound by the
Chat Agent's toolset denylist — and files the sweep as a **kanban task assigned
to** ``platform``, the privileged specialist. The dispatcher spawns that worker
with its full toolset; the worker writes the report and completes the card.

Idempotency is the board's, not ours: the card is created with a fixed
``idempotency_key``, so re-firing every minute returns the existing task id
instead of stacking duplicates. Once ``INVENTORY.md`` exists (or onboarding is
complete) this becomes a no-op that never touches the board again.

Output is intentionally empty: ``deliver: local`` plus empty stdout means the
scheduler treats every run as silent. The report reaches the user through
``bootstrap_delivery.py``, not through this job.
"""

import os
import shlex
import sys
from pathlib import Path

SCAN_TASK_TITLE = "First-time environment discovery: write the onboarding inventory report"
# Fixed key -> the board dedupes for us, so a 1-minute interval cannot stack cards.
SCAN_IDEMPOTENCY_KEY = "bootstrap-inventory-scan"
SCAN_ASSIGNEE = "platform"

# The scan runs as a `platform` worker, whose HERMES_HOME is the platform profile
# home — but every other piece of onboarding state (`.user_aligned`,
# `.bootstrap_completed`, and the delivery job that reads the report) lives in the
# Chat Agent's home. Pin the output to an absolute path so both halves agree.
INVENTORY_PATH = "/opt/data/INVENTORY.md"
INSTRUCTIONS_PATHS = (
    "/opt/data/profiles/platform/governance/inventory.md",
    "/opt/platform-template/governance/inventory.md",
)
# Present only where per-cluster agents are deployed. When absent, the sweep degrades to
# a single-agent walk of the fleet; when present, the scan fans out one card per cluster.
RECONCILE_SCRIPT = "/opt/data/scripts/cluster_agent_reconcile.py"


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def should_skip(data_dir: Path) -> bool:
    """True when discovery is already done and no scan needs to be filed.

    Gating on ``.bootstrap_completed`` as well as the report means that removing
    ``INVENTORY.md`` during cleanup can never re-trigger a fresh scan.
    """
    return (data_dir / "INVENTORY.md").exists() or (data_dir / ".bootstrap_completed").exists()


def _task_body() -> str:
    instruction_list = "\n".join(f"  - {p}" for p in INSTRUCTIONS_PATHS)
    return (
        "First-time onboarding discovery sweep. Follow the inventory SOP, reading whichever "
        "of these exists:\n"
        f"{instruction_list}\n\n"
        "Audit control plane options, node pools, Workload Identity settings, and running "
        "workloads. Scale the work out per cluster rather than walking the fleet serially:\n\n"
        "**Step 1 — reconcile the Cluster Agent roster.** If "
        f"`{RECONCILE_SCRIPT}` exists, run it first so every managed cluster has an agent. "
        "It is idempotent and deliberately does NOT create an agent for the management "
        "cluster this pod runs on. If the script is absent, skip this step — this deployment "
        "has no Cluster Agents and you will do the whole sweep yourself (Step 4).\n\n"
        "**Step 2 — scan the management cluster yourself.** No Cluster Agent covers it, so "
        "its inventory is yours to produce. Skipping it would leave a hole exactly where the "
        "harness runs.\n\n"
        "**Step 3 — fan out.** For every OTHER cluster that has an agent (`hermes profile "
        "list`, names starting `cluster-`), open one child card per cluster with "
        "`kanban_create(assignee=<that agent>, ...)` asking it to report its own cluster's "
        "inventory, and to return the findings as structured `metadata` on completion. Each "
        "Cluster Agent is read-only and pinned to its own cluster, so these run in parallel "
        "and none can touch another's. Then create ONE aggregation card assigned to "
        "`platform` with `parents=[<all child card ids>]` — a fan-in child receives every "
        "parent's `metadata` in its worker context, which is how you collect the results. "
        "Complete your own card once those are filed; the aggregation card does the write-up.\n\n"
        "**Step 4 — write the report** (in the aggregation card, or directly here if there "
        "were no Cluster Agents to fan out to). Combine your management-cluster findings with "
        "every child's metadata into a COMPLETE, verbose, presentation-ready report at "
        f"`{INVENTORY_PATH}` — a greeting header, the full fleet and workload tables, and the "
        "full prioritized SRE remediation suggestions.\n\n"
        f"**The exact path matters.** `{INVENTORY_PATH}` is the Chat Agent's home, not yours; "
        "a separate delivery job reads that file and posts it to the user **verbatim, with no "
        "further editing**. Writing it anywhere else means the user never receives it.\n\n"
        "If a cluster's scan fails or its agent never reports, say so explicitly in the report "
        "rather than omitting the cluster — a silent gap reads as 'clean'.\n\n"
        "Do not message the user directly — delivery is handled for you."
    )


def file_scan_task() -> str | None:
    """Create (idempotently) the kanban card that performs the sweep.

    Returns the raw board response, or None if the board was unreachable — a
    failure here is non-fatal: the next tick simply retries.
    """
    try:
        from hermes_cli.kanban import run_slash
    except Exception as e:  # noqa: BLE001 - kanban unavailable; retry next tick
        sys.stderr.write(f"bootstrap_scan_gate: kanban API unavailable: {e}\n")
        return None

    cmd = (
        f"create --assignee {shlex.quote(SCAN_ASSIGNEE)} "
        f"--idempotency-key {shlex.quote(SCAN_IDEMPOTENCY_KEY)} "
        f"--body {shlex.quote(_task_body())} "
        f"{shlex.quote(SCAN_TASK_TITLE)}"
    )
    try:
        out = run_slash(cmd)
    except Exception as e:  # noqa: BLE001 - never fail the cron run
        sys.stderr.write(f"bootstrap_scan_gate: could not file scan task: {e}\n")
        return None

    sys.stderr.write(f"bootstrap_scan_gate: {str(out).strip()}\n")
    return str(out).strip()


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()
    if should_skip(data_dir):
        return 0  # silent no-op: discovery already done
    file_scan_task()
    # Stdout stays empty on purpose — this job never speaks to the user.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
