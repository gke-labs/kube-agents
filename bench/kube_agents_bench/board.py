"""Read delegated cards' statuses straight off the kanban board, without a model turn.

The delegation wait in :mod:`kube_agents_bench.harness` used to learn whether a
card had settled by re-prompting the front door every poll interval to call
``kanban_show``. Each of those status turns replays the whole conversation, so
a card that ran for 45 minutes cost about ninety model turns and several
million input tokens before the harness even had a result to grade -- and
those tokens came out of the same per-minute quota the worker under test was
trying to use.

The board itself is a SQLite file on the agent's data volume, the same one
:mod:`kube_agents_bench.worker_trajectory` reads the workers' cards from once
they settle. This module reads one thing from it, the ``status`` column of
the awaited cards, through the same ``kubectl exec`` the harness already
relies on for artifacts and session stores. The harness asks the board each
poll and spends a front-door turn only when the board says a card has
stopped moving, which is the one turn that can carry the card's result back
into the conversation.

Best effort in the same sense as the artifact read-back: a pod that cannot
be reached, a board that cannot be opened, or a card the board does not know
all return ``None`` for that read, and the harness falls back to the status
turn it always made. A wrong reading is worse than no reading, so the in-pod
script prints a sentinel before its JSON and a reply without it is a failed
read, never an empty one.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Callable

__all__ = ["read_statuses"]

_log = logging.getLogger("kube_agents_bench.board")

# Line the in-pod script prints before its JSON. A reply without it means the
# script never ran to completion.
BOARD_PRESENT = "__KANBAN_BOARD__"

# The hermes data volume; ``kanban.db`` sits at its root (worker_trajectory
# documents the layout). Passed to the script as an argument so the tests can
# point it at a temporary tree.
DATA_ROOT = "/opt/data"

# The board file under DATA_ROOT. Named here rather than in the script so the
# harness side and the tests spell it once.
BOARD_FILE = "kanban.db"

# Interpreters to run the in-pod script under, in order: hermes' own venv,
# the one binary the image is guaranteed to carry, then whatever ``python3``
# resolves to on the container's PATH. The same pair worker_trajectory uses.
HERMES_PYTHON = "/opt/hermes/.venv/bin/python3"
FALLBACK_PYTHON = "python3"

# Runs inside the agent container. Plain ``sqlite3`` on a read-only URI, so a
# writer holding the WAL lock is waited on briefly rather than fought with,
# and nothing from hermes is imported. Positional arguments carry the data
# root, the board file, the sentinel and then the card ids.
_IN_POD_SCRIPT = r"""
import json, sqlite3, sys

ROOT, BOARD, SENTINEL = sys.argv[1:4]
ids = [a for a in sys.argv[4:] if a]
# Seconds a read waits on a locked store before giving up. A hermes writer
# holds a WAL lock for milliseconds; anything longer is stuck.
SQLITE_BUSY_TIMEOUT = 10
out = {"statuses": {}, "error": None}
try:
    conn = sqlite3.connect("file:%s/%s?mode=ro" % (ROOT, BOARD), uri=True, timeout=SQLITE_BUSY_TIMEOUT)
    marks = ",".join("?" for _ in ids)
    rows = conn.execute("SELECT id, status FROM tasks WHERE id IN (%s)" % marks, ids).fetchall()
    conn.close()
    for tid, status in rows:
        out["statuses"][str(tid)] = str(status)
except sqlite3.Error as exc:
    out["error"] = "kanban board: %s" % exc
print(SENTINEL)
print(json.dumps(out))
"""


def command(task_ids: list[str]) -> str:
    """The ``sh -c`` line that reads ``task_ids``' statuses in the pod."""
    args = " ".join(
        shlex.quote(a) for a in [DATA_ROOT, BOARD_FILE, BOARD_PRESENT, *task_ids]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_IN_POD_SCRIPT)} {args}'
    )


def read_statuses(
    shell: Callable[[str, float], str], task_ids: list[str], timeout: float
) -> dict[str, str] | None:
    """The board's current status for each of ``task_ids``, or ``None``.

    ``shell`` is :func:`harness._agent_shell`, taken as a parameter so this
    module stays importable without the harness and testable with a canned
    reply.

    ``None`` means the read cannot be trusted: no card was asked for, the
    script did not run to completion, its reply was not JSON, or the board
    could not be opened. A card the board does not know is simply absent from
    the returned map; the caller decides what an unknown card means.
    """
    if not task_ids:
        return None
    reply = shell(command(task_ids), timeout)
    marker = reply.find(BOARD_PRESENT)
    if marker < 0:
        _log.debug("kanban board could not be read for %s", ", ".join(task_ids))
        return None
    body = reply[marker + len(BOARD_PRESENT) :].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        _log.warning("kanban board reply is not JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        _log.warning("kanban board: %s", payload["error"])
        return None
    statuses = payload.get("statuses")
    if not isinstance(statuses, dict):
        return None
    return {str(k): str(v) for k, v in statuses.items() if k in task_ids}
