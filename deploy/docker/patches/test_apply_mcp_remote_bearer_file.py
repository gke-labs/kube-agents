"""Unit tests for the mcp-remote bearer-file patch applied by the Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import tempfile
import unittest
from pathlib import Path

from apply_mcp_remote_bearer_file import BUILD_MARKER, RELATIVE, SUBSTITUTIONS, apply

# The regions being patched, reproduced from mcp-remote src/proxy.ts at the
# pinned commit (2ff8f167), with the code between them elided.
SOURCE = """\
import { GoogleAuth } from 'google-auth-library'

const auth = new GoogleAuth({
  scopes: ['https://www.googleapis.com/auth/cloud-platform'],
})

async function getGcloudAccessToken() {
  try {
    const client = await auth.getClient()
    const token = await client.getAccessToken()
    return token.token || null
  } catch (error) {
    return null
  }
}

async function getGcloudProjectId() {
  try {
    const projectId = await auth.getProjectId()
    return projectId
  } catch (error) {
    return null
  }
}

async function runProxy() {
  log('Discovering OAuth server configuration...')
  const discoveryResult = await discoverOAuthServerInfo(serverUrl, headers)

  const authInitializer = async () => {
    const authState = await authCoordinator.initializeAuth()
    return authState
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
    def patched(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_source(root, SOURCE)
            apply(Path(root))
            return path.read_text()

    def test_the_token_comes_from_the_file_before_adc_is_tried(self):
        patched = self.patched()
        self.assertIn(BUILD_MARKER, patched)
        token_fn = patched.split("async function getGcloudAccessToken() {")[1]
        # The file branch sits ahead of the GoogleAuth call, so it is what runs.
        self.assertLess(token_fn.index("readFileSync(BEARER_FILE"), token_fn.index("auth.getClient()"))
        # Outside the try: a missing file throws instead of being logged and
        # turned into null, which would fall through to the OAuth provider.
        self.assertLess(token_fn.index("readFileSync(BEARER_FILE"), token_fn.index("  try {"))
        self.assertIn("throw new Error", token_fn.split("  try {")[0])

    def test_the_project_and_discovery_lookups_are_skipped(self):
        patched = self.patched()
        project_fn = patched.split("async function getGcloudProjectId() {")[1]
        self.assertLess(project_fn.index("return null"), project_fn.index("auth.getProjectId()"))
        self.assertIn("? { authorizationServerUrl: serverUrl }", patched)
        self.assertIn(": await discoverOAuthServerInfo(serverUrl, headers)", patched)

    def test_the_token_never_goes_to_an_https_url_and_a_401_starts_no_oauth(self):
        patched = self.patched()
        run = patched.split("async function runProxy() {")[1]
        # The https guard runs before discovery, so before any request is made.
        self.assertLess(run.index("protocol === 'https:'"), run.index("discoverOAuthServerInfo"))
        init = run.split("const authInitializer = async () => {")[1]
        self.assertLess(init.index("if (BEARER_FILE)"), init.index("authCoordinator.initializeAuth()"))

    def test_every_anchor_is_consumed_once(self):
        patched = self.patched()
        for label, anchor, replacement in SUBSTITUTIONS:
            self.assertEqual(patched.count(replacement), 1, label)

    def test_a_second_run_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            write_source(root, SOURCE)
            apply(Path(root))
            with self.assertRaises(SystemExit):
                apply(Path(root))

    def test_a_moved_anchor_fails_the_build(self):
        with tempfile.TemporaryDirectory() as root:
            write_source(root, SOURCE.replace("async function getGcloudProjectId()", "async function projectId()"))
            with self.assertRaises(SystemExit):
                apply(Path(root))


if __name__ == "__main__":
    unittest.main()
