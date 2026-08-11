#!/usr/bin/env python3
"""Reconcile the image's cron job declarations into the running agent's job file.

Why this exists
---------------
``docker-entrypoint.sh`` syncs ``/opt/defaults`` onto the PVC with ``cp -ru``,
which copies a file only when the *source* is newer. The Hermes scheduler writes
``last_run`` back into ``$HERMES_HOME/cron/jobs.json`` on every tick, so the PVC
copy is **always** newer than the image's. The force-sync beside it covers
``config.yaml SOUL.md AGENTS.md CAPABILITIES.md`` and not ``cron/``.

The consequence is that a cron job added to ``agents/chat/defaults/cron/jobs.json``
never appears on an existing deployment — only on a fresh PVC. That is the same
class of silent failure the force-sync was introduced to fix, applied to the file
that decides what the agent does unattended.

A blanket ``cp -f`` is not the remedy. It would reset every ``last_run`` (making
every job look due at once), discard the chat binding the ``bootstrap_onboarding``
plugin writes (``deliver: origin`` plus ``origin``), and resurrect the two
onboarding jobs that ``bootstrap_delivery.py:_cleanup`` deliberately removes once
onboarding has finished. So this merges by job id instead.

The split
---------
Per key, not per field-list: **the image wins every key it ships, and every key
it does not ship is left as the volume had it.** ``name``, ``schedule``,
``prompt``, ``script``, ``skills``, ``no_agent`` and ``enabled`` are all shipped,
so all of them track the image. The scheduler's own state is not shipped by
anything, so all of it survives — including whatever Hermes starts recording
next, which is the reason for the rule rather than a list. See ``merge_job``.

This is `profile_scaffold.merge_cron_store`'s rule, deliberately: that function
governs the Platform Agent's roster and this one governs the Chat Agent's, and
two rosters obeying opposite merge rules is a trap for whoever edits either.

``enabled`` being image-owned is the load-bearing consequence. Shipping
``enabled: false`` is how a watchdog is turned off fleet-wide, which is the
protocol ``concepts/autonomous-watchdogs.md`` documents and the one the five
retired watchdogs took. The cost is the other direction: a job disabled by hand
on a live pod is switched back on by the next image roll, because the image is
the declaration of record. Retire a job by shipping ``enabled: false`` and
leaving the entry in place; dropping the id is safe only once every live cluster
has merged that disabled form, since nothing here prunes.

``RUNTIME_WINS`` is the single exception, for the one shipped key a runtime hook
owns.

The ledger
----------
A job absent from the runtime file is ambiguous — either it is new in this image,
or it was deliberately removed at runtime (which is exactly what ``_cleanup``
does). The ledger records every id this script has installed, which separates the
two: an id in the ledger but missing from the runtime file was removed on purpose
and is never reinstalled.

The ledger cannot help on its *first* run, though, because it starts empty: a
deployment that finished onboarding before this script existed has no record that
the two onboarding jobs were retired, so they would look new and come back. They
would come back inert — both scripts check ``.bootstrap_completed`` and return
silently — but they would tick every minute forever. ``--assume-retired`` closes
that: the caller, which is the only thing that knows *why* a job is gone, seeds
those ids into the ledger. The entrypoint passes the onboarding ids when
``.bootstrap_completed`` exists.

Concurrency
-----------
Within one pod there is one writer, and it takes two separate facts to say so.
This runs from the entrypoint ahead of ``exec "$@"``, so the scheduler in that
container has not started; and step 1.5 of the entrypoint hands the shared tree to
a single owning container, so the dashboard — which runs the same image over the
same PVC, and has no scheduler of its own — never reaches the step that calls this.

**Both facts are scoped to one pod, and that is the limit of the guarantee.** Step
1.5 elects an owner per pod, not per volume: the operator sets
``AGENT_SHARED_STATE_SETUP=owner`` on the gateway container of the pod *template*
(``platformagent_manifests.go``), so every replica's gateway is an owner. At
``availability.replicas > 1`` the operator gives those replicas one shared volume —
``getDefaultStorageConfig`` switches the data PVC to ReadWriteMany on
``standard-rwx``, and ``useStatefulSet`` stays false without custom ReadWriteOnce
storage, so it is several pods against one PVC rather than one volume each. A
rolling update then runs this in a new pod while old pods are still up
(``maxUnavailable`` is 25%, which rounds down to 0 of 2).

What that costs is bounded but real, and stating it precisely matters more than
naming a mechanism:

* Concurrent runs of this script cannot tear ``jobs.json`` — ``write_json`` names
  its temp file per *pod* (see ``writer_tag``; the pid alone is not distinct
  across replicas), so every ``os.replace`` publishes a whole file — but two of
  them can still lose one merge to the other. The loser retries next boot.
* A merge racing a live scheduler's own write is the case that does not
  self-correct. If a tick's write lands between this script's ``jobs.json`` write
  and its ledger write, the job is absent from the volume with its id recorded as
  installed, which ``reconcile`` reads as "removed on purpose" on that boot and
  every boot after it.

How wide that second window is depends on whether Hermes' ``mark_job_run``
re-reads the store or writes back a list cached at tick time. This module does not
know, and neither did the comment that used to assert the latter here. The repo's
own evidence points the other way — ``bootstrap_delivery._cleanup`` removes a job
mid-run and relies on the subsequent ``mark_job_run`` warning about a missing job
rather than resurrecting it, which only happens if it re-reads — so the exposure is
probably a narrow read-modify-write window rather than a guaranteed clobber. It is
recorded as unverified because nobody has read ``/opt/hermes/cron/jobs.py`` to
settle it, and an unverified premise stated as fact is what put the wrong claim
here in the first place.

Neither is fixable from inside this script: the competing writer is a scheduler in
another pod that takes no lock this one could hold. Running the reconcile once per
volume — behind leader election, or from an init container or per-rollout Job —
is the fix, and it is a change to the deployment topology rather than to this file.
Single-replica installs, which are the default, are unaffected.
"""

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path

# The one key the image ships whose value the *deployment* nevertheless owns.
#
#   deliver   bootstrap_onboarding's pre_llm_call hook rewrites it to "origin"
#             on the delivery job, alongside an `origin` binding. Taking the
#             image's "local" back would emit the single-use onboarding report
#             into the void.
#
# `origin` needs no entry: no shipped job carries the key, so the per-key rule
# below already leaves it alone. Neither does any scheduler field — that is the
# point of the rule. See `merge_job`.
RUNTIME_WINS = ("deliver",)

DEFAULT_LEDGER_NAME = ".cron_jobs_installed"


def log(msg: str) -> None:
    print(f"[CRON-JOBS-SYNC] {msg}", file=sys.stderr)


def load_jobs(path: Path) -> tuple[object, list[dict]]:
    """Read a jobs file, returning (container, jobs).

    Both shapes seen in this repo are accepted — a ``{"jobs": [...]}`` wrapper and
    a bare list — and the shape that was read is the shape written back, so this
    script never silently rewrites the file's structure.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return data, data["jobs"]
    if isinstance(data, list):
        return data, data
    raise ValueError(f"{path}: expected a list of jobs or an object with a 'jobs' list")


def load_ledger(path: Path) -> set[str]:
    """Read the installed-id ledger. A missing or unreadable ledger reads as empty."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if isinstance(raw, list):
        return {str(x) for x in raw}
    if isinstance(raw, dict) and isinstance(raw.get("installed"), list):
        return {str(x) for x in raw["installed"]}
    return set()


def merge_job(image_job: dict, runtime_job: dict) -> dict:
    """Refresh one job from the image, key by key.

    **The image wins every key it ships, and every key it does not ship is left
    as the volume had it.** That is `merge_cron_store`'s rule, verbatim, and it
    is the rule because it needs no inventory of Hermes's state fields. An
    allowlist of "runtime-owned" names has to be complete to be correct, and it
    is complete only against the Hermes that was current when it was written:
    the day upstream records something new per job, this function starts erasing
    it on every pod start, silently, and nothing here can tell.

    That is not hypothetical. ``tools/cronjob_tools.py`` reads ``last_status``
    and ``last_error`` back out of the job dict, and `merge_cron_store` names
    ``last_run_at`` and ``next_run_at`` as the fields that matter — losing
    ``last_run_at`` re-fires a one-shot, because that field *is* its already-ran
    guard, and a wiped ``next_run_at`` is recomputed from now, so a merely-late
    recurring job is skipped rather than caught up. Not one of those four is
    ``last_run``, which is the name the allowlist here used to carry.

    `RUNTIME_WINS` is the exception, and it inverts the risk: it can only ever
    name a key this repo puts in the image file, so it cannot fall behind an
    upstream that starts writing something new. A key absent from the runtime
    job is not one the deployment owns, so the image's value stands — otherwise
    adding ``deliver: "all"`` to an existing job would be stripped on arrival,
    which is the silent-alert-drop this file's own history is about.
    """
    merged = {**image_job, **{k: v for k, v in runtime_job.items() if k not in image_job}}
    for field in RUNTIME_WINS:
        if field in runtime_job:
            merged[field] = runtime_job[field]
    return merged


def reconcile(
    image_jobs: list[dict], runtime_jobs: list[dict], ledger: set[str]
) -> tuple[list[dict], set[str], dict[str, list[str]]]:
    """Merge image declarations into the runtime job list.

    Returns the new job list, the new ledger, and a summary of what changed.

    Jobs present at runtime but absent from the image are left untouched: they may
    have been added by an operator, and this script is not authoritative enough to
    delete work. Retirement therefore does not propagate — a job dropped from the
    image lingers on existing deployments until removed by hand. That is the safe
    direction to be wrong in, and it is the narrower half of the bug being fixed.
    """
    by_id = {j.get("id"): j for j in runtime_jobs if isinstance(j, dict) and j.get("id")}
    summary: dict[str, list[str]] = {"added": [], "refreshed": [], "skipped_removed": []}

    result: list[dict] = []
    seen: set[str] = set()

    for image_job in image_jobs:
        job_id = image_job.get("id")
        if not job_id:
            log("WARN: image job without an 'id'; skipping")
            continue
        seen.add(job_id)
        existing = by_id.get(job_id)
        if existing is not None:
            merged = merge_job(image_job, existing)
            result.append(merged)
            if merged != existing:
                summary["refreshed"].append(job_id)
        elif job_id in ledger:
            # Installed by an earlier boot and gone now: removed on purpose
            # (bootstrap_delivery._cleanup). Reinstalling would undo that.
            summary["skipped_removed"].append(job_id)
        else:
            result.append(dict(image_job))
            summary["added"].append(job_id)

    # Preserve runtime-only jobs, in their original relative order, after the
    # image-declared ones.
    for job in runtime_jobs:
        if not isinstance(job, dict):
            continue
        job_id = job.get("id")
        if job_id not in seen:
            result.append(job)

    return result, ledger | {j.get("id") for j in image_jobs if j.get("id")}, summary


def writer_tag() -> str:
    """A suffix that is distinct per *pod*, not merely per process.

    The pid alone does not do it, which is worth spelling out because it is the
    obvious choice and it is wrong here. Every replica boots the same image
    through the same entrypoint in the same order, and each container gets its
    own pid namespace, so this script is very likely handed the *same* small pid
    in every pod of a scale-up. Two writers agreeing on ``jobs.json.tmp.7`` are
    no better off than two agreeing on ``jobs.json.tmp``.

    The pod name is the disambiguator that actually holds: the kubelet sets the
    container hostname to it, it is unique across the cluster, and it is not
    reused. The pid is kept after it for the same-pod case — a restarted
    container has the same hostname as the run that died, and this leaves the
    corpse of a killed writer distinguishable from the live one.

    Outside Kubernetes ``gethostname`` still answers, and the pid carries the
    difference between two writers on one host.
    """
    host = os.environ.get("HOSTNAME") or socket.gethostname() or "nohost"
    # A pod name is RFC 1123 and cannot contain a separator, but this also runs
    # in tests and on a workstation, where the hostname is whatever it is.
    host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return f"{host}.{os.getpid()}"


def write_json(path: Path, payload: object) -> None:
    """Write JSON through a temp file in the same directory, then rename.

    The temp name is per writer because a fixed one is private only while there
    is exactly one, and at ``availability.replicas > 1`` several pods run this
    against one ReadWriteMany volume (see Concurrency above). Two writers sharing
    ``jobs.json.tmp`` can interleave into the very tear the rename exists to
    prevent: A truncates and begins writing while B's ``os.replace`` installs the
    half-written file as ``jobs.json``. With a distinct name each ``os.replace``
    publishes a whole file, so concurrent writers can lose a merge to each other
    but cannot produce a torn one — and an agent with a stale schedule still has
    a schedule. See ``writer_tag`` for why the name is not just the pid.
    """
    tmp = path.with_name(f"{path.name}.tmp.{writer_tag()}")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        # A temp file that outlives its writer is not inert: the tag is stable
        # across a container restart, so the next boot of this same pod inherits
        # a file it did not finish and truncates it blind.
        tmp.unlink(missing_ok=True)
        raise


def sync(
    image_path: Path,
    runtime_path: Path,
    ledger_path: Path,
    dry_run: bool = False,
    assume_retired: set[str] | None = None,
) -> int:
    if not image_path.is_file():
        log(f"no image job file at {image_path}; nothing to reconcile")
        return 0
    if not runtime_path.is_file():
        # cp -ru in the entrypoint creates this on a fresh PVC. If it is missing,
        # the copy has not happened (or failed) and inventing one here would race
        # with it.
        log(f"no runtime job file at {runtime_path}; leaving it to the defaults copy")
        return 0

    try:
        _, image_jobs = load_jobs(image_path)
    except (OSError, ValueError) as e:
        log(f"WARN: cannot read image job file: {e}")
        return 1
    try:
        runtime_container, runtime_jobs = load_jobs(runtime_path)
    except (OSError, ValueError) as e:
        log(f"WARN: cannot read runtime job file: {e}")
        return 1

    ledger = load_ledger(ledger_path) | (assume_retired or set())
    merged, new_ledger, summary = reconcile(image_jobs, runtime_jobs, ledger)

    for label, ids in summary.items():
        if ids:
            log(f"{label}: {', '.join(sorted(ids))}")
    if not any(summary.values()) and ledger == new_ledger:
        return 0

    if dry_run:
        log("dry run; not writing")
        return 0

    if isinstance(runtime_container, dict):
        runtime_container["jobs"] = merged
        payload: object = runtime_container
    else:
        payload = merged

    try:
        write_json(runtime_path, payload)
    except OSError as e:
        log(f"WARN: could not write {runtime_path}: {e}")
        return 1

    # The ledger is written only after the jobs file lands. The other order
    # would record an id as installed that never made it to disk, and the
    # never-resurrect rule would then keep it out for good.
    try:
        write_json(ledger_path, sorted(new_ledger))
    except OSError as e:
        log(f"WARN: could not write ledger {ledger_path}: {e}")
        return 1

    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--image-jobs",
        default="/opt/defaults/cron/jobs.json",
        help="Job file baked into the image (source of truth for job definitions).",
    )
    ap.add_argument(
        "--runtime-jobs",
        default=None,
        help="Job file the scheduler reads. Defaults to $HERMES_HOME/cron/jobs.json.",
    )
    ap.add_argument(
        "--ledger",
        default=None,
        help=f"Installed-id ledger. Defaults to $HERMES_HOME/{DEFAULT_LEDGER_NAME}.",
    )
    ap.add_argument(
        "--assume-retired",
        default="",
        help="Comma-separated job ids to treat as already retired (see module docstring).",
    )
    ap.add_argument("--dry-run", action="store_true", help="Report what would change and exit.")
    args = ap.parse_args(argv)

    hermes_home = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    runtime = Path(args.runtime_jobs) if args.runtime_jobs else hermes_home / "cron" / "jobs.json"
    ledger = Path(args.ledger) if args.ledger else hermes_home / DEFAULT_LEDGER_NAME
    assume_retired = {s.strip() for s in args.assume_retired.split(",") if s.strip()}

    return sync(
        Path(args.image_jobs),
        runtime,
        ledger,
        dry_run=args.dry_run,
        assume_retired=assume_retired,
    )


if __name__ == "__main__":
    sys.exit(main())
