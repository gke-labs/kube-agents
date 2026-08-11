import importlib
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Add the directory containing router_server.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))

import agent_roster  # noqa: E402


def _load_router_server():
    """Import the module under test.

    These tests exercise only stdlib logic (delegation to agent_roster + the
    absence of the removed relay). When the hermes runtime deps (FastMCP /
    pydantic) aren't importable, fall back to minimal stubs so the module still
    imports in a bare checkout. `FastMCP().tool()` returns identity, so the
    decorated tools remain plain callables.
    """
    try:
        return importlib.import_module("router_server")
    except Exception:
        mcp = types.ModuleType("mcp"); mcp.__path__ = []
        mcp_server = types.ModuleType("mcp.server"); mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = lambda *a, **k: types.SimpleNamespace(
            tool=lambda *a, **k: (lambda f: f), run=lambda: None)
        pydantic = types.ModuleType("pydantic")
        pydantic.Field = lambda *a, **k: None
        sys.modules.update({
            "mcp": mcp, "mcp.server": mcp_server, "mcp.server.fastmcp": fastmcp,
            "pydantic": pydantic,
        })
        return importlib.import_module("router_server")


router = _load_router_server()


class TestListAgentsDelegates(unittest.TestCase):
    """The tool is a thin wrapper over the shared roster.

    Discovery and formatting are covered in test_agent_roster.py. What matters
    here is that the tool reads the SAME module the injected block does: a
    refresh path that renders the fleet differently from the block it refreshes
    is worse than no refresh path at all.
    """

    def test_returns_the_shared_roster(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "platform", "cluster-a"):
                (base / name).mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            agent_roster.PROFILES_BASE = base

            self.assertEqual(router.list_agents(), agent_roster.render())
            self.assertIn("- platform: Fleet + GitOps write path.", router.list_agents())

    def test_resolves_the_roster_base_at_call_time(self):
        # A `from agent_roster import PROFILES_BASE` in router_server would be a
        # second binding that never sees a rebind of the module's own — the tool
        # would keep reading whichever path was current at import.
        with TemporaryDirectory() as tmp:
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("No specialist agents", router.list_agents())

    def test_an_unreadable_roster_says_so_rather_than_claiming_none(self):
        # render() answers None when discovery itself failed. A tool has to
        # return a string, and "no specialist agents" is the one string it must
        # not be: the front door would stop routing on an I/O fault.
        with mock.patch.object(agent_roster, "render", return_value=None):
            out = router.list_agents()
        self.assertEqual(out, agent_roster.UNKNOWN_ROSTER)
        self.assertNotIn("No specialist agents", out)


class TestKanbanOnly(unittest.TestCase):
    """The router is discovery-only: the synchronous ask_agent relay is gone.

    Delegation happens exclusively via the asynchronous kanban board so the user
    sees non-blocking progress in the thread; the router only advertises the
    dynamic specialist roster used to pick an assignee.
    """

    def test_ask_agent_removed(self):
        self.assertFalse(hasattr(router, "ask_agent"))

    def test_no_blocking_subprocess_machinery(self):
        # These only existed to support the removed synchronous relay.
        self.assertFalse(hasattr(router, "INVOKE_TIMEOUT"))
        self.assertFalse(hasattr(router, "_run_env"))


if __name__ == "__main__":
    unittest.main()
