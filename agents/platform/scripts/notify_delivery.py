#!/usr/bin/env python3
# notify_delivery.py - Shared chat-egress delivery for the `send_notification` tool.
#
# Two profiles expose `send_notification` and both call straight into here:
#
#   - the Platform Agent, via platform_control (agents/platform/scripts/
#     platform_mcp_server.py), which is where this code used to live inline;
#   - the front-door Chat Agent, via its router MCP server (agents/chat/scripts/
#     router_server.py).
#
# The Chat Agent needs it because of how kanban delivers a finished card. For a
# push-capable adapter the notifier posts the worker's `result` to the thread
# itself; for the API server — which is how an event alert arrives — it cannot,
# so it wakes the card's CREATOR instead and that turn's own post is the
# delivery (`kanban.wake_on_events` in agents/chat/config.yaml spells this out).
# The creator of an event-triage card is the Chat Agent, whose toolset is
# router + kanban + memory. It was woken to deliver a report it had no way to
# send, so every RCA delegated to a profile without platform_control — every
# Cluster Agent, and the Chat Agent answering by itself — was written and then
# dropped. See issue #630.
#
# Giving the Chat Agent platform_control instead would have handed the
# lowest-privilege profile in the fleet the whole provisioning surface. One tool
# on the MCP server it already runs is the smaller grant.
#
# Deliberately dependency-light: `import os, json, subprocess, urllib` and
# nothing else. The obvious alternative home, agent_common_server.py, builds a
# SessionManager and pulls in pydantic at import, and the point of the Chat
# Agent is that it carries as little as possible.
#
# `config_path` and `run_env` are parameters rather than module constants
# because the two callers resolve them differently: the Platform Agent shares
# agent_common_server's, and the Chat Agent reads its own profile's config.

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
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
        platform = "slack" if "slack" in platforms else "google_chat"
    if not (chat_id and thread_id):
        return None, None, None
    return chat_id, thread_id, f"{platform}:{chat_id}:{thread_id}"


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


def incident_delivered(chat_id: str, thread_id: str) -> bool:
    """Has a report already been posted into this thread?

    Fails OPEN — an unreachable KV server reports "not delivered" and the post
    goes ahead. That is the opposite of session_kv_server._incident_stored,
    which fails closed, and deliberately so: there the cost of being wrong is a
    spurious log line, here it is a report nobody ever sees. A duplicate is the
    cheaper mistake.
    """
    query = urllib.parse.urlencode({"chat_id": chat_id, "thread_id": thread_id})
    try:
        req = urllib.request.Request(
            f"{SESSION_KV_BASE}/v1/incidents/by-thread?{query}",
            headers=session_kv_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            print(f"[notify] incident lookup for {thread_id} failed: {exc}", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[notify] incident lookup for {thread_id} failed: {exc}", file=sys.stderr)
        return False


def deliver_notification(
    message: str,
    session_id: str = "",
    *,
    config_path: str,
    run_env,
    only_if_undelivered: bool = False,
) -> str:
    """Post `message` to chat, threaded under `session_id`'s alert when possible.

    Args:
        message: the text to post.
        session_id: the session whose thread to reply in. Empty, or a session
            with no recorded thread, falls back to the configured home channel.
        config_path: the profile config to read `platforms` from.
        run_env: callable returning the environment for `hermes send`.
        only_if_undelivered: skip the post when this thread already has a
            report. The Chat Agent relays a completed card without knowing
            whether the worker could post it — a Platform Agent can and does, a
            Cluster Agent cannot — so the front door sets this and the worker
            does not. Ordering is what makes it work: the worker posts before
            its card completes, and the creator is only woken afterwards.

    Returns a per-target status line, which is what the model sees.
    """
    platforms = enabled_platforms(config_path)
    chat_id, thread_id, target = resolve_thread(session_id, platforms)

    if only_if_undelivered and chat_id and thread_id and incident_delivered(chat_id, thread_id):
        return (
            "SKIPPED: the agent that did the work already posted a report into this "
            "thread, so this would have been a duplicate. Nothing further to do."
        )

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
