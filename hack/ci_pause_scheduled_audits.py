"""Pause the Platform Agent's clock-started audits for an eval run. Runs in the agent container.

Usage: python3 - <home> < ci_pause_scheduled_audits.py

hack/ci-eval-pr.sh pipes this into the gateway once per lease, before any unit runs
(pause_scheduled_audits). The governance watchdogs fire on the clock between 06:20 and
09:20 UTC, and a run that writes to the GitOps repository mid-eval is charged to
whichever cases are running when it lands: `no-github-writes-the-case-did-not-request`
dates a write, it doesn't attribute it (#2611). The 08:00 networking audit's
remediation pull requests failed four unrelated cases that way (#3020). Cases measure
request handling; each audit has cases of its own, which start their runs themselves.

Pauses with Hermes' own `cron pause`, which takes the store's lock and writes
`enabled: false` plus the pause markers (`state`, `paused_at`). The markers are what
make it last. `profile_scaffold.merge_cron_store` puts the image's `enabled` back on
every container start, but the image ships no `state` or `paused_at`, so those
survive, and Hermes' due scan refuses a job that carries them and disables it again
(`_self_disable_half_paused`). A gateway restart mid-run therefore keeps the pause.

Pauses only jobs the image ships (`/opt/platform-template/cron/jobs.json`), judged
by the image's copy of each. Leaves alone:
- any job the image doesn't ship: an operator's own, or a stale id an old roster
  left on a long-lived volume;
- the `no_agent` jobs, which are plumbing (workspace GC, board health, delivery watch)
  and write nothing to the repository;
- the audits the Chat Agent's `oobe` stage starts on a fresh install
  (`FIRST_RUN_AUDITS`, read from the image's copy of oobe.py). The stage holds a
  paused audit rather than starting it, and the oobe cases grade that chain.
- a job already disabled, which has nothing to pause.

Prints one line naming what it paused and what it left. Exits 1 when a pause it
asked for didn't take, so the caller can say so.
"""

import ast
import json
import os
import subprocess
import sys

home = sys.argv[1]
PROFILE = "platform"
ROSTER = os.path.join(home, "profiles", PROFILE, "cron", "jobs.json")
# The image's copy, not the volume's: the volume is the agent's to write.
STAGE_SOURCE = os.environ.get("OOBE_STAGE_SOURCE", "/opt/defaults/scripts/oobe.py")
STAGE_AUDITS = "FIRST_RUN_AUDITS"
# The roster the image ships, which `profile_scaffold` merges onto the volume's.
SHIPPED_ROSTER = os.environ.get("PLATFORM_ROSTER_SOURCE", "/opt/platform-template/cron/jobs.json")
HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")
PAUSE_TIMEOUT_SECONDS = 60
PAUSED_STATE = "paused"


def jobs_in(path):
    with open(path, encoding="utf-8") as fh:
        stored = json.load(fh)
    jobs = stored.get("jobs", []) if isinstance(stored, dict) else stored
    if not isinstance(jobs, list):
        raise ValueError(f"{path}: its jobs are not a list")
    return [job for job in jobs if isinstance(job, dict) and job.get("id")]


def stage_audits():
    tree = ast.parse(open(STAGE_SOURCE, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == STAGE_AUDITS for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise ValueError(f"{STAGE_SOURCE} names no {STAGE_AUDITS}")


def paused(job):
    return not job.get("enabled", True) and (job.get("state") == PAUSED_STATE or bool(job.get("paused_at")))


def main():
    try:
        jobs = jobs_in(ROSTER)
        shipped = {job["id"]: job for job in jobs_in(SHIPPED_ROSTER)}
        held_by_stage = stage_audits()
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"nothing paused: {exc}")
        return 1
    targets, left = [], []
    for job in jobs:
        jid = job["id"]
        if jid not in shipped:
            left.append(f"{jid} (not shipped)")
            continue
        if shipped[jid].get("no_agent"):
            continue
        if jid in held_by_stage:
            left.append(f"{jid} (oobe stage)")
        elif paused(job):
            left.append(f"{jid} (already paused)")
        elif not job.get("enabled", True):
            left.append(f"{jid} (disabled)")
        else:
            targets.append(jid)

    failed = []
    for jid in targets:
        try:
            result = subprocess.run(
                [HERMES_BIN, "-p", PROFILE, "cron", "pause", jid],
                capture_output=True, text=True, timeout=PAUSE_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failed.append(f"{jid} ({exc})")
            continue
        if result.returncode != 0:
            failed.append(f"{jid} (exit {result.returncode}: {' '.join((result.stderr or result.stdout).split())[:200]})")

    # Believe the store, not the exit codes.
    try:
        after = {job["id"]: job for job in jobs_in(ROSTER)}
    except (OSError, ValueError) as exc:
        print(f"paused {', '.join(targets) or 'nothing'}, but the roster could not be read back: {exc}")
        return 1
    done = [jid for jid in targets if paused(after.get(jid, {}))]
    failed += [f"{jid} (not paused in the store)" for jid in targets if jid not in done and not any(f.startswith(f"{jid} ") for f in failed)]
    print(
        f"paused {', '.join(done) or 'nothing'}; left {', '.join(left) or 'nothing'}"
        + (f"; FAILED {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


sys.exit(main())
