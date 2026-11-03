#!/usr/bin/env python3
# router_server.py - Chat Agent discovery MCP server.
#
# Exposes a single discovery tool so the front-door Chat Agent (the `default`
# profile) can learn which specialist Hermes profiles exist and what each is
# responsible for. The Chat Agent uses this to pick the right `assignee` before
# it delegates work.
#
# The roster itself is now injected into every turn by the `agent_roster`
# plugin, so this tool is the REFRESH path rather than the common path: a
# specialist created moments ago, or an injected block the model has reason to
# doubt. It is also the LOOKUP path: above a threshold the injected block
# counts a shared-role group (the Cluster Agents) instead of naming each, and
# `filter` hands back the names for one cluster, project or location. Both read
# the same `agent_roster` module, so the tool and the injected block can never
# describe the fleet differently.
#
# Delegation itself does NOT happen here: the Chat Agent delegates exclusively
# via the asynchronous kanban board (`kanban_create`), so the user sees live,
# non-blocking progress in the thread. This module used to also expose a
# synchronous `ask_agent` relay (`hermes -p <name> -z ...`); that path blocked
# for up to 5 minutes with no visible progress and was removed in favor of the
# kanban-only model.

from mcp.server import MCPServer

# Same directory as this script, which Python puts on sys.path[0] when it runs
# a file — the MCP server is launched as `python3 <home>/scripts/router_server.py`.
# Imported as a module, not by name: `agent_roster.PROFILES_BASE` is read at call
# time, and a `from ... import PROFILES_BASE` here would be a second binding that
# rebinding the module's own never reaches.
import agent_roster

mcp = MCPServer("Chat Router")


@mcp.tool()
def list_agents(filter: str = "") -> str:
    """Refresh the roster of specialist agents you can route to, or look one up.

    The current roster is ALREADY in your context — it is injected at the start of
    every turn. Call this only to re-read it: when an agent you expect is missing,
    when one was just created, or when a name you are about to use as `assignee`
    does not appear above. It does no work itself.

    Agents sharing an identical role description (every Cluster Agent is scaffolded from the
    same template) are grouped so the description is stated once instead of repeated verbatim
    per agent. A group larger than the install's threshold is counted by project and location
    instead of named; pass `filter` (a cluster name, assignee id, project or location; a
    case-insensitive substring) to get the matching agents by name, capped at a count the
    answer's heading reports, so you can copy the one `assignee` you need. With no match the answer says so; route a named cluster that
    has no agent to `platform`. Without `filter` the tool renders what the injected block
    renders.
    """
    # A tool has to answer with a string, so an unreadable roster (render() -> None)
    # is spelled out rather than collapsed into "no agents exist". The injecting
    # plugin has the better option there and simply stays quiet.
    if filter.strip():
        roster = agent_roster.render_filtered(filter)
    else:
        roster = agent_roster.render()
    return agent_roster.UNKNOWN_ROSTER if roster is None else roster


if __name__ == "__main__":
    mcp.run()
