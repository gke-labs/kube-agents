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
here. When the sweep first fanned out to subagents, the card this job filed
completed almost immediately — the worker of that era delegated to
per-cluster child cards plus an aggregation card and finished, so the board
said "done" while the disk said "no report" for the whole sweep, which is
indistinguishable from "never scanned" — and a 1-minute job with no memory of
its own re-filed the sweep, once a minute, for as long as the real work took.
The sweep card now stays open until it has waited out its children and
written ``INVENTORY.raw.md`` itself (#1010 retired the complete-at-fan-out
shape), which narrows that window without closing it: ``INVENTORY.md`` still
appears minutes later, from the prioritization card the sweep files, and a
crashed sweep still leaves board-done/disk-empty. Only a marker written at
file time covers every case.

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
import subprocess
import sys
import time
from pathlib import Path

SCAN_TASK_TITLE = "First-time environment discovery: write the onboarding inventory report"
# Second line of defence only — see the module docstring. The marker below is
# the actual guarantee.
SCAN_IDEMPOTENCY_KEY = "bootstrap-inventory-scan"
# Propagated to the cards the worker fans out to, so a duplicate root card (if
# one ever slips through) still cannot produce a duplicate sweep underneath it.
# (The retired aggregation card's key, `bootstrap-inventory-aggregate`, is gone
# with the fan-in shape it guarded: the sweep card now waits for its children
# and writes the findings itself — issue #1010.)
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
CLUSTER_AUDIT_INSTRUCTIONS_PATHS = (
    "/opt/data/profiles/platform/governance/cluster_inventory_audit_sop.md",
    "/opt/platform-template/governance/cluster_inventory_audit_sop.md",
)
# Present only where per-cluster agents are deployed. When absent, the sweep degrades to
# a single-agent walk of the fleet; when present, the scan fans out one card per cluster.
# Resolved under the data dir rather than hardcoded: `spec.harness.hermes.agentHome` moves
# the whole tree, and a missing path here silently files the solo sweep.
RECONCILE_SCRIPT_NAME = "cluster_agent_reconcile.py"

# The reconcile that creates the Cluster Agents runs on its own cron at `11 * * * *`,
# while this gate runs every minute. On a fresh install the gate therefore reaches the
# roster up to 59 minutes before anything has populated it, reads it as empty, and the
# sweep degrades to the Platform Agent walking the whole fleet alone. So the gate runs
# the reconcile itself and waits for it, rather than racing it.
RECONCILE_ATTEMPTS_MARKER = ".bootstrap_reconcile_attempts"
RECONCILE_TIMEOUT_SECONDS = 240
# `cluster_agent_reconcile.EXIT_ALREADY_RUNNING`. Mutual exclusion lives in that
# script, because the hourly `cluster-agent-reconcile` job runs it too and the
# gateway's cron lock is per job id — a lock held here would not keep the two apart.
RECONCILE_ALREADY_RUNNING = 4
# After this many failed reconciles, AND this much wall clock since the first of them,
# the sweep proceeds anyway. A reconcile that cannot succeed (no IAM to list clusters)
# must not hold onboarding shut forever — a solo sweep is a worse report, no report is
# none.
#
# The clock is what makes the count safe. The gate ticks every minute with no backoff, so
# a count alone gives up five minutes into the pod's life — and a brand-new install is
# both the only state this gate runs in and the one where `gcloud container clusters list`
# fails for reasons that clear on their own, IAM propagation being routine minutes. Giving
# up there files the solo sweep that `.bootstrap_scan_filed` then makes permanent, which
# is the failure this gate exists to remove.
MAX_RECONCILE_ATTEMPTS = 5
RECONCILE_GIVE_UP_SECONDS = 1800


def _data_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def _reconcile_script(data_dir: Path) -> Path:
    return data_dir / "scripts" / RECONCILE_SCRIPT_NAME


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


def _reconcile_attempts(data_dir: Path) -> int:
    try:
        first = (data_dir / RECONCILE_ATTEMPTS_MARKER).read_text(encoding="utf-8").splitlines()[0]
        return int(first.strip())
    except Exception:  # noqa: BLE001 - absent or unreadable counts as no attempts yet
        return 0


def _reconcile_since(data_dir: Path) -> float | None:
    """Epoch seconds of the first failure in the current streak, or None.

    Second line of the counter file. None on a marker written before this line
    existed, which is read as "the clock has run out" so an upgrade cannot extend
    a streak that already exhausted the count.
    """
    try:
        lines = (data_dir / RECONCILE_ATTEMPTS_MARKER).read_text(encoding="utf-8").splitlines()
        return float(lines[1].strip())
    except Exception:  # noqa: BLE001 - absent, short, or unparseable
        return None


def _record_reconcile_attempt(data_dir: Path, attempts: int, since: float | None = None) -> None:
    body = f"{attempts}\n" if since is None else f"{attempts}\n{since}\n"
    try:
        (data_dir / RECONCILE_ATTEMPTS_MARKER).write_text(body, encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - never fail the cron run
        sys.stderr.write(f"bootstrap_scan_gate: could not record reconcile attempt: {e}\n")


def ensure_cluster_agents(data_dir: Path) -> bool:
    """Create the Cluster Agents, blocking until that finishes.

    Returns True when the sweep may be filed. False means "not yet, retry on the
    next tick" — the caller must not file the card, because a sweep filed against
    an empty roster is a sweep with no fan-out, and the marker it writes makes that
    permanent.

    Running the reconcile here rather than asking the sweep's worker to run it (as
    Step 1 of the card body used to) is what makes the result checkable. The worker
    read the script's stderr and interpreted it: on 2026-08-21 the reconcile
    succeeded — ``created=0 pruned=1 kept=4`` — while printing two ENOENT warnings,
    and the worker reported the roster as unavailable and audited the fleet alone.
    An exit code cannot be misread that way — but only under
    ``--require-create-pass``, without which the script exits 0 whatever happened.
    """
    script = _reconcile_script(data_dir)
    if not script.exists():
        return True  # deployment without Cluster Agents; the solo sweep is correct here

    attempts = _reconcile_attempts(data_dir)
    since = _reconcile_since(data_dir)
    elapsed = time.time() - since if since is not None else None
    if attempts >= MAX_RECONCILE_ATTEMPTS and (elapsed is None or elapsed >= RECONCILE_GIVE_UP_SECONDS):
        sys.stderr.write(
            f"bootstrap_scan_gate: reconcile failed {attempts} times over "
            f"{'an unknown period' if elapsed is None else f'{int(elapsed)}s'}; "
            "filing the sweep anyway against whatever roster exists\n"
        )
        return True

    # The attempt is recorded before the run, not after: this process can itself be
    # killed mid-reconcile, and an attempt that leaves no trace is one the ceiling
    # never counts.
    _record_reconcile_attempt(data_dir, attempts + 1, since if since is not None else time.time())
    try:
        proc = subprocess.run(
            # `--require-create-pass` is what makes the exit code mean anything: on
            # the cron path the script swallows every failure and exits 0, so a bare
            # run cannot tell "this project has no clusters" from "the list call
            # failed" — and the second one files a solo sweep that `.bootstrap_scan_filed`
            # then makes permanent.
            [sys.executable, str(script), "--require-create-pass"],
            capture_output=True,
            text=True,
            timeout=RECONCILE_TIMEOUT_SECONDS,
            env={**os.environ, "HERMES_HOME": str(data_dir)},
        )
    except Exception as e:  # noqa: BLE001 - timeout or spawn failure; retry next tick
        sys.stderr.write(f"bootstrap_scan_gate: reconcile did not complete: {e}\n")
        return False

    if proc.returncode == RECONCILE_ALREADY_RUNNING:
        # A previous tick, or the hourly reconcile job, still holds the script's lock.
        # The gate fires every minute and a reconcile takes tens of seconds, so overlap
        # is expected. Give the attempt back: nothing was learned about the roster, and
        # counting it would spend the ceiling on contention.
        _record_reconcile_attempt(data_dir, attempts, since)
        return False

    if proc.returncode != 0:
        sys.stderr.write(
            f"bootstrap_scan_gate: reconcile exited {proc.returncode}; "
            f"retrying next tick. stderr: {proc.stderr.strip()[:500]}\n"
        )
        return False

    _record_reconcile_attempt(data_dir, 0)
    sys.stderr.write("bootstrap_scan_gate: Cluster Agent roster reconciled\n")
    return True


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
    cluster_audit_list = "\n".join(f"  - {p}" for p in CLUSTER_AUDIT_INSTRUCTIONS_PATHS)
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
        "**The step numbers below are the inventory SOP's** — the numbering is aligned so a "
        "reference to a step means the same thing in both documents.\n\n"
        "**Step 1 — do not reconcile the roster yourself.** This gate already ran "
        f"`{RECONCILE_SCRIPT_NAME}`, and profile lifecycle belongs to that script alone: it "
        "holds the `RECONCILE_EXCLUDE` opt-out and the create/prune rules, so a profile you "
        "make by calling `cluster_agent_profile.py` directly is one the next reconcile run may "
        "immediately prune, and you will loop. Do not run it, and do not repair or delete a "
        "profile.\n\n"
        "**The roster may be empty or incomplete, and that is your finding to report, not "
        "yours to fix.** It says which clusters can audit themselves — not which clusters "
        "count. Audit every cluster the project has: the ones with no Cluster Agent you take "
        "yourself in Step 4, and the report names each one as lacking an agent. A fleet swept "
        "without Cluster Agents is a degraded sweep and must read as one, because this report "
        "is delivered to the user as the state of their environment. If you cannot list the "
        "project's clusters at all, put that at the top of the report and file it anyway — "
        "onboarding runs once, and a report saying discovery failed is worth more than a thin "
        "one that reads as a clean fleet.\n\n"
        "**Step 2 — fan out.** Read the roster with exactly this command, exactly once:\n\n"
        f"    {_roster_command()}\n\n"
        "Cluster Agents are the profiles whose names start `cluster-`. **If that command "
        "fails or lists no `cluster-` profiles, there are no Cluster Agents: skip the rest of "
        "this step and do the whole sweep yourself in Step 4, following Steps 2 to 4 of the "
        "single-cluster audit SOP (its own numbering) for each cluster so the topology and the "
        "workload checks both happen.** That is the normal case for a "
        "single-cluster install and it is not an error. Use the command as written — the "
        "absolute path and the `HERMES_HOME` are both required, and a bare `hermes profile "
        "list` will either fail or quietly return an incomplete roster.\n\n"
        "For every cluster that has an agent, open one child card per cluster with "
        "`kanban_create(assignee=<that agent>, "
        f"idempotency_key='{CLUSTER_IDEMPOTENCY_KEY_PREFIX}<cluster-name>-<location>', ...)`. The body must "
        "send that agent to the single-cluster audit SOP, reading whichever of these exists:\n"
        f"{cluster_audit_list}\n\n"
        "and tell it to complete its card with the structured `metadata` that SOP specifies. "
        "**Point at the SOP; do not describe the checks in the card body.** Both the checks and "
        "the `metadata` shape the aggregation stage reads are specific, and a body written "
        "freehand loses them: what comes back is a topology listing with no findings in it. "
        "Each Cluster Agent is read-only and pinned to its own "
        "cluster, so these run in parallel and none can touch another's. "
        "**Step 3 — wait for the children on this card.** Poll each child with "
        "`kanban_show(<id>)`, running `sleep 60` between polling rounds (double it once the wait passes five minutes), until every one is "
        "`done` or `archived`; their structured `metadata` is how you collect the results. Do "
        "NOT complete this card while they are unfinished — completing is how a card hands back "
        "its final result, a dispatch receipt is not the report, and the board refuses such a "
        "completion — and do NOT `kanban_block` on them (that deadlocks; see the inventory SOP). "
        "Steps 4 and 5 below are your job, in this same run, once the children settle; the "
        "full mechanics are in Step 3 of the inventory SOP above.\n\n"
        "**Use those exact idempotency keys.** This is onboarding: it must happen once. The "
        "keys are what guarantees that a retry, a second dispatch, or a duplicate of this "
        "card re-attaches to the sweep already in flight instead of launching a second "
        "fleet-wide scan on top of it.\n\n"
        "**Step 4 — write the raw findings** (here, once the children have settled — or "
        "immediately if there were no Cluster Agents to fan out to). Audit any cluster the roster did not "
        "cover yourself, and combine those findings "
        "with every child's metadata into a COMPLETE, verbose findings file at "
        f"`{RAW_INVENTORY_PATH}` — the full fleet and workload tables and the full set of SRE "
        "remediation suggestions. Do not summarize and do not trim for length: this file is the "
        "only record of what the sweep saw, and the next stage reads it and nothing else.\n\n"
        f"**Step 5 — file the prioritization card, then complete this one.** "
        f"`{RAW_INVENTORY_PATH}` is not what the user "
        "receives. Once it is on disk, file exactly one card — "
        f"`kanban_create(assignee='{SCAN_ASSIGNEE}', "
        f"idempotency_key='{PRIORITIZE_IDEMPOTENCY_KEY}', "
        "parents=[<this card's id>], ...)` — `parents` matters: it queues the ranking to run "
        "after you finish, which is what lets your own `kanban_complete` close this card while "
        "the ranking is still pending. Tell that worker to follow "
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
        "Then finish by calling `kanban_complete`: `result` is a short factual account of the "
        f"sweep (clusters audited, findings count, that the full findings are at "
        f"`{RAW_INVENTORY_PATH}` and ranking is queued). Completing is what releases the "
        "prioritization card to run.\n\n"
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
    if not ensure_cluster_agents(data_dir):
        return 0  # roster not ready; the next tick retries, no marker written
    file_scan_task(data_dir)
    # Stdout stays empty on purpose — this job never speaks to the user.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
