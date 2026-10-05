"""Unit tests for apply_api_failure_reason_header.py and its verifier.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The applier's contract against a miniature chat-completions handler, and the
verifier against the same stub patched and unpatched.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_api_failure_reason_header as verify
from apply_api_failure_reason_header import HEADERS_ANCHOR, MARKER, ROUTES_RELATIVE, apply

# The shape of the non-streaming chat-completions tail at v2026.9.14.
ROUTES_STUB = '''class Routes:
    async def handle(self, result, provided_session_id, session_id, gateway_session_key):
        response_headers = {"X-Hermes-Session-Id": (provided_session_id or result.get("session_id", session_id))}
        if gateway_session_key:
            response_headers["X-Hermes-Session-Key"] = gateway_session_key
        return response_headers
'''


def stage(source=ROUTES_STUB):
    root = Path(tempfile.mkdtemp())
    path = root / ROUTES_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(source)
    return root


def run_verifier(root):
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class ApplierTest(unittest.TestCase):
    def test_the_header_follows_the_assignment(self):
        root = stage()
        apply(root)
        patched = (root / ROUTES_RELATIVE).read_text()
        self.assertIn(MARKER, patched)
        self.assertIn(HEADERS_ANCHOR, patched, "the upstream assignment stays")
        self.assertLess(patched.index(HEADERS_ANCHOR), patched.index(MARKER))

    def test_a_second_apply_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_moved_anchor_is_refused(self):
        root = stage(ROUTES_STUB.replace("provided_session_id or", "provided_session_id  or"))
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertNotIn(MARKER, (root / ROUTES_RELATIVE).read_text())


class VerifierTest(unittest.TestCase):
    def test_a_patched_file_names_the_reason(self):
        root = stage()
        apply(root)
        self.assertEqual(run_verifier(root), (0, []))

    def test_an_unpatched_file_is_refused_with_a_reason(self):
        rc, failures = run_verifier(stage())
        self.assertEqual(rc, 1)
        self.assertTrue(any("missing or moved" in f or "marker" in f for f in failures), failures)

    def test_a_patch_that_drops_the_sanitizing_is_caught(self):
        root = stage()
        apply(root)
        path = root / ROUTES_RELATIVE
        path.write_text(path.read_text().replace('if ch.isascii() and (ch.isalnum() or ch in "_:.-")', ""))
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("newline" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
