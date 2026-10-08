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

"""Arm the first-run audits stage on a long-lived install. Runs in the agent container.

Usage: python3 - <home> <hermes> <run-suffix> [<shipped jobs.json>] < arm.py

Puts the install where a fresh one is when its inventory scan settles, as far as
`agents/chat/scripts/oobe.py` reads it: an archived stand-in sweep card, an archived
ranking card filed after it, `.bootstrap_scan_filed` naming the sweep, and no
`.oobe_audits_fired`. Then puts back the `oobe` job if the deployed image ships one
and the install retired it. On an image that ships none, nothing is put back and
nothing starts the audits, which is the case's red.

The state file is written first and after every change, so the disarm restores what
this run changed even when it stopped part-way.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

from cron.jobs import _jobs_lock, compute_next_run, is_job_runnable, load_jobs, save_jobs

home, hermes, run = sys.argv[1:4]
STATE = os.path.join(home, ".bench-oobe.json")
SCAN_MARKER = os.path.join(home, ".bootstrap_scan_filed")
AUDITS_MARKER = os.path.join(home, ".oobe_audits_fired")
# The roster the image ships; the tests pass their own.
SHIPPED_JOBS = sys.argv[4] if len(sys.argv) > 4 else "/opt/defaults/cron/jobs.json"
JOB_ID = "oobe"
# oobe.py counts a ranking card by this key or the key plus a suffix.
RANKING_KEY = "bootstrap-inventory-prioritize-oobe-eval-" + run
SWEEP_KEY = "oobe-eval-sweep-" + run
TMP_SUFFIX = ".tmp"


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def write(path, text):
    tmp = path + TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def save(state):
    write(STATE, json.dumps(state))


def archived_card(state, key, title):
    """File a card no worker picks up (blocked, unassigned), record it, then archive it.

    Recorded before the archive, so a failed archive leaves a card the disarm can find.
    """
    out = subprocess.run(
        [hermes, "kanban", "create", "--json", "--initial-status", "blocked", "--idempotency-key", key, title],
        capture_output=True, text=True, check=True,
    ).stdout
    card = json.loads(out[out.find("{"):out.rfind("}") + 1])["id"]
    state["cards"].append(card)
    save(state)
    subprocess.run([hermes, "kanban", "archive", card], capture_output=True, text=True, check=True)
    return card


def shipped_job():
    with open(SHIPPED_JOBS, encoding="utf-8") as fh:
        jobs = json.load(fh).get("jobs", [])
    return next((job for job in jobs if job.get("id") == JOB_ID), None)


if os.path.exists(STATE):
    sys.exit(f"{STATE} exists: the stage is already armed")
# Before anything changes: a paused or disabled job never runs, and the case would read as an
# image that ships none.
paused = next((j for j in load_jobs() if j.get("id") == JOB_ID and not is_job_runnable(j)), None)
if paused is not None:
    sys.exit(f"the {JOB_ID} job is paused or disabled on this install; resume it or remove it before running this case")

state = {
    "applied_at": datetime.now(timezone.utc).isoformat(),
    "scan_marker": read(SCAN_MARKER),
    "audits_marker": read(AUDITS_MARKER),
    "job_added": False,
    # A job already in the store at arm time: the stage will remove it once it has fired, and the
    # disarm puts this record back.
    "job_present": None,
    "cards": [],
}
save(state)

sweep = archived_card(state, SWEEP_KEY, "oobe eval: stand-in onboarding sweep")
ranking = archived_card(state, RANKING_KEY, "oobe eval: stand-in onboarding ranking card")

write(SCAN_MARKER, f"task_id={sweep}\nfiled_at={int(datetime.now(timezone.utc).timestamp())}\n")
try:
    os.remove(AUDITS_MARKER)
except FileNotFoundError:
    pass

job = shipped_job()
if job is None:
    print(f"this image ships no {JOB_ID} job, so nothing will start the audits")
else:
    with _jobs_lock():
        jobs = load_jobs()
        present = next((j for j in jobs if j.get("id") == JOB_ID), None)
        if present is not None:
            state["job_present"] = present
        else:
            job = dict(job)
            job["next_run_at"] = compute_next_run(job["schedule"])
            save_jobs(jobs + [job])
            state["job_added"] = True
    save(state)
    print(f"put back the shipped {JOB_ID} job" if state["job_added"] else f"{JOB_ID} is already in the cron store")
print(f"armed at {state['applied_at']}: sweep {sweep}, ranking {ranking}")
