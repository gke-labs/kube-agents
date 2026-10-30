"""Unit tests for apply_rate_limit_backoff_floor.py against a miniature backoff.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The miniature is the one test_apply_rate_limit_retry_delay.py stages, with that
patch applied first, as the image does. The real anchor is asserted against the
shipped image by verify_rate_limit_backoff_floor.py; these tests cover the
applier's own contract.
"""

import ast
import importlib
import importlib.util
import sys
import types
import unittest

from apply_rate_limit_backoff_floor import (
    LOOP_RELATIVE,
    MARKER,
    RETRY_DELAY_MARKER,
    WAIT_ANCHOR,
    apply,
)
from apply_rate_limit_retry_delay import apply as apply_retry_delay
from rate_limit_backoff_floor import FLOOR_MAX_SECONDS, FLOOR_MIN_SECONDS
from test_apply_rate_limit_retry_delay import LOOP_STUB, stage

# ``ast.unparse`` normalises the spelling; derive it rather than guess it.
BRANCH_TEST = ast.unparse(ast.parse("is_rate_limited and _retry_after is None", mode="eval").body)


def stage_with_retry_delay(source=LOOP_STUB):
    root = stage(source)
    apply_retry_delay(root)
    return root


class ApplierTest(unittest.TestCase):
    def test_the_loop_is_patched_and_stays_parseable(self):
        root = stage_with_retry_delay()
        apply(root)
        patched = (root / LOOP_RELATIVE).read_text()
        ast.parse(patched)
        self.assertIn(MARKER, patched)
        self.assertIn("if is_rate_limited and _retry_after is None:", patched)

    def test_the_floor_lands_after_the_retry_delay_branch_and_before_the_wait(self):
        root = stage_with_retry_delay()
        apply(root)
        patched = (root / LOOP_RELATIVE).read_text()
        retry_delay = patched.index(RETRY_DELAY_MARKER)
        floor = patched.index(MARKER)
        wait = patched.index(WAIT_ANCHOR)
        self.assertLess(retry_delay, floor, "a delay the body names has to be read first")
        self.assertLess(floor, wait)

    def test_the_anchor_line_is_kept_verbatim(self):
        root = stage_with_retry_delay()
        apply(root)
        self.assertEqual((root / LOOP_RELATIVE).read_text().count(WAIT_ANCHOR), 1)

    def test_the_branch_is_gated_on_nothing_having_named_a_wait(self):
        root = stage_with_retry_delay()
        apply(root)
        tree = ast.parse((root / LOOP_RELATIVE).read_text())
        gated = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and ast.unparse(node.test) == BRANCH_TEST and MARKER in ast.unparse(node)
        ]
        self.assertEqual(len(gated), 1)
        self.assertNotIn("retry_count", ast.unparse(gated[0]), "the floor never widens the retry count")
        self.assertNotIn("max_retries", ast.unparse(gated[0]))

    def test_a_failing_floor_falls_back_to_the_stock_wait(self):
        root = stage_with_retry_delay()
        apply(root)
        tree = ast.parse((root / LOOP_RELATIVE).read_text())
        branch = next(
            node for node in ast.walk(tree) if isinstance(node, ast.If) and ast.unparse(node.test) == BRANCH_TEST
        )
        self.assertTrue(any(isinstance(s, ast.Try) for s in branch.body), "the floor call is not wrapped")
        handler_text = " ".join(
            ast.unparse(h) for s in branch.body if isinstance(s, ast.Try) for h in s.handlers
        )
        self.assertIn("_retry_after = None", handler_text)

    def test_the_patched_backoff_waits_the_floor_only_when_nothing_named_a_wait(self):
        """Run the miniature with both real companions, as the image does."""
        root = stage_with_retry_delay()
        apply(root)
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.rate_limit_retry_delay = importlib.import_module("rate_limit_retry_delay")
        hermes_cli.rate_limit_backoff_floor = importlib.import_module("rate_limit_backoff_floor")
        sys.modules["hermes_cli"] = hermes_cli
        sys.modules["hermes_cli.rate_limit_retry_delay"] = hermes_cli.rate_limit_retry_delay
        sys.modules["hermes_cli.rate_limit_backoff_floor"] = hermes_cli.rate_limit_backoff_floor
        try:
            spec = importlib.util.spec_from_file_location("patched_recovery_floor", root / LOOP_RELATIVE)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            kwargs = dict(
                retry_count=1, max_retries=3, is_zai_coding_overload=False,
                base_url="http://inference-gateway", model="gemini",
            )
            argon = Exception(
                "Error code: 429 - TPM quota exhausted. No spare capacity is currently "
                "available to serve over-limit requests."
            )
            for _ in range(20):
                wait = module.compute_error_backoff(None, argon, is_rate_limited=True, **kwargs)
                self.assertGreaterEqual(wait, FLOOR_MIN_SECONDS)
                self.assertLessEqual(wait, FLOOR_MAX_SECONDS)
            storm = Exception('{"details": [{"retryDelay": "54s"}]}')
            self.assertEqual(
                module.compute_error_backoff(None, storm, is_rate_limited=True, **kwargs), 54.0,
                "a delay the body names still wins over the floor",
            )
            self.assertEqual(
                module.compute_error_backoff(None, argon, is_rate_limited=False, **kwargs), 2.0,
                "a non-429 keeps the stock backoff",
            )
        finally:
            for name in ("hermes_cli.rate_limit_backoff_floor", "hermes_cli.rate_limit_retry_delay", "hermes_cli"):
                sys.modules.pop(name, None)

    def test_a_file_without_the_retry_delay_patch_is_refused(self):
        root = stage()
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn(RETRY_DELAY_MARKER, str(ctx.exception))
        self.assertNotIn(MARKER, (root / LOOP_RELATIVE).read_text())

    def test_a_missing_anchor_is_fatal_not_silent(self):
        root = stage(f"# {RETRY_DELAY_MARKER}\ndef compute_error_backoff():\n    pass\n")
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_is_refused(self):
        root = stage_with_retry_delay()
        apply(root)
        with self.assertRaises(SystemExit) as ctx:
            apply(root)
        self.assertIn("already patched", str(ctx.exception))

    def test_the_anchor_is_the_retry_delay_patch_s_anchor(self):
        from apply_rate_limit_retry_delay import WAIT_ANCHOR as RETRY_DELAY_ANCHOR

        self.assertEqual(WAIT_ANCHOR, RETRY_DELAY_ANCHOR)


class FloorTest(unittest.TestCase):
    def test_the_draw_stays_inside_the_bounds(self):
        from rate_limit_backoff_floor import rate_limit_floor_seconds

        for _ in range(200):
            wait = rate_limit_floor_seconds()
            self.assertGreaterEqual(wait, FLOOR_MIN_SECONDS)
            self.assertLessEqual(wait, FLOOR_MAX_SECONDS)

    def test_the_bounds_cover_a_quota_minute_and_stay_under_the_header_cap(self):
        from rate_limit_retry_delay import RETRY_DELAY_CAP_SECONDS

        self.assertGreaterEqual(3 * FLOOR_MIN_SECONDS, 60.0, "three retries have to outlast one quota minute")
        self.assertLessEqual(FLOOR_MAX_SECONDS, RETRY_DELAY_CAP_SECONDS)

    def test_bad_bounds_raise_rather_than_return_a_wait(self):
        from rate_limit_backoff_floor import rate_limit_floor_seconds

        for low, high in ((0, 10), (-1, 10), (60, 30)):
            with self.subTest(low=low, high=high), self.assertRaises(ValueError):
                rate_limit_floor_seconds(low=low, high=high)


if __name__ == "__main__":
    unittest.main()
