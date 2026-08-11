"""Credential handling in agent_common_server.

The module's one tool, `call_agent`, is no longer mounted — no profile declares
the `agent_common` MCP server (see the note above `mcp_servers` in
deploy/shared/defaults/config.yaml). The module itself stays, because
platform_mcp_server and session_kv_server import helpers from it, and so does
this contract: resolve_agent_credentials must fail closed on an unconfigured
key rather than authenticate as a guessable literal. That is the property any
future synchronous delegation path would have to keep, and the reason the tool
failed the way it did rather than reaching the network with a bad key.
"""

import importlib
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

# Add the directory containing agent_common_server.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))

from session_manager import SessionManager


def _load_agent_common_server():
    """Import the module under test.

    These tests run in CI via `make test-python`, and the credential logic under
    test (resolve_agent_credentials) depends only on the stdlib. When the hermes
    runtime deps (FastMCP / pydantic / session_manager) aren't importable, fall
    back to minimal stubs so the module still imports in a bare checkout. Each
    stub package sets __path__ so it is treated as a real package.
    """
    try:
        return importlib.import_module("agent_common_server")
    except Exception:
        import session_manager as real_session_manager

        mcp = types.ModuleType("mcp"); mcp.__path__ = []
        mcp_server = types.ModuleType("mcp.server"); mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = lambda *a, **k: types.SimpleNamespace(
            tool=lambda *a, **k: (lambda f: f), run=lambda: None)
        pydantic = types.ModuleType("pydantic")
        pydantic.Field = lambda *a, **k: None
        sys.modules.update({
            "mcp": mcp, "mcp.server": mcp_server, "mcp.server.fastmcp": fastmcp,
            "pydantic": pydantic, "session_manager": real_session_manager,
        })
        return importlib.import_module("agent_common_server")


_agent_common_server = _load_agent_common_server()
resolve_agent_credentials = _agent_common_server.resolve_agent_credentials


class TestNoSubprocessAtImport(unittest.TestCase):
    """Importing this module must not spawn a process.

    platform_mcp_server imports agent_common_server, so module scope runs twice
    per platform-profile kanban worker spawn (the agent_common and
    platform_control MCP children) and once more per pod in session_kv_server.
    A spawn from here costs an extra sandboxed interpreter start on every
    worker. See the note in agent_common_server.py for why the load_slack_token()
    shell-out this pins against could not have worked on either path.
    """

    def test_import_spawns_no_subprocess(self):
        import subprocess

        # The helper this pins against was guarded by
        # `if "SLACK_BOT_TOKEN" not in os.environ`, so without clearing that the
        # reload short-circuits and the assertions pass vacuously on any machine
        # where the variable is already set — including one where an earlier,
        # unmocked import populated it. subprocess.run/call/check_call/
        # check_output all route through Popen; os.system is patched separately
        # because it does not.
        with mock.patch.dict(os.environ):
            os.environ.pop("SLACK_BOT_TOKEN", None)
            with mock.patch.object(subprocess, "Popen") as popen, \
                    mock.patch.object(os, "system") as system:
                importlib.reload(_agent_common_server)

        popen.assert_not_called()
        system.assert_not_called()

    def test_load_slack_token_is_gone(self):
        """Regression pin: reintroducing the helper reintroduces the cost on
        every worker spawn, and it never worked. Named separately from the
        subprocess pin because it catches a reintroduction whose own guard
        would short-circuit the reload."""
        self.assertFalse(hasattr(_agent_common_server, "load_slack_token"))


class TestResolveAgentCredentials(unittest.TestCase):
    """API_SERVER_KEY must fail closed — never silently authenticate as a
    guessable literal when the shared secret is unconfigured (MCP-001)."""

    def setUp(self):
        self._saved = os.environ.get("API_SERVER_KEY")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("API_SERVER_KEY", None)
        else:
            os.environ["API_SERVER_KEY"] = self._saved

    def test_raises_when_key_unset(self):
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_raises_when_key_empty(self):
        os.environ["API_SERVER_KEY"] = ""
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_raises_when_key_whitespace(self):
        os.environ["API_SERVER_KEY"] = "   "
        with self.assertRaises(ValueError):
            resolve_agent_credentials("platform")

    def test_never_falls_back_to_none_literal(self):
        """Regression pin: an unconfigured key must fail closed — raise, never
        yield the guessable literal 'none'."""
        os.environ.pop("API_SERVER_KEY", None)
        with self.assertRaises(ValueError) as ctx:
            resolve_agent_credentials("platform")
        self.assertIn("API_SERVER_KEY is not configured", str(ctx.exception))

    def test_returns_endpoint_and_key_when_set(self):
        os.environ["API_SERVER_KEY"] = "s3cret"
        endpoint, api_key = resolve_agent_credentials("platform")
        self.assertEqual(api_key, "s3cret")
        self.assertIn("8642", endpoint)
        self.assertTrue(endpoint.startswith("https://"))


if __name__ == "__main__":
    unittest.main()
