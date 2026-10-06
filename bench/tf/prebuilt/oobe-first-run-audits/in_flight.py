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

"""Print how many first-run audits are still running. Runs in the agent container.

Usage: python3 - <home> <audit-id>... < in_flight.py

An audit already in flight is not started again when it is marked due, so the
stage could not be graded until these finish. Prints a count, or nothing when the
Platform Agent's cron store cannot be read.
"""

import os
import sqlite3
import sys

home, audits = sys.argv[1], sys.argv[2:]
LEDGER = os.path.join(home, "profiles", "platform", "cron", "executions.db")
SQLITE_BUSY_TIMEOUT_SECONDS = 10
IN_FLIGHT = ("claimed", "running")

if not os.path.exists(LEDGER):
    print(0)
    sys.exit(0)
conn = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
try:
    marks = ",".join("?" * len(audits))
    count = conn.execute(
        f"SELECT count(*) FROM executions WHERE job_id IN ({marks}) AND status IN (?, ?)",
        (*audits, *IN_FLIGHT),
    ).fetchone()[0]
finally:
    conn.close()
print(count)
