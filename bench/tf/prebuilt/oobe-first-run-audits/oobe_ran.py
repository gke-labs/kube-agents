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

"""Print how many runs of the `oobe` job have ended since arm.py armed it. Runs in the agent container.

Usage: python3 - <home> < oobe_ran.py

The put-back job first runs on the gateway's next minute, and the audits it marks due
start on the profile-cron-tick after that, which together outlast the verifier's window.
The apply waits on this so the verifier has only the last hop left. Prints a count, or
nothing when the state file or the Planning Agent's cron store cannot be read.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

home = sys.argv[1]
STATE = os.path.join(home, ".bench-oobe.json")
LEDGER = os.path.join(home, "cron", "executions.db")
SQLITE_BUSY_TIMEOUT_SECONDS = 10
JOB_ID = "oobe"
ENDED = ("completed", "failed")

with open(STATE, encoding="utf-8") as fh:
    armed = datetime.fromisoformat(json.load(fh)["applied_at"]).timestamp()
conn = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
try:
    rows = conn.execute(
        "SELECT claimed_at FROM executions WHERE job_id = ? AND status IN (?, ?)", (JOB_ID, *ENDED)
    ).fetchall()
finally:
    conn.close()
print(sum(1 for (claimed,) in rows if claimed and datetime.fromisoformat(claimed).timestamp() >= armed))
