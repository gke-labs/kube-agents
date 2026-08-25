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

[LiteLLM](https://litellm.ai) is an OpenAI-Completions-compatible proxy in front of every major model provider. The `kube-agents` Helm chart deploys it with the API key you provide (the dev copy is `make -C k8s-operator deploy-litellm`).

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

The two substituted values come from the install (`MODEL_PROVIDER` and `MODEL_DEFAULT_NAME`, saved in `vars.sh` and carried into the chart values). Supported providers and their shipping defaults:

| `MODEL_PROVIDER`   | Default `MODEL_DEFAULT_NAME` | Notes                                      |
| ------------------ | ---------------------------- | ------------------------------------------ |
| `gemini` (default) | `gemini-3.5-flash`           | Uses `GEMINI_API_KEY`.                     |
| `anthropic`        | `claude-opus-5`              | Uses `ANTHROPIC_API_KEY`.                  |
| `openai`           | `gpt-5.4`                    | Uses `OPENAI_API_KEY`.                     |
| `vertex_ai`        | `gemini-3.5-flash`           | No API key — Workload Identity. See below. |

Any model string the chosen provider accepts is valid — there is no allow-list in the harness. For example, [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) pins `gemini-3.1-flash-lite`.

To change the default on an installed system, re-run `./install.sh` (or its `--menu` panel's model-provider entry followed by **Save & Apply**) — one `terraform apply` rewrites the LiteLLM `ConfigMap` and rolls the gateway. On a dev cluster, set the variables and redeploy the dev copy:

```bash
export MODEL_PROVIDER=gemini
export MODEL_DEFAULT_NAME=gemini-3.5-flash
make -C k8s-operator deploy-litellm
```

Either way the agent picks up the new model on its next request without any change to its own config.

### Prompt caching

Agent turns are mostly re-sent context: the same system prompt, skills, and conversation tail go up again on every tool call. Anthropic-family models bill that at full price unless the request marks where the reusable prefix ends, and the marks have to be in the request — so the gateway adds them, via [`cache_control_injection_points`](https://docs.litellm.ai/docs/tutorials/prompt_caching) in the shipped `config.yaml`:

```yaml
router_settings:
  default_litellm_params:
    cache_control_injection_points:
      - location: message
        role: system
        control:
          type: ephemeral
          ttl: 1h
      - location: message
        index: -3
      - location: message
        index: -1
```

The agent cannot do this itself, and that is the point of putting it here. It asks for `model-default` over the Completions API and never learns what is behind the alias, while the harness only emits its own cache markers when it recognises a Claude-named model — so on an Anthropic backend it caches nothing. Teaching it otherwise would mean naming the model in the agent config, which is exactly the coupling the gateway exists to prevent. A 45-call agent session measured 3.5M input tokens and zero cache reads before this block; the first cron tick after it re-ran the same 83k-token prompt as a cache write, and subsequent ticks read it back.

The system prompt takes the 1h tier because it is the largest static span, every profile and cron tick shares it, and a read refreshes the TTL — so a half-hourly cron schedule keeps it warm instead of missing a 5-minute window every time. The two rolling points ride the conversation tail on the default 5-minute tier. Anthropic allows four breakpoints per request; LiteLLM counts any the caller supplied and never overwrites them, so a client with its own layout still wins.

Nothing here is provider-specific. Non-Anthropic backends drop the markers in their provider transforms — Gemini and Gemma routes answer normally with unchanged token counts — and Gemini's own implicit caching, which needs no markers at all, is unaffected. Leaving the block in place on a Gemini install costs nothing and means switching to `MODEL_PROVIDER=anthropic` doesn't quietly switch caching off.

### Vertex AI and Model Garden

`MODEL_PROVIDER=vertex_ai` routes `model-default` to Vertex AI in your own GCP project — the same first-party Gemini models, plus every Model Garden publisher model your project has access to (Anthropic Claude, Llama, Mistral, and the rest). Requests stay inside your project's billing and data boundary, and no model API key exists anywhere in the cluster. That boundary is a project, not a geography: the default location is the global endpoint, which makes no promise about the region a request is processed in — see the location bullet below.

Two things differ from the API-key providers:

- **Authentication is Workload Identity.** The gateway gets its own service-account pair rather than an API key — see [Security & IAM](/kube-agents/reference/security-and-iam/#the-vertex-ai-gateway-is-a-separate-identity). There is no entry in `platform-agent-secrets` for Vertex.
- **The endpoint is a project and a location.** `VERTEX_PROJECT_ID` and `VERTEX_LOCATION` become `VERTEXAI_PROJECT` and `VERTEXAI_LOCATION` on the gateway pod. The project defaults to the install's. The location defaults to `global` in `install.sh`, Terraform, and the chart — not the cluster's region, which need not serve the model you asked for and on a zonal cluster is not a valid Vertex location at all. (The kustomize dev path substitutes `VERTEX_LOCATION` with no fallback, so export it there.) Set `VERTEX_LOCATION` to a region when you have a data-residency requirement, when org policy blocks the global endpoint, or when the model is a Model Garden partner model served only from specific regions. Google's [locations page](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/locations) lists which locations serve which model, and its [data-residency page](https://docs.cloud.google.com/gemini-enterprise-agent-platform/resources/data-residency) covers what the global endpoint does not guarantee.

`MODEL_DEFAULT_NAME` is the Vertex **publisher model ID**, which is not always the same string the provider's own API uses — Model Garden Claude models, for instance, carry an `@`-suffixed version (`claude-sonnet-4-5@20250929`). Check the model's Model Garden card for the exact ID; a wrong one surfaces as a 404 from the gateway rather than a provisioning error.

```bash
./install.sh \
  --model-provider=vertex_ai \
  --model-default-name=gemini-3.5-flash \
  --vertex-project-id=my-gcp-project \
  --vertex-location=us-east4   # both optional: project defaults to the install's, location to "global"
```

A re-run against an existing install reconciles the switch in one `terraform apply` — the gateway's IAM pair, its KSA, and the rolled ConfigMap land together.

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

Deploy it with `make -C k8s-operator deploy-inference-replay` — it is a development tool, never part of the installer.

## What the agent doesn't care about

The Platform Agent's config (`agents/platform/config.yaml`) doesn't mention the LLM provider. Provider selection is entirely at the LiteLLM / vLLM layer — the agent always talks to the `litellm` Service, and the install decides what that Service resolves to. When the replay proxy is deployed, the `litellm` Service is repointed at the replay proxy and the original LiteLLM pods are re-exposed through a new `litellm-gateway` Service that the proxy forwards cache misses to. That means:

- Swapping Gemini for Anthropic is a LiteLLM `ConfigMap` change.
- So is [prompt caching](#prompt-caching) — the breakpoints are injected gateway-side, because only the gateway knows which model they are for.
- Turning on replay is a `make -C k8s-operator deploy-inference-replay` on a dev cluster.
- Neither touches the agent's persona, skills, or governance layer.

## Where to go next

- [Reference → Examples](/kube-agents/reference/examples/) — the inference example bundles walked through.
- [Deploy → Kustomize](/kube-agents/deploy/kustomize/) — what the LiteLLM Deployment looks like on disk.
- [Concepts → Observability](/kube-agents/concepts/observability/) — LLM telemetry export.
