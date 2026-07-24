---
title: Glossary
description: Terminology used in kube-agents and the wider agentic-Kubernetes ecosystem.
sidebar:
  order: 4
---

Terms used throughout the `kube-agents` docs and the wider agentic-Kubernetes ecosystem. Ported from [`docs/glossary.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/glossary.md).

## `kube-agents` concepts

### Platform Agent

The single autonomous agent shipped in `agents/platform/`. Configured with the `SOUL.md` persona, a library of skills, governance SOPs, and cron watchdogs. Deployed as a Kubernetes Deployment running the [Hermes runtime](https://github.com/NousResearch/hermes-agent).

### `SOUL.md`

The persona and operating charter for the Platform Agent at `agents/platform/SOUL.md`. Defines the agent's role, guardrails, and the declarative GitOps workflow it must follow.

### Governance SOP

A standard operating procedure in `agents/platform/governance/`. Codifies how a fleet-wide audit or reconciliation is performed. Invoked by cron watchdogs or on request.

### Skill

A Claude-style `SKILL.md` bundle in `agents/platform/skills/`. Loaded on demand based on its frontmatter `description`.

### Watchdog

A cron-scheduled job in `agents/platform/cron/jobs.json` that fires a pre-authored prompt at the Platform Agent on a schedule.

### Declarative workflow

The GitOps PR path all infrastructure changes take. Enforced by `SOUL.md` and implemented via the `submit-suggestion` skill + Minty.

### `kubeagents-system`

The Kubernetes namespace that hosts the kube-agents control plane: the operator, the Platform Agent gateway Deployment, the LiteLLM gateway, Minty, and related integration workloads.

### Toolset

A named set of tools and MCP servers exposed to the agent, declared under `platform_toolsets` in `agents/platform/config.yaml`. Separate `cli` and `api_server` toolsets select which capabilities (e.g. `mcp-platform_control`, `mcp-gke`, `mcp-agent_common`) are available in each mode. `platform_toolsets` is a reserved framework key in Hermes.

## Runtime and framework

### Hermes

The agent runtime the Platform Agent runs on ([nousresearch/hermes-agent](https://github.com/NousResearch/hermes-agent)). Handles chat ingress, tool-calling loop, MCP server lifecycle, and plugin execution.

### MCP (Model Context Protocol)

Anthropic's protocol for exposing tools and data sources to LLMs. `kube-agents` uses MCP for the `gke` remote server and the in-pod `platform_control` server.

### LiteLLM

Multi-provider LLM proxy exposing an OpenAI-compatible Completions API. Default inference gateway in `kube-agents`.

### vLLM

Open-source inference server for local model serving. Alternative to LiteLLM when models need to run in-cluster on GPUs.

### Minty (GitHub Token Minter)

In-cluster broker that mints short-lived GitHub App installation tokens via GCP KMS. Deployed as the `github-token-minter` workload (upstream [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter)) and queried by `github_token_refresh.py`. Lets `submit-suggestion` open PRs without a long-lived credential.

### Credential proxy

An in-pod sidecar (Envoy plus `credential_proxy.py`) that mediates credentialed CLI execution. The agent runs `gcloud`, `kubectl`, `gh`, and `git` through the proxy against an executable allowlist, so it never holds the raw credentials directly. Started by `deploy/shared/envoy-credential-sidecar.sh`.

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
