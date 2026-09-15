"""Unit tests for apply_state_db_synchronous_full.py against a miniature hermes_state.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real file is asserted against the shipped image by
verify_state_db_synchronous_full.py; these tests cover the applier's own contract:
it widens the one gate, leaves the sibling gate alone, and refuses anything else.
"""

import ast
import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from apply_state_db_synchronous_full import (
    DARWIN_GATE,
    ENFORCE_DEF,
    LINUX_GATE,
    MARKER,
    STATE_RELATIVE,
    apply,
)

BARRIER_DEF = "_apply_macos_checkpoint_barrier"

STATE_STUB = '''import sqlite3
import sys


def _apply_macos_checkpoint_barrier(conn):
    """F_FULLFSYNC is macOS-only; a no-op elsewhere."""
    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA checkpoint_fullfsync=1")
    except sqlite3.OperationalError:
        pass


def _enforce_macos_synchronous_full(conn):
    """Enforce ``PRAGMA synchronous=FULL`` on macOS to prevent btree corruption.

    Best-effort: never raises.
    """
    if sys.platform != "darwin":
        return
    try:
        conn.execute("PRAGMA synchronous=FULL")
    except sqlite3.OperationalError:
        pass
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
    def test_the_gate_is_widened_and_the_file_stays_parseable(self):
        root = stage()
        apply(root)
        patched = (root / STATE_RELATIVE).read_text()
        ast.parse(patched)
        self.assertIn(MARKER, patched)
        self.assertIn(LINUX_GATE, _def_source(patched, ENFORCE_DEF))
        self.assertNotIn(DARWIN_GATE, _def_source(patched, ENFORCE_DEF))

    def test_the_sibling_gate_is_left_alone(self):
        """checkpoint_fullfsync is an F_FULLFSYNC barrier; it has no meaning on linux."""
        root = stage()
        before = _def_source(STATE_STUB, BARRIER_DEF)
        apply(root)
        patched = (root / STATE_RELATIVE).read_text()
        self.assertEqual(_def_source(patched, BARRIER_DEF), before)
        self.assertEqual(patched.count(MARKER), 1)

    def test_only_the_gate_line_changes(self):
        root = stage()
        apply(root)
        patched = (root / STATE_RELATIVE).read_text()
        self.assertEqual(
            patched.replace(LINUX_GATE, DARWIN_GATE),
            STATE_STUB,
            "the applier changed something other than the one gate line",
        )

    def test_the_patched_function_restores_full_on_linux(self):
        """The point of the patch, run through the real interpreter on this platform."""
        if sys.platform != "linux":
            self.skipTest("the widened gate is exercised on linux")
        root = stage()
        apply(root)
        spec = importlib.util.spec_from_file_location("patched_state", root / STATE_RELATIVE)
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

    def test_a_def_whose_gate_moved_is_fatal(self):
        """A base that rewrote the gate needs a re-derived patch, not a silent no-op."""
        moved = STATE_STUB.replace(
            'def _enforce_macos_synchronous_full(conn):\n    """Enforce ``PRAGMA synchronous=FULL`` on macOS to prevent btree corruption.\n\n    Best-effort: never raises.\n    """\n    if sys.platform != "darwin":\n        return\n',
            'def _enforce_macos_synchronous_full(conn):\n    if sys.platform == "darwin":\n        pass\n    else:\n        return\n',
        )
        self.assertNotEqual(moved, STATE_STUB)
        root = stage(moved)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("expected 1 platform gate", str(ctx.exception))

    def test_two_gates_in_the_def_are_fatal(self):
        doubled = STATE_STUB.replace(
            '    if sys.platform != "darwin":\n        return\n    try:\n        conn.execute("PRAGMA synchronous=FULL")',
            '    if sys.platform != "darwin":\n        return\n    if sys.platform != "darwin":\n        return\n    try:\n        conn.execute("PRAGMA synchronous=FULL")',
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
