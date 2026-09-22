"""Unit tests for apply_state_db_synchronous_full.py against a miniature hermes_state_wal.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real file is asserted against the shipped image by
verify_state_db_synchronous_full.py; these tests cover the applier's own contract:
it adds the one Linux branch, leaves the sibling delegation and the shared helper
alone, and refuses anything else.
"""

import ast
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from apply_state_db_synchronous_full import (
    DELEGATE_CALL,
    ENFORCE_DEF,
    MARKER,
    STATE_RELATIVE,
    apply,
    linux_branch,
)

BARRIER_DEF = "_apply_macos_checkpoint_barrier"
HELPER_DEF = "_darwin_pragma"
BODY_INDENT = "    "

# The shape of hermes_state_wal.py at v2026.9.14: both macOS settings delegate
# to one helper that carries the platform gate.
STATE_STUB = '''import contextlib
import sqlite3
import sys


def _darwin_pragma(conn, pragma):
    """Best-effort PRAGMA on macOS only (no-op elsewhere, never raises)."""
    if sys.platform == "darwin":
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(pragma)


def _apply_macos_checkpoint_barrier(conn):
    """Enable ``PRAGMA checkpoint_fullfsync`` on macOS."""
    _darwin_pragma(conn, "PRAGMA checkpoint_fullfsync=1")


def _enforce_macos_synchronous_full(conn):
    """Enforce ``PRAGMA synchronous=FULL`` on macOS: with NORMAL a WAL checkpoint
    racing process termination leaves half-written btree pages."""
    _darwin_pragma(conn, "PRAGMA synchronous=FULL")


def _apply_wal_companions(conn):
    _apply_macos_checkpoint_barrier(conn)
    _enforce_macos_synchronous_full(conn)
'''


def stage(source=STATE_STUB):
    root = Path(tempfile.mkdtemp())
    (root / STATE_RELATIVE).write_text(source)
    return root


def _def_source(source, name):
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(source, node)


class ApplierTest(unittest.TestCase):
    def test_the_branch_is_added_and_the_file_stays_parseable(self):
        root = stage()
        apply(root)
        patched = (root / STATE_RELATIVE).read_text()
        ast.parse(patched)
        self.assertIn(MARKER, patched)
        enforce = _def_source(patched, ENFORCE_DEF)
        self.assertIn(linux_branch(BODY_INDENT), enforce)
        self.assertIn(BODY_INDENT + DELEGATE_CALL, enforce, "the darwin delegation is kept")
        self.assertLess(enforce.index(MARKER), enforce.index(DELEGATE_CALL))

    def test_the_sibling_delegation_and_the_helper_are_left_alone(self):
        """checkpoint_fullfsync is an F_FULLFSYNC barrier; it has no meaning on linux."""
        root = stage()
        before_barrier = _def_source(STATE_STUB, BARRIER_DEF)
        before_helper = _def_source(STATE_STUB, HELPER_DEF)
        apply(root)
        patched = (root / STATE_RELATIVE).read_text()
        self.assertEqual(_def_source(patched, BARRIER_DEF), before_barrier)
        self.assertEqual(_def_source(patched, HELPER_DEF), before_helper)
        self.assertEqual(patched.count(MARKER), 1)

    def test_only_the_branch_is_added(self):
        root = stage()
        apply(root)
        patched = (root / STATE_RELATIVE).read_text()
        self.assertEqual(
            patched.replace(linux_branch(BODY_INDENT), "", 1),
            STATE_STUB,
            "the applier changed something other than inserting the one branch",
        )

    def test_the_branch_follows_the_body_indentation(self):
        root = stage(STATE_STUB.replace("    ", "  "))
        apply(root)
        patched = (root / STATE_RELATIVE).read_text()
        ast.parse(patched)
        self.assertIn(linux_branch("  "), patched)

    def test_the_patched_function_restores_full_on_linux(self):
        """The point of the patch, run through the real interpreter on this platform."""
        if sys.platform != "linux":
            self.skipTest("the new branch is exercised on linux")
        root = stage()
        apply(root)
        spec = importlib.util.spec_from_file_location("patched_state_wal", root / STATE_RELATIVE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA synchronous=NORMAL")
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 1)
        module._enforce_macos_synchronous_full(conn)
        self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2)
        conn.close()

    def test_a_missing_def_is_fatal_not_silent(self):
        root = stage("import sys\n\ndef something_else():\n    pass\n")
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_a_def_whose_delegation_moved_is_fatal(self):
        """A base that rewrote the enforcement needs a re-derived patch, not a silent no-op."""
        moved = STATE_STUB.replace(
            '    _darwin_pragma(conn, "PRAGMA synchronous=FULL")\n',
            '    if sys.platform == "darwin":\n        conn.execute("PRAGMA synchronous=FULL")\n',
        )
        self.assertNotEqual(moved, STATE_STUB)
        root = stage(moved)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("expected 1 synchronous=FULL delegation", str(ctx.exception))

    def test_two_delegations_in_the_def_are_fatal(self):
        doubled = STATE_STUB.replace(
            '    _darwin_pragma(conn, "PRAGMA synchronous=FULL")\n',
            '    _darwin_pragma(conn, "PRAGMA synchronous=FULL")\n' * 2,
        )
        self.assertNotEqual(doubled, STATE_STUB)
        root = stage(doubled)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 2", str(ctx.exception))

    def test_applying_twice_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
