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

1. **Synchronous Delegation**: This skill delegates generic instructions, operational tasks, or data queries to another specialized agent (e.g., GKE Operator or DevTeam worker agent) and blocks synchronously until the worker returns its final completion response.
2. **Thought Streaming**: While the worker agent runs, its intermediate reasoning thoughts and tool audit updates stream live into the chat space (for user observation) via `emit_thought` calls.
3. **Reasoning over Output**: The terminal tool returns the worker's final completion response to your context. You must evaluate this output to answer the user's original request or execute further reasoning.

## Procedure

To delegate a task or query to a specialized worker agent (e.g., `operator-agent-staging-us-central1` or `devteam-agent-production-payments`), execute the helper script:

```bash
python3 /opt/data/skills/delegate-workload/scripts/call_agent.py "<target_agent_id>" "<query>"
```

## Output Protocol

When you execute this skill script, wait for it to complete. Reason over its output to formulate your final response to the user.
