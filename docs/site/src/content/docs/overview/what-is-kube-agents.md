---
title: What is kube-agents
description: The concrete artifacts that make up kube-agents — what installs where, and what runs when the operator is done reconciling.
---

`kube-agents` is a small collection of first-party components you install into a Kubernetes cluster (GKE today) plus a persona-and-skills workspace that tells the resulting agent how to behave.

## The components

### 1. Kubernetes operator (`k8s-operator/`)

A Go controller built with [Kubebuilder](https://kubebuilder.io) that defines the `PlatformAgent` custom resource and reconciles it into a running Platform Agent Deployment, Service, ServiceAccount, RBAC bindings, and a `ConfigMap` for the persona and skills, and, under `spec.mode: next`, the A2A stack below. Source: [`k8s-operator/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator).

### 2. The agent Deployment (Planning Agent + Platform Agent + Cluster Agents)

The `PlatformAgent` CR reconciles into a Deployment running the [Hermes runtime](https://github.com/NousResearch/hermes-agent). The default image is `ghcr.io/gke-labs/kube-agents/platform-agent`, built on top of `nousresearch/hermes-agent`. One gateway process hosts the co-located Hermes profiles:

**The Planning Agent** (`agents/chat/`, the `default` profile) — the conversational front door and the only profile that receives chat ingress. Its `agent_roster` plugin injects the current specialists into every turn, so picking one to delegate to costs no tool call; the `router` MCP server ([`agents/chat/scripts/router_server.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/scripts/router_server.py)) exposes the same list as `list_agents` for an on-demand refresh. Both render from [`agents/chat/scripts/agent_roster.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/scripts/agent_roster.py). It delegates work to the specialists as kanban cards and holds no infrastructure tools of its own.

**The Platform Agent** (`agents/platform/`, the `platform` profile) — the privileged specialist, scaffolded at pod startup from the workspace template by `profile_scaffold.py`. Inside it:

- **Persona (`SOUL.md`)** — the system prompt. Describes the Platform Agent's role, safety rails, kanban worker protocol, autonomous recovery ladder, and reporting style.
- **Routing description (`CAPABILITIES.md`)** — the one-liner that describes this profile on the Planning Agent's roster, so it knows what to route here.
- **Skills** (`agents/platform/skills/*/SKILL.md`) — Claude-style skill bundles the agent loads on demand.
- **Governance SOPs** (`agents/platform/governance/*.md`) — standard operating procedures the cron watchdogs invoke.
- **Cron watchdogs** (`agents/platform/cron/jobs.json`) — scheduled autonomous jobs, each pointing at a governance SOP. Ticking is a property of the Planning Agent's gateway, the only running one, so a job on its roster advances this profile's schedule once a minute.
- **MCP servers** — declared in `agents/platform/config.yaml`. Shipping today: `platform_control` (an in-pod Python MCP server for session and agent-internal tooling) and `gke` (the [remote GKE MCP server](https://container.googleapis.com/mcp) via `mcp-remote`).
- **Toolsets** — `cli` and `api_server` variants aggregate the MCP servers into what the Hermes CLI and REST API surface, plus a `kanban` toolset for creating and routing delegation cards.

**The Cluster Agents** (`agents/cluster/`, per-cluster `cluster-*` profiles) — read-only single-cluster SREs, scaffolded at runtime from the baked template by `cluster_agent_profile.py`, one per managed GKE cluster. Each is pinned to its cluster (scoped `KUBECONFIG`, a `cluster_identity` block in its config), carries only the read-only `gke` and `developer_knowledge` MCP servers plus the runtime-debugging skills in `agents/cluster/skills/`, and returns diagnoses over the kanban board — it never mutates cluster state or opens PRs. See [Cluster Agents](/kube-agents/concepts/cluster-agents/).

### 3. Inference gateway

The Platform Agent talks to an LLM through a **Completions API** proxy so provider choice is a config toggle:

- **[LiteLLM](https://litellm.ai)** for hosted models — Gemini (default), Vertex AI / Model Garden, Anthropic, OpenAI, or a personal ChatGPT subscription — or for an OpenAI-compatible server you run ([`openai_compatible`](/kube-agents/concepts/inference-gateway/#openai-compatible-endpoints-self-hosted-or-private)), [SAP AI Core](/kube-agents/concepts/inference-gateway/#sap-ai-core) or any other LiteLLM provider. Example manifests: [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini), [`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription).
- **[vLLM](https://vllm.ai)** for local, open models on GPU node pools — [`examples/vllm-gemma/`](https://github.com/gke-labs/kube-agents/tree/main/examples/vllm-gemma) serves Gemma 4 26B-A4B through vLLM's OpenAI-compatible server on two L4 GPUs, with a 128k-token context.
- An optional **inference-replay proxy** in front of either can cache responses from a `PersistentVolumeClaim` so demos and tests replay deterministically — [`examples/inference-replay/`](https://github.com/gke-labs/kube-agents/tree/main/examples/inference-replay).

### 4. GitHub Token Minter (Minty)

Short-lived GitHub App installation tokens signed via GCP KMS and delivered through Workload Identity. This lets the `submit-suggestion` skill (and the `github-issue-resolver` watchdog) open pull requests against your GitOps repo without a long-lived PAT. Source: [`k8s-operator/config/integrations/github/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/config/integrations/github).

### 5. The A2A stack (`spec.mode: next`)

A first `install.sh` run records `spec.mode: next` on the `PlatformAgent`, unless it mirrors its third-party images or is passed `--mode=today` ([the quick start](/kube-agents/install/quickstart-gke/#verify-a-specmode-next-install)), and the operator then renders a message bus and its clients in front of the agent. A chart or Terraform install that sets no mode runs `today`, the previous layout, which is kept as the rollback: [Switching back to `today`](/kube-agents/install/upgrade/#switching-back-to-today) is the way back, and `today` renders none of the pods below.

- **The bus** (`<name>-a2a-nats`), a NATS JetStream StatefulSet, with an **auth callout** (`<name>-a2a-callout`, two replicas) that authenticates every bus client and a **capability verifier** (`<name>-a2a-verifier`, two replicas) that checks each submitted task. A provisioning Job creates the streams once.
- **The A2A gateway** (`<name>-a2a-gateway`), which holds the chat backend: Google Chat when it is enabled, Slack otherwise. It publishes each message as a task on the bus and posts the answer back into the thread. With no chat platform enabled it runs on its console door alone ([Conversations under `spec.mode: next`](/kube-agents/concepts/chatops/#conversations-under-specmode-next)).
- **The Hermes bridge**, a `hermes-bridge` container the operator adds to the agent pod. It takes tasks off the bus and runs each as a Planning Agent turn on the agent's API server.
- **The A2A console** (`<name>-a2a-console`), a browser page over the bus with a chat pane, reached through `kubectl port-forward` ([User interfaces](/kube-agents/reference/user-interfaces/#a2a-console)).
- **Session pods**, one per conversation moved onto the `/session` route, started by the gateway and deleted when idle. The [`AgentProfile`](/kube-agents/operator/agentprofile-crd/) resource describes further kinds of agent pod on the bus.
- Opt-in, behind an operator setting: the [MCP server](/kube-agents/reference/a2a-mcp/) (`<name>-a2a-mcp`), a door for an external coding agent.

## What actually runs after installation

Once the [installer](/kube-agents/install/quickstart-gke/) finishes, you have:

- A GKE cluster with Workload Identity.
- The operator controller manager Deployment.
- One `PlatformAgent` custom resource and the three agent pods it reconciles into: the gateway running Hermes, the shell sandbox and the credential broker ([Credential isolation](/kube-agents/reference/credential-isolation/#pod-anatomy)).
- The A2A stack, on an install running `spec.mode: next`, the first-install default: the bus, two auth callout and two verifier replicas, the A2A gateway and the A2A console, with the `hermes-bridge` container added to the gateway pod. [Verify a `spec.mode: next` install](/kube-agents/install/quickstart-gke/#verify-a-specmode-next-install) lists the pods.
- A LiteLLM Deployment (or vLLM if you opted in).
- A Minty Deployment plus a GCP KMS keyring and key.
- A Google Chat Pub/Sub topic + subscription and a Kubernetes `Secret` holding your model provider API key.
- Optionally: Slack tokens in the credentials `Secret` (only if Slack was enabled at install).

## What is _not_ included

- **No local Kind install** — the installer and the Terraform composition it drives both target GKE, and a deployment needs a real GKE cluster. A kind cluster exists only as a development path for contributors; see [Helm and Kind](/kube-agents/install/helm-and-kind/). (For versioned Helm/Terraform installs on GKE, see [Helm and Kind](/kube-agents/install/helm-and-kind/).)
- **One browser page by default, and no CLI outside the pod** — chat is the primary user interface. A `next` install serves the A2A console, reached through `kubectl port-forward`; the web console and the Hermes dashboard are opt-in, and the admin console runs on your own machine. [User interfaces](/kube-agents/reference/user-interfaces/) compares them. The Hermes CLI is reachable with `kubectl exec` into the agent pod; `kubectl port-forward` does not reach the agent pod, because the install defaults to a GKE Sandbox (gVisor) node pool whose loopback listeners it cannot reach. [ChatOps](/kube-agents/concepts/chatops/) is canonical.
- **No cross-cloud abstractions** — the shipping MCP toolset, IAM assumptions, and install path all target GKE. The runtime and persona are cluster-agnostic; the skill catalog is not.

## Where to go next

- [Proactive autonomy](/kube-agents/overview/proactive-autonomy/) — the background watchdogs and how they close loops.
- [Architecture](/kube-agents/overview/architecture/) — how requests and cron ticks flow through the components.
- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — run the installer end-to-end.
