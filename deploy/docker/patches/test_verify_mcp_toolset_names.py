"""Unit tests for verify_mcp_toolset_names.py against miniature cli.py files.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The verifier is a build gate: what matters here is that every shape of the
upstream file it can meet ends in a ``verify_mcp_toolset_names:`` line, not a
traceback, and that it says ok only for a file the patch has reached.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_mcp_toolset_names as verify
from apply_mcp_toolset_names import CLI_RELATIVE, apply
from test_apply_mcp_toolset_names import CLI_STUB

# The same check with the comprehension wrapped the way black would wrap a
# 93-column line, which the applier accepts (its anchor is the assignment
# above it) and which used to end the text-sliced block at a bare `invalid = [`.
CLI_STUB_WRAPPED = CLI_STUB.replace(
    "            invalid = [t for t in toolsets if not validate_toolset(t) and t not in mcp_names]\n",
    "            invalid = [\n"
    "                t\n"
    "                for t in toolsets\n"
    "                if not validate_toolset(t) and t not in mcp_names\n"
    "            ]\n",
)


def run_verifier(source, patch=True):
    root = Path(tempfile.mkdtemp())
    (root / CLI_RELATIVE).write_text(source)
    if patch:
        apply(root)
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class VerifierTest(unittest.TestCase):
    def test_a_patched_file_passes(self):
        rc, failures = run_verifier(CLI_STUB)
        self.assertEqual((rc, failures), (0, []))

    def test_a_wrapped_comprehension_still_passes(self):
        rc, failures = run_verifier(CLI_STUB_WRAPPED)
        self.assertEqual((rc, failures), (0, []), "the block must end where the invalid statement ends, not at its first line")

    def test_an_unpatched_file_is_refused_with_a_reason(self):
        rc, failures = run_verifier(CLI_STUB, patch=False)
        self.assertEqual(rc, 1)
        self.assertTrue(any("marker" in f for f in failures), failures)

    def test_a_moved_check_is_reported_not_raised(self):
        gone = CLI_STUB.replace("invalid = [", "unknown = [").replace("if invalid:", "if unknown:").replace("self.invalid = invalid", "self.invalid = unknown")
        rc, failures = run_verifier(gone, patch=True)
        self.assertEqual(rc, 1)
        self.assertTrue(any("invalid list is gone" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
