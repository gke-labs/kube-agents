#!/usr/bin/env python3
import os
import sys
import json
import urllib.request

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

    api_key = os.environ.get("SWARM_API_KEY") or os.environ.get("API_SERVER_KEY")
    if not api_key:
        print("Error: SWARM_API_KEY or API_SERVER_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    clean_id = target_agent_id.replace("http://", "").replace("https://", "").split("/")[0]
    if ".svc" not in clean_id:
        clean_id = f"{clean_id}.agent-system.svc.cluster.local:8642"
    endpoint = clean_id

    wrapped_query = f"""[SWARM DELEGATION DISPATCH]
You have been delegated the following task by the Platform Coordinator:

{query}

CRITICAL EXECUTION MANDATES:
1. AUTONOMOUS EXECUTION: You are an autonomous expert. You are free to reason, write scripts, or execute whatever tools you deem necessary to fulfill precisely the delegated task or query.
2. STREAMING REASONING & TOOL AUDIT: As you investigate or work through this task, you MUST stream your intermediate reasoning thoughts and the specific tools/commands used back to the Coordinator by invoking 'emit_thought'. Do NOT censor your findings.
When calling 'emit_thought', you MUST use precisely these tracking parameters:
- worker_id: "{target_agent_id}"
- space_id: "{active_space or ''}"
- thread_id: "{active_thread or ''}"
- thought: "<your concise reasoning thought or tool/command audit update>"

3. FINAL OUTPUT DELIVERY: Once your task is fully complete and you have the final definitive result or retrieved data, simply present it in your final text response.
"""

    url = f"http://{endpoint}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    payload = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": wrapped_query}]
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

    try:
        # Long timeout of 30 minutes to allow the worker agent to execute long-running reasoning/tool runs
        with urllib.request.urlopen(req, timeout=1800) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            content = resp_data["choices"][0]["message"]["content"]
            print(content)
            sys.exit(0)
    except Exception as e:
        print(f"ERROR: Failed to communicate with worker agent: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
