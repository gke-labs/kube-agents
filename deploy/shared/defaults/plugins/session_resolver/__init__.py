import logging
import os
from contextvars import ContextVar
from typing import Any, Dict, Optional
import requests
from gateway import session_context
from gateway.session_context import _SESSION_ID, _SESSION_USER_ID

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
                
            k8s_host = metadata.get("KUBERNETES_SERVICE_HOST")
            if k8s_host:
                logger.info("Injecting KUBERNETES_SERVICE_HOST=%s for tool %s", k8s_host, tool_name)
                KUBERNETES_SERVICE_HOST_VAR.set(k8s_host)
            else:
                KUBERNETES_SERVICE_HOST_VAR.set("")
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


def register(ctx: Any) -> None:
    """Register hooks and bind ContextVar to Hermes environment manager."""
    # Register the ContextVar in session_context._VAR_MAP so local.py native bridge copies it!
    session_context._VAR_MAP["KUBERNETES_SERVICE_HOST"] = KUBERNETES_SERVICE_HOST_VAR
    
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    logger.info("Session Resolver plugin registered successfully!")
