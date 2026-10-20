---
title: Examples
description: LLM-inference example stacks shipped in examples/.
sidebar:
  order: 3
---

Self-contained example stacks live in [`examples/`](https://github.com/gke-labs/kube-agents/tree/main/examples). Each is a plain Kubernetes manifest bundle you deploy with `kubectl apply -f` (see the `README.md` in each directory) — no framework, no rendering.

## `inference-replay`

[`examples/inference-replay/`](https://github.com/gke-labs/kube-agents/tree/main/examples/inference-replay)

A proxy that sits between the Platform Agent and LiteLLM. Requests are keyed by prompt hash; hits return cached responses, misses forward to LiteLLM and cache the reply. Backed by a `PersistentVolumeClaim`.

**When to use:** deterministic demos, cheap CI tests against the agent's tool loop, cost containment during development.

**Modes:** `off` (passthrough), `on` (cache hits, forward misses). Toggle via `ConfigMap` patch — see [Inference gateway → Inference replay](/kube-agents/concepts/inference-gateway/#inference-replay).

**Deploy (development only):** `make -C k8s-operator deploy-inference-replay` — the replay proxy is a dev tool and is never part of the installer.

## `litellm-gemini`

[`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini)

LiteLLM Deployment + Service + `ConfigMap` fronting Gemini, plus a `Secret`, `PodDisruptionBudget`, `NetworkPolicy`, and `PodMonitoring`. Reads `GEMINI_API_KEY` from the Secret. The default install path (the chart's `litellm.*` values) deploys an equivalent LiteLLM + Gemini config rather than this example directory, plus the pre-call hook described under [Replayed thinking on Gemini](/kube-agents/concepts/inference-gateway/#replayed-thinking-on-gemini), which this example does not mount; the dev copy lives at `k8s-operator/config/integrations/litellm/base`.

**When to use:** the default install path; anything except explicit local-inference or subscription-based demos.

## `litellm-chatgpt-subscription`

[`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription)

LiteLLM configured to proxy a personal ChatGPT subscription via OAuth device flow. No per-token cost — useful for demos where you don't want to burn API credit.

**When to use:** demos, education, hackathons. Not for production.

## `vllm-gemma`

[`examples/vllm-gemma/`](https://github.com/gke-labs/kube-agents/tree/main/examples/vllm-gemma)

vLLM's OpenAI-compatible server (`vllm serve`) serving Gemma 4 26B-A4B (`RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic`) on two L4 GPUs with a 128k-token context, for the `openai_compatible` model provider. Ships the vLLM Deployment, Service, `PodDisruptionBudget`, `NetworkPolicy`, and `PodMonitoring`. The README gives the GPU node-pool command and measured sizing for other Gemma 4 sizes and GPUs.

**When to use:** data-locality, air-gapped, or open-model requirements. Provision a GPU node pool first (or use the `gke-compute-classes` skill to spec one).

## Layering

Both LiteLLM examples speak OpenAI-compatible Completions. `vllm-gemma` serves the OpenAI API too; to put LiteLLM in front of it, set `MODEL_PROVIDER=openai_compatible` with the values in its README ([Inference gateway](/kube-agents/concepts/inference-gateway/#openai-compatible-endpoints-self-hosted-or-private)).

## Not shipped as examples (but reference-worthy)

- `k8s-operator/examples/platformagent.yaml` — a working sample `PlatformAgent` custom resource.

## Where to go next

- [Inference gateway](/kube-agents/concepts/inference-gateway/) — decision framework for picking a provider.
- [Deploy → Network policies and Service](/kube-agents/deploy/kustomize/) — what the operator renders around the agent Pod.
