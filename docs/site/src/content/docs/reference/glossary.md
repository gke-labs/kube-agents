---
title: Glossary
description: Terminology used in kube-agents and the wider agentic-Kubernetes ecosystem.
sidebar:
  order: 4
---

Terms used throughout the `kube-agents` docs and the wider agentic-Kubernetes ecosystem.

This page is the canonical glossary for humans. The agents carry their own trimmed grounding copy at [`agents/platform/docs/glossary.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/docs/glossary.md), which is baked into the agent image at `/opt/defaults/docs/glossary.md` and shared by the profiles.

## `kube-agents` concepts

### Chat Agent

The conversational front door shipped in `agents/chat/` — the `default` [Hermes profile](#hermes-profile), and the only profile that receives chat ingress (Google Chat / Slack). The available specialists are injected into every turn by its `agent_roster` plugin (the `router` MCP tool `list_agents` re-reads the same list on demand); it delegates each request as a [kanban card](#kanban-task-delegation). Progress and results reach the thread on their own: the specialist's heartbeats and the card's completion post there directly, verbatim and without waking the front door, which is woken only when a card blocks or fails. It holds no infrastructure tools of its own: the front door can route, not mutate.

### Platform Agent

The privileged specialist shipped in `agents/platform/` — the `platform` [Hermes profile](#hermes-profile), scaffolded at pod startup from the workspace template. Configured with the `SOUL.md` persona, a library of skills, governance SOPs, and cron watchdogs, it owns the GitOps write path and the lifecycle of the per-cluster [Cluster Agents](#cluster-agent). It no longer receives chat directly; the Chat Agent routes work to it over the kanban board. All profiles run in the same operator-deployed Deployment on the [Hermes runtime](https://github.com/NousResearch/hermes-agent).

### Cluster Agent

A read-only SRE scoped to exactly one GKE cluster — a `cluster-*` [Hermes profile](#hermes-profile) the Platform Agent scaffolds at runtime from the `agents/cluster/` template (`cluster_agent_profile.py`), one per managed cluster, persisting until that cluster is deleted. Each is pinned to its cluster via a scoped `KUBECONFIG`, exposes only the read-only `gke` and `developer_knowledge` MCP servers, and carries six single-cluster runtime-debugging skills. It diagnoses and proposes fixes over the [kanban board](#kanban-task-delegation); it never mutates cluster state or opens PRs — the Platform Agent owns acting on its proposals. The hourly `cluster-agent-reconcile` job keeps profiles aligned with the live fleet (creating missing ones, pruning profiles whose cluster is definitively gone). See [Cluster Agents](/kube-agents/concepts/cluster-agents/).

### `SOUL.md`

The persona and operating charter for an agent (e.g. `agents/platform/SOUL.md`, `agents/chat/SOUL.md`). Defines the agent's role, guardrails, and — for the Platform Agent — the kanban worker protocol and the declarative GitOps workflow it must follow.

### Governance SOP

A standard operating procedure in `agents/platform/governance/`. Codifies how a fleet-wide audit or reconciliation is performed. Invoked by cron watchdogs or on request.

### Skill

A Claude-style `SKILL.md` bundle in `agents/platform/skills/` (Platform Agent) or `agents/cluster/skills/` (Cluster Agents). Loaded on demand based on its frontmatter `description`.

### Watchdog

A cron-scheduled job in `agents/platform/cron/jobs.json` carrying a pre-authored prompt the Platform Agent runs on a schedule.

### Declarative workflow

The GitOps PR path all infrastructure changes take. Enforced by `SOUL.md` and implemented via the `submit-suggestion` skill + Minty.

### `kubeagents-system`

The Kubernetes namespace that hosts the kube-agents control plane: the operator, the Platform Agent gateway Deployment, the LiteLLM gateway, Minty, and related integration workloads.

### Toolset

A named set of tools and MCP servers exposed to an agent, declared under `platform_toolsets` in that agent's `config.yaml`. In `agents/platform/config.yaml`, separate `cli` and `api_server` toolsets select which capabilities (e.g. `mcp-platform_control`, `mcp-gke`, `mcp-developer_knowledge`) are available in each mode; `agents/chat/config.yaml` pins every mode to `mcp-router` + `kanban` plus the `memory` gate for its per-user memory tool. A separate top-level `toolsets: [kanban]` key gates the kanban orchestrator surface. `platform_toolsets` is a reserved framework key in Hermes.

### Kanban task (delegation)

The unit of coordination between the agent profiles: a card on the shared kanban board at the Hermes root (`kanban.db`). An orchestrator creates a card (`kanban_create(assignee=..., body=...)`); the gateway's kanban **dispatcher** auto-spawns the assigned specialist as a worker (`hermes -p <profile> chat -q "work kanban task <id>"`), which reads the card (`kanban_show`), does the work, and reports back (`kanban_complete` / `kanban_block`). The originating chat session is auto-subscribed, so the worker's `kanban_heartbeat(note=…)` progress notes and its completion both post into the thread; a worker propagates that subscription onto any child cards it creates (`kanban_notify_propagate.py`) so their completions stay visible too. The design of record is [`docs/designs/agent-communication.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/agent-communication.md).

## Runtime and framework

### Hermes

The agent runtime all the agents run on ([nousresearch/hermes-agent](https://github.com/NousResearch/hermes-agent)). Handles chat ingress, tool-calling loop, MCP server lifecycle, and plugin execution.

### Hermes profile

A native Hermes feature (`hermes -p <name>`) giving multiple isolated Hermes instances — each with its own config, sessions, skills, and home directory — inside a single gateway process. In `kube-agents`, the `default` profile is the [Chat Agent](#chat-agent), the `platform` profile is the [Platform Agent](#platform-agent), and each `cluster-*` profile is a [Cluster Agent](#cluster-agent). Executable scripts are shared across profiles; persona, config, and skills are per-profile.

### MCP (Model Context Protocol)

Anthropic's protocol for exposing tools and data sources to LLMs. `kube-agents` uses MCP for the `gke` remote server and the in-pod `platform_control` server.

### LiteLLM

Multi-provider LLM proxy exposing an OpenAI-compatible Completions API. Default inference gateway in `kube-agents`.

### vLLM

Open-source inference server for local model serving. Alternative to LiteLLM when models need to run in-cluster on GPUs.

### Minty (GitHub Token Minter)

In-cluster broker that mints short-lived GitHub App installation tokens via GCP KMS. Deployed as the `github-token-minter` workload (upstream [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter)) and queried by `github_token_refresh.py`. Lets `submit-suggestion` open PRs without a long-lived credential.

### Credential proxy

An in-pod sidecar (Envoy plus `credential_proxy.py`) that mediates credentialed CLI execution. The agent runs `gcloud`, `kubectl`, `gh`, and `git` through the proxy against an executable allowlist, so it never holds the raw credentials directly. Started by `deploy/shared/start-services.sh`, which launches it alongside the `k8s-event-watcher` as peer services in the same container.

### Inference Replay Proxy

An optional caching proxy that sits in front of the `litellm` gateway. It hashes each request (prompt + available skills + target model), serves cache hits from a Persistent Disk, and forwards misses upstream. Used for deterministic, low-cost replay of agent trajectories. Provisioned by `make gcp-provision-11-inference-replay`; example in `examples/inference-replay/`.

## Related Kubernetes-native agent projects

### Agent Substrate

Open-source Kubernetes-native platform for orchestrating AI agent workloads. Introduces Workers (managed compute pools) and Actors (agent instances) for multiplexed, stateful execution.

Source: [agent-substrate/substrate](https://github.com/agent-substrate/substrate).

### Agent Sandbox

Kubernetes SIG Apps project for isolated, stateful, singleton agent workloads. Provides warm pod pools, stable identity, and sandboxed execution (gVisor / Kata).

Source: [kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox).

### Agent Executor (AX)

Distributed agent runtime from Google with durable-execution features — pause, resume, snapshot, replay — to survive infrastructure failures.

Source: [google/ax](https://github.com/google/ax).
