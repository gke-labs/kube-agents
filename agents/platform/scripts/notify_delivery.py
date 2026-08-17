#!/usr/bin/env python3
# notify_delivery.py - Shared chat-egress delivery for the `send_notification` tool.
#
# Two kinds of profile expose `send_notification` and both call straight into
# here:
#
#   - the Platform Agent, via platform_control (agents/platform/scripts/
#     platform_mcp_server.py), which is where this code used to live inline;
#   - every Cluster Agent, via the single-tool notify MCP server
#     (agents/platform/scripts/notify_server.py).
#
# A Cluster Agent needs it because it is now the agent that triages a Kubernetes
# event on its own cluster: the event wakes the front door, which delegates the
# whole diagnosis to that cluster's agent as one kanban card, and the card's
# requester is an `api_server` session with no chat thread subscribed to it. So
# the agent that writes the RCA is the only one positioned to deliver it, and
# `kanban_complete` is not a delivery. Before this, a Cluster Agent's toolset was
# two read-only remote MCP proxies and nothing else — it produced a diagnosis
# with no way to post it, and every RCA it wrote was dropped. See issue #630.
#
# Giving a Cluster Agent platform_control instead would have handed a
# deliberately read-only profile the whole provisioning surface. One tool on a
# server that exposes only that tool is the smaller grant.
#
# Deliberately dependency-light: `import os, json, subprocess, urllib` and
# nothing else. The obvious alternative home, agent_common_server.py, builds a
# SessionManager and pulls in pydantic at import, and a Cluster Agent worker is
# a fresh process per card that should carry as little as possible.
#
# `config_path` and `run_env` are parameters rather than module constants
# because the two callers resolve them differently: the Platform Agent shares
# agent_common_server's, and a Cluster Agent reads its own scaffolded profile's
# config.

import json
import os
import subprocess
import sys
import urllib.request

# The loopback Session KV server. The same literal is in platform_mcp_server.py's
# start_session_kv_server and in the session_kv_server uvicorn line of
# deploy/shared/docker-entrypoint.sh.
SESSION_KV_BASE = "http://127.0.0.1:8699"


def session_kv_headers(base: dict | None = None) -> dict:
    """Authenticate a call to the loopback Session KV server.

    Not API_SERVER_KEY: that value is the non-secret loopback sentinel. The key
    used here comes from the pod secret. Whichever MCP server calls this must
    name SESSION_KV_API_KEY in its config `env` block — Hermes hands a stdio MCP
    child only a safe baseline plus the keys listed there, and without it the
    metadata read 401s, the thread cannot be resolved, and the report silently
    falls back to the home channel.
    """
    headers = dict(base or {})
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def enabled_platforms(config_path: str) -> list[str]:
    """Which chat platforms this install posts to, config first, env as fallback."""
    found: list[str] = []
    try:
        import yaml

        if os.path.exists(config_path):
            with open(config_path, "r") as fh:
                cfg = yaml.safe_load(fh) or {}
            platforms = cfg.get("platforms", {})
            if platforms.get("slack", {}).get("enabled"):
                found.append("slack")
            if platforms.get("google_chat", {}).get("enabled"):
                found.append("google_chat")
    except Exception:
        pass

    if not found:
        if os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_HOME_CHANNEL"):
            found.append("slack")
        if os.environ.get("GOOGLE_CHAT_PROJECT_ID") or os.environ.get("GOOGLE_CHAT_HOME_CHANNEL"):
            found.append("google_chat")

    if not found:
        found.append("google_chat")

    return found


def resolve_thread(session_id: str, platforms: list[str]) -> tuple[str | None, str | None, str | None]:
    """Look up the chat thread a session belongs to.

    Returns `(chat_id, thread_id, target)`, all None when the session has no
    recorded thread. `target` is the `platform:chat:thread` string `hermes send`
    wants; without it the report goes to the home channel instead of threading
    under the alert it answers.
    """
    if not session_id:
        return None, None, None
    try:
        req = urllib.request.Request(
            f"{SESSION_KV_BASE}/v1/sessions/{session_id}/metadata",
            headers=session_kv_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status != 200:
                return None, None, None
            meta = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        # Fail open to the home channel: a report in the wrong place still beats
        # no report, which is the failure this whole module exists to end.
        print(f"[notify] could not resolve thread for {session_id}: {exc}", file=sys.stderr)
        return None, None, None

    chat_id = meta.get("chat_id")
    thread_id = meta.get("thread_id")
    platform = meta.get("platform")
    # "k8s-watcher" is the event watcher naming itself as the session's origin,
    # not a chat platform anyone can be messaged on.
    if not platform or platform == "k8s-watcher":
        platform = _platform_of_thread(thread_id, platforms)
    if not (chat_id and thread_id):
        return None, None, None
    return chat_id, thread_id, f"{platform}:{chat_id}:{thread_id}"


def _platform_of_thread(thread_id: str | None, platforms: list[str]) -> str:
    """Which platform a recorded thread belongs to, when the row does not say.

    `_register_session_routing` records `platform` now, but rows written before
    it did still exist and this is the only thing standing between them and the
    wrong platform. A thread belongs to exactly one, and sending it to another
    does not fall back to anything — `hermes send` rejects the target outright
    (`Could not resolve 'spaces/…:spaces/…/threads/…' on slack`) and the report
    is simply not delivered.

    A Google Chat thread id is a resource path (`spaces/<id>/threads/<id>`),
    which no Slack channel or message timestamp looks like, so the shape is a
    reliable tell where the install's enabled set is not: an install with both
    platforms on has no majority to appeal to.
    """
    if thread_id and thread_id.startswith("spaces/"):
        return "google_chat"
    return "slack" if "slack" in platforms else "google_chat"


def store_incident(chat_id: str, thread_id: str, message: str) -> None:
    """Record the report so a human's follow-up in the thread has context.

    Non-fatal: the report has already been posted by the time this runs, and
    losing the reply context is a smaller failure than raising over it.
    """
    try:
        req = urllib.request.Request(
            f"{SESSION_KV_BASE}/v1/incidents",
            data=json.dumps({"chat_id": chat_id, "thread_id": thread_id, "report": message}).encode(),
            headers=session_kv_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception as exc:
        print(f"[notify] incident store failed (non-fatal): {exc}", file=sys.stderr)


def deliver_notification(
    message: str,
    session_id: str = "",
    *,
    config_path: str,
    run_env,
) -> str:
    """Post `message` to chat, threaded under `session_id`'s alert when possible.

    Args:
        message: the text to post.
        session_id: the session whose thread to reply in. Empty, or a session
            with no recorded thread, falls back to the configured home channel.
        config_path: the profile config to read `platforms` from.
        run_env: callable returning the environment for `hermes send`.

    Returns a per-target status line, which is what the model sees.
    """
    platforms = enabled_platforms(config_path)
    chat_id, thread_id, target = resolve_thread(session_id, platforms)

    targets = [target] if target else []
    if not targets:
        for name in platforms:
            if name == "slack":
                home = os.environ.get("SLACK_HOME_CHANNEL", "").strip()
                targets.append(f"slack:{home}" if home else "slack")
            elif name == "google_chat":
                home = os.environ.get("GOOGLE_CHAT_HOME_CHANNEL", "").strip()
                targets.append(f"google_chat:{home}" if home else "google_chat")
            else:
                targets.append(name)

    results = []
    for target in targets:
        platform_name = target.split(":", 1)[0]
        try:
            res = subprocess.run(
                ["hermes", "send", "--to", target, message],
                capture_output=True,
                text=True,
                check=True,
                env=run_env(),
            )
            results.append(f"SUCCESS: Notification posted to {platform_name}. Output: {res.stdout.strip()}")
        except subprocess.CalledProcessError as exc:
            results.append(f"ERROR: Failed to send notification to {platform_name}: {exc.stderr.strip()}")
        except Exception as exc:  # noqa: BLE001 - one dead target must not stop the others
            results.append(f"ERROR: {platform_name}: {exc}")

    if chat_id and thread_id:
        store_incident(chat_id, thread_id, message)

    return "\n".join(results) if results else "ERROR: No target platform configured."
