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

# Delegate Generic Workload Skill

## Architecture & Lifecycle
1. **Asynchronous Delegation**: This skill asynchronously delegates generic instructions, operational tasks, or data queries to another specialized agent (e.g., GKE Operator, DevTeam, or Database Expert).
2. **Thought Streaming**: While the delegated worker agent reasons and executes tools, its intermediate progress updates will stream live back to the session via `emit_thought` webhooks.
3. **Event-Driven Continuation**: The sending agent (you) will conclude your turn silently upon dispatch. When the worker agent finishes, you will receive a subsequent `report_task_done` webhook event containing the worker's complete final output.
4. **Resumption & Reasoning**: You MUST treat that incoming `report_task_done` webhook event precisely as if it were a synchronous response from the delegated agent, and evaluate its outputs to answer the user's original request or execute further reasoning.

## Procedure
To delegate a task or query to a specialized worker agent (e.g., `operator-agent-staging-us-central1` or `devteam-agent-production-payments`), invoke your native FastMCP tool **`delegate_workload`**:
```json
{
  "target_agent_id": "<target_agent_id>",
  "query": "<query>"
}
```

## Output Protocol
When you execute this skill script, the terminal tool will display the confirmation message preview to the user automatically. You MUST NOT generate any conversational text or repeat the confirmation string in your response turn. Conclude your turn silently.
