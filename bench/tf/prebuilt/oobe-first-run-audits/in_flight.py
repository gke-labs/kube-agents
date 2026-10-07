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

"""Print how many first-run audits are running or marked due. Runs in the agent container.

Usage: python3 - <home> <audit-id>... < in_flight.py

An audit already in flight is not started again when it is marked due, so the
stage could not be graded until these finish. A row claimed more than
``STALE_SECONDS`` ago is not counted: a gateway restart leaves a cut-off run's row
at running for good, and no audit takes that long. An audit marked due and not yet
claimed counts too, since the next profile-cron-tick starts it: a teardown that read
it as idle would release the stream locks just before that run. Prints a count, or
nothing when the Platform Agent's cron store cannot be read.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

home, audits = sys.argv[1], sys.argv[2:]
LEDGER = os.path.join(home, "profiles", "platform", "cron", "executions.db")
ROSTER = os.path.join(home, "profiles", "platform", "cron", "jobs.json")
PAUSED_STATE = "paused"
SQLITE_BUSY_TIMEOUT_SECONDS = 10
IN_FLIGHT = ("claimed", "running")
# Several times the longest audit run (9-15 minutes, #985), so a live run always counts.
STALE_SECONDS = 60 * 60



def stamp(value):
    """An ISO timestamp as an aware datetime: a naive one predates Hermes' offset-aware
    stamps and meant local time (profile_cron_tick.due_job_ids). None when it does not parse.
    """
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when.astimezone() if when.tzinfo is None else when


def due(now):
    """Audits whose next run is already due: marked, or overdue, and not yet claimed.

    A disabled or paused job is never claimed, so it is not waited on; one with no next
    run is not scheduled. A stamp that does not parse counts as due, as the tick reads it.
    """
    try:
        with open(ROSTER, encoding="utf-8") as fh:
            stored = json.load(fh)
    except FileNotFoundError:
        return 0
    jobs = stored.get("jobs", []) if isinstance(stored, dict) else stored
    count = 0
    for job in jobs:
        if not isinstance(job, dict) or job.get("id") not in audits:
            continue
        if not job.get("enabled", True) or job.get("state") == PAUSED_STATE or job.get("paused_at"):
            continue
        next_run = job.get("next_run_at")
        if not isinstance(next_run, str) or not next_run:
            continue
        when = stamp(next_run)
        if when is None or when <= now:
            count += 1
    return count


now = datetime.now(timezone.utc)
count = due(now)
if os.path.exists(LEDGER):
    conn = sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    try:
        placeholders = ",".join("?" * len(audits))
        since = now - timedelta(seconds=STALE_SECONDS)
        rows = conn.execute(
            f"SELECT claimed_at FROM executions WHERE job_id IN ({placeholders}) AND status IN (?, ?)",
            (*audits, *IN_FLIGHT),
        ).fetchall()
        # A row whose stamp does not parse counts: it is claimed or running, and its age unknown.
        for (claimed,) in rows:
            when = stamp(claimed)
            if when is None or when >= since:
                count += 1
    finally:
        conn.close()
print(count)
