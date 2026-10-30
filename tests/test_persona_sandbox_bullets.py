"""The persona bullets on MCP tools and overflow files name only what exists.

`agents/cluster/SOUL.md` and `agents/platform/AGENTS.md` each carry a
"Prefer dedicated MCP tools (...)" list and a "Sandbox Execution & Overflow
Files" bullet. A tool in that list that no profile declares sends the agent to
search for it; a fixed overflow file name in the sandbox's shared `/tmp` lets
two agents running at once overwrite each other's output.

Nothing executes the prose, so these tests hold it to the source: every
backticked name in the list is an `mcp_servers` alias of that profile's
config (bare or `mcp-` prefixed) or a tool the platform_control server
defines, and the overflow bullet names its file through `mktemp`.

Run:
  python3 -m unittest discover -s tests -p 'test_persona_sandbox_bullets.py' -v
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# The Platform profile's config is merged onto the shared defaults at image
# build (deploy/docker/Dockerfile, merge_configs.py); the Cluster profile's is not.
SHARED_DEFAULTS = "deploy/shared/defaults/config.yaml"
PERSONAS = {
    "agents/cluster/SOUL.md": ("agents/cluster/config.yaml",),
    "agents/platform/AGENTS.md": ("agents/platform/config.yaml", SHARED_DEFAULTS),
}
PLATFORM_MCP_SERVER = REPO_ROOT / "agents/platform/scripts/platform_mcp_server.py"
PLATFORM_CONTROL_ALIAS = "platform_control"
MCP_TOOLSET_PREFIX = "mcp-"

TOOL_LIST = re.compile(r"Prefer dedicated MCP tools \(([^)]*)\)")
BACKTICKED = re.compile(r"`([^`]+)`")
OVERFLOW_BULLET = re.compile(r"^- \*\*Sandbox Execution & Overflow Files:\*\*.*$", re.M)
FIXED_TMP_FILE = re.compile(r"`/tmp/[\w.-]+`")
PLATFORM_TOOL = re.compile(r"^@mcp\.tool\(\)\s*\ndef (\w+)\(", re.M)


def declared_tools(configs: tuple[str, ...]) -> set[str]:
    servers: set[str] = set()
    included: set[str] = set()
    for config in configs:
        declared = (yaml.safe_load((REPO_ROOT / config).read_text()) or {}).get("mcp_servers") or {}
        servers |= set(declared)
        for spec in declared.values():
            included |= set(((spec or {}).get("tools") or {}).get("include") or [])
    names = servers | {MCP_TOOLSET_PREFIX + alias for alias in servers} | included
    if PLATFORM_CONTROL_ALIAS in servers:
        names |= set(PLATFORM_TOOL.findall(PLATFORM_MCP_SERVER.read_text()))
    return names


class PersonaSandboxBulletsTest(unittest.TestCase):
    def test_the_preferred_mcp_tools_are_declared(self):
        for persona, configs in PERSONAS.items():
            text = (REPO_ROOT / persona).read_text()
            lists = TOOL_LIST.findall(text)
            with self.subTest(persona=persona):
                self.assertTrue(lists, "the 'Prefer dedicated MCP tools' list is gone; update this test")
                named = {name for group in lists for name in BACKTICKED.findall(group)}
                self.assertEqual(named - declared_tools(configs), set(), f"{persona} names MCP tools {configs} do not declare")

    def test_the_overflow_file_is_named_by_mktemp(self):
        for persona in PERSONAS:
            bullets = OVERFLOW_BULLET.findall((REPO_ROOT / persona).read_text())
            with self.subTest(persona=persona):
                self.assertEqual(len(bullets), 1, "the overflow bullet is gone or doubled; update this test")
                # /tmp goes with the pod; a file under /opt/data outlives it on the volume.
                self.assertIn("`mktemp /tmp/", bullets[0])
                self.assertEqual(FIXED_TMP_FILE.findall(bullets[0]), [], "a fixed /tmp file name is shared by every session")


if __name__ == "__main__":
    unittest.main()
