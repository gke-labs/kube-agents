"""The installed `mcp` package still has to be one the agent's servers can use.

Three modules build an MCP server on `mcp.server.MCPServer`:
agents/platform/scripts/agent_common_server.py, its sibling
platform_mcp_server.py, and agents/chat/scripts/router_server.py. mcp 1.x has
no such module -- there it is `mcp.server.fastmcp.FastMCP` -- so an environment
on 1.x cannot run any of them.

This file used to assert the mirror image of that, and the flip is the whole
content of #800. mcp reaches the runtime image from the digest-pinned Hermes
base (tags.env), never from a requirement of ours, and that base moved to mcp
2.0.0 in v2026.8.19: `mcp.server.fastmcp` is gone, hermes-agent's own
mcp_serve.py and agent/transports/hermes_tools_mcp_server.py import
`mcp.server.MCPServer` instead, and all three of our servers stopped importing
at all until they followed. The floor in requirements-test.txt is now what
keeps a test environment on the same side of that line as the image.

Nothing used to say so. Each of those modules' test files falls back to a stub
when the import fails, which is right for a bare checkout with no mcp installed
and wrong for an mcp that is installed and incompatible: the fallback swallows
the ImportError and the suite goes green having exercised a SimpleNamespace.
That is how #751 widened the ceiling in requirements-test.txt from <2 to <3 and
passed CI. Those fallbacks re-raise when mcp is present (ABSENT is not BROKEN);
this module is the part that fails by name rather than by side effect.

This file lives under agents/platform/scripts because the Makefile's
`agents/*/scripts/test_*.py` glob discovers it there, but it covers the chat
module too -- the subprocess list below names all three.
"""

import os
import subprocess
import sys
import unittest
from importlib.metadata import PackageNotFoundError, distribution, version
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[3]

# Each server, and the directory it must be imported from: all three resolve
# their siblings through sys.path[0], which is the interpreter's cwd under -c.
SERVERS = (
    ("agent_common_server", REPO_ROOT / "agents" / "platform" / "scripts"),
    ("platform_mcp_server", REPO_ROOT / "agents" / "platform" / "scripts"),
    ("router_server", REPO_ROOT / "agents" / "chat" / "scripts"),
)


def _mcp_distribution_installed():
    """Is a real mcp distribution installed?

    importlib.metadata and not importlib.util.find_spec: find_spec consults
    sys.modules first and reads __spec__ off whatever it finds, which is None
    on a types.ModuleType and raises ValueError rather than answering. That is
    not hypothetical here -- unittest discovery imports this whole directory
    into one process in alphabetical order, test_agent_common_server sorts
    first, and in a bare checkout it leaves exactly such a stub under "mcp".
    The question being asked is about the installed distribution anyway, which
    is the one thing sys.modules cannot tell you.

    This is the one copy of that reasoning. The four guards that ask the same
    question -- test_agent_common_server, test_platform_mcp_server,
    test_session_kv_server, agents/chat/scripts/test_router_server -- point
    here rather than restate it, so moving the constraint changes one place.
    """
    try:
        distribution("mcp")
        return True
    except PackageNotFoundError:
        return False


MCP_INSTALLED = _mcp_distribution_installed()

# A bare checkout has no third-party packages at all and is a supported way to
# run the rest of the suite -- `make test-python` warns and carries on. There is
# no contract to check when the package under discussion is not there.
requires_mcp = unittest.skipUnless(
    MCP_INSTALLED, "mcp is not installed; run `make test-python-deps`"
)


def _installed_mcp_version():
    try:
        return version("mcp")
    except PackageNotFoundError:  # importable but not an installed distribution
        return "unknown"


@requires_mcp
class InstalledMcpExposesMcpServerTest(unittest.TestCase):
    """The symbol all three servers are written against."""

    def test_the_installed_mcp_exposes_mcpserver(self):
        try:
            from mcp.server import MCPServer  # noqa: F401
        except ImportError as exc:
            self.fail(
                f"mcp {_installed_mcp_version()} does not provide "
                f"mcp.server.MCPServer ({exc}).\n"
                "agent_common_server.py, platform_mcp_server.py and "
                "router_server.py are all built on it, and the runtime image "
                "is on the 2.x line that has it. requirements-test.txt holds "
                "mcp>=2 for exactly this reason -- if that floor was just "
                "lowered, this is what it broke."
            )


@requires_mcp
class ServersImportAgainstTheRealPackageTest(unittest.TestCase):
    """Each server imports for real, in a clean interpreter.

    The subprocess is the whole point. unittest discovery imports every test
    module in this directory into ONE process, and several of them put stubs
    into sys.modules on the way past; an in-process import here would prove
    only that some earlier module had already faked what it needed. A fresh
    interpreter sees the installed packages and nothing else.
    """

    def test_the_servers_import_against_the_real_package(self):
        for module, directory in SERVERS:
            with self.subTest(module=module):
                # platform_mcp_server writes per-thread kubeconfigs under
                # HERMES_HOME at import, and its default (/opt/data) exists
                # only in the container -- python-tests.yml redirects it for
                # the same reason.
                with TemporaryDirectory() as home:
                    # Prepend rather than replace: the Makefile already exports
                    # a PYTHONPATH for the discovery process, and dropping it
                    # would make this subprocess see a different tree from the
                    # one the rest of the suite runs against.
                    existing = os.environ.get("PYTHONPATH")
                    env = {
                        **os.environ,
                        "HERMES_HOME": home,
                        "PYTHONPATH": os.pathsep.join(
                            p for p in (str(REPO_ROOT), existing) if p
                        ),
                    }
                    result = subprocess.run(
                        [sys.executable, "-c", f"import {module}"],
                        cwd=directory,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{module} does not import against the installed packages "
                    f"(mcp {_installed_mcp_version()}). Its own test module "
                    "may still pass by falling back to a stub, which is what "
                    "this test exists to get in front of.\n"
                    f"--- stderr ---\n{result.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
