---
title: Examples
description: LLM-inference example stacks shipped in examples/.
sidebar:
  order: 3
---

Self-contained example stacks live in [`examples/`](https://github.com/gke-labs/kube-agents/tree/main/examples). Each is a Kubernetes manifest bundle you can deploy directly with `kubectl apply -k` — no framework, no rendering.

## `inference-replay`

[`examples/inference-replay/`](https://github.com/gke-labs/kube-agents/tree/main/examples/inference-replay)

A proxy that sits between the Platform Agent and LiteLLM. Requests are keyed by prompt hash; hits return cached responses, misses forward to LiteLLM and cache the reply. Backed by a `PersistentVolumeClaim`.

**When to use:** deterministic demos, cheap CI tests against the agent's tool loop, cost containment during development.

**Modes:** `off` (passthrough), `on` (cache hits, forward misses). Toggle via `ConfigMap` patch — see [Inference gateway → Inference replay](/kube-agents/concepts/inference-gateway/#inference-replay).

**Deploy via provisioner:** `INFERENCE_REPLAY_ENABLED=true ./provision.sh`.

## `litellm-gemini`

[`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini)

LiteLLM Deployment + Service + `ConfigMap` fronting Gemini. Reads `GEMINI_API_KEY` from a Secret. This is what `provision_09_deploy_litellm.sh` uses by default.

**When to use:** the default install path; anything except explicit local-inference or subscription-based demos.

## `litellm-chatgpt-subscription`

[`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription)

LiteLLM configured to proxy a personal ChatGPT subscription via OAuth device flow. No per-token cost — useful for demos where you don't want to burn API credit.

**When to use:** demos, education, hackathons. Not for production.

## `vllm-gemma`

[`examples/vllm-gemma/`](https://github.com/gke-labs/kube-agents/tree/main/examples/vllm-gemma)

vLLM serving Gemma on a GKE GPU node pool. Based on GKE's official inference tutorial. Includes the node pool spec, GPU driver installer, and vLLM Deployment.

**When to use:** data-locality, air-gapped, or open-model requirements. Provision a GPU node pool first (or use the `gke-compute-classes` skill to spec one).

## Layering

Both LiteLLM examples and `vllm-gemma` speak OpenAI-compatible Completions. You can layer LiteLLM in front of vLLM to get routing and observability across a mix of hosted and local models — that's the pattern for "one config for many providers".

## Not shipped as examples (but reference-worthy)

- `k8s-operator/examples/platformagent.yaml` — a working sample `PlatformAgent` custom resource.
- `k8s-operator/testing/staging_workloads/` — a multi-cluster GKE staging PoC with workloads and traffic simulators.

## Where to go next

- [Inference gateway](/kube-agents/concepts/inference-gateway/) — decision framework for picking a provider.
- [Deploy → Kustomize](/kube-agents/deploy/kustomize/) — what the Kustomize surface looks like.
