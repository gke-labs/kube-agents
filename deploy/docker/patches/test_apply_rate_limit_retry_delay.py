"""Unit tests for apply_rate_limit_retry_delay.py against a miniature backoff.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The real anchor is asserted against the shipped image by
verify_rate_limit_retry_delay.py; these tests cover the applier's own contract.
"""

import ast
import importlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

from apply_rate_limit_retry_delay import LOOP_RELATIVE, MARKER, WAIT_ANCHOR, apply

# ``ast.unparse`` parenthesises the ``not``; derive the spelling rather than
# guess it so the comparison survives a Python bump.
BRANCH_TEST = ast.unparse(
    ast.parse("is_rate_limited and not _retry_after", mode="eval").body
)

# The shape of compute_error_backoff in agent/turn_recovery.py at v2026.9.14.
LOOP_STUB = '''import logging

logger = logging.getLogger("agent.conversation_loop")


def parse_retry_after_seconds(value_or_headers):
    return None


def jittered_backoff(attempt, base_delay, max_delay):
    return base_delay


def adaptive_rate_limit_backoff(attempt, *, base_url, model, error, default_wait):
    return default_wait, None


def compute_error_backoff(agent, api_error, *, retry_count, max_retries, is_rate_limited,
                          is_zai_coding_overload, base_url, model):
    _retry_after = parse_retry_after_seconds(
        getattr(getattr(api_error, "response", None), "headers", None)
    )
    if _retry_after is None:
        _error_body = getattr(api_error, "body", None)
        if isinstance(_error_body, dict):
            _nested = _error_body.get("error")
            _payload = _nested if isinstance(_nested, dict) else _error_body
            _retry_after = parse_retry_after_seconds(_payload.get("retry_after"))
    if _retry_after is not None:
        _retry_after = min(_retry_after, 600)
        if _retry_after <= 0:
            _retry_after = None
    wait_time = _retry_after if _retry_after is not None else jittered_backoff(retry_count, base_delay=2.0, max_delay=60.0)
    _backoff_policy = None
    _adaptive = is_rate_limited or is_zai_coding_overload
    if _adaptive and _retry_after is None:
        wait_time, _backoff_policy = adaptive_rate_limit_backoff(
            retry_count, base_url=str(base_url), model=model, error=api_error, default_wait=wait_time,
        )
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
        header = patched.index("_retry_after = parse_retry_after_seconds(")
        field = patched.index('_payload.get("retry_after")')
        cap = patched.index("min(_retry_after, 600)")
        branch = patched.index(MARKER)
        wait = patched.index(WAIT_ANCHOR)
        self.assertLess(header, field)
        self.assertLess(field, cap)
        self.assertLess(cap, branch)
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

    def test_the_patched_backoff_waits_out_the_body_delay(self):
        """Run the miniature with the real companion: a Google 429 body yields its retryDelay."""
        root = stage()
        apply(root)
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.rate_limit_retry_delay = importlib.import_module("rate_limit_retry_delay")
        sys.modules["hermes_cli"] = hermes_cli
        sys.modules["hermes_cli.rate_limit_retry_delay"] = hermes_cli.rate_limit_retry_delay
        try:
            spec = importlib.util.spec_from_file_location("patched_recovery", root / LOOP_RELATIVE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            storm = Exception('{"details": [{"retryDelay": "54s"}]}')
            kwargs = dict(retry_count=1, max_retries=3, is_zai_coding_overload=False,
                          base_url="http://litellm", model="gemini")
            self.assertEqual(
                module.compute_error_backoff(None, storm, is_rate_limited=True, **kwargs), 54.0
            )
            self.assertEqual(
                module.compute_error_backoff(None, storm, is_rate_limited=False, **kwargs), 2.0,
                "a non-429 keeps the stock backoff",
            )
            self.assertEqual(
                module.compute_error_backoff(None, Exception("Rate limit reached"), is_rate_limited=True, **kwargs),
                2.0,
                "a 429 without a delay keeps the stock backoff",
            )
        finally:
            sys.modules.pop("hermes_cli.rate_limit_retry_delay", None)
            sys.modules.pop("hermes_cli", None)

    def test_a_missing_anchor_is_fatal_not_silent(self):
        root = stage("def compute_error_backoff():\n    pass\n")
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
        self.assertIn("wait_time = _retry_after if _retry_after is not None else", WAIT_ANCHOR)
        self.assertIn("jittered_backoff(retry_count, base_delay=2.0", WAIT_ANCHOR)


if __name__ == "__main__":
    unittest.main()
