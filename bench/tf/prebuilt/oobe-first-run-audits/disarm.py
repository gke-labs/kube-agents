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

Usage: python3 - <home> < disarm.py

Puts both markers back as they were, and removes the `oobe` job when arm.py put it
there and it has not already removed itself. The stand-in cards were archived when
they were filed. Audits the stage started are left to finish: they are real runs,
and stopping one part-way leaves its ledger issue half-written.
"""

import json
import os
import sys

from cron.jobs import remove_job

home = sys.argv[1]
STATE = os.path.join(home, ".bench-oobe.json")
SCAN_MARKER = os.path.join(home, ".bootstrap_scan_filed")
AUDITS_MARKER = os.path.join(home, ".oobe_audits_fired")
JOB_ID = "oobe"
TMP_SUFFIX = ".tmp"


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
restore(SCAN_MARKER, state.get("scan_marker"))
restore(AUDITS_MARKER, state.get("audits_marker"))
os.remove(STATE)
print("disarmed: markers restored" + (f", {JOB_ID} removed" if state.get("job_added") else ""))
