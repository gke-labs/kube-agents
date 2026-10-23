"""Append a cron script's details to a log file that fluent-bit ships.

The scheduler keeps a `no_agent` script's stderr only when the script exits
non-zero (`cron/scheduler_script.py` returns stdout alone on a zero exit), so
a detail meant for an operator, written to stderr by a script that exits 0,
reaches nobody. It goes to `<agent home>/logs/<name>.log` instead, which
fluent-bit tails with every other `logs/*.log` (the operator's fluent-bit
config): the "agent's log" a failure line in chat points at.
`kanban_workspace_gc.append_log` is the same idea for one script.
"""

from __future__ import annotations

import time
from pathlib import Path

import gitops_workspace

LOGS_DIR = "logs"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def append(file_name: str, lines: list[str]) -> None:
    """Append `lines`, timestamped, one physical line each; a log that cannot be written is dropped.

    One physical line per record because fluent-bit's tail has no multiline
    parser, and an exception's text can carry newlines.
    """
    if not lines:
        return
    now = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    path = Path(gitops_workspace.agent_home()) / LOGS_DIR / file_name
    records = (" ".join(str(line).splitlines()) for line in lines)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", errors="backslashreplace") as handle:
            handle.write("".join(f"{now} {record}\n" for record in records))
    except OSError:
        pass
