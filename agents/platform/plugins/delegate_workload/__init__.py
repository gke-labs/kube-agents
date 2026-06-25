import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any, Dict

logger = logging.getLogger(__name__)

def resolve_endpoint(agent_id: str) -> str:
    """Resolve target agent endpoint from state registry."""
    hermes_home = os.environ.get("HERMES_HOME") or "/opt/data"
    if agent_id.startswith("operator-"):
        state_file = os.path.join(hermes_home, "operator_agents.jsonl")
    else:
        state_file = os.path.join(hermes_home, "devteam_agents.jsonl")

    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("agent_id") == agent_id:
                        return entry.get("endpoint")
        except Exception as e:
            logger.warning(f"Failed to read state file '{state_file}': {e}")
    return None

def delegate_workload_handler(args: Dict[str, Any], session_id: str = "", **kwargs) -> str:
    logger.info("delegate_workload_handler called: session_id=%r, kwargs=%r", session_id, kwargs)
    
    try:
        from gateway.session_context import get_session_env
        env_session_id = get_session_env("HERMES_SESSION_ID")
    except ImportError:
        env_session_id = ""
        
    session_id = session_id or env_session_id or os.environ.get("HERMES_SESSION_ID", "") or kwargs.get("task_id") or ""
    target_agent_id = args.get("target_agent")
    query = args.get("query")
    
    if not target_agent_id or not query:
        return json.dumps({"error": "Missing 'target_agent' or 'query' in arguments"})

    # Resolve endpoint from state registry
    resolved = resolve_endpoint(target_agent_id)
    if resolved:
        endpoint = resolved
        logger.info(f"Resolved target agent '{target_agent_id}' to '{endpoint}' from state registry.")
    else:
        # Fallback to direct resolution (normalizing only for fallback)
        fallback_id = target_agent_id
        if fallback_id.startswith("operator-") and not fallback_id.startswith("operator-agent-"):
            fallback_id = fallback_id.replace("operator-", "operator-agent-", 1)

        clean_id = fallback_id.replace("http://", "").replace("https://", "").split("/")[0]
        if ".svc" not in clean_id:
            namespace = "kubeagents-system"
            try:
                with open("/var/run/secrets/kubernetes.io/serviceaccount/namespace", "r") as f:
                    namespace = f.read().strip()
            except Exception:
                pass
            clean_id = f"{clean_id}.{namespace}.svc.cluster.local:8642"
        endpoint = clean_id
        logger.warning(f"Could not resolve target agent '{target_agent_id}' from state registry. Using fallback: '{endpoint}'")

    # Auth key
    api_key = os.environ.get("SWARM_API_KEY") or os.environ.get("API_SERVER_KEY")
    if not api_key:
        raise ValueError("Neither SWARM_API_KEY nor API_SERVER_KEY is set in the environment.")

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
    
    if session_id:
        headers["X-Hermes-Session-Id"] = session_id

    payload = {
        "input": wrapped_query,
        "prompt": wrapped_query
    }
    if session_id:
        payload["session_id"] = session_id

    run_url = f"http://{endpoint}/v1/runs"
    
    logger.info(f"Creating delegation run on {run_url} with session_id={session_id}")
    
    try:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(run_url, data=data_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=60) as response:
            run_data = json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to create run on {run_url}: {e}")
        return json.dumps({"error": f"Failed to create run on remote agent: {e}"})

    run_id = run_data.get("run_id") or run_data.get("id")
    if not run_id:
        logger.error(f"Server did not return a run_id: {run_data}")
        return json.dumps({"error": f"Remote agent did not return a run_id. Response: {run_data}"})

    events_url = f"http://{endpoint}/v1/runs/{run_id}/events"
    logger.info(f"Connecting to event stream: {events_url}")
    
    req_events = urllib.request.Request(events_url, headers={k: v for k, v in headers.items() if k != "Content-Type"})
    
    try:
        stream_response = urllib.request.urlopen(req_events, timeout=1800)
    except Exception as e:
        logger.error(f"Failed to connect to event stream {events_url}: {e}")
        return json.dumps({"error": f"Failed to connect to event stream on remote agent: {e}"})

    current_event = None
    final_output = ""

    try:
        while True:
            line = stream_response.readline()
            if not line:
                break
            line_str = line.decode('utf-8').strip()
            
            if not line_str:
                current_event = None
                continue
                
            if line_str.startswith("event:"):
                current_event = line_str[6:].strip()
            elif line_str.startswith("data:"):
                data_str = line_str[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    if not isinstance(data, dict):
                        continue
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
                        final_output += delta

                elif event_type == "run.completed" or event_type == "message.completed":
                    break

                elif event_type == "run.failed" or event_type == "error":
                    error_msg = data.get("error", "unknown error")
                    logger.error(f"Remote run failed: {error_msg}")
                    return json.dumps({"error": f"Remote run failed: {error_msg}"})

    except Exception as e:
        logger.error(f"Exception during streaming: {e}")
        return json.dumps({"error": f"Exception during streaming from remote agent: {e}"})
    finally:
        stream_response.close()

    return final_output

def register(ctx: Any) -> None:
    ctx.register_tool(
        name="delegate_workload",
        toolset="custom",
        schema={
            "name": "delegate_workload",
            "description": "Delegate generic instructions, operational tasks, or data queries to specialized peer or worker agents (Operator or DevTeam agents).",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_agent": {
                        "type": "string",
                        "description": "The target agent ID/service name (e.g. 'devteam-mercury-09-us-central1-ollama-gemma4' or 'operator-mercury-09-us-central1')."
                    },
                    "query": {
                        "type": "string",
                        "description": "The task description or query to execute."
                    }
                },
                "required": ["target_agent", "query"]
            }
        },
        handler=delegate_workload_handler,
        description="Delegate task to remote agent.",
    )
