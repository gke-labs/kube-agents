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

"""Count the kanban cards carrying a run's insert id, and how many have finished.

Run inside the agent container by `drift_wait_for_card`, which pipes this file
to the pod's interpreter on stdin and passes the insert id as argv[1]. It prints
one line, `<matching> <finished>`, and nothing else; the caller polls it.
`<finished>` is -1 when the board has no `status` column to read, so the caller
can tell "not finished" from "cannot tell".

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
# The statuses after which a card's result will not change: the same three the
# drift cases' prompts wait for before reproducing a card.
FINISHED_STATUSES = ("done", "blocked", "archived")
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
        if STATUS_COLUMN not in {row[1] for row in info}:
            return matching, UNKNOWN
        marks = ",".join("?" for _ in FINISHED_STATUSES)
        finished = conn.execute(
            f'SELECT COUNT(*) FROM {TASKS_TABLE} WHERE ({where}) AND "{STATUS_COLUMN}" IN ({marks})',
            params + list(FINISHED_STATUSES),
        ).fetchone()[0]
        return matching, finished
    except sqlite3.Error:
        return 0, 0


if __name__ == "__main__":
    print(*probe(sys.argv[1]))
