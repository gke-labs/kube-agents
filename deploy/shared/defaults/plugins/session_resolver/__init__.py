import logging
import os
import hmac
import hashlib
import json
from contextvars import ContextVar
from typing import Any, Dict, Optional
import requests
from gateway import session_context
from gateway.session_context import _SESSION_ID, _SESSION_USER_ID, _SESSION_CHAT_ID, _SESSION_THREAD_ID

logger = logging.getLogger("hermes.plugin.session_resolver")

# Define KUBERNETES_SERVICE_HOST ContextVar
KUBERNETES_SERVICE_HOST_VAR = ContextVar("KUBERNETES_SERVICE_HOST", default="")

SESSION_RESOLVER_URL = os.getenv("SESSION_RESOLVER_URL", "http://platform-agent.agent-system.svc.cluster.local:8699")


def fetch_metadata_from_agent_a(session_id: str) -> Optional[Dict[str, Any]]:
    """Query Platform Agent's metadata API to fetch the active session's metadata."""
    if not session_id:
        return None

    url = f"{SESSION_RESOLVER_URL.rstrip('/')}/v1/sessions/{session_id}/metadata"
    headers = {
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.error(
            "Failed to retrieve metadata from Platform Agent (%s) for session %s: %s",
            url, session_id, exc
        )
        return None


def emit_thought_to_webhook(worker_id: str, space_id: str, thread_id: str, thought_text: str):
    """Emit intermediate thoughts live to Google Chat via platform agent webhook."""
    if not space_id or space_id in ("default_space", "string", "none", "null", "") or not space_id.startswith("spaces/"):
        return
        
    url = "http://platform-agent.agent-system.svc.cluster.local:8644/webhooks/swarm-thought-stream"
    payload = {
        "worker_id": worker_id,
        "user_space": space_id,
        "user_thread": thread_id,
        "thought": thought_text
    }
    try:
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = hmac.new(b"k8s-swarm-secret-999", body_bytes, hashlib.sha256).hexdigest()
        
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Signature": sig
        }
        response = requests.post(url, data=body_bytes, headers=headers, timeout=2.0)
        response.raise_for_status()
    except Exception as e:
        logger.debug("Failed to emit pre-tool thought to webhook: %s", e)


def on_pre_tool_call(
    tool_name: str, 
    args: Dict[str, Any], 
    session_id: str = "", 
    **kwargs: Any
) -> Optional[Dict[str, Any]]:
    """Resolve metadata using session_id and bind variables to the thread context."""
    
    if session_id:
        logger.info("Injecting HERMES_SESSION_ID=%s for tool %s", session_id, tool_name)
        _SESSION_ID.set(session_id)
        
        metadata = fetch_metadata_from_agent_a(session_id)
        if metadata:
            user_email = metadata.get("user_email")
            if user_email:
                logger.info("Injecting HERMES_SESSION_USER_ID=%s for tool %s", user_email, tool_name)
                _SESSION_USER_ID.set(user_email)
                
            chat_id = metadata.get("google_chat_id")
            if chat_id:
                logger.info("Injecting HERMES_SESSION_CHAT_ID=%s for tool %s", chat_id, tool_name)
                _SESSION_CHAT_ID.set(chat_id)
                
            thread_id = metadata.get("google_thread_id")
            if thread_id:
                logger.info("Injecting HERMES_SESSION_THREAD_ID=%s for tool %s", thread_id, tool_name)
                _SESSION_THREAD_ID.set(thread_id)
                
            k8s_host = metadata.get("KUBERNETES_SERVICE_HOST")
            if k8s_host:
                logger.info("Injecting KUBERNETES_SERVICE_HOST=%s for tool %s", k8s_host, tool_name)
                KUBERNETES_SERVICE_HOST_VAR.set(k8s_host)
            else:
                KUBERNETES_SERVICE_HOST_VAR.set("")
                
            # Emit thought to webhook to indicate tool is about to be called
            if chat_id:
                worker_id = os.getenv("OTEL_SERVICE_NAME") or os.getenv("HOSTNAME") or "subagent"
                args_preview = str(args)[:200] + ("..." if len(str(args)) > 200 else "")
                thought_text = f"⚙️ Calling tool '{tool_name}' with args: {args_preview}"
                emit_thought_to_webhook(worker_id, chat_id, thread_id, thought_text)
        else:
            KUBERNETES_SERVICE_HOST_VAR.set("")
            _SESSION_USER_ID.set("")
    else:
        KUBERNETES_SERVICE_HOST_VAR.set("")
        
    return None


def on_post_tool_call(tool_name: str, **kwargs: Any) -> None:
    """Reset ContextVars to prevent leaking context across thread pools."""
    KUBERNETES_SERVICE_HOST_VAR.set("")
    _SESSION_ID.set("")
    _SESSION_USER_ID.set("")
    _SESSION_CHAT_ID.set("")
    _SESSION_THREAD_ID.set("")


def register(ctx: Any) -> None:
    """Register hooks and bind ContextVar to Hermes environment manager."""
    # Register the ContextVar in session_context._VAR_MAP so local.py native bridge copies it!
    session_context._VAR_MAP["KUBERNETES_SERVICE_HOST"] = KUBERNETES_SERVICE_HOST_VAR
    
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    logger.info("Session Resolver plugin registered successfully!")
