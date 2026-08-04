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
Image-owned fields track the image and are overwritten: ``name``, ``schedule``,
``prompt``, ``script``, ``skills``, ``no_agent`` — anything describing *what the
job is*. Runtime-owned fields are preserved: see ``RUNTIME_OWNED_FIELDS``.

``enabled`` is runtime-owned on purpose: an operator who disables a job in a live
deployment must not have it switched back on by an image roll. To retire a job,
remove it from the image file rather than disabling it there.

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
This runs from the entrypoint before ``exec "$@"`` starts Hermes, so the scheduler
is not yet running and there is no second writer. The write still goes through a
temp file and ``os.replace``: a torn ``jobs.json`` would leave the agent with no
schedule at all.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Fields that belong to the running deployment, not to the image. Everything not
# listed here is refreshed from the image on every start.
#
#   last_run  scheduler state; resetting it makes every job fire at once
#   enabled   an operator's runtime decision (see module docstring)
#   deliver   rewritten to "origin" by bootstrap_onboarding's pre_llm_call hook
#   origin    the chat binding that same hook writes; meaningless from an image
RUNTIME_OWNED_FIELDS = ("last_run", "enabled", "deliver", "origin")

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
    """Refresh one job from the image while keeping its runtime-owned fields."""
    merged = dict(image_job)
    for field in RUNTIME_OWNED_FIELDS:
        if field in runtime_job:
            merged[field] = runtime_job[field]
        else:
            merged.pop(field, None)
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


def write_json(path: Path, payload: object) -> None:
    """Write JSON through a temp file in the same directory, then rename."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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
