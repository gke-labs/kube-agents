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
# doubt. Both read the same `agent_roster` module, so the tool and the injected
# block can never describe the fleet differently.
#
# Delegation itself does NOT happen here: the Chat Agent delegates exclusively
# via the asynchronous kanban board (`kanban_create`), so the user sees live,
# non-blocking progress in the thread. This module used to also expose a
# synchronous `ask_agent` relay (`hermes -p <name> -z ...`); that path blocked
# for up to 5 minutes with no visible progress and was removed in favor of the
# kanban-only model.

import os

from mcp.server.fastmcp import FastMCP

# Same directory as this script, which Python puts on sys.path[0] when it runs
# a file — the MCP server is launched as `python3 <home>/scripts/router_server.py`.
# Imported as a module, not by name: `agent_roster.PROFILES_BASE` is read at call
# time, and a `from ... import PROFILES_BASE` here would be a second binding that
# rebinding the module's own never reaches.
import agent_roster

# Shared with the Platform Agent's platform_control server, which exposes the
# same tool. Both profiles' scripts land in /opt/defaults/scripts (two COPY
# lines in deploy/docker/Dockerfile) and the entrypoint copies that whole
# directory to $HERMES_HOME/scripts, so this import resolves the same way the
# agent_roster one above does.
from notify_delivery import deliver_notification

mcp = FastMCP("Chat Router")

# The default profile's own config, which is where the `platforms` block saying
# whether Google Chat or Slack is live ends up. Same file the Platform Agent
# reads through agent_common_server.CONFIG_PATH on a stock install; spelled with
# HERMES_HOME because a CR that sets spec.harness.hermes.agentHome moves it.
CONFIG_PATH = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "config.yaml")


def _run_env() -> dict[str, str]:
    """Environment for the `hermes send` subprocess.

    HOME is redirected for the same reason agent_common_server._run_env does it:
    the container's home is not writable by the agent user.
    """
    return {**os.environ, "HOME": "/tmp"}


@mcp.tool()
def list_agents() -> str:
    """Refresh the roster of specialist agents you can route to.

    The current roster is ALREADY in your context — it is injected at the start of
    every turn. Call this only to re-read it: when an agent you expect is missing,
    when one was just created, or when a name you are about to use as `assignee`
    does not appear above. It does no work itself.

    Agents sharing an identical role description (every Cluster Agent is scaffolded from the
    same template) are grouped so the description is stated once instead of repeated verbatim
    per agent. Assignee names are always listed individually.
    """
    # A tool has to answer with a string, so an unreadable roster (render() -> None)
    # is spelled out rather than collapsed into "no agents exist". The injecting
    # plugin has the better option there and simply stays quiet.
    roster = agent_roster.render()
    return agent_roster.UNKNOWN_ROSTER if roster is None else roster


@mcp.tool()
def send_notification(message: str, session_id: str = "") -> str:
    """Post a message into the chat thread a session belongs to.

    Use this to DELIVER a result that arrived somewhere the user cannot see —
    above all a kanban card that finished on a session which reached you over
    the API rather than over chat, such as a Kubernetes event alert (session ids
    beginning `k8s-evt-`). Kanban posts a worker's progress into a chat thread
    by itself; it cannot do that for an API session, so it wakes you instead and
    your post IS the delivery. Without this call the report is written and then
    lost.

    Call it whenever you are woken for such a card, without trying to work out
    whether the worker managed to post the report itself — some can and some
    cannot, and you have no way to tell. If one already did, this returns
    SKIPPED and posts nothing, so the duplicate you are worried about cannot
    happen.

    Do not use it to answer someone who is talking to you in a chat thread
    already: your normal reply goes there on its own, and this would double it.

    Args:
        message: the text to post — for a finished card, the worker's `result`
            passed through as-is. It is already written for the user, and a
            paraphrase can only lose detail.
        session_id: the session whose thread to reply in (e.g. `k8s-evt-a2cb3234`),
            quoted from the request that started the work. Omitting it, or naming
            a session with no chat thread, falls back to the home channel — the
            report still lands, but not under the alert it answers.
    """
    # only_if_undelivered is what makes the instruction above safe: the front
    # door relays unconditionally and the tool drops the post if the worker beat
    # it to the thread. A Platform Agent has send_notification of its own; a
    # Cluster Agent does not.
    return deliver_notification(
        message,
        session_id,
        config_path=CONFIG_PATH,
        run_env=_run_env,
        only_if_undelivered=True,
    )


if __name__ == "__main__":
    mcp.run()
