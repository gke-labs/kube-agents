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

A fresh install has the `oobe` job until its scan has settled, its own chain has started the
last audit, and the tick after that has removed the job. Arming over that would point the job at
the stand-in cards, start the audits beside the real scan or chain, and use up the install's own
first run; arming in the minute between `done` and the removal would record the job as present
while its own tick takes it away, leaving nothing to run the stand-in chain. So this waits for
the job itself to be gone. Prints `pending` or `clear`.
"""

import sys

from cron.jobs import is_job_runnable, load_jobs

home = sys.argv[1]
JOB_ID = "oobe"

# A disabled or paused job never runs, so it never finishes: nothing to wait for.
present = any(job.get("id") == JOB_ID and is_job_runnable(job) for job in load_jobs())
print("pending" if present else "clear")
