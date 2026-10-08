#!/usr/bin/env python3
"""The inventory scan: a stage of the ``oobe`` cron job (``oobe.py``), and before that the
``bootstrap-inventory-scan`` job, which still runs it on an install with no ``oobe`` job.

First-time onboarding needs a full GKE discovery sweep (control plane options,
node pools, Workload Identity, running workloads): ``bootstrap_handoff.py``
files one audit card per Cluster Agent and writes their findings to
``INVENTORY.raw.md``, the sweep card lists the fleet and audits clusters with
no Cluster Agent, and a second card ranks it all into the short report the user
receives. The audits are LLM work AND privileged work, and the
profile this cron runs on can do neither: the Chat Agent's toolsets are
deliberately stripped to ``mcp-router`` + ``kanban`` (no terminal, no gcloud,
no kubectl), so it cannot run the sweep itself even as an LLM job.

Nor can the job simply move to ``platform``. Every marker that makes onboarding
once-only — ``.bootstrap_scan_filed`` below, ``.bootstrap_greeted`` and
``.bootstrap_completed`` — lives in the Chat Agent's home, and a
job on the platform profile would gate itself on a different directory. (Cron
on a named profile does now fire, via ``profile_cron_tick.py``; that is no
longer the reason this lives here.)

So this runs as a ``no_agent`` script — a plain subprocess, not bound by the
Chat Agent's toolset denylist — and files the sweep as a **kanban task assigned
to** ``platform``, the privileged specialist. The dispatcher spawns that worker
with its full toolset; the worker lists the fleet, audits any cluster with no
Cluster Agent, and completes its own card. ``bootstrap_handoff.py``, on this
job's ticks, files one audit card per Cluster Agent, waits for them, writes the
raw findings, and files the prioritization card.

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
The sweep card completes long before the audits do, and the
raw file and the prioritization card (or, with nothing audited, the report
itself) come from the hand-off minutes later, so
board-done/disk-empty is the normal middle of a sweep. Only a marker written at
file time covers every case.

Archiving the previous run's ``bootstrap-inventory-*`` cards and then deleting
``.bootstrap_scan_filed`` — together with ``INVENTORY.raw.md``, which nothing
else ever removes and which a reader would take for this run's findings
(``should_skip`` checks it too, but sees it only with the shell sandbox off) —
is the supported way
to re-arm discovery after a sweep has genuinely failed (the runbook is
bootstrap_onboarding/README.md §5). Deleting the marker alone leaves the gate
closed; deleting it without archiving lets the board answer the new sweep card's
create with the old card, so no sweep runs.

While the marker exists and ``.bootstrap_completed`` does not, each tick runs
the hand-off instead of filing; it is a no-op once it has filed its card.

Output is intentionally empty: ``oobe`` sends this stage's stdout to stderr, and the old job
delivers locally, so no run of it posts anything. The report reaches the user through
``bootstrap_delivery.py``, not through this stage.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import bootstrap_handoff  # beside this script in the pod

SCAN_TASK_TITLE = "First-time environment discovery: write the onboarding inventory report"
# Second line of defence only — see the module docstring. The marker below is
# the actual guarantee.
SCAN_IDEMPOTENCY_KEY = "bootstrap-inventory-scan"
# The per-Cluster-Agent audit cards the hand-off files, so a retried create, or a
# duplicate root card if one ever slips through, cannot file a second audit.
CLUSTER_IDEMPOTENCY_KEY_PREFIX = "bootstrap-inventory-cluster-"
# The card that ranks the raw findings into the short report the user receives. It is
# a separate card so it runs in a fresh context that sees the raw findings and nothing
# else; bootstrap_handoff.py files it once the per-cluster cards settle.
PRIORITIZE_IDEMPOTENCY_KEY = "bootstrap-inventory-prioritize"
SCAN_ASSIGNEE = "platform"

# Records that the sweep card has been filed, and which card it was. Its
# presence — not the existence of the report — is what stops this job filing
# again. Delete it, after archiving the previous run's cards, to deliberately
# re-arm discovery (bootstrap_onboarding/README.md §5).
SCAN_FILED_MARKER = ".bootstrap_scan_filed"
# Written by bootstrap_delivery.py once the report is delivered; past it there is
# nothing left to hand off.
COMPLETED_MARKER = ".bootstrap_completed"

# The scan runs as a `platform` worker, whose HERMES_HOME is the platform profile
# home, but the delivery job looks for the report at one absolute path: on the
# sandbox pod when the shell sandbox is on, in the Chat Agent's home when it is
# off. Pin the output to that path so both halves agree.
INVENTORY_PATH = "/opt/data/INVENTORY.md"
# Every finding, no length limit, never delivered directly; bootstrap_handoff.py
# writes it. It stays on disk after delivery so the user can ask for the full inventory.
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
# a single-agent walk of the fleet; when present, the gate files one audit card per cluster.
# Resolved under the data dir rather than hardcoded: `spec.harness.hermes.agentHome` moves
# the whole tree, and a missing path here silently files the solo sweep.
RECONCILE_SCRIPT_NAME = "cluster_agent_reconcile.py"
# Written by that script beside the profiles (docs/designs/multi-project-scope.md §5): which
# projects the roster covers and which it could not list this run.
SCOPE_SNAPSHOT_NAME = "fleet_scope.json"
SCOPE_OUTCOME_OK = "ok"
# The one non-ok container outcome whose lookup succeeded: its members would cross the
# reconcile's listing cap, so they are carried unlisted. Named apart from a failed lookup.
SCOPE_OUTCOME_OVER_CAP = "over-cap"
SCOPE_OUTCOME_UNKNOWN = "unknown"
# How many unlisted projects the sweep's task prompt names before it counts the rest; a
# folder or organisation can carry thousands, and the prompt names the container instead.
SCOPE_GAP_NAMED_LIMIT = 20

# The reconcile that creates the Cluster Agents runs on its own cron at `11 * * * *`,
# while this gate runs every minute. On a fresh install the gate therefore reaches the
# roster up to 59 minutes before anything has populated it, reads it as empty, and the
# sweep degrades to the Platform Agent walking the whole fleet alone. So the gate runs
# the reconcile itself and waits for it, rather than racing it.
RECONCILE_ATTEMPTS_MARKER = ".bootstrap_reconcile_attempts"
# At the default cap with few profiles: the reconcile bounds its listing phase to its
# listing budget (300 s at a cap of 100) and its prune's describes to theirs (60 s), writes
# its snapshot after, and the settle time covers the creates. A declared
# `spec.scope.maxProjects` scales the listing budget and the profiles on the volume the
# prune's, so `_reconcile_timeout_seconds` reads both the way the reconcile does and adds
# the settle time; this constant is the floor, and the ceiling when there is no declaration
# to read (the scope file's variable unset) or the reconcile cannot be imported.
RECONCILE_TIMEOUT_SECONDS = 390
RECONCILE_SETTLE_SECONDS = 30
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


def cluster_agents() -> list[dict]:
    """The ready Cluster Agents, one ``{name, key, title, cluster_label}`` each.

    The gate files one card per entry itself (``bootstrap_handoff``) and lists
    them in the sweep card. It reads the roster because the sweep's worker cannot. The worker's
    ``terminal`` runs in the shell sandbox, which has no ``hermes``, and whose
    ``/opt/data/profiles`` is a mirror that leaves out every ``config.yaml``
    (so no ``cluster_identity``) and keeps profiles the reconcile has pruned.
    This process runs in the agent pod next to the real profiles, and only
    after ``ensure_cluster_agents`` returns True, so the list is the ready part
    of the roster the reconcile left, or once it has given up, of whatever
    roster exists.

    ``cluster_agent_profile`` resolves the profiles under this process's
    ``HERMES_HOME``, the same root the markers and the reconcile use, so it
    follows ``spec.harness.hermes.agentHome``.

    A profile is listed only when it meets ``platform_control``'s
    ``list_cluster_profiles`` rule: registered with Hermes and its scaffold
    finished. A card assigned to an unregistered directory is never dispatched
    and the hand-off waits on it until its time limit; a profile without
    ``USER.md`` blocks at preflight, and its cluster is better audited by the
    sweep itself in Step 3.

    A profile without a readable ``cluster_identity`` is left out, as the
    reconcile neither counts nor prunes one: there is no cluster to name on its
    card, and the card already sends every cluster the list misses to Step 3. A
    profile whose directory or config cannot be read is skipped the same way: a
    file like that is what keeps the reconcile failing until it gives up and
    files the sweep, so it must not take the other profiles with it. An entry
    whose stat fails with EACCES or EIO fails ``list_profiles`` itself, below;
    ``list_profiles`` leaves out a dangling symlink.

    Failing to list the profiles, or to import the readiness rule, returns an
    empty list, which files the solo sweep. Raising would fail the run before
    the card is filed, on every tick for as long as the failure lasts; like
    the give-up in ``ensure_cluster_agents``, this gate prefers a degraded
    report to none.
    """
    try:
        import cluster_agent_profile as cap  # beside this script in the pod, as for the reconcile
        from cluster_agent_reconcile import SCAFFOLD_ARTIFACTS
        from profile_scaffold import is_scaffolded

        names = cap.list_profiles()
    except Exception as e:  # noqa: BLE001 - never fail the cron run; see the docstring
        sys.stderr.write(f"bootstrap_scan_gate: could not read the Cluster Agent roster: {e}\n")
        return []
    agents = []
    for name in names:
        home = cap.profile_home(name)
        # The probe is inside the try: the image's Python re-raises an is_file()
        # that fails with EACCES or EIO rather than answering False.
        try:
            registered = is_scaffolded(home)
            missing = [f for f in SCAFFOLD_ARTIFACTS if not (home / f).is_file()]
            if not registered or missing:
                sys.stderr.write(
                    f"bootstrap_scan_gate: skipping Cluster Agent {name}: scaffold not finished "
                    f"(registered={registered}, missing={missing})\n"
                )
                continue
            identity = cap.read_cluster_identity(home)
        except Exception as e:  # noqa: BLE001 - see the docstring
            sys.stderr.write(f"bootstrap_scan_gate: skipping Cluster Agent {name}: {e}\n")
            continue
        if identity is None:
            sys.stderr.write(
                f"bootstrap_scan_gate: skipping Cluster Agent {name}: no complete cluster_identity in its config\n"
            )
            continue
        # Keyed by the profile name: the identity fields all allow hyphens, so
        # joining them with hyphens gives two clusters one key, and the board
        # answers the second create with the first card.
        label = f"`{identity['cluster']}` (`{identity['project']}`, `{identity['location']}`)"
        agents.append({
            "name": name,
            "key": f"{CLUSTER_IDEMPOTENCY_KEY_PREFIX}{name}",
            "title": f"Report cluster inventory: {label}",
            "cluster_label": label,
        })
    return agents


def _unlisted_projects(data_dir: Path) -> list[tuple[str, str]]:
    """Projects in scope the last reconcile could not list, as (project, outcome).

    Read from the scope snapshot. No snapshot, an unreadable one, or one with every
    project `ok` all answer empty: the sweep then reads the roster as covering the
    whole scope, which is what it did before scopes existed.
    """
    try:
        snapshot = json.loads((data_dir / SCOPE_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent or unreadable: nothing to name
        return []
    out = []
    projects = snapshot.get("projects") if isinstance(snapshot, dict) else None
    for entry in projects if isinstance(projects, list) else []:
        if isinstance(entry, dict) and entry.get("id") and entry.get("outcome") != SCOPE_OUTCOME_OK:
            out.append((str(entry["id"]), str(entry.get("outcome") or SCOPE_OUTCOME_UNKNOWN)))
    return sorted(out)


def _unresolved_containers(data_dir: Path) -> list[tuple[str, str, int]]:
    """Every row of the snapshot's `containers` array the last reconcile could not resolve, as (id, outcome, projects).

    A row is a folder, an organisation, a Shared VPC host or a Metrics Scope; the kind is not read here.
    """
    try:
        snapshot = json.loads((data_dir / SCOPE_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent or unreadable: nothing to name
        return []
    containers = snapshot.get("containers") if isinstance(snapshot, dict) else None
    out = []
    for entry in containers if isinstance(containers, list) else []:
        if isinstance(entry, dict) and entry.get("id") and entry.get("outcome") != SCOPE_OUTCOME_OK:
            count = entry.get("projects") if isinstance(entry.get("projects"), int) else 0
            out.append((str(entry["id"]), str(entry.get("outcome") or SCOPE_OUTCOME_UNKNOWN), count))
    return sorted(out)


def _scope_has_other_projects(data_dir: Path) -> bool:
    """Whether the last reconcile's scope reaches beyond one project.

    True when the snapshot names more than one project, or any declared folder,
    organisation, Shared VPC host or Metrics Scope. The task body speaks of "the project" on an install with no scope,
    exactly as it did before scopes existed, and of "the projects in scope" only then,
    so a single-project install renders the same prompt as before.
    """
    try:
        snapshot = json.loads((data_dir / SCOPE_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - absent or unreadable: one project, as before
        return False
    projects = snapshot.get("projects") if isinstance(snapshot, dict) else None
    containers = snapshot.get("containers") if isinstance(snapshot, dict) else None
    if isinstance(containers, list) and any(isinstance(c, dict) and c.get("id") for c in containers):
        return True
    return isinstance(projects, list) and len([p for p in projects if isinstance(p, dict) and p.get("id")]) > 1


def _scope_gap_paragraph(data_dir: Path) -> str:
    """The sweep's note on projects, containers and selectors the reconcile could not resolve.

    Rendered when a declared folder, organisation, Shared VPC host or Metrics Scope could not
    be resolved (each has a row in the snapshot's `containers` array), or when the
    snapshot names more than one project (or any container) and one project was not listed. An install with
    no scope renders the prompt it rendered before scopes existed, whatever its one
    project's outcome: that prompt already tells the worker what to do when the project
    cannot be listed.
    """
    containers = _unresolved_containers(data_dir)
    unlisted = _unlisted_projects(data_dir)
    # A container the reconcile could not resolve is a gap on its own, even when it
    # carried no project rows (a first run, or a container the snapshot never reached).
    if not containers and not (unlisted and _scope_has_other_projects(data_dir)):
        return ""
    container_note = ""
    failed = [c for c in containers if c[1] != SCOPE_OUTCOME_OVER_CAP]
    over_cap = [c for c in containers if c[1] == SCOPE_OUTCOME_OVER_CAP]
    if failed:
        container_note += (
            "The last reconcile could not resolve "
            + ", ".join(f"`{cid}` ({outcome}, {count} project(s) carried)" for cid, outcome, count in failed)
            + ", so every project it reaches is unlisted and the container or selector is what to name. "
        )
    if over_cap:
        container_note += (
            "It resolved "
            + ", ".join(f"`{cid}` ({count} project(s))" for cid, _outcome, count in over_cap)
            + " past its listing cap, so those members are carried `over-cap` and unlisted; name the "
            "container as over the cap, which the declaration fixes (narrower excludes or sub-folders), "
            "not as unreadable. "
        )
    if unlisted:
        named = ", ".join(f"`{project}` ({outcome})" for project, outcome in unlisted[:SCOPE_GAP_NAMED_LIMIT])
        rest = len(unlisted) - SCOPE_GAP_NAMED_LIMIT
        if rest > 0:
            named += f", and {rest} more (the full list is in `fleet_scope.json`)"
        project_note = f"The last reconcile did not list these projects: {named}. "
    else:
        project_note = "Every project it did resolve was listed. "
    return (
        "**Some projects in scope have no Cluster Agents for a reason the roster cannot show.** "
        f"{container_note}{project_note}The roster holds only the "
        "clusters of theirs that already had a profile, and you cannot list them yourself. A "
        "project marked `over-cap` is reachable but past the reconcile's listing cap, and the "
        "others the last run could not list. Record each one in `gaps` as not fully covered, "
        "with its reason, so the sweep reads as partial rather than as a clean fleet.\n\n"
    )


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


def _reconcile_timeout_seconds() -> int:
    """The ceiling for this install: the reconcile's own listing budget for the declared
    `spec.scope.maxProjects`, its prune budget for that cap and the profiles on the volume,
    and RECONCILE_SETTLE_SECONDS, read through the reconcile's functions so the two cannot
    drift; RECONCILE_TIMEOUT_SECONDS when there is no declaration to read or the module
    cannot be imported (the value at the default cap with few profiles)."""
    try:
        import cluster_agent_profile as profiles  # beside this script in the pod, as for the roster
        import cluster_agent_reconcile as rec
        if not os.environ.get(rec.SCOPE_FILE_ENV):
            return RECONCILE_TIMEOUT_SECONDS
        cap = rec._cap_of(rec._load_scope()[0])
        try:
            count = len(profiles.list_profiles())
        except Exception:  # noqa: BLE001 - no roster yet is the bootstrap case; the floor's prune budget then
            count = 0
        ceiling = rec._list_budget_seconds(cap) + rec._prune_budget_seconds(cap, count) + RECONCILE_SETTLE_SECONDS
        return max(RECONCILE_TIMEOUT_SECONDS, int(ceiling))
    except Exception:  # noqa: BLE001 - the floor is the safe answer; never fail the tick over it
        return RECONCILE_TIMEOUT_SECONDS


def ensure_cluster_agents(data_dir: Path) -> bool:
    """Create the Cluster Agents, blocking until that finishes.

    Returns True when the sweep may be filed. False means "not yet, retry on the
    next tick" — the caller must not file the card, because a sweep filed against
    an empty roster tells its worker to audit every cluster itself, and the marker
    it writes keeps that card's instructions for the whole run.

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
    timeout = _reconcile_timeout_seconds()

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
            timeout=timeout,
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
      flight. ``main`` checks it before calling this, to run the hand-off
      instead, so here it only keeps this function's answer complete.
    - ``INVENTORY.raw.md`` — the hand-off ran; prioritization may still be
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
        or (data_dir / COMPLETED_MARKER).exists()
    )


def _task_body() -> str:
    multi = _scope_has_other_projects(_data_dir())
    every_cluster = "every cluster the projects in scope have" if multi else "every cluster the project has"
    cannot_list = "a project's clusters" if multi else "the project's clusters"
    lifecycle_holds = "the scope and its exclusions" if multi else "the `RECONCILE_EXCLUDE` opt-out"
    instruction_list = "\n".join(f"  - {p}" for p in INSTRUCTIONS_PATHS)
    cluster_audit_list = "\n".join(f"  - {p}" for p in CLUSTER_AUDIT_INSTRUCTIONS_PATHS)
    roster = "\n".join(f"    - `{a['name']}`: {a['cluster_label']}" for a in cluster_agents()) or "    (none)"
    return (
        "First-time onboarding discovery sweep. Follow the inventory SOP, reading whichever "
        "of these exists:\n"
        f"{instruction_list}\n\n"
        "Audit control plane options, node pools, Workload Identity settings, and running "
        "workloads. The Cluster Agents audit their own clusters; your part is below:\n\n"
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
        f"holds {lifecycle_holds} and the create/prune rules, so a profile you "
        "make by calling `cluster_agent_profile.py` directly is one the next reconcile run may "
        "immediately prune, and you will loop. Do not run it, and do not repair or delete a "
        "profile.\n\n"
        "**The roster may be empty or incomplete, and that is your finding to report, not "
        "yours to fix.** It says which clusters can audit themselves — not which clusters "
        f"count. Audit {every_cluster}: the ones with no Cluster Agent you take "
        "yourself in Step 3, and the report names each one as lacking an agent. A fleet swept "
        "without Cluster Agents is a degraded sweep and must read as one, because this report "
        "is delivered to the user as the state of their environment. If you cannot list "
        f"{cannot_list} at all, put that in `gaps` and complete the card anyway — "
        "onboarding runs once, and a report saying discovery failed is worth more than a thin "
        "one that reads as a clean fleet.\n\n"
        f"{_scope_gap_paragraph(_data_dir())}"
        "**Step 2 — do not fan out; the gate has.** This gate files one audit card per Cluster "
        "Agent itself, read from the profiles when this card was filed:\n\n"
        f"{roster}\n\n"
        "**Do not create cluster cards, and do not look the roster up yourself:** your terminal "
        "runs in a sandbox that has neither `hermes` nor the profiles' configuration. **If the "
        "list above is `(none)`, there are no Cluster Agents: audit every cluster yourself in "
        "Step 3.** That is the normal case for a single-cluster install and it is not an "
        "error.\n\n"
        "**Step 3 — audit the clusters the list does not cover**, following Steps 2 to 4 of the "
        "single-cluster audit SOP (its own numbering) for each, reading whichever of these "
        f"exists:\n{cluster_audit_list}\n\n"
        "and record each in that SOP's `metadata` shape. Usually there are none.\n\n"
        "**Step 4 — complete this card now. Do not wait for the per-cluster cards, do not write "
        f"`{RAW_INVENTORY_PATH}` or `{INVENTORY_PATH}`, and do not file a ranking card.** The "
        "onboarding gate waits for the per-cluster cards, writes the raw findings from their "
        "`metadata`, and files the ranking card itself. Call `kanban_complete` with a short "
        "factual `result` and this `metadata`: `fleet` — one `{project, cluster, location, "
        "status}` object per cluster Step 1 listed; `clusters` — the Step 3 audits, each in the "
        "single-cluster SOP's shape (an empty list when every cluster had an agent); `telemetry` — "
        "the PlatformAgent's `.status.telemetry`, one line; `gaps` — anything you could not do, and why. A cluster you could not reach goes in `gaps`, not "
        "silently out of `fleet`.\n\n"
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
    marker = data_dir / SCAN_FILED_MARKER
    if marker.exists():
        if not (data_dir / COMPLETED_MARKER).exists():
            _hand_off(data_dir, marker)
        return 0
    if should_skip(data_dir):
        return 0  # silent no-op: already filed, scanned, or delivered
    if not ensure_cluster_agents(data_dir):
        return 0  # roster not ready; the next tick retries, no marker written
    if file_scan_task(data_dir):
        # Files the Cluster Agents' cards now rather than a tick later.
        _hand_off(data_dir, marker)
    # Stdout stays empty on purpose — this job never speaks to the user.
    return 0


def _hand_off(data_dir: Path, marker: Path) -> None:
    try:
        bootstrap_handoff.hand_off(data_dir, marker, _parse_task_id, roster=cluster_agents)
    except Exception as e:  # noqa: BLE001 - never fail the cron run; the next tick retries
        sys.stderr.write(f"bootstrap_scan_gate: hand-off failed: {e!r}\n")


if __name__ == "__main__":
    raise SystemExit(main())
