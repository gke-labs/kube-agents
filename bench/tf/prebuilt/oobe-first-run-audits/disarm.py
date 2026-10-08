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

"""Undo what arm.py changed, from its state file. Runs in the agent container.

Usage: python3 - <home> <hermes> < disarm.py

Puts both markers back as they were. Removes the `oobe` job when arm.py put it there
and it has not already removed itself, and puts back the job arm.py found in the store
when the stage has since removed it, and archives the stand-in cards again in case an arm
stopped between filing one and archiving it. The stand-in cards were archived when
they were filed. Audits the stage started are left to finish: they are real runs,
and stopping one part-way leaves its ledger issue half-written.
"""

import json
import os
import subprocess
import sys

from cron.jobs import _jobs_lock, compute_next_run, load_jobs, remove_job, save_jobs

home, HERMES = sys.argv[1:3]
STATE = os.path.join(home, ".bench-oobe.json")
SCAN_MARKER = os.path.join(home, ".bootstrap_scan_filed")
AUDITS_MARKER = os.path.join(home, ".oobe_audits_fired")
JOB_ID = "oobe"
TMP_SUFFIX = ".tmp"
# Scheduler bookkeeping a put-back record must not carry over.
RUN_STATE_KEYS = ("fire_claim", "pending_slot")


def restore(path, text):
    if text is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    tmp = path + TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


if not os.path.exists(STATE):
    print("the stage was not armed")
    sys.exit(0)
with open(STATE, encoding="utf-8") as fh:
    state = json.load(fh)
# The job first: once the markers are back, a run of it could act on them.
if state.get("job_added"):
    remove_job(JOB_ID)
saved = state.get("job_present")
if saved:
    with _jobs_lock():
        jobs = load_jobs()
        if not any(j.get("id") == JOB_ID for j in jobs):
            job = {k: v for k, v in saved.items() if k not in RUN_STATE_KEYS}
            job["next_run_at"] = compute_next_run(job["schedule"])
            save_jobs(jobs + [job])
# Archiving an archived card changes nothing, so every recorded card is archived again.
for card in state.get("cards", []):
    try:
        subprocess.run([HERMES, "kanban", "archive", card], capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"could not archive {card}: {exc}", file=sys.stderr)
restore(SCAN_MARKER, state.get("scan_marker"))
restore(AUDITS_MARKER, state.get("audits_marker"))
os.remove(STATE)
print("disarmed: markers restored" + (f", {JOB_ID} removed" if state.get("job_added") else "") + (f", {JOB_ID} put back" if saved else ""))
