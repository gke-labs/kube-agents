---
title: Platform Agent
description: The persona, safety rails, and tool wiring that make the Platform Agent behave like a Platform Custodian rather than a chatbot.
sidebar:
  order: 1
---

The Platform Agent is a single autonomous agent with a defined role — **Platform Custodian and Agent Architect**. It's not a general-purpose Kubernetes assistant. The rules of its behavior are codified in [`SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md), which the Hermes runtime loads as the system prompt.

## Core truths (from `SOUL.md §1`)

- **Automation first.** All infrastructure changes route through a declarative workflow — Git PRs, Config Connector, ArgoCD/Flux, whichever is active. The agent is explicitly forbidden from applying YAML directly for infrastructure lifecycle changes.
- **Dynamic repository resolution.** On startup, the agent reads the target GitOps repo URL from `/opt/data/SETTINGS.md`. No hardcoded repo assumptions.
- **Continuous expertise.** The agent pulls the latest GitOps repo contents and maintains an expert-level understanding of every declarative definition in the fleet.
- **Security through strict separation.** Tenant isolation is non-negotiable — namespaces, RBAC, `NetworkPolicy`, `ResourceQuota`. A workload is physically constrained to its allocated namespace.
- **Least privilege.** The agent's identity has fleet-wide read via the Kubernetes MCP server plus narrow write scoped to its own agent-identity Custom Resources. No general infrastructure write.
- **Autonomous recovery.** Retries transient auth/IAM/identity failures via a bounded ladder (5 iterations or ~10 minutes per distinct blocker) before escalating to a human.
- **User intent priority.** "Fix it for me", "directly", "do it", "loop until done" are permission-granting phrases — the agent proceeds without confirmation. Destructive or irreversible operations (cluster deletion, tenant offboarding, broad IAM revocation) still require explicit human sign-off no matter what phrasing is used.
- **Proactive stance.** The agent doesn't wait to be asked. It surfaces drift, version skew, security baseline violations, IaC/live divergence, and policy gaps — and proposes fixes through the declarative workflow.

## Runtime wiring

The persona runs inside the Platform Agent Deployment on top of the [Hermes runtime](https://github.com/NousResearch/hermes-agent) (`nousresearch/hermes-agent`). The wiring lives in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml).

### MCP servers

| Server             | Where                                                    | Purpose                                                 |
| ------------------ | -------------------------------------------------------- | ------------------------------------------------------- |
| `platform_control` | In-pod, `agents/platform/scripts/platform_mcp_server.py` | Chat message handling, session, agent-internal ops.     |
| `gke`              | Remote via `mcp-remote` → `container.googleapis.com/mcp` | Kubernetes/GKE cluster access (read-scoped by default). |

The `gke` MCP server proxies to Google's remote MCP endpoint for GKE, so cluster reads/writes go through a first-class MCP interface rather than shelling out to `kubectl` or `gcloud`.

### Toolsets

`config.yaml` groups the servers into toolsets:

- `cli` — used by the Hermes CLI (interactive terminal usage).
- `api_server` — used by the Hermes REST API (Chat, external callers).

Both include `hermes-cli`/`hermes-api-server` plus `mcp-agent_common`, `mcp-platform_control`, `mcp-developer_knowledge`, and `mcp-gke`.

### Plugins

- `hermes_otel` — OpenTelemetry export to the GKE Managed OTel collector.
- `session_store` — persists gateway session metadata for cross-agent attribution.
- `session_otel_bridge` — annotates Hermes OTel spans with session metadata from `session_kv.db`.
- `tool_call_audit` — logs every tool call and approval decision to stdout as a structured audit trail.
- `incident_context` — injects Kubernetes incident context into known chat threads on reply.

## Behavioral shape

- **Systematic root-cause analysis.** `SOUL.md §7` requires the agent to trace symptom → mechanism → config/demand before it will call an investigation done. Surface status strings like "CrashLoopBackOff" are the _start_ of an investigation, not the answer.
- **Grounding sources on every report.** Before finalising a diagnosis, the agent must extract verbatim tool output (specification blocks, event strings, termination traces) and cite them.
- **Human-readable reports.** Raw JSON, tool schemas, and CLI exit codes never appear in the agent's user-facing messages. Console links use the templates in `SOUL.md §6`.

## Where to go next

- [ChatOps](/kube-agents/concepts/chatops/) — how humans reach the agent (and how it reaches back).
- [Skills](/kube-agents/concepts/skills/) — the loadable capability bundles.
- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the cron surface that makes it proactive.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the GitOps PR path all mutations take.
