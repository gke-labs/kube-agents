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

Filing is once-only, and this job owns that guarantee locally: the id of the
card it filed is recorded in ``.bootstrap_scan_filed``, and while that marker
exists no further card is ever filed. The board's ``idempotency_key`` is kept
as a second line of defence for the one window the marker cannot cover (the
card was created but the run died before the marker was written) — but it is
not the guarantee. It cannot be: it dedupes against non-archived rows in one
board's database, so an archived card, a recreated board, or a reset volume
would hand a 1-minute cron job licence to launch a fresh fleet-wide sweep
every single minute. That is the "bootstrap ran several times" failure.

The marker is also what makes a delegated sweep safe, and that is what broke
here. Since the sweep started fanning out to subagents, the card this job
files is completed almost immediately — the worker's job is to delegate, not
to scan, so it hands the real work to per-cluster child cards and finishes.
``INVENTORY.md`` then appears minutes later, from the aggregation card. For
that whole window the board says "done" and the disk says "no report", which
is indistinguishable from "never scanned" — so a 1-minute job with no memory
of its own re-files the sweep, once a minute, for as long as the real work
takes. Only a marker written at file time closes that window.

Deleting ``.bootstrap_scan_filed`` is the supported way to re-arm discovery
after a sweep has genuinely failed.

Output is intentionally empty: ``deliver: local`` plus empty stdout means the
scheduler treats every run as silent. The report reaches the user through
``bootstrap_delivery.py``, not through this job.
"""

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

SCAN_TASK_TITLE = "First-time environment discovery: write the onboarding inventory report"
# Second line of defence only — see the module docstring. The marker below is
# the actual guarantee.
SCAN_IDEMPOTENCY_KEY = "bootstrap-inventory-scan"
# Propagated to the cards the worker fans out to, so a duplicate root card (if
# one ever slips through) still cannot produce a duplicate sweep underneath it.
AGGREGATE_IDEMPOTENCY_KEY = "bootstrap-inventory-aggregate"
CLUSTER_IDEMPOTENCY_KEY_PREFIX = "bootstrap-inventory-cluster-"
SCAN_ASSIGNEE = "platform"

# Records that the sweep card has been filed, and which card it was. Its
# presence — not the existence of the report — is what stops this job filing
# again. Delete it to deliberately re-arm discovery.
SCAN_FILED_MARKER = ".bootstrap_scan_filed"

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
    """True when a sweep has already been filed, run, or delivered.

    Three markers, because the sweep is only observable at three different
    points in its life:

    - ``.bootstrap_scan_filed`` — a card exists. Covers the long middle of the
      sweep, when there is no report yet and nothing else says work is in
      flight. This is the one that stops the every-60-seconds re-file.
    - ``INVENTORY.md`` — the report landed.
    - ``.bootstrap_completed`` — the report was delivered and cleaned up.
      Checked because cleanup removes ``INVENTORY.md``, which would otherwise
      look exactly like "never scanned".
    """
    return (
        (data_dir / SCAN_FILED_MARKER).exists()
        or (data_dir / "INVENTORY.md").exists()
        or (data_dir / ".bootstrap_completed").exists()
    )


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
        "`kanban_create(assignee=<that agent>, "
        f"idempotency_key='{CLUSTER_IDEMPOTENCY_KEY_PREFIX}<cluster-name>', ...)` asking it to "
        "report its own cluster's inventory, and to return the findings as structured "
        "`metadata` on completion. Each Cluster Agent is read-only and pinned to its own "
        "cluster, so these run in parallel and none can touch another's. Then create ONE "
        "aggregation card assigned to `platform` with `parents=[<all child card ids>]` and "
        f"`idempotency_key='{AGGREGATE_IDEMPOTENCY_KEY}'` — a fan-in child receives every "
        "parent's `metadata` in its worker context, which is how you collect the results. "
        "Complete your own card once those are filed; the aggregation card does the write-up."
        "\n\n"
        "**Use those exact idempotency keys.** This is onboarding: it must happen once. The "
        "keys are what guarantees that a retry, a second dispatch, or a duplicate of this "
        "card re-attaches to the sweep already in flight instead of launching a second "
        "fleet-wide scan on top of it.\n\n"
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


def _parse_task_id(out: str) -> str | None:
    """Pull the card id out of a ``create`` response.

    ``--json`` is asked for, but the board's own stderr can share the buffer,
    so locate the JSON object rather than assuming the whole string is one.
    Falls back to the human line (``Created <id>  (...)``) in case the board
    is older than ``--json`` on this subcommand.
    """
    start = out.find("{")
    end = out.rfind("}")
    if start != -1 and end > start:
        try:
            task_id = json.loads(out[start : end + 1]).get("id")
            if task_id:
                return str(task_id)
        except Exception:  # noqa: BLE001 - fall through to the text form
            pass
    match = re.search(r"Created\s+(\S+)", out)
    return match.group(1) if match else None


def file_scan_task(data_dir: Path) -> str | None:
    """File the kanban card that performs the sweep, exactly once.

    Records the resulting card id in ``.bootstrap_scan_filed`` so no later
    tick files another. The marker is written only for a card the board
    confirmed, because a marker written after a failed create would silence
    discovery permanently — the failure mode that costs the user the entire
    onboarding report, rather than merely repeating it.

    Returns the card id, or None if the card could not be filed (non-fatal:
    the next tick retries, since no marker was written).
    """
    try:
        from hermes_cli.kanban import run_slash
    except Exception as e:  # noqa: BLE001 - kanban unavailable; retry next tick
        sys.stderr.write(f"bootstrap_scan_gate: kanban API unavailable: {e}\n")
        return None

    cmd = (
        f"create --json --assignee {shlex.quote(SCAN_ASSIGNEE)} "
        f"--idempotency-key {shlex.quote(SCAN_IDEMPOTENCY_KEY)} "
        f"--body {shlex.quote(_task_body())} "
        f"{shlex.quote(SCAN_TASK_TITLE)}"
    )
    try:
        out = str(run_slash(cmd)).strip()
    except Exception as e:  # noqa: BLE001 - never fail the cron run
        sys.stderr.write(f"bootstrap_scan_gate: could not file scan task: {e}\n")
        return None

    task_id = _parse_task_id(out)
    if not task_id:
        sys.stderr.write(
            f"bootstrap_scan_gate: could not read a task id from the board response: {out}\n"
        )
        return None

    _mark_filed(data_dir, task_id)
    sys.stderr.write(f"bootstrap_scan_gate: filed sweep card {task_id}\n")
    return task_id


def _mark_filed(data_dir: Path, task_id: str) -> None:
    """Record the filed card so no later tick files a second one.

    Written with the card id and timestamp rather than an empty touch file:
    when someone asks why onboarding is not progressing, this is the file that
    tells them which card to go and look at.
    """
    marker = data_dir / SCAN_FILED_MARKER
    try:
        marker.write_text(
            f"task_id={task_id}\nfiled_at={int(time.time())}\n",
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001 - never fail the cron run
        # The card is already filed; the board's idempotency key is now the
        # only thing standing between us and a duplicate sweep. Say so loudly.
        sys.stderr.write(
            f"bootstrap_scan_gate: FILED {task_id} but could not write {marker}: {e}. "
            "Duplicate-scan protection has fallen back to the board's idempotency key.\n"
        )


def main(data_dir: Path | None = None) -> int:
    if data_dir is None:
        data_dir = _data_dir()
    if should_skip(data_dir):
        return 0  # silent no-op: already filed, scanned, or delivered
    file_scan_task(data_dir)
    # Stdout stays empty on purpose — this job never speaks to the user.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
