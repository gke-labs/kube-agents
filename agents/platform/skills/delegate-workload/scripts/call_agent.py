#!/usr/bin/env python3
import os
import sys
import json
import subprocess
import urllib.request

def _bg_dispatch(target_agent_id, query, active_space, active_thread, api_key):
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

3. FINAL OUTPUT DELIVERY: Once your task is fully complete and you have the final definitive result or retrieved data, you MUST instantly invoke the 'report_task_done' tool. After calling 'report_task_done', you MUST immediately stop and NOT emit any further thoughts.
When calling 'report_task_done', you MUST use precisely these tracking parameters:
- worker_id: "{target_agent_id}"
- space_id: "{active_space or ''}"
- thread_id: "{active_thread or ''}"
- task_name: "Delegated Workload"
- outputs: "<PRECISELY your complete final definitive answer, summary report, or retrieved tabular data. Do NOT censor or omit data, as the calling agent requires this complete output for further reasoning>"
"""

    url = f"http://{endpoint}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "X-Hermes-Async-Dispatch": "true"
    }
    if active_space:
        headers["X-Hermes-Reply-Space"] = active_space
    if active_thread:
        headers["X-Hermes-Reply-Thread"] = active_thread

    payload = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": wrapped_query}]
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            pass
    except Exception:
        pass

def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--bg-dispatch":
        _bg_dispatch(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
        sys.exit(0)

    if len(sys.argv) < 3:
        print("Error: Missing target_agent_id or query", file=sys.stderr)
        sys.exit(1)
        
    target_agent_id = sys.argv[1]
    query = sys.argv[2]
    
    active_space = os.getenv("HERMES_SESSION_CHAT_ID", "").strip()
    active_thread = os.getenv("HERMES_SESSION_THREAD_ID", "").strip()

    if target_agent_id.startswith("operator-") and not target_agent_id.startswith("operator-agent-"):
        target_agent_id = target_agent_id.replace("operator-", "operator-agent-", 1)

    api_key = os.environ.get("SWARM_API_KEY") or os.environ.get("API_SERVER_KEY") or "your-strong-api-server-key-here"

    # Spawn detached background process group so outgoing socket stays alive independently of PID 1 turn termination!
    cmd = [sys.executable, __file__, "--bg-dispatch", target_agent_id, query, active_space, active_thread, api_key]
    subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"Delegated task to {target_agent_id}")

if __name__ == "__main__":
    main()
