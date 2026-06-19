import os
import sys
import json
import hmac
import hashlib
import urllib.request
import urllib.error
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Worker Emission and Sync RPC Toolset")

def log(msg: str):
    print(f"[worker-mcp] {msg}", file=sys.stderr, flush=True)

@mcp.tool()
def emit_thought(worker_id: str, space_id: str, thread_id: str, thought_text: str) -> str:
    """
    Emit intermediate thoughts live to user chat via webhook deliver_only: true proxy.
    Bypasses Coordinator LLM entirely for zero-cost sub-millisecond chat streaming.
    """
    env_space = os.getenv("HERMES_SESSION_CHAT_ID", "").strip()
    env_thread = os.getenv("HERMES_SESSION_THREAD_ID", "").strip()
    clean_space = (space_id or env_space).strip()
    clean_thread = (thread_id or env_thread).strip()
    if clean_space == "default_space" or not clean_space:
        clean_space = env_space
    if clean_thread == "default_thread":
        clean_thread = env_thread

    log(f"[emit_thought INVOCATION] Worker: '{worker_id}', Space: '{clean_space}', Thread: '{clean_thread}', Thought: '{thought_text[:60]}'")
    if not clean_space or clean_space in ("default_space", "string", "none", "null", "") or not clean_space.startswith("spaces/"):
        log(f"Thought emitted locally (stateless turn): [{worker_id}] {thought_text}")
        return "Thought recorded locally in execution log."

    url = "http://platform-agent.kubeagents-system.svc.cluster.local:8644/webhooks/swarm-thought-stream"
    payload = {
        "worker_id": worker_id,
        "user_space": clean_space,
        "user_thread": clean_thread,
        "thought": thought_text
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    webhook_secret = os.getenv("SWARM_WEBHOOK_SECRET", "k8s-swarm-secret-999").encode("utf-8")
    sig = hmac.new(webhook_secret, body_bytes, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body_bytes, headers={"Content-Type": "application/json", "X-Webhook-Signature": sig}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5.0)
        return "Thought successfully emitted live to Google Chat thread."
    except Exception as e:
        log(f"Warning: thought webhook failed silently: {e}")
        return "Thought recorded locally (webhook unreachable)."



@mcp.tool()
def notify_user(worker_id: str, space_id: str, thread_id: str, message: str) -> str:
    """
    Send a proactive, direct user-facing message/notification to Google Chat.
    Use this to alert the user of critical failures, completion results, or request clarification.
    """
    env_space = os.getenv("HERMES_SESSION_CHAT_ID", "").strip()
    env_thread = os.getenv("HERMES_SESSION_THREAD_ID", "").strip()
    clean_space = (space_id or env_space).strip()
    clean_thread = (thread_id or env_thread).strip()
    if clean_space == "default_space" or not clean_space:
        clean_space = env_space
    if clean_thread == "default_thread":
        clean_thread = env_thread

    log(f"[notify_user INVOCATION] Worker: '{worker_id}', Space: '{clean_space}', Thread: '{clean_thread}', Message: '{message[:60]}'")
    if not clean_space or clean_space in ("default_space", "string", "none", "null", "") or not clean_space.startswith("spaces/"):
        log(f"Notification printed locally (stateless turn): [{worker_id}] {message}")
        return "Notification printed locally in execution log."

    url = "http://platform-agent.kubeagents-system.svc.cluster.local:8644/webhooks/swarm-notification"
    payload = {
        "worker_id": worker_id,
        "user_space": clean_space,
        "user_thread": clean_thread,
        "message": message
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    webhook_secret = os.getenv("SWARM_WEBHOOK_SECRET", "k8s-swarm-secret-999").encode("utf-8")
    sig = hmac.new(webhook_secret, body_bytes, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body_bytes, headers={"Content-Type": "application/json", "X-Webhook-Signature": sig}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5.0)
        return "Notification successfully sent live to Google Chat thread."
    except Exception as e:
        log(f"Warning: notification webhook failed silently: {e}")
        return "Notification recorded locally (webhook unreachable)."



@mcp.tool()
def call_agent(target_agent_id: str, query: str, session_id: str = "") -> str:
    """
    Directly and securely execute a synchronous, token-authorized completions API call
    to another GKE Operator or DevTeam peer agent across clusters in your GKE fleet.
    """
    if target_agent_id.startswith("operator-") and not target_agent_id.startswith("operator-agent-"):
        target_agent_id = target_agent_id.replace("operator-", "operator-agent-", 1)
        log(f"Auto-normalized target peer ID to '{target_agent_id}'")
        
    clean_target = target_agent_id.replace("http://", "").replace("https://", "").split("/")[0]
    if ".svc" not in clean_target:
        clean_target = f"{clean_target}.kubeagents-system.svc.cluster.local:8642"
        
    url = f"http://{clean_target}/v1/chat/completions"
    api_key = os.environ.get("API_SERVER_KEY") or os.environ.get("SWARM_API_KEY") or "none"
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    clean_session_id = "".join(c for c in str(session_id) if c.isalnum() or c in "-_.").strip() if session_id else ""
    if clean_session_id:
        headers["X-Hermes-Session-Id"] = clean_session_id
    payload = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": query}]
    }

    log(f"Sending secure synchronous peer call to '{target_agent_id}'")
    req = urllib.request.Request(
        url, 
        data=json.dumps(payload).encode("utf-8"), 
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=1800) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            return resp_data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return f"ERROR: Peer agent returned HTTP {e.code}: {err_body}"
    except Exception as e:
        return f"ERROR: Peer network communication failed: {e}"

if __name__ == "__main__":
    mcp.run()
