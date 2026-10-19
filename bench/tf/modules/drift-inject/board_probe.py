"""Count kanban cards carrying a run's insert id, read from the board in the agent's pod.

Run inside the agent container by `drift_wait_for_card`, which pipes this file
to the pod's interpreter on stdin and passes the insert id as argv[1]. It prints
a single integer and nothing else; the caller polls it.

Read out of the board's SQLite file rather than from a log line, which is the
one place the drift stacks are coupled to an internal path. There is no external
seam: the daemon logs to a file inside the pod, and the front door's
kanban_create produces no line of its own.

Every text column is searched rather than one being named, because the board's
schema is upstream Hermes' and is not in this repository to check a column name
against.
"""

import sqlite3
import sys

# Read-only URI so the probe cannot create the file, add a journal beside it, or
# block the agent writing to it. A board that is not there yet is a 0, not a
# traceback: the caller polls.
BOARD = "file:/opt/data/kanban.db?mode=ro"
TEXT_COLUMN_TYPE = "TEXT"
TASKS_TABLE = "tasks"

try:
    conn = sqlite3.connect(BOARD, uri=True)
    columns = [
        row[1]
        for row in conn.execute(f"PRAGMA table_info({TASKS_TABLE})")
        if (row[2] or "").upper().startswith(TEXT_COLUMN_TYPE)
    ]
    if not columns:
        print(0)
        raise SystemExit(0)
    needle = "%" + sys.argv[1] + "%"
    where = " OR ".join(f'"{c}" LIKE ?' for c in columns)
    found = conn.execute(
        f"SELECT COUNT(*) FROM {TASKS_TABLE} WHERE {where}", [needle] * len(columns)
    ).fetchone()[0]
    print(found)
except sqlite3.Error:
    print(0)
