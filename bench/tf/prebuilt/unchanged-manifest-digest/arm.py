# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Fire one scheduled fleet-audit run on the install and wait for it to end.

Runs in the agent container, streamed in by main.tf, after plant.py left the
previous scheduled run in the store. The shape is bench/tf/prebuilt/audit-chat-report/arm.py's,
with one more step: an eval run pauses the clock-started audits
(hack/ci_pause_scheduled_audits.py), and Hermes' ``trigger_job`` re-enables a
paused job, so ``wait`` and ``clean`` pause it again when ``fire`` found it
paused. Four commands:

  idle <home> <job> <seconds>  wait until no run of <job> is claimed or running
  fire <home> <python> <job>   mark <job> due with ``cron.jobs.trigger_job`` and
                               record when, and whether it was paused, in STATE_FILE
  wait <home> <job> <seconds>  wait until a run claimed after the mark has finished;
                               pause the job again when it was paused
  clean <home>                 pause the job again if that is still owed; remove STATE_FILE

Runs are read from the profile's ``cron/executions.db``. The run is a real
scheduled run: the profile's cron tick claims the mark and runs the stream's
roster prompt, which is what passes ``--scheduled`` to ``precheck`` and ``finish``.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

STATE_NAME = ".bench-unchanged-manifest-digest.json"
PROFILE = "platform"
PROFILE_HOME = ("profiles", PROFILE)
ROSTER = ("cron", "jobs.json")
EXECUTIONS = ("cron", "executions.db")
ACTIVE = ("claimed", "running")
SKIPPED = "skipped"
PAUSED_STATE = "paused"
SQLITE_BUSY_TIMEOUT_S = 10
TRIGGER_SCRIPT = "import sys\nfrom cron.jobs import trigger_job\nsys.exit(0 if trigger_job(sys.argv[1]) else 3)\n"
TRIGGER_TIMEOUT_S = 60
HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
HERMES_TIMEOUT_S = 30
POLL_S = 20


def _state_path(home):
    return os.path.join(home, STATE_NAME)


def _job(home, job_id):
    with open(os.path.join(home, *PROFILE_HOME, *ROSTER), encoding="utf-8") as handle:
        store = json.load(handle)
    jobs = store.get("jobs") if isinstance(store, dict) else store
    for job in jobs or []:
        if isinstance(job, dict) and job.get("id") == job_id:
            return job
    raise SystemExit(f"{job_id} is not in the Platform Agent's cron roster")


def _paused(job):
    """What hack/ci_pause_scheduled_audits.py and Hermes' due scan read as paused."""
    return job.get("enabled") is False or job.get("state") == PAUSED_STATE or bool(job.get("paused_at"))


def _executions(home, job_id):
    """The job's runs as dicts, newest claim first."""
    path = os.path.join(home, *PROFILE_HOME, *EXECUTIONS)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_S)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT status, claimed_at, started_at, finished_at, delivery_outcome FROM executions "
            "WHERE job_id = ? ORDER BY claimed_at DESC",
            (job_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _read(home):
    with open(_state_path(home), encoding="utf-8") as handle:
        return json.load(handle)


def _write(home, state):
    with open(_state_path(home), "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def _pause_again(home, state):
    """Put the pause back that `trigger_job` lifted; best effort, recorded in the state."""
    if not state.get("was_paused") or state.get("repaused"):
        return
    try:
        done = subprocess.run(
            [HERMES_BIN, "-p", PROFILE, "cron", "pause", state["job_id"]],
            capture_output=True, text=True, timeout=HERMES_TIMEOUT_S, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # A hung or missing CLI is no reason for `wait` or `clean` to fail:
        # the pause is best effort, and the state file is still removed.
        print(f"WARNING: could not pause {state['job_id']} again: {exc}", file=sys.stderr)
        return
    if done.returncode == 0:
        state["repaused"] = True
        print(f"paused {state['job_id']} again, as the eval run had it")
    else:
        print(f"WARNING: could not pause {state['job_id']} again: {(done.stderr or done.stdout).strip()}", file=sys.stderr)
    _write(home, state)


def idle(home, job_id, seconds):
    _job(home, job_id)
    deadline = time.monotonic() + float(seconds)
    while any(run["status"] in ACTIVE for run in _executions(home, job_id)):
        if time.monotonic() > deadline:
            raise SystemExit(f"a {job_id} run is still claimed or running after {seconds}s")
        print(f"a {job_id} run is going; waiting for it to finish")
        time.sleep(POLL_S)


def fire(home, python, job_id):
    job = _job(home, job_id)
    was_paused = _paused(job)
    fired_at = datetime.now(timezone.utc).isoformat()
    _write(home, {"job_id": job_id, "fired_at": fired_at, "was_paused": was_paused, "repaused": False})
    env = {**os.environ, "HERMES_HOME": os.path.join(home, *PROFILE_HOME)}
    done = subprocess.run(
        [python, "-c", TRIGGER_SCRIPT, job_id],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TRIGGER_TIMEOUT_S, check=False,
    )
    if done.returncode != 0:
        raise SystemExit(f"trigger_job({job_id}) failed: {(done.stderr or done.stdout).strip()}")
    print(f"marked {job_id} due at {fired_at}{' (it was paused; it is paused again after the run)' if was_paused else ''}")


def wait(home, job_id, seconds):
    state = _read(home)
    fired = datetime.fromisoformat(state["fired_at"])
    deadline = time.monotonic() + float(seconds)
    try:
        while True:
            ours = [
                run for run in _executions(home, job_id)
                if run["status"] != SKIPPED and run["started_at"]
                and run["claimed_at"] and datetime.fromisoformat(run["claimed_at"]) > fired
            ]
            if ours and ours[-1]["finished_at"]:
                run = ours[-1]
                state.update(last_run_at=run["finished_at"], delivery_outcome=run["delivery_outcome"])
                _write(home, state)
                print(f"{job_id} finished at {run['finished_at']}: {run['status']}, {run['delivery_outcome']}")
                return
            if time.monotonic() > deadline:
                raise SystemExit(f"{job_id} did not finish a run within {seconds}s of {state['fired_at']}")
            time.sleep(POLL_S)
    finally:
        _pause_again(home, state)


def clean(home):
    try:
        state = _read(home)
    except (OSError, ValueError):
        state = None
    if state:
        _pause_again(home, state)
    try:
        os.unlink(_state_path(home))
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    command, home = sys.argv[1], sys.argv[2]
    if command == "idle":
        idle(home, sys.argv[3], sys.argv[4])
    elif command == "fire":
        fire(home, sys.argv[3], sys.argv[4])
    elif command == "wait":
        wait(home, sys.argv[3], sys.argv[4])
    elif command == "clean":
        clean(home)
    else:
        raise SystemExit(f"unknown command {command!r}")
