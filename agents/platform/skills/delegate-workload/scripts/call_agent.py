#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import urllib.error
import hmac
import hashlib

def emit_thought_to_webhook(worker_id: str, space_id: str, thread_id: str, thought_text: str):
    """
    Emit intermediate thoughts live to platform agent webhook, which forwards to Google Chat.
    """
    if not space_id or space_id in ("default_space", "string", "none", "null", "") or not space_id.startswith("spaces/"):
        return
        
    url = "http://localhost:8644/webhooks/swarm-thought-stream"
    payload = {
        "worker_id": worker_id,
        "user_space": space_id,
        "user_thread": thread_id,
        "thought": thought_text
    }
    try:
        body_bytes = json.dumps(payload).encode("utf-8")
        sig = hmac.new(b"k8s-swarm-secret-999", body_bytes, hashlib.sha256).hexdigest()
        
        req = urllib.request.Request(
            url, 
            data=body_bytes, 
            headers={"Content-Type": "application/json", "X-Webhook-Signature": sig}, 
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            resp.read()
    except Exception as e:
        # Log to stderr but do not crash the script
        print(f"DEBUG: Webhook emission failed: {e}", file=sys.stderr, flush=True)


def main():
    if len(sys.argv) < 3:
        print("Error: Missing target_agent_id or query", file=sys.stderr)
        sys.exit(1)
        
    target_agent_id = sys.argv[1]
    query = sys.argv[2]
    active_space = os.getenv("HERMES_SESSION_CHAT_ID", "").strip()
    active_thread = os.getenv("HERMES_SESSION_THREAD_ID", "").strip()

    if target_agent_id.startswith("operator-") and not target_agent_id.startswith("operator-agent-"):
        target_agent_id = target_agent_id.replace("operator-", "operator-agent-", 1)

    # api_key = os.environ.get("SWARM_API_KEY") or os.environ.get("API_SERVER_KEY")
    # if not api_key:
    #     print("Error: SWARM_API_KEY or API_SERVER_KEY environment variable is required", file=sys.stderr)
    #     sys.exit(1)
    api_key = "your-strong-api-server-key-here"

    clean_id = target_agent_id.replace("http://", "").replace("https://", "").split("/")[0]
    if ".svc" not in clean_id:
        clean_id = f"{clean_id}.agent-system.svc.cluster.local:8642"
    endpoint = clean_id

    wrapped_query = f"""[SWARM DELEGATION DISPATCH]
You have been delegated the following task by the Platform Coordinator:

{query}

CRITICAL EXECUTION MANDATES:
1. AUTONOMOUS EXECUTION: You are an autonomous expert. You are free to reason, write scripts, or execute whatever tools you deem necessary to fulfill precisely the delegated task.
2. FINAL OUTPUT DELIVERY: Once your task is fully complete and you have the final definitive result or retrieved data, present it clearly in your final response.
"""

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    session_id = os.getenv("HERMES_SESSION_ID", "").strip()
    if session_id:
        headers["X-Hermes-Session-Id"] = session_id

    payload = {
        "input": wrapped_query,
        "prompt": wrapped_query
    }

    run_url = f"http://{endpoint}/v1/runs"
    
    try:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(run_url, data=data_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as response:
            run_data = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        print(f"ERROR: Failed to create run on {run_url}: {e}", file=sys.stderr)
        sys.exit(1)

    run_id = run_data.get("run_id") or run_data.get("id")
    if not run_id:
        print(f"ERROR: Server did not return a run_id: {run_data}", file=sys.stderr)
        sys.exit(1)

    events_url = f"http://{endpoint}/v1/runs/{run_id}/events"
    
    req_events = urllib.request.Request(events_url, headers={k: v for k, v in headers.items() if k != "Content-Type"})
    
    try:
        stream_response = urllib.request.urlopen(req_events, timeout=1800)
    except Exception as e:
        print(f"ERROR: Failed to connect to event stream {events_url}: {e}", file=sys.stderr)
        sys.exit(1)

    current_event = None
    final_output = ""

    try:
        while True:
            line = stream_response.readline()
            if not line:
                break
            line_str = line.decode('utf-8').strip()
            
            if line_str.startswith("event:"):
                current_event = line_str[6:].strip()
            elif line_str.startswith("data:"):
                data_str = line_str[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except Exception:
                    continue

                event_type = current_event or data.get("event") or data.get("object")

                if event_type == "message.delta":
                    delta = ""
                    if "delta" in data and isinstance(data["delta"], dict):
                        delta = data["delta"].get("content", "")
                    else:
                        delta = data.get("content", "") or data.get("delta", "")
                    if delta:
                        sys.stdout.write(delta)
                        sys.stdout.flush()
                        final_output += delta

                elif event_type == "reasoning.available":
                    text = data.get("text", "")
                    if text:
                        # Deduplicate: only print if it's not a duplicate of the final output
                        norm_text = text.strip()
                        norm_final = final_output.strip()
                        if norm_text and norm_text not in norm_final and norm_final not in norm_text:
                            if not active_space:
                                sys.stdout.write(f"\n\033[90m💭 [thinking]: {text}\033[0m\n")
                                sys.stdout.flush()
                            emit_thought_to_webhook(target_agent_id, active_space, active_thread, f"💭 {text}")

                elif event_type == "tool.started":
                    tool_name = data.get("tool") or data.get("name")
                    preview = data.get("preview") or data.get("arguments") or ""
                    if not active_space:
                        print(f"\n⚙️ [worker] Started tool: {tool_name} ({preview})\n", flush=True)
                    emit_thought_to_webhook(target_agent_id, active_space, active_thread, f"⚙️ Started tool: {tool_name} ({preview})")

                elif event_type == "tool.completed":
                    tool_name = data.get("tool") or data.get("name")
                    duration = data.get("duration", 0)
                    is_err = data.get("error", False)
                    status_str = "failed" if is_err else "completed"
                    if not active_space:
                        print(f"\n✅ [worker] Tool '{tool_name}' {status_str} (duration: {duration}s)\n", flush=True)
                    icon = "❌" if is_err else "✅"
                    emit_thought_to_webhook(target_agent_id, active_space, active_thread, f"{icon} Tool '{tool_name}' {status_str} ({duration}s)")

                elif event_type == "approval.request":
                    tool_name = data.get("name") or data.get("tool")
                    command = data.get("command") or data.get("arguments") or ""
                    if not active_space:
                        print(f"\n⚠️ [worker] Requires approval to run tool '{tool_name}' with command: {command}\n", flush=True)
                    emit_thought_to_webhook(target_agent_id, active_space, active_thread, f"⚠️ Requires approval to run tool '{tool_name}' with command: {command}")

                elif event_type == "run.completed" or event_type == "message.completed":
                    break

                elif event_type == "run.failed" or event_type == "error":
                    error_msg = data.get("error", "unknown error")
                    print(f"\n❌ [worker] Run failed: {error_msg}\n", file=sys.stderr, flush=True)
                    sys.exit(1)

    except Exception as e:
        print(f"ERROR: Exception during streaming: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        stream_response.close()

if __name__ == "__main__":
    main()

