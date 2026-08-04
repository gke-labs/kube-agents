---
title: What is kube-agents
description: The concrete artifacts that make up kube-agents — what installs where, and what runs when the operator is done reconciling.
---

`kube-agents` is a small collection of first-party components you install into a Kubernetes cluster (GKE today) plus a persona-and-skills workspace that tells the resulting agent how to behave.

## The components

### 1. Kubernetes operator (`k8s-operator/`)

A Go controller built with [Kubebuilder](https://kubebuilder.io) that defines the `PlatformAgent` custom resource and reconciles it into a running Platform Agent Deployment, Service, ServiceAccount, RBAC bindings, and a `ConfigMap` for the persona and skills. Source: [`k8s-operator/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator).

### 2. The agent Deployment (Chat Agent + Platform Agent + Cluster Agents)

The `PlatformAgent` CR reconciles into a Deployment running the [Hermes runtime](https://github.com/NousResearch/hermes-agent). The default image is `ghcr.io/gke-labs/kube-agents/platform-agent`, built on top of `nousresearch/hermes-agent`. One gateway process hosts the co-located Hermes profiles:

**The Chat Agent** (`agents/chat/`, the `default` profile) — the conversational front door and the only profile that receives chat ingress. It carries a `router` MCP server ([`agents/chat/scripts/router_server.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/scripts/router_server.py)) for discovering specialists (`list_agents`) and delegates work to them as kanban cards. It holds no infrastructure tools of its own.

**The Platform Agent** (`agents/platform/`, the `platform` profile) — the privileged specialist, scaffolded at pod startup from the workspace template by `profile_scaffold.py`. Inside it:

- **Persona (`SOUL.md`)** — the system prompt. Describes the Platform Agent's role, safety rails, kanban worker protocol, autonomous recovery ladder, and reporting style.
- **Routing description (`CAPABILITIES.md`)** — the one-liner the Chat Agent's `list_agents` uses to decide what to route here.
- **Skills** (`agents/platform/skills/*/SKILL.md`) — Claude-style skill bundles the agent loads on demand.
- **Governance SOPs** (`agents/platform/governance/*.md`) — standard operating procedures the cron watchdogs invoke.
- **Cron watchdogs** (`agents/platform/cron/jobs.json`) — scheduled autonomous jobs, each pointing at a governance SOP.
- **MCP servers** — declared in `agents/platform/config.yaml`. Shipping today: `platform_control` (an in-pod Python MCP server for session and agent-internal tooling) and `gke` (the [remote GKE MCP server](https://container.googleapis.com/mcp) via `mcp-remote`).
- **Toolsets** — `cli` and `api_server` variants aggregate the MCP servers into what the Hermes CLI and REST API surface, plus a `kanban` toolset for creating and routing delegation cards.

**The Cluster Agents** (`agents/cluster/`, per-cluster `cluster-*` profiles) — read-only single-cluster SREs, scaffolded at runtime from the baked template by `cluster_agent_profile.py`, one per managed GKE cluster. Each is pinned to its cluster (scoped `KUBECONFIG`, a `cluster_identity` block in its config), carries only the read-only `gke` and `developer_knowledge` MCP servers plus six runtime-debugging skills (`agents/cluster/skills/`), and returns diagnoses over the kanban board — it never mutates cluster state or opens PRs. See [Cluster Agents](/kube-agents/concepts/cluster-agents/).

### 3. Inference gateway

The Platform Agent talks to an LLM through a **Completions API** proxy so provider choice is a config toggle:

- **[LiteLLM](https://litellm.ai)** for hosted models — Gemini (default), Anthropic, OpenAI, or a personal ChatGPT subscription. Example manifests: [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini), [`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription).
- **[vLLM](https://vllm.ai)** for local, open models on GPU node pools — [`examples/vllm-gemma/`](https://github.com/gke-labs/kube-agents/tree/main/examples/vllm-gemma) serves Gemma via GKE's official inference tutorial.
- An optional **inference-replay proxy** in front of either can cache responses from a `PersistentVolumeClaim` so demos and tests replay deterministically — [`examples/inference-replay/`](https://github.com/gke-labs/kube-agents/tree/main/examples/inference-replay).

### 4. GitHub Token Minter (Minty)

Short-lived GitHub App installation tokens signed via GCP KMS and delivered through Workload Identity. This lets the `submit-suggestion` skill (and the `github-issue-resolver` watchdog) open pull requests against your GitOps repo without a long-lived PAT. Source: [`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github).

## What actually runs after `./provision.sh`

Once the [provisioning script](/kube-agents/install/quickstart-gke/) finishes, you have:

- A GKE cluster with Workload Identity.
- The operator controller manager Deployment.
- One `PlatformAgent` custom resource and the reconciled Platform Agent Deployment, running Hermes.
- A LiteLLM Deployment (or vLLM if you opted in).
- A Minty Deployment plus a GCP KMS keyring and key.
- A Google Chat Pub/Sub topic + subscription and a Kubernetes `Secret` holding your model provider API key.
- Optionally: Slack tokens as a `Secret` (only if `SLACK_ENABLED=true` during provisioning) and the inference-replay proxy (only if `INFERENCE_REPLAY_ENABLED=true`).

## What is _not_ included

- **No Helm chart** — install is via `make gcp-provision` + Kustomize. A GKE-oriented chart and a Terraform module have been proposed but are not in `main`.
- **No local Kind path** — there is no `kind` workflow in the repo and no scripted installer outside `k8s-operator/scripts/`. You need a real GKE cluster.
- **No web UI or CLI beyond `kubectl` port-forward + the Hermes API** — chat is the primary user interface.
- **No cross-cloud abstractions** — the shipping MCP toolset, IAM assumptions, and provisioning scripts all target GKE. The runtime and persona are cluster-agnostic; the skill catalog is not.

## Where to go next

- [Proactive autonomy](/kube-agents/overview/proactive-autonomy/) — the background watchdogs and how they close loops.
- [Architecture](/kube-agents/overview/architecture/) — how requests and cron ticks flow through the components.
- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — run `make gcp-provision` end-to-end.
