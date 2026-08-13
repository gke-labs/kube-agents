#!/usr/bin/env python3
"""Dispatcher for the ``bootstrap-inventory-scan`` cron job.

First-time onboarding needs a full GKE discovery sweep (control plane options,
node pools, Workload Identity, running workloads): the sweep writes the
complete findings to ``INVENTORY.raw.md``, and a second card ranks them into
the short report the user receives. That is LLM work AND privileged work, and the
profile this cron runs on can do neither: the Chat Agent's toolsets are
deliberately stripped to ``mcp-router`` + ``kanban`` (no terminal, no gcloud,
no kubectl), so it cannot run the sweep itself even as an LLM job.

Nor can the job simply move to ``platform``. Every marker that makes onboarding
once-only — ``.bootstrap_scan_filed`` below, ``.bootstrap_completed``,
``INVENTORY.raw.md``, and the ``INVENTORY.md`` the delivery job reads — lives
in the Chat Agent's home, and a
job on the platform profile would gate itself on a different directory. (Cron
on a named profile does now fire, via ``profile_cron_tick.py``; that is no
longer the reason this lives here.)

So this runs as a ``no_agent`` script — a plain subprocess, not bound by the
Chat Agent's toolset denylist — and files the sweep as a **kanban task assigned
to** ``platform``, the privileged specialist. The dispatcher spawns that worker
with its full toolset; the worker writes the raw findings, files the
prioritization card, and completes its own card.

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
The findings appear minutes later, from the aggregation card, and
``INVENTORY.md`` minutes after that, from the prioritization card the sweep
files. For that whole window the board says "done" and the disk says "no
report", which is indistinguishable from "never scanned" — so a 1-minute job
with no memory of its own re-files the sweep, once a minute, for as long as
the real work takes. Only a marker written at file time closes that window,
and adding a prioritization stage lengthened the window it has to cover.

Deleting ``.bootstrap_scan_filed`` — together with ``INVENTORY.raw.md``, which
nothing else ever removes and which ``should_skip`` also gates on — is the
supported way to re-arm discovery after a sweep has genuinely failed. Deleting
the marker alone leaves the gate closed.

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
# The sweep no longer writes the delivered report. It writes the complete findings
# set, then files one card that ranks it down to the short report the user actually
# receives. Ranking is a separate card so it runs in a fresh context that sees the
# raw findings and nothing else — run inline, it would rank them against whatever
# the sweep's own transcript happened to contain, which differs run to run.
PRIORITIZE_IDEMPOTENCY_KEY = "bootstrap-inventory-prioritize"
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
# What the sweep writes: every finding, no length limit, never delivered directly.
# It stays on disk after delivery so the user can ask for the full inventory.
RAW_INVENTORY_PATH = "/opt/data/INVENTORY.raw.md"
INSTRUCTIONS_PATHS = (
    "/opt/data/profiles/platform/governance/inventory.md",
    "/opt/platform-template/governance/inventory.md",
)
PRIORITIZE_INSTRUCTIONS_PATHS = (
    "/opt/data/profiles/platform/governance/inventory_prioritize_sop.md",
    "/opt/platform-template/governance/inventory_prioritize_sop.md",
)
# Present only where per-cluster agents are deployed. When absent, the sweep degrades to
# a single-agent walk of the fleet; when present, the scan fans out one card per cluster.
RECONCILE_SCRIPT = "/opt/data/scripts/cluster_agent_reconcile.py"

def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _roster_command() -> str:
    """The one command that answers "which Cluster Agents exist".

    Both halves are load-bearing and were learned the hard way.

    Absolute path, because the kanban worker's terminal runs with a stripped
    environment in which ``/opt/hermes/.venv/bin`` is not on PATH — a bare
    ``hermes profile list`` exits 127 there while working fine from an
    interactive shell, which is why this was not obvious.

    HERMES_HOME pinned unconditionally, because the absolute path ALONE is
    worse than the 127. The worker is not missing a HERMES_HOME — it has one,
    pinned to its own profile home, and profiles resolve at
    ``$HERMES_HOME/profiles/<name>``. Under the worker's value hermes exits 0
    and prints a plausible roster that is missing profiles (observed:
    ``default`` only, with ``platform`` absent — the view from inside a
    profile home). A defaulted expansion like ``${HERMES_HOME:-...}``
    preserves exactly that wrong value; only an unconditional pin gives the
    fleet-wide view. A loud failure gets retried; a quiet wrong answer gets
    believed, and on a real fleet it silently drops clusters from the sweep.

    The pinned value is this gate's own data root, resolved when the card is
    filed — not a literal ``/opt/data``. The gate's HERMES_HOME IS the root
    the profiles live under (the same one the marker files rely on), while
    the root itself moves with ``spec.harness.hermes.agentHome``. A hardcoded
    ``/opt/data`` under a custom home points hermes at a tree with no
    profiles — the same quiet empty roster, one configuration over; this repo
    has hit that twice before (see the notes in agents/platform/config.yaml).
    """
    return f"HERMES_HOME={_data_dir()} /opt/hermes/.venv/bin/hermes profile list"


def should_skip(data_dir: Path) -> bool:
    """True when a sweep has already been filed, run, or delivered.

    Three markers, because the sweep is only observable at three different
    points in its life:

    - ``.bootstrap_scan_filed`` — a card exists. Covers the long middle of the
      sweep, when there is no report yet and nothing else says work is in
      flight. This is the one that stops the every-60-seconds re-file.
    - ``INVENTORY.raw.md`` — the sweep finished; prioritization may still be
      running. Checked separately from the report because the gap between the
      two is now a distinct stage, not an instant.
    - ``INVENTORY.md`` — the report landed.
    - ``.bootstrap_completed`` — the report was delivered and cleaned up.
      Checked because cleanup removes ``INVENTORY.md``, which would otherwise
      look exactly like "never scanned".
    """
    return (
        (data_dir / SCAN_FILED_MARKER).exists()
        or (data_dir / "INVENTORY.raw.md").exists()
        or (data_dir / "INVENTORY.md").exists()
        or (data_dir / ".bootstrap_completed").exists()
    )


def _task_body() -> str:
    instruction_list = "\n".join(f"  - {p}" for p in INSTRUCTIONS_PATHS)
    prioritize_list = "\n".join(f"  - {p}" for p in PRIORITIZE_INSTRUCTIONS_PATHS)
    return (
        "First-time onboarding discovery sweep. Follow the inventory SOP, reading whichever "
        "of these exists:\n"
        f"{instruction_list}\n\n"
        "Audit control plane options, node pools, Workload Identity settings, and running "
        "workloads. Scale the work out per cluster rather than walking the fleet serially:\n\n"
        "**Discovery steps run ONCE. If a step does not answer, treat its answer as empty and "
        "move on — do not improvise a different way to get it.** Every step below names the "
        "exact command that answers it. If that command fails, returns nothing, or returns "
        "something you cannot parse, record that fact for the report and continue to the next "
        "step. Do not substitute another tool, re-run the command with variations, inspect "
        "the filesystem or a database directly, or query the metadata server to derive the "
        "answer another way. A step that cannot answer is a finding, not a puzzle. Guessing "
        "costs far more than the missing answer is worth, and it produces a report that looks "
        "complete while resting on invented data.\n\n"
        "**Step 1 — reconcile the Cluster Agent roster.** If "
        f"`{RECONCILE_SCRIPT}` exists, run it **exactly once**. It is best-effort: it may create "
        "no agents at all, and `created=0 pruned=0 kept=0` is a normal, successful result. It "
        "deliberately does NOT create an agent for the management cluster this pod runs on, and "
        "it skips creating anything at all when it cannot list the project's clusters. Running "
        "it again does not change that. If the script is absent, or the run fails, skip this "
        "step.\n\n"
        "**Whatever it reports, do not create, repair, or delete a profile yourself.** Profile "
        "lifecycle belongs to that script alone. It holds the guard that keeps the management "
        "cluster from getting its own agent, and calling `cluster_agent_profile.py` directly "
        "goes around that guard: the profile you create is one the next reconcile run will "
        "immediately prune, and you will loop. An empty roster is a supported state, not damage "
        "to repair.\n\n"
        "**Step 2 — scan the management cluster yourself.** No Cluster Agent covers it, so "
        "its inventory is yours to produce. Skipping it would leave a hole exactly where the "
        "harness runs.\n\n"
        "**Step 3 — fan out.** Read the roster with exactly this command, once:\n\n"
        f"    {_roster_command()}\n\n"
        "Cluster Agents are the profiles whose names start `cluster-`. **If that command "
        "fails or lists no `cluster-` profiles, there are no Cluster Agents: skip the rest of "
        "this step and do the whole sweep yourself in Step 4.** That is the normal case for a "
        "single-cluster install and it is not an error. Use the command as written — the "
        "absolute path and the `HERMES_HOME` are both required, and a bare `hermes profile "
        "list` will either fail or quietly return an incomplete roster.\n\n"
        "For every OTHER cluster that has an agent, open one child card per cluster with "
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
        "**Step 4 — write the raw findings** (in the aggregation card, or directly here if "
        "there were no Cluster Agents to fan out to). Combine your management-cluster findings "
        "with every child's metadata into a COMPLETE, verbose findings file at "
        f"`{RAW_INVENTORY_PATH}` — the full fleet and workload tables and the full set of SRE "
        "remediation suggestions. Do not summarize and do not trim for length: this file is the "
        "only record of what the sweep saw, and the next stage reads it and nothing else.\n\n"
        f"**Step 5 — file the prioritization card.** `{RAW_INVENTORY_PATH}` is not what the user "
        "receives. Once it is on disk, file exactly one card — "
        f"`kanban_create(assignee='{SCAN_ASSIGNEE}', "
        f"idempotency_key='{PRIORITIZE_IDEMPOTENCY_KEY}', ...)` — telling that worker to follow "
        "the prioritization SOP, reading whichever of these exists:\n"
        f"{prioritize_list}\n\n"
        f"Its input is `{RAW_INVENTORY_PATH}` and its output is `{INVENTORY_PATH}`, the ranked "
        "report a separate delivery job posts to the user **verbatim, with no further editing**. "
        f"`{INVENTORY_PATH}` is the Chat Agent's home, not yours; writing it anywhere else means "
        "the user never receives it.\n\n"
        "**Do not rank the findings yourself, and do not write "
        f"`{INVENTORY_PATH}` from this card.** Ranking runs separately so it sees the raw "
        "findings and nothing else. Done inline it would rank them against your whole sweep "
        "transcript instead, which changes the report depending on how the sweep went.\n\n"
        "If a cluster's scan fails or its agent never reports, say so explicitly in the raw "
        "findings rather than omitting the cluster — a silent gap reads as 'clean'.\n\n"
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
