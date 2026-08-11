#!/usr/bin/env python3
# router_server.py - Chat Agent discovery MCP server.
#
# Exposes a single discovery tool so the front-door Chat Agent (the `default`
# profile) can learn which specialist Hermes profiles exist and what each is
# responsible for. The Chat Agent uses this to pick the right `assignee` before
# it delegates work.
#
# Delegation itself does NOT happen here: the Chat Agent delegates exclusively
# via the asynchronous kanban board (`kanban_create`), so the user sees live,
# non-blocking progress in the thread. This module used to also expose a
# synchronous `ask_agent` relay (`hermes -p <name> -z ...`); that path blocked
# for up to 5 minutes with no visible progress and was removed in favor of the
# kanban-only model. `list_agents` remains because choosing an assignee still
# requires an up-to-date roster of the dynamic specialist set.

import os
import sys

from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Chat Router")

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Hermes stores each profile at $HERMES_HOME/profiles/<name> (persists on the data PVC).
PROFILES_BASE = HERMES_HOME / "profiles"
# The front door itself; never a valid delegation target.
SELF_PROFILE = "default"


def log(msg: str) -> None:
    print(f"[ROUTER-MCP] {msg}", file=sys.stderr)


def _summarize_soul(home: Path) -> str:
    """Fallback responsibilities: the first prose line of the profile's SOUL.md."""
    soul = home / "SOUL.md"
    # is_file() is inside the try: pathlib only swallows ENOENT/ENOTDIR/EBADF/ELOOP,
    # so a stat() that fails with EACCES or EIO on the shared PVC raises here too.
    try:
        if not soul.is_file():
            return ""
        lines = soul.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        return s
    return ""


def _responsibilities(home: Path) -> str:
    """A one-shot description of what a profile is responsible for.

    Prefers an explicit CAPABILITIES.md (the routing contract a specialist
    advertises to the Chat Agent); falls back to the SOUL.md summary so a
    profile is still discoverable even without a capabilities file.
    """
    cap = home / "CAPABILITIES.md"
    try:
        text = cap.read_text(encoding="utf-8", errors="replace").strip() if cap.is_file() else ""
    except OSError:
        text = ""
    if text:
        return text
    return _summarize_soul(home)


def _discover() -> list[dict[str, str]]:
    """Enumerate every routable specialist profile (all profiles except `default`).

    Degrades rather than raises. This backs the Chat Agent's only discovery tool,
    and the profiles live on a shared PVC, so an I/O or permission fault must not
    turn routing into a traceback. Errors are isolated per profile: one unreadable
    directory costs that one agent, not the whole roster.
    """
    agents: list[dict[str, str]] = []
    try:
        entries = sorted(PROFILES_BASE.iterdir()) if PROFILES_BASE.is_dir() else []
    except OSError as e:
        log(f"cannot list profiles at {PROFILES_BASE}: {e}")
        return agents
    for p in entries:
        try:
            if not p.is_dir() or p.name == SELF_PROFILE:
                continue
            agents.append({"name": p.name, "responsibilities": _responsibilities(p)})
        except OSError as e:
            log(f"skipping unreadable profile {p.name}: {e}")
    return agents


@mcp.tool()
def list_agents() -> str:
    """Discover every specialist agent you can route to, and what each is responsible for.

    Call this before delegating so you pick the right target. The registry is dynamic: newly
    created agents (for example a per-cluster agent spun up when a cluster is onboarded) appear
    here automatically.

    Agents sharing an identical role description (every Cluster Agent is scaffolded from the
    same template) are grouped so the description is stated once instead of repeated verbatim
    per agent. Assignee names are always listed individually.
    """
    agents = _discover()
    if not agents:
        return "No specialist agents are currently available to route to."

    groups: dict[str, list[str]] = {}
    for a in agents:
        desc = a["responsibilities"] or "(no description provided)"
        groups.setdefault(desc, []).append(a["name"])

    # Distinct specialists first, then the shared-role fleets — the front door routes to a
    # named specialist far more often than to a specific cluster.
    blocks: list[str] = []
    for desc, names in sorted(groups.items(), key=lambda kv: len(kv[1])):
        if len(names) == 1:
            blocks.append(f"- {names[0]}: {desc}")
            continue
        listed = "\n".join(f"  - {n}" for n in names)
        blocks.append(
            f"The following {len(names)} agents share one role — pick the one whose cluster "
            f"you need:\n{listed}\n  Shared role: {desc}"
        )
    return "\n\n".join(blocks)


if __name__ == "__main__":
    mcp.run()
