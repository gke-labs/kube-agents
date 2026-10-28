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

"""Count the kanban cards carrying a run's insert id, and say whether the newest has finished.

Run inside the agent container by `drift_wait_for_card`, which pipes this file
to the pod's interpreter on stdin and passes the insert id as argv[1]. It prints
one line, `<matching> <finished>`, and nothing else; the caller polls it.
`<finished>` says how the newest matching card stands: 0 still running, 1
finished with a result to read (done, blocked, archived), 2 finished without
one (failed, cancelled), and -1 when the board has no `status` column, so the
caller can tell "not finished" from "cannot tell". 1 and 2 are kept apart
because only one of them leaves the agent a triage to read: a card that failed
or was cancelled is the install's failure, and the caller fails the plant on it
rather than handing the agent a card with nothing in it.

The newest card rather than any of them, because that is the one the drift
cases' prompts send the agent to. If more than one card carries the id, a
re-queued front-door turn say, the first to finish is not necessarily the one
the agent reads.

Read out of the board's SQLite file rather than from a log line, which is the
one place the drift stacks are coupled to an internal path. There is no external
seam: the daemon logs to a file inside the pod, and the front door's
kanban_create produces no line of its own.

Every text column is searched for the insert id rather than one being named,
because the board's schema is upstream Hermes' and is not in this repository to
check a column name against. `status` is the one column this does name, and it
is optional: read when present, reported as -1 when not.
"""

import os
import sqlite3
import sys

# Read-only URI so the probe cannot create the file, add a journal beside it, or
# block the agent writing to it. A board that is not there yet is `0 0`, not a
# traceback: the caller polls. Overridable so a test can point it at a fixture.
BOARD = os.environ.get("DRIFT_BOARD_URI", "file:/opt/data/kanban.db?mode=ro")
TEXT_COLUMN_TYPE = "TEXT"
TASKS_TABLE = "tasks"
STATUS_COLUMN = "status"
# Hermes' terminal statuses, the same five bench/kube_agents_bench/harness.py's
# _TERMINAL_STATUSES and agents/platform/scripts/kanban_workspace_gc.py name,
# split by whether the card left a result. The first three are the ones the
# drift prompts accept and the sibling fixtures that wait on a card treat as
# success; the last two must end the wait too -- left out, a failed card is
# waited on to the ceiling -- but as a failure, not as something to read.
SETTLED_WITH_RESULT = ("done", "blocked", "archived")
SETTLED_WITHOUT_RESULT = ("failed", "cancelled")
RUNNING, FINISHED, FAILED = 0, 1, 2
# Orders matching cards so the newest is read; absent, any row stands for all.
CREATED_COLUMN = "created_at"
UNKNOWN = -1


def probe(insert_id):
    """Return (matching, finished) for cards whose text carries insert_id."""
    try:
        conn = sqlite3.connect(BOARD, uri=True)
        info = list(conn.execute(f"PRAGMA table_info({TASKS_TABLE})"))
        columns = [row[1] for row in info if (row[2] or "").upper().startswith(TEXT_COLUMN_TYPE)]
        if not columns:
            return 0, 0
        needle = "%" + insert_id + "%"
        where = " OR ".join(f'"{c}" LIKE ?' for c in columns)
        params = [needle] * len(columns)
        matching = conn.execute(f"SELECT COUNT(*) FROM {TASKS_TABLE} WHERE {where}", params).fetchone()[0]
        names = {row[1] for row in info}
        if STATUS_COLUMN not in names:
            return matching, UNKNOWN
        if not matching:
            return 0, 0
        order = f'ORDER BY "{CREATED_COLUMN}" DESC' if CREATED_COLUMN in names else ""
        newest = conn.execute(
            f'SELECT "{STATUS_COLUMN}" FROM {TASKS_TABLE} WHERE {where} {order} LIMIT 1', params
        ).fetchone()[0]
        if newest in SETTLED_WITH_RESULT:
            return matching, FINISHED
        if newest in SETTLED_WITHOUT_RESULT:
            return matching, FAILED
        return matching, RUNNING
    except sqlite3.Error:
        return 0, 0


if __name__ == "__main__":
    print(*probe(sys.argv[1]))
