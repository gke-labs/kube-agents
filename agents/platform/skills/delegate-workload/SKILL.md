---
name: delegate-workload
description: "Asynchronously delegate generic instructions, operational tasks, or data queries to specialized peer or worker agents."
version: 1.0.0
platforms: [linux, macos]
required_environment_variables:
  - name: HERMES_SESSION_CHAT_ID
    optional: true
  - name: HERMES_SESSION_THREAD_ID
    optional: true
  - name: SWARM_API_KEY
    optional: true
---

# Delegate Workload Skill

## Architecture & Lifecycle

1. **Runs API Delegation**: This skill delegates tasks or queries to a specialized peer agent (e.g. Operator or DevTeam agent) using the Hermes Runs API (`POST /v1/runs`).
2. **Real-time Event Streaming**: The helper script connects to the event stream (`GET /v1/runs/{run_id}/events`) and outputs intermediate thought blocks (`💭 [thinking]`) and tool execution logs (`⚙️ [worker] Started tool: ...` / `✅ [worker] Tool completed`) as they happen.
3. **Deduplication**: The script automatically filters out reasoning text chunks that are duplicates of the final response to prevent double-printing.
4. **Final Response Delivery**: Once the run completes, the final result is accumulated and printed.

## Procedure

To delegate a task, run the script from the platform agent container:

```bash
python3 /opt/data/skills/delegate-workload/scripts/call_agent.py "<target_agent_id>" "<query>"
```
