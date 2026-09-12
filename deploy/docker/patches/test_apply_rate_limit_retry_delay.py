"""Unit tests for apply_rate_limit_retry_delay.py against a miniature loop.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real anchor is asserted against the shipped image by
verify_rate_limit_retry_delay.py; these tests cover the applier's own contract.
"""

import ast
import tempfile
import unittest
from pathlib import Path

from apply_rate_limit_retry_delay import LOOP_RELATIVE, MARKER, WAIT_ANCHOR, apply

# ``ast.unparse`` parenthesises the ``not``; derive the spelling rather than
# guess it so the comparison survives a Python bump.
BRANCH_TEST = ast.unparse(
    ast.parse("is_rate_limited and not _retry_after", mode="eval").body
)

LOOP_STUB = '''import logging

logger = logging.getLogger(__name__)

def jittered_backoff(attempt, base_delay, max_delay):
    return base_delay

def run_conversation(agent, api_error, is_rate_limited, retry_count):
    while True:
        try:
            raise api_error
        except Exception as api_error:
            if retry_count >= 3:
                return {"failed": True}
            if is_rate_limited:
                pass
                # For rate limits, respect the Retry-After header if present
                _retry_after = None
                if is_rate_limited:
                    _resp_headers = getattr(getattr(api_error, "response", None), "headers", None)
                    if _resp_headers and hasattr(_resp_headers, "get"):
                        _ra_raw = _resp_headers.get("retry-after") or _resp_headers.get("Retry-After")
                        if _ra_raw:
                            try:
                                _retry_after = min(float(_ra_raw), 600)
                            except (TypeError, ValueError):
                                pass
                wait_time = _retry_after if _retry_after else jittered_backoff(retry_count, base_delay=2.0, max_delay=60.0)
                _backoff_policy = None
                if is_rate_limited and not _retry_after:
                    wait_time = wait_time
                return wait_time
'''


def stage(source=LOOP_STUB):
    root = Path(tempfile.mkdtemp())
    (root / "agent").mkdir()
    (root / LOOP_RELATIVE).write_text(source)
    return root


class ApplierTest(unittest.TestCase):
    def test_the_loop_is_patched_and_stays_parseable(self):
        root = stage()
        apply(root)
        patched = (root / LOOP_RELATIVE).read_text()
        ast.parse(patched)
        self.assertIn(MARKER, patched)
        self.assertIn("if is_rate_limited and not _retry_after:", patched)

    def test_the_branch_lands_between_the_header_read_and_the_wait(self):
        root = stage()
        apply(root)
        patched = (root / LOOP_RELATIVE).read_text()
        header = patched.index('_resp_headers.get("retry-after")')
        branch = patched.index(MARKER)
        wait = patched.index(WAIT_ANCHOR)
        self.assertLess(header, branch)
        self.assertLess(branch, wait)

    def test_the_anchor_line_is_kept_verbatim(self):
        """Upstream's assignment is the one that has to consume _retry_after."""
        root = stage()
        apply(root)
        patched = (root / LOOP_RELATIVE).read_text()
        self.assertEqual(patched.count(WAIT_ANCHOR), 1)

    def test_the_branch_is_gated_on_the_header_having_said_nothing(self):
        root = stage()
        apply(root)
        tree = ast.parse((root / LOOP_RELATIVE).read_text())
        gated = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == BRANCH_TEST
            and MARKER in ast.unparse(node)
        ]
        self.assertEqual(len(gated), 1)

    def test_a_failing_parser_falls_back_to_the_stock_wait(self):
        """The insert swallows its own exceptions; the worker must not die."""
        root = stage()
        apply(root)
        tree = ast.parse((root / LOOP_RELATIVE).read_text())
        branch = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and ast.unparse(node.test) == BRANCH_TEST
        )
        self.assertTrue(
            any(isinstance(s, ast.Try) for s in branch.body),
            "the parser call is not wrapped",
        )
        handler_text = " ".join(
            ast.unparse(h)
            for s in branch.body
            if isinstance(s, ast.Try)
            for h in s.handlers
        )
        self.assertIn("_retry_after = None", handler_text)

    def test_a_missing_anchor_is_fatal_not_silent(self):
        root = stage("def run_conversation():\n    pass\n")
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_a_duplicated_anchor_is_fatal_too(self):
        root = stage(LOOP_STUB + "\n" + WAIT_ANCHOR)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 2", str(ctx.exception))

    def test_applying_twice_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))

    def test_the_anchor_is_the_line_the_image_greps_for(self):
        self.assertIn("wait_time = _retry_after if _retry_after else", WAIT_ANCHOR)
        self.assertIn("jittered_backoff(retry_count, base_delay=2.0", WAIT_ANCHOR)


if __name__ == "__main__":
    unittest.main()
