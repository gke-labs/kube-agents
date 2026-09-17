#!/usr/bin/env python3
"""Build gate for the synchronous=FULL patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_state_db_synchronous_full.py``. The applier proves its gate matched
once; that says nothing about whether the widened gate lets the pragma run on
the platform the image ships for, or whether the sibling gate it must not touch
survived.

Three things are checked:

1. **Placement.** Parsed out of the patched ``hermes_state.py``: the gate in
   ``_enforce_macos_synchronous_full`` names linux, and the gate in
   ``_apply_macos_checkpoint_barrier`` still does not — ``checkpoint_fullfsync``
   is an F_FULLFSYNC barrier that exists on macOS alone.
2. **Behaviour.** On a temporary WAL database whose connection has been set to
   ``synchronous=NORMAL``, the real patched function reads back FULL (2).
3. **Composition.** ``apply_wal_with_fallback``, the entry point every state.db
   and kanban.db opener goes through, restores FULL on an existing WAL database
   after NORMAL has been set — whichever of its branches the bundled SQLite takes.

Usage::

    cd /opt/hermes && python3 verify_state_db_synchronous_full.py
"""

from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

FAILURES: list[str] = []

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))

# Set before any Hermes import: hermes_state resolves its paths from the home
# at module scope, and the build has no home of its own.
HOME = Path(tempfile.mkdtemp(prefix="verify-synchronous-full-"))
os.environ["HERMES_HOME"] = str(HOME)

ENFORCE_DEF = "_enforce_macos_synchronous_full"
BARRIER_DEF = "_apply_macos_checkpoint_barrier"
MARKER = "kube-agents patch: state-db-synchronous-full"

# ``ast.unparse`` normalises quoting; derive both spellings rather than guess.
LINUX_GATE_TEST = ast.unparse(ast.parse('sys.platform not in ("darwin", "linux")', mode="eval").body)
DARWIN_GATE_TEST = ast.unparse(ast.parse('sys.platform != "darwin"', mode="eval").body)

#: ``PRAGMA synchronous`` values.
SYNCHRONOUS_NORMAL = 1
SYNCHRONOUS_FULL = 2


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))


def _gates(tree: ast.Module, name: str) -> list[str]:
    defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(defs) != 1:
        return []
    return [
        ast.unparse(node.test)
        for node in ast.walk(defs[0])
        if isinstance(node, ast.If) and "sys.platform" in ast.unparse(node.test)
    ]


# --- 1. Placement -----------------------------------------------------------
print("platform gates (hermes_state.py):")

source = (HERMES / "hermes_state.py").read_text()
tree = ast.parse(source)

check("the patch marker is present exactly once", source.count(MARKER) == 1, f"found {source.count(MARKER)}")
enforce_gates = _gates(tree, ENFORCE_DEF)
check(
    f"{ENFORCE_DEF} has one platform gate and it admits linux",
    enforce_gates == [LINUX_GATE_TEST],
    f"gates: {enforce_gates}",
)
barrier_gates = _gates(tree, BARRIER_DEF)
check(
    f"{BARRIER_DEF} still gates on darwin alone",
    barrier_gates == [DARWIN_GATE_TEST],
    f"gates: {barrier_gates}; checkpoint_fullfsync is macOS-only and the patch must not widen it",
)
check("this build is running on linux, so the widened gate is live here", sys.platform == "linux", sys.platform)


# --- 2. Behaviour -----------------------------------------------------------
print("behaviour:")

import hermes_state  # noqa: E402


def _wal_database(name: str) -> Path:
    path = HOME / name
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    return path


def _synchronous(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA synchronous").fetchone()[0])


direct = sqlite3.connect(_wal_database("direct.db"))
direct.execute(f"PRAGMA synchronous={SYNCHRONOUS_NORMAL}")
check("the connection starts at NORMAL for the test to mean anything", _synchronous(direct) == SYNCHRONOUS_NORMAL)
hermes_state._enforce_macos_synchronous_full(direct)
check(
    "the patched function restores FULL on linux",
    _synchronous(direct) == SYNCHRONOUS_FULL,
    f"PRAGMA synchronous = {_synchronous(direct)}",
)
direct.close()


# --- 3. Composition ---------------------------------------------------------
print("composition with apply_wal_with_fallback:")

composed = sqlite3.connect(_wal_database("composed.db"))
composed.execute(f"PRAGMA synchronous={SYNCHRONOUS_NORMAL}")
mode = hermes_state.apply_wal_with_fallback(composed, db_label="verify-synchronous-full")
check("an existing WAL database is kept in WAL", mode == "wal", f"returned {mode!r}")
check(
    "the opener path restores FULL after a caller set NORMAL",
    _synchronous(composed) == SYNCHRONOUS_FULL,
    f"PRAGMA synchronous = {_synchronous(composed)}",
)
composed.close()


print()
if FAILURES:
    print(f"verify_state_db_synchronous_full: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_state_db_synchronous_full: all checks passed")
