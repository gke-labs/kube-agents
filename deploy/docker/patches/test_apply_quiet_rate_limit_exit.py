"""Unit tests for apply_quiet_rate_limit_exit.py and its verifier.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The applier's contract against a miniature cli.py exit block, and the verifier
against the same stub patched and unpatched: every shape ends in a
``verify_quiet_rate_limit_exit:`` line, and ok only when the condition is the
patched one.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_quiet_rate_limit_exit as verify
from apply_quiet_rate_limit_exit import CLI_RELATIVE, EXIT_ANCHOR, MARKER, apply

# The shape of the one-shot exit block in cli.py at v2026.9.14, after the
# kanban-guardrail patch (which inserts above it and leaves these lines alone).
CLI_STUB = '''import os
import sys


def _run_quiet_single_query(result):
    _exit_code = 0
    if isinstance(result, dict) and result.get("failed"):
        _exit_code = 1
        if os.environ.get("HERMES_KANBAN_TASK") and result.get("failure_reason") in ("rate_limit", "billing"):
            try:
                from hermes_cli.kanban_db import KANBAN_RATE_LIMIT_EXIT_CODE as _RL_CODE
                _exit_code = _RL_CODE
            except Exception:
                _exit_code = 1
    sys.exit(_exit_code)
'''


def stage(source=CLI_STUB):
    root = Path(tempfile.mkdtemp())
    (root / CLI_RELATIVE).write_text(source)
    return root


def run_verifier(root):
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class ApplierTest(unittest.TestCase):
    def test_the_kanban_condition_is_dropped_and_the_rest_kept(self):
        root = stage()
        apply(root)
        patched = (root / CLI_RELATIVE).read_text()
        self.assertIn(MARKER, patched)
        self.assertNotIn(EXIT_ANCHOR, patched)
        self.assertIn('if result.get("failure_reason") in ("rate_limit", "billing"):', patched)
        self.assertIn("KANBAN_RATE_LIMIT_EXIT_CODE as _RL_CODE", patched, "the exit-code import is upstream's and stays")

    def test_a_second_apply_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_moved_anchor_is_refused(self):
        root = stage(CLI_STUB.replace('("rate_limit", "billing")', '("rate_limit",)'))
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertNotIn(MARKER, (root / CLI_RELATIVE).read_text())


class VerifierTest(unittest.TestCase):
    def test_a_patched_file_exits_75_on_a_rate_limit_without_a_kanban_task(self):
        root = stage()
        apply(root)
        rc, failures = run_verifier(root)
        self.assertEqual((rc, failures), (0, []))

    def test_an_unpatched_file_is_refused_with_a_reason(self):
        rc, failures = run_verifier(stage())
        self.assertEqual(rc, 1)
        self.assertTrue(any("marker" in f for f in failures), failures)

    def test_a_moved_block_is_reported_not_raised(self):
        gone = CLI_STUB.replace("sys.exit(_exit_code)", "return _exit_code")
        root = stage(gone)
        apply(root)
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("exit block moved" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
