#!/usr/bin/env python3
"""Build gate for the synchronous=FULL patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_state_db_synchronous_full.py``. The applier proves its delegation
matched once; that says nothing about whether the inserted branch lets the
pragma run on the platform the image ships for, or whether the sibling
delegation it must not touch survived.

Three things are checked:

1. **Placement.** Parsed out of the patched ``hermes_state_wal.py``: the one
   platform gate in ``_enforce_macos_synchronous_full`` names linux and the
   Darwin delegation is still there behind it; ``_apply_macos_checkpoint_barrier``
   still has no gate of its own and still delegates — ``checkpoint_fullfsync``
   is an F_FULLFSYNC barrier that exists on macOS alone; and ``_darwin_pragma``,
   the helper both delegate to, still gates on darwin alone.
2. **Behaviour.** On a temporary WAL database whose connection has been set to
   ``synchronous=NORMAL``, the real patched function reads back FULL (2).
3. **Composition.** ``apply_wal_with_fallback``, the entry point every state.db
   and kanban.db opener goes through (``hermes_state`` re-exports it and
   ``hermes_cli.kanban_db_connect`` calls it), restores FULL on an existing WAL
   database after NORMAL has been set — whichever of its branches the bundled
   SQLite takes.

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

WAL_MODULE = "hermes_state_wal.py"
KANBAN_CONNECT_MODULE = Path("hermes_cli") / "kanban_db_connect.py"
ENFORCE_DEF = "_enforce_macos_synchronous_full"
BARRIER_DEF = "_apply_macos_checkpoint_barrier"
HELPER_DEF = "_darwin_pragma"
OPENER = "apply_wal_with_fallback"
MARKER = "kube-agents patch: state-db-synchronous-full"

# ``ast.unparse`` normalises quoting; derive both spellings rather than guess.
LINUX_GATE_TEST = ast.unparse(ast.parse('sys.platform == "linux"', mode="eval").body)
DARWIN_GATE_TEST = ast.unparse(ast.parse('sys.platform == "darwin"', mode="eval").body)
ENFORCE_DELEGATION = ast.unparse(
    ast.parse('_darwin_pragma(conn, "PRAGMA synchronous=FULL")', mode="eval").body
)
BARRIER_DELEGATION = ast.unparse(
    ast.parse('_darwin_pragma(conn, "PRAGMA checkpoint_fullfsync=1")', mode="eval").body
)

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


def _def(tree: ast.Module, name: str):
    defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    return defs[0] if len(defs) == 1 else None


def _gates(node) -> list[str]:
    if node is None:
        return []
    return [
        ast.unparse(child.test)
        for child in ast.walk(node)
        if isinstance(child, ast.If) and "sys.platform" in ast.unparse(child.test)
    ]


def _calls(node) -> list[str]:
    if node is None:
        return []
    return [ast.unparse(child) for child in ast.walk(node) if isinstance(child, ast.Call)]


# --- 1. Placement -----------------------------------------------------------
print(f"platform gates ({WAL_MODULE}):")

source = (HERMES / WAL_MODULE).read_text()
tree = ast.parse(source)

check("the patch marker is present exactly once", source.count(MARKER) == 1, f"found {source.count(MARKER)}")
enforce = _def(tree, ENFORCE_DEF)
check(
    f"{ENFORCE_DEF} has one platform gate and it names linux",
    _gates(enforce) == [LINUX_GATE_TEST],
    f"gates: {_gates(enforce)}",
)
check(
    f"{ENFORCE_DEF} still delegates to {HELPER_DEF} for darwin",
    ENFORCE_DELEGATION in _calls(enforce),
    f"calls: {_calls(enforce)}",
)
barrier = _def(tree, BARRIER_DEF)
check(
    f"{BARRIER_DEF} has no platform gate of its own and still delegates",
    _gates(barrier) == [] and BARRIER_DELEGATION in _calls(barrier),
    f"gates: {_gates(barrier)} calls: {_calls(barrier)}; checkpoint_fullfsync is macOS-only and the patch must not widen it",
)
helper = _def(tree, HELPER_DEF)
check(
    f"{HELPER_DEF} still gates on darwin alone",
    _gates(helper) == [DARWIN_GATE_TEST],
    f"gates: {_gates(helper)}",
)
check("this build is running on linux, so the new branch is live here", sys.platform == "linux", sys.platform)


# --- 2. Behaviour -----------------------------------------------------------
print("behaviour:")

import hermes_state  # noqa: E402
import hermes_state_wal  # noqa: E402


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
hermes_state_wal._enforce_macos_synchronous_full(direct)
check(
    "the patched function restores FULL on linux",
    _synchronous(direct) == SYNCHRONOUS_FULL,
    f"PRAGMA synchronous = {_synchronous(direct)}",
)
direct.close()


# --- 3. Composition ---------------------------------------------------------
print(f"composition with {OPENER}:")

check(
    f"hermes_state re-exports {OPENER} from {WAL_MODULE}",
    getattr(hermes_state, OPENER, None) is getattr(hermes_state_wal, OPENER, None),
)
check(
    f"the kanban.db opener calls {OPENER}",
    f"{OPENER}(" in (HERMES / KANBAN_CONNECT_MODULE).read_text(),
)

composed = sqlite3.connect(_wal_database("composed.db"))
composed.execute(f"PRAGMA synchronous={SYNCHRONOUS_NORMAL}")
mode = hermes_state_wal.apply_wal_with_fallback(composed, db_label="verify-synchronous-full")
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
