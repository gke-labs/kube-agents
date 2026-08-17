#!/usr/bin/env python3
# notify_server.py - Single-tool chat-egress MCP server for Cluster Agent profiles.
#
# A Cluster Agent is the agent that triages a Kubernetes event on its own
# cluster: session_kv_server.trigger_agent_troubleshooter creates the triage
# session on that cluster's profile, so the agent that writes the RCA is the
# agent that has to post it. Its toolset was two read-only remote MCP proxies
# and nothing else, so the report was written and then dropped (issue #630).
#
# This server exists so the grant can be exactly one tool. The alternative —
# handing a Cluster Agent `platform_control` — would give a deliberately
# read-only profile the whole provisioning and Config-Connector surface to buy
# one `hermes send`.
#
# The implementation is shared with the Platform Agent's platform_control
# server, which exposes the same tool under the same name; see
# notify_delivery.py. Cluster profiles reference this file as
# ${HERMES_HOME}/scripts/notify_server.py (see agents/cluster/config.yaml) —
# scripts live on the shared PVC pod-wide, not per profile.

import os

from mcp.server.fastmcp import FastMCP

# Same directory as this script, which Python puts on sys.path[0] when it runs
# a file — the MCP server is launched as `python3 <home>/scripts/notify_server.py`.
from notify_delivery import deliver_notification

mcp = FastMCP("Notify")

# NOT the calling profile's own config.yaml: a Cluster Agent profile is
# scaffolded from agents/cluster/config.yaml, which carries no `platforms` block
# and never will — which chat platform the install posts to is an install-wide
# fact, not a per-cluster one. $HERMES_HOME/config.yaml is the default profile's
# config, where that block lives. On a deployed pod the operator's managed
# overlay is authoritative for it and this copy may say nothing, which is
# harmless: enabled_platforms() then falls back to the SLACK_/GOOGLE_CHAT_
# environment variables, the same path the Platform Agent has always taken
# (agents/platform/config.yaml has no `platforms` block either).
CONFIG_PATH = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "config.yaml")


def _run_env() -> dict[str, str]:
    """Environment for the `hermes send` subprocess.

    HOME is redirected for the same reason agent_common_server._run_env does it:
    the container's home is not writable by the agent user.
    """
    return {**os.environ, "HOME": "/tmp"}


@mcp.tool()
def send_notification(message: str, session_id: str = "") -> str:
    """Post your report into the chat thread of the incident you are working on.

    This is the ONLY way anything you write reaches a human. You are running as
    a background worker on a session that arrived over the API — a Kubernetes
    event alert (session ids beginning `k8s-evt-`) — and nothing you return is
    shown to anyone unless you post it here. An analysis you finish without
    calling this is lost.

    Args:
        message: the report to post, complete and formatted, exactly as the
            person reading the alert should see it.
        session_id: the session whose thread to reply in (e.g.
            `k8s-evt-a2cb3234`), quoted from the request that started the work.
            Omitting it, or naming a session with no chat thread, falls back to
            the home channel — the report still lands, but not under the alert
            it answers, where nobody can tell which incident it explains.
    """
    return deliver_notification(
        message,
        session_id,
        config_path=CONFIG_PATH,
        run_env=_run_env,
    )


if __name__ == "__main__":
    mcp.run()
