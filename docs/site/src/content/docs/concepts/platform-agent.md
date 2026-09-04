---
title: Platform Agent
description: The persona, safety rails, and tool wiring that make the Platform Agent behave like a Platform Custodian rather than a chatbot.
sidebar:
  order: 1
---

The Platform Agent is an autonomous agent with a defined role — **Platform Custodian and Agent Architect**. It's not a general-purpose Kubernetes assistant. The rules of its behavior are codified in [`SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md), which the Hermes runtime loads as the system prompt.

It runs as the `platform` Hermes profile in the agent pod, scaffolded at pod startup from the `agents/platform/` template by [`profile_scaffold.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/profile_scaffold.py). It does not receive chat directly: the **Planning Agent** (the pod's `default` profile — see [ChatOps](/kube-agents/concepts/chatops/)) is the conversational front door and delegates work to the Platform Agent as cards on the shared kanban board. Per `SOUL.md §0`, the Platform Agent is invoked with `work kanban task <id>`, reads the request with `kanban_show`, does the work, and always finishes with `kanban_complete` or `kanban_block`. A completed card's `result` field carries the answer and is posted verbatim into the chat thread, so that — not the one-line `summary` — is what the user actually receives; `result` is a required argument, enforced by a patch the image applies to the `kanban_complete` tool (`deploy/docker/patches/kanban_result_required.py`). Because that answer is rendered as Slack blocks, its shape is specified too: `kanban_create` appends a report-format stanza to every card body that does not already give format instructions (`deploy/docker/patches/kanban_report_format.py`). That stanza is an instruction, not a gate — shape is never a reason to refuse a completion, because a report thrown away for its formatting answers nothing at all. The delivery path measures the result and logs what it finds: a `#` H1 duplicating the card title the chat message already shows, or ASCII section markers Slack renders as flat text, raise a `WARNING` naming the edit; cosmetic defects go to `INFO`. Two things are refused, each at most once per card before the card closes with whatever the worker sends: a blank `result`, and — since #1010 shipped "Awaiting synthesis" to a user as a finished answer — a completion over the card's own unfinished fan-out children, which is told to wait for them and synthesize instead (`deploy/docker/patches/kanban_children_settled.py`). On a long job it reports progress as it goes with `kanban_heartbeat(note=…)`; the notes accumulate into a single rolling `⏳` message that the notifier edits in place, so the thread stays current without waking the Planning Agent and without notifying the space once per note (`deploy/docker/patches/kanban_progress_lines.py`). Child cards are reserved for genuine delegation and parallel work — when it does create them, it propagates the chat subscription onto each one (`kanban_notify_propagate.py`) so their completions stay visible too. Its one-line routing description for the Planning Agent's roster lives in [`agents/platform/CAPABILITIES.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/CAPABILITIES.md).

## Core truths (from `SOUL.md §1`)

- **Automation first.** All infrastructure changes route through a declarative workflow — Git PRs, Config Connector, ArgoCD/Flux, whichever is active. The agent is explicitly forbidden from applying YAML directly for infrastructure lifecycle changes.
- **Dynamic multi-repository resolution.** On startup, the agent reads the configured GitOps repo URLs from the ConfigMap specified in `$GITOPS_STATE_CONFIGMAP`. No hardcoded single-repo assumptions.
- **Dynamic project resolution.** The agent reads its GCP project, location, and cluster from `$GKE_PROJECT_ID` / `$GKE_LOCATION` / `$GKE_CLUSTER_NAME`, which the operator injects from the `PlatformAgent` resource. No hardcoded project IDs — and never `-` in the project segment of a resource path, which GKE accepts only as the location wildcard.
- **Continuous expertise.** The agent pulls the latest GitOps repo contents and maintains an expert-level understanding of every declarative definition in the fleet.
- **Security through strict separation.** Tenant isolation is non-negotiable — namespaces, RBAC, `NetworkPolicy`, `ResourceQuota`. A workload is physically constrained to its allocated namespace.
- **Least privilege.** The agent's Kubernetes identity is read-only and cannot read Secrets. Its GCP identity is governed by an install-time permission set (`read-only` by default; widening it means choosing `custom` and naming every role) — see [Security &amp; IAM](/kube-agents/reference/security-and-iam/#what-the-agent-can-and-cannot-do) for exactly what is enforced on which plane.
- **Autonomous recovery.** Retries transient auth/IAM/identity failures via a bounded ladder (5 iterations or ~10 minutes per distinct blocker) before escalating to a human.
- **User intent priority.** "Fix it for me", "directly", "do it", "loop until done" are permission-granting phrases — the agent proceeds without confirmation. Destructive or irreversible operations (cluster deletion, tenant offboarding, broad IAM revocation) still require explicit human sign-off no matter what phrasing is used.
- **Proactive stance.** The agent doesn't wait to be asked. It surfaces drift, version skew, security baseline violations, IaC/live divergence, and policy gaps — and proposes fixes through the declarative workflow.

## Runtime wiring

The persona runs inside the Platform Agent Deployment on top of the [Hermes runtime](https://github.com/NousResearch/hermes-agent) (`nousresearch/hermes-agent`). The wiring lives in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml).

### MCP servers

| Server             | Where                                                    | Purpose                                                                          |
| ------------------ | -------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `platform_control` | In-pod, `agents/platform/scripts/platform_mcp_server.py` | Session and agent-internal ops (chat ingress now lives with the Planning Agent). |
| `gke`              | Remote via `mcp-remote` → `container.googleapis.com/mcp` | Kubernetes/GKE cluster access (read-scoped by default).                          |

The `gke` MCP server proxies to Google's remote MCP endpoint for GKE, so cluster reads/writes go through a first-class MCP interface rather than shelling out to `kubectl` or `gcloud`.

### Toolsets

`config.yaml` groups the servers into toolsets:

- `cli` — used by the Hermes CLI (interactive terminal usage).
- `api_server` — used by the Hermes REST API (Chat, external callers).

Both include `hermes-cli`/`hermes-api-server` plus `mcp-platform_control`, `mcp-developer_knowledge`, and `mcp-gke`.

### Toolsets (kanban)

A top-level `toolsets: [kanban]` key additionally exposes the kanban orchestrator surface (`kanban_create` and friends), so the Platform Agent can itself create and route cards when it delegates or stages work.

### Plugins

- `hermes_otel` — OpenTelemetry export to the GKE Managed OTel collector.
- `tool_call_audit` — logs every tool call and approval decision to stdout as a structured audit trail.
- `incident_context` — injects Kubernetes incident context into known chat threads on reply.

The chat-ingress plugins (`session_store`, `session_otel_bridge`) run on the Planning Agent profile, which owns chat ingress — see [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml).

## Behavioral shape

- **Delegation-first for single-cluster runtime work.** `SOUL.md §6` makes the Platform Agent the fleet orchestrator, not a per-workload operator: work scoped to one cluster's live runtime (crash loops, OOMs, scheduling failures) is delegated to that cluster's read-only [Cluster Agent](/kube-agents/concepts/cluster-agents/) over the kanban board. Fleet-wide audits, provisioning, and the GitOps write path stay with the Platform Agent.
- **Incident triage discipline.** `SOUL.md §7` codifies how the agent communicates during triage — findings first, concrete evidence, no raw dumps.
- **Human-readable reports.** Raw JSON, tool schemas, and CLI exit codes never appear in the agent's user-facing messages. Console links are built from the shared URL templates in [`agents/platform/docs/gcp-console-links.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/docs/gcp-console-links.md) (baked into the image at `/opt/defaults/docs/`), per `SOUL.md §5`.

## Where to go next

- [ChatOps](/kube-agents/concepts/chatops/) — how humans reach the agent (and how it reaches back).
- [Cluster Agents](/kube-agents/concepts/cluster-agents/) — the per-cluster read-only specialists it creates and delegates to.
- [Skills](/kube-agents/concepts/skills/) — the loadable capability bundles.
- [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) — the cron surface that makes it proactive.
- [Declarative workflow](/kube-agents/concepts/declarative-workflow/) — the GitOps PR path all mutations take.
