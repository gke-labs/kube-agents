"""Unit tests for the mcp-remote forward-error patch applied by the Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import tempfile
import unittest
from pathlib import Path

from apply_mcp_remote_forward_errors import ANCHOR, BUILD_MARKER, RELATIVE, apply

# The shape of the region being patched, reproduced from mcp-remote
# src/lib/utils.ts at the pinned commit. Only the anchor line matters, but the
# surroundings keep the fixture honest about indentation.
PRELUDE = """\
export function mcpProxy({ transportToClient, transportToServer }: Args) {
  transportToClient.onmessage = (_message) => {
    const message = messageTransformer.interceptRequest(_message as any)
"""

CODA = """\
  }

  function onClientError(error: Error) {
    log('Error from local client:', error)
  }

  function onServerError(error: Error) {
    log('Error from remote server:', error)
  }
}
"""


def write_source(root, body):
    """Materialise a fake mcp-remote clone containing ``body``."""
    path = Path(root) / RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class ApplyTest(unittest.TestCase):
    def test_replaces_the_anchor_and_answers_the_client(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_source(root, PRELUDE + ANCHOR + CODA)
            apply(Path(root))
            patched = path.read_text()

        # The fire-and-forget form is gone and the client is now answered.
        self.assertNotIn(ANCHOR, patched)
        self.assertIn("transportToServer.send(message).catch((error: Error) =>", patched)
        self.assertIn("transportToClient", patched.split("onClientError(error: Error)")[0])
        self.assertIn(BUILD_MARKER, patched)
        self.assertIn("code: -32603", patched)
        # onServerError still runs, so the existing stderr breadcrumb survives.
        self.assertIn("onServerError(error)", patched)
        # Everything outside the anchor is untouched.
        self.assertTrue(patched.startswith(PRELUDE))
        self.assertTrue(patched.endswith(CODA))

    def test_only_requests_are_answered(self):
        """Notifications have no id and responses have no method; neither gets a reply."""
        with tempfile.TemporaryDirectory() as root:
            path = write_source(root, PRELUDE + ANCHOR + CODA)
            apply(Path(root))
            patched = path.read_text()
        self.assertIn(
            "if (message.id !== undefined && message.id !== null && message.method) {",
            patched,
        )

    def test_missing_file_fails_loudly(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SystemExit) as caught:
                apply(Path(root))
        self.assertIn("does not exist", str(caught.exception))

    def test_absent_anchor_fails_loudly(self):
        """A pin bump that moves the line must break the build, not no-op."""
        with tempfile.TemporaryDirectory() as root:
            write_source(root, PRELUDE + CODA)
            with self.assertRaises(SystemExit) as caught:
                apply(Path(root))
        self.assertIn("found 0", str(caught.exception))

    def test_duplicate_anchor_fails_loudly(self):
        with tempfile.TemporaryDirectory() as root:
            write_source(root, PRELUDE + ANCHOR + ANCHOR + CODA)
            with self.assertRaises(SystemExit) as caught:
                apply(Path(root))
        self.assertIn("found 2", str(caught.exception))

    def test_already_patched_upstream_fails_loudly(self):
        """If PR #308 merges upstream, drop this patch rather than double-applying."""
        with tempfile.TemporaryDirectory() as root:
            write_source(root, PRELUDE + ANCHOR + f"// {BUILD_MARKER}\n" + CODA)
            with self.assertRaises(SystemExit) as caught:
                apply(Path(root))
        self.assertIn("already contains", str(caught.exception))

    def test_applying_twice_fails_loudly(self):
        with tempfile.TemporaryDirectory() as root:
            write_source(root, PRELUDE + ANCHOR + CODA)
            apply(Path(root))
            with self.assertRaises(SystemExit):
                apply(Path(root))


if __name__ == "__main__":
    unittest.main()
