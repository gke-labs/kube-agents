---
title: Inference gateway
description: LiteLLM for hosted models, vLLM for local models. Plus optional replay caching for demos.
sidebar:
  order: 8
---

The Platform Agent talks to an LLM through a **Completions API** proxy so provider choice is a config toggle. There are shipping options for both hosted and local models, plus a replay layer.

## Choosing a provider

| You want                                            | Use                                                    | Why                                                                                                                                      |
| --------------------------------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Fastest path with a hosted frontier model           | **LiteLLM → Gemini** (default)                         | One API key, no GPU node pool, no cluster egress beyond the LiteLLM pod.                                                                 |
| Provider redundancy or A/B                          | **LiteLLM → Gemini + Anthropic + OpenAI**              | LiteLLM handles the router config; agent config is unchanged.                                                                            |
| Inference billed to your own GCP project            | **LiteLLM → Vertex AI / Model Garden**                 | Workload Identity instead of an API key; Gemini plus Model Garden publishers. See [below](#vertex-ai-and-model-garden).                  |
| Free local prototyping with a consumer subscription | **LiteLLM → ChatGPT subscription** (OAuth device flow) | See [`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription). |
| Data-locality or air-gapped inference               | **vLLM → Gemma / Llama / Qwen**                        | Runs on a GKE GPU node pool. Higher setup cost, no egress to a hosted provider.                                                          |
| Deterministic demos / cheap tests                   | **Any of the above + inference-replay proxy**          | Caches responses in a PVC; replays on cache hit.                                                                                         |

## LiteLLM (hosted models)

[LiteLLM](https://litellm.ai) is an OpenAI-Completions-compatible proxy in front of every major model provider. `provision_09_deploy_litellm.sh` deploys it with the API key you provide.

### What ships

- [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) — Gemini-only default. Uses `GEMINI_API_KEY`.
- [`examples/litellm-chatgpt-subscription/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-chatgpt-subscription) — proxies to a personal ChatGPT subscription via OAuth device flow. Useful for demos where you don't want a per-token cost.

To switch providers, edit the LiteLLM `config.yaml` (mounted from a `ConfigMap`) and set the corresponding API key secret. The Platform Agent config doesn't change — it always talks to a Service named `litellm`.

### Setting the default model

The agent always requests a single logical model, `model-default`. LiteLLM maps that alias to a real provider model in its [`config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/integrations/litellm/base/config.yaml):

```yaml
model_list:
  - model_name: model-default
    litellm_params:
      model: ${MODEL_PROVIDER}/${MODEL_DEFAULT_NAME}
```

Two things have to name that alias, not one. The profile config covers Chat, which resolves the model on every message; sessions created through the agent's HTTP API instead take a model resolved once at gateway startup, and that path reads `API_SERVER_MODEL_NAME`. The operator sets both from the same constant so they cannot drift — if they do, Chat keeps working while every API-created session (autonomous event triage, for one) dies asking LiteLLM for a model it does not serve.

The two substituted values come from provisioning (`MODEL_PROVIDER` and `MODEL_DEFAULT_NAME`, cached in `vars.sh`). Supported providers and their shipping defaults:

| `MODEL_PROVIDER`   | Default `MODEL_DEFAULT_NAME` | Notes                                      |
| ------------------ | ---------------------------- | ------------------------------------------ |
| `gemini` (default) | `gemini-3.5-flash`           | Uses `GEMINI_API_KEY`.                     |
| `anthropic`        | `claude-sonnet-4-5-20250929` | Uses `ANTHROPIC_API_KEY`.                  |
| `openai`           | `gpt-5.4`                    | Uses `OPENAI_API_KEY`.                     |
| `chatgpt`          | `gpt-5.4`                    | Personal ChatGPT subscription (OAuth).     |
| `vertex_ai`        | `gemini-3.5-flash`           | No API key — Workload Identity. See below. |

Any model string the chosen provider accepts is valid — there is no allow-list in the harness. For example, [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) pins `gemini-3.1-flash-lite`.

To change the default, set the variables and re-run the LiteLLM step (or `make deploy-litellm`):

```bash
export MODEL_PROVIDER=gemini
export MODEL_DEFAULT_NAME=gemini-3.5-flash
cd k8s-operator/scripts && ./provision_09_deploy_litellm.sh
```

This rewrites the LiteLLM `ConfigMap` and rolls the gateway; the agent picks up the new model on its next request without any change to its own config.

### Vertex AI and Model Garden

`MODEL_PROVIDER=vertex` routes `model-default` to Vertex AI in your own GCP project — the same first-party Gemini models, plus every Model Garden publisher model your project has access to (Anthropic Claude, Llama, Mistral, and the rest). Requests stay inside your project's billing and data boundary, and no model API key exists anywhere in the cluster.

Two things differ from the API-key providers:

- **Authentication is Workload Identity.** The gateway gets its own service-account pair rather than an API key — see [Security & IAM](/kube-agents/reference/security-and-iam/#the-vertex-ai-gateway-is-a-separate-identity). There is no entry in `platform-agent-secrets` for Vertex.
- **The endpoint is a project and a location.** `VERTEX_PROJECT_ID` and `VERTEX_LOCATION` (defaulting to the install's project and region) become `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` on the gateway pod. A publisher model is only callable from a location that serves it, so a model unavailable in your cluster's region needs `VERTEX_LOCATION` pointed at one that has it — often `global`.

`MODEL_DEFAULT_NAME` is the Vertex **publisher model ID**, which is not always the same string the provider's own API uses — Model Garden Claude models, for instance, carry an `@`-suffixed version (`claude-sonnet-4-5@20250929`). Check the model's Model Garden card for the exact ID; a wrong one surfaces as a 404 from the gateway rather than a provisioning error.

```bash
export MODEL_PROVIDER=vertex_ai
export MODEL_DEFAULT_NAME=gemini-3.5-flash
export VERTEX_PROJECT_ID=my-gcp-project   # optional; defaults to PROJECT_ID
export VERTEX_LOCATION=us-east4           # optional; defaults to REGION
cd k8s-operator/scripts && ./provision_04_gcp_iam.sh && ./provision_09_deploy_litellm.sh
```

## vLLM (local models)

[vLLM](https://vllm.ai) serves open models with continuous batching, chunked prefill, and prefix caching for high throughput on GPU node pools.

### What ships

- [`examples/vllm-gemma/`](https://github.com/gke-labs/kube-agents/tree/main/examples/vllm-gemma) — Gemma via GKE's official inference tutorial. Requires an accelerator node pool (see `gke-compute-classes` skill).

vLLM speaks OpenAI-compatible Completions, so LiteLLM can be layered on top (or in front) for routing and observability.

## Inference replay

[`examples/inference-replay/`](https://github.com/gke-labs/kube-agents/tree/main/examples/inference-replay) is a small proxy that sits between the Platform Agent and LiteLLM. Requests are keyed by a SHA-256 hash of the canonicalized request body (messages plus params); hits return the cached response, misses forward to LiteLLM and cache the reply.

### Modes

- `mode: off` (default) — passthrough. Every request forwards.
- `mode: on` — cache hits return; misses forward and cache.
- Toggle at runtime:

  ```bash
  kubectl patch configmap inference-replay-config -n <ns> --type merge \
    -p '{"data":{"mode":"on"}}'
  ```

The proxy uses a `PersistentVolumeClaim` for the cache so replays survive pod restarts.

### When to use it

- Demos where you want repeatable output for the same inputs.
- CI tests against the agent's tool loop where LLM cost or non-determinism would be a problem.
- Cost containment during development.

Deploy it as part of the provisioner by setting `INFERENCE_REPLAY_ENABLED=true`.

## What the agent doesn't care about

The Platform Agent's config (`agents/platform/config.yaml`) doesn't mention the LLM provider. Provider selection is entirely at the LiteLLM / vLLM layer — the agent always talks to the `litellm` Service, and provisioning decides what that Service resolves to. When the replay proxy is deployed, the `litellm` Service is repointed at the replay proxy and the original LiteLLM pods are re-exposed through a new `litellm-gateway` Service that the proxy forwards cache misses to. That means:

- Swapping Gemini for Anthropic is a LiteLLM `ConfigMap` change.
- Turning on replay is a `INFERENCE_REPLAY_ENABLED=true` reprovision.
- Neither touches the agent's persona, skills, or governance layer.

## Where to go next

- [Reference → Examples](/kube-agents/reference/examples/) — the inference example bundles walked through.
- [Deploy → Kustomize](/kube-agents/deploy/kustomize/) — what the LiteLLM Deployment looks like on disk.
- [Concepts → Observability](/kube-agents/concepts/observability/) — LLM telemetry export.
