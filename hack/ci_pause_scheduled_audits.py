"""Pause the Platform Agent's clock-started audits for an eval run. Runs in the agent container.

Usage: python3 - <home> [--resume] < ci_pause_scheduled_audits.py

hack/ci-eval-pr.sh pipes this into the gateway once per lease, before any unit runs
(scheduled_audits pause; the EXIT trap runs scheduled_audits resume). The governance watchdogs fire on the clock between 06:20 and
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

Those four still fire on their clock (06:20, 06:50, Mondays 07:50, 09:20 UTC), and
their `finish` can open remediation pull requests like any audit's, so a write from
one of them mid-eval is still possible (#3020 tracks it).

Records what it paused in `<home>/.ci-paused-audits.json`, and `--resume` resumes
exactly those. ci-eval-pr.sh runs the resume from its EXIT trap: a Prow lease's
teardown sweeps the volume anyway, but a developer's run against an install that
outlives it would otherwise leave five audits off for good.

Prints one line naming what it did. Exit codes, which the caller's retry reads:
0 done; 1 worth another try (a pause that timed out, a store it couldn't read
back); 3 decided, so a retry would only repeat it (its inputs are unreadable, or
Hermes refused a pause or wrote no markers).
"""

import ast
import json
import os
import subprocess
import sys

home = sys.argv[1]
RESUME = sys.argv[2:] == ["--resume"]
PROFILE = "platform"
ROSTER = os.path.join(home, "profiles", PROFILE, "cron", "jobs.json")
RECORD = os.path.join(home, ".ci-paused-audits.json")
# The image's copy, not the volume's: the volume is the agent's to write.
STAGE_SOURCE = os.environ.get("OOBE_STAGE_SOURCE", "/opt/defaults/scripts/oobe.py")
STAGE_AUDITS = "FIRST_RUN_AUDITS"
# The roster the image ships, which `profile_scaffold` merges onto the volume's.
SHIPPED_ROSTER = os.environ.get("PLATFORM_ROSTER_SOURCE", "/opt/platform-template/cron/jobs.json")
# Absolute, as profile_scaffold.py runs it, so PATH order can't change which binary answers.
HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
# Per call. Five pauses at this bound stay well inside the caller's
# EVAL_PAUSE_AUDITS_TIMEOUT_SECONDS, so a stalled pause is named here rather than
# cut off by the caller's timeout before this prints.
HERMES_TIMEOUT_SECONDS = 30
PAUSED_STATE = "paused"
OK, RETRY, DECIDED = 0, 1, 3
EXCERPT_CHARS = 200


def jobs_in(path):
    with open(path, encoding="utf-8") as fh:
        stored = json.load(fh)
    jobs = stored.get("jobs", []) if isinstance(stored, dict) else stored
    if not isinstance(jobs, list):
        raise ValueError(f"{path}: its jobs are not a list")
    # Ids as strings, as profile_scaffold.merge_cron_store reads them.
    return [{**job, "id": str(job["id"])} for job in jobs if isinstance(job, dict) and job.get("id")]


def stage_audits():
    tree = ast.parse(open(STAGE_SOURCE, encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == STAGE_AUDITS for t in node.targets):
            return {str(audit) for audit in ast.literal_eval(node.value)}
    raise ValueError(f"{STAGE_SOURCE} names no {STAGE_AUDITS}")


def paused(job):
    """A pause marker alone is a pause, whatever `enabled` says.

    Hermes' `is_job_runnable`, `ci_platform_runs.runnable` and `oobe.py` all read it so:
    after a restart the merge puts `enabled` back to true and leaves the markers.
    """
    return job.get("state") == PAUSED_STATE or bool(job.get("paused_at"))


def hermes(action, jid):
    """One `hermes cron <action>`; returns (retryable, failure) or (False, None)."""
    try:
        result = subprocess.run(
            [HERMES_BIN, "-p", PROFILE, "cron", action, jid],
            capture_output=True, text=True, timeout=HERMES_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return True, f"{jid} (timed out after {HERMES_TIMEOUT_SECONDS}s)"
    except OSError as exc:
        return False, f"{jid} ({exc})"
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout).split())[:EXCERPT_CHARS]
        return False, f"{jid} (exit {result.returncode}: {detail})"
    return False, None


def read_record():
    try:
        with open(RECORD, encoding="utf-8") as fh:
            return [str(jid) for jid in json.load(fh)]
    except (OSError, ValueError, TypeError):
        return []


def write_record(ids):
    tmp = f"{RECORD}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(sorted(ids), fh)
    os.replace(tmp, RECORD)


def pause():
    try:
        jobs = jobs_in(ROSTER)
        shipped = {job["id"]: job for job in jobs_in(SHIPPED_ROSTER)}
        held_by_stage = stage_audits()
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"nothing paused: {exc}")
        return DECIDED
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

    failed, retryable = [], False
    for jid in targets:
        again, failure = hermes("pause", jid)
        retryable |= again
        if failure:
            failed.append(failure)

    # Believe the store, not the exit codes.
    try:
        after = {job["id"]: job for job in jobs_in(ROSTER)}
    except (OSError, ValueError) as exc:
        print(f"asked to pause {', '.join(targets) or 'nothing'}, but the roster could not be read back: {exc}")
        return RETRY
    done = [jid for jid in targets if paused(after.get(jid, {}))]
    named = {f.split(" ", 1)[0] for f in failed}
    failed += [f"{jid} (not paused in the store)" for jid in targets if jid not in done and jid not in named]
    if done:
        try:
            write_record(set(read_record()) | set(done))
        except OSError as exc:
            failed.append(f"record ({exc}; --resume will not know these)")
    print(
        f"paused {', '.join(done) or 'nothing'}; left {', '.join(left) or 'nothing'}"
        + (f"; FAILED {', '.join(failed)}" if failed else "")
    )
    if not failed:
        return OK
    return RETRY if retryable else DECIDED


def resume():
    ids = read_record()
    if not ids:
        print("nothing to resume")
        return OK
    try:
        live = {job["id"]: job for job in jobs_in(ROSTER)}
    except (OSError, ValueError) as exc:
        print(f"could not read the roster to resume {', '.join(ids)}: {exc}")
        return RETRY
    failed, retryable = [], False
    targets = [jid for jid in ids if paused(live.get(jid, {}))]
    for jid in targets:
        again, failure = hermes("resume", jid)
        retryable |= again
        if failure:
            failed.append(failure)
    try:
        after = {job["id"]: job for job in jobs_in(ROSTER)}
    except (OSError, ValueError) as exc:
        print(f"asked to resume {', '.join(targets)}, but the roster could not be read back: {exc}")
        return RETRY
    still = [jid for jid in targets if paused(after.get(jid, {}))]
    if not still:
        os.remove(RECORD)
    print(
        f"resumed {', '.join(jid for jid in targets if jid not in still) or 'nothing'}"
        + (f"; FAILED {', '.join(failed) or ', '.join(still)}" if still else "")
    )
    if not still:
        return OK
    return RETRY if retryable else DECIDED


sys.exit(resume() if RESUME else pause())
