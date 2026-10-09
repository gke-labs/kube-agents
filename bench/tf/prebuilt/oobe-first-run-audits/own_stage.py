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

A fresh install has the `oobe` job until onboarding is over: its first-run audits stage is done
and its report was claimed a few minutes ago, after which a tick removes the job. On an install
nobody has spoken to, the report is never claimed and the job stays.

Pending while the audits stage is not done: arming over it would point the job at the stand-in
cards, start the audits beside the real scan or chain, and use up the install's own first run.
Pending, too, while the stage is done and the report claimed (`.bootstrap_completed`) but the job
is still there: a tick is about to remove it, and an arm in that minute would record the job as
present while that tick takes it away, leaving nothing to run the stand-in chain. Otherwise clear.
Prints `pending` or `clear`.
"""

import json
import os
import sys

from cron.jobs import is_job_runnable, load_jobs

home = sys.argv[1]
JOB_ID = "oobe"
AUDITS_MARKER = os.path.join(home, ".oobe_audits_fired")
COMPLETED_MARKER = os.path.join(home, ".bootstrap_completed")
DONE_KEY = "done"

# A disabled or paused job never runs, so it never finishes: nothing to wait for.
present = any(job.get("id") == JOB_ID and is_job_runnable(job) for job in load_jobs())
try:
    with open(AUDITS_MARKER, encoding="utf-8") as fh:
        done = bool(json.load(fh).get(DONE_KEY))
except (OSError, ValueError, AttributeError):
    done = False
retiring = done and os.path.exists(COMPLETED_MARKER)
print("pending" if present and (not done or retiring) else "clear")
