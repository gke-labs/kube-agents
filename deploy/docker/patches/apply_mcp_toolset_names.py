"""Stop the CLI warning that every MCP toolset is unknown.

One anchored edit in ``cli.py``. Before MCP discovery has run, ``HermesCLI``
validates the profile's toolset list against the static registry and excludes
the configured MCP servers from the check, because their toolsets are
registered later, during ``discover_mcp_tools``. Upstream excludes them by the
bare server name (``gke``), while the toolset a profile lists is the alias the
registry will carry (``mcp-gke``; ``register_toolset_alias`` in
``tools/mcp_tool_registration.py`` at v2026.9.14). So every MCP toolset fails
the check and the CLI prints ``Warning: Unknown toolsets: mcp-...`` on stdout
at the start of every run. The tools are not missing: on a dev install the
same invocation, run inside the platform-agent container, prints the line and
then calls ``mcp__gke__list_clusters`` and answers from it. Under ``hermes chat
-Q`` that stdout is the answer, and the Hermes bridge publishes it as the
task's result, so every reply through the bus opened with the warning
(kube-agents #2035, which measured by that line). Upstream: NousResearch/hermes-agent#78102,
with the same fix open as #78158; this patch goes when a Hermes bump carries it.

The edit widens the exclusion set to the prefixed aliases as well as the bare
names. The anchor is the one assignment; the replacement keeps it and adds the
aliases beside it, so the anchor count cannot tell a fresh file from a patched
one and the marker check does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

CLI_RELATIVE = "cli.py"

# The exclusion set as upstream spells it, immediately above the check that
# prints the warning.
NAMES_ANCHOR = (
    '            mcp_names = set((CLI_CONFIG.get("mcp_servers") or {}).keys())\n'
)

NAMES_REPLACEMENT = (
    '            mcp_names = set((CLI_CONFIG.get("mcp_servers") or {}).keys())\n'
    "            # kube-agents patch: the toolset a profile lists is the alias the\n"
    "            # registry carries once discovery runs (mcp-<server>), not the\n"
    "            # bare server name, so exclude both spellings or every MCP\n"
    "            # toolset prints as unknown on stdout ahead of the answer.\n"
    '            mcp_names |= {f"mcp-{_kube_mcp_name}" for _kube_mcp_name in mcp_names}\n'
)

MARKER = "_kube_mcp_name"


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, CLI_RELATIVE, prefix="mcp-toolset-names")
    patch.refuse_if_patched(MARKER)
    patch.substitute(NAMES_ANCHOR, NAMES_REPLACEMENT, label="mcp toolset exclusion")
    patch.commit("prefixed MCP toolset aliases excluded from the pre-discovery check")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
