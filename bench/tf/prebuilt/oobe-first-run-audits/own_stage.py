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

"""Print whether the install's own first-run stage is still pending. Runs in the agent container.

Usage: python3 - <home> < own_stage.py

A fresh install has the `oobe` job and no finished `.oobe_audits_fired` until its own scan
settles. Arming over that would point the job at the stand-in cards, start the audits beside
the real scan, and use up the install's own first run. Prints `pending` or `clear`.
"""

import json
import os
import sys

from cron.jobs import is_job_runnable, load_jobs

home = sys.argv[1]
AUDITS_MARKER = os.path.join(home, ".oobe_audits_fired")
JOB_ID = "oobe"
DONE_KEY = "done"

# A disabled or paused job never runs, so it never finishes: nothing to wait for.
present = any(job.get("id") == JOB_ID and is_job_runnable(job) for job in load_jobs())
try:
    with open(AUDITS_MARKER, encoding="utf-8") as fh:
        done = bool(json.load(fh).get(DONE_KEY))
except (OSError, ValueError, AttributeError):
    done = False
print("pending" if present and not done else "clear")
