# Hosting the model in your cluster

> **STATUS: in progress, #1433.** Real today: the hosted-provider path (`gemini`, `anthropic`,
> `openai`, `vertex_ai`) and the ChatGPT-subscription path (`chatgpt`), each a row in
> `k8s-operator/config/integrations/litellm/providers.json`, and a hand-applied vLLM recipe under
> `examples/vllm-gemma/`. This document is the design of the `hosted_vllm` provider that joins
> them. Tracking issue: #1418. It supersedes #608.

## 1. Goal

A third way for a kube-agents user to give the agent a model, beside an API key and a
subscription: **a model server in the same cluster**, any model the server can load. Nothing is
defaulted or hard-coded: the install names the model the server was started with, the server's
address, and its pod port, the way Vertex names a project and a location.

```bash
kubectl apply -f examples/vllm-gemma/      # the server, on a GPU node pool you provide
# install.env
MODEL_PROVIDER=hosted_vllm
MODEL_DEFAULT_NAME=<the model the server was started with>
HOSTED_VLLM_API_BASE=http://<service>.<namespace>.svc.cluster.local/v1
HOSTED_VLLM_TARGET_PORT=<the server pod's port>
```

The agent is untouched. It speaks the OpenAI wire to `model-default` at the gateway, and only the
gateway's `model_list` knows where that alias goes
([inference gateway](../site/src/content/docs/concepts/inference-gateway.md)). The end-state
architecture already names the split: "LiteLLM proxy for hosted models, vLLM for local GPU
models" ([05-system-architecture](../architecture/05-system-architecture.md) §5, C5).

## 2. Two facts that shape the design

**LiteLLM routes; it does not run a model.** It has no inference engine, so a GPU on the LiteLLM
pod does nothing unless a model server shares the pod. LiteLLM's own provider for a vLLM server
is `hosted_vllm/<model>` ([its docs](https://docs.litellm.ai/docs/providers/vllm)); it needs no key, and it reads the server address from the
`HOSTED_VLLM_API_BASE` environment variable when the config carries no `api_base`. The base
config already renders `${MODEL_PROVIDER}/${MODEL_DEFAULT_NAME}`, so `hosted_vllm/<model>` comes
out of the template that exists and the address travels as one environment variable, the way
`VERTEXAI_PROJECT` does.

**The model server is its own workload, not a sidecar.** The gateway runs two replicas behind a
PodDisruptionBudget at 100m CPU; a sidecar would mean two GPUs and two copies of the weights for
one model's throughput, and every gateway rollout would reload the model for minutes.

## 3. What ships

Everything mirrors `vertex_ai`, the provider variant already in the tree, and nothing new is
written for the server: the harness integrates an OpenAI-compatible endpoint and ships no
model-specific code, which is the line the #608 review drew.

| Surface                         | Change                                                                                                                                                                                                                                                                  |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Chart `litellm.yaml`            | `hosted_vllm` admitted by the provider guard; three required values (`modelDefaultName`, `hostedVllm.apiBase`, `hostedVllm.targetPort`), each refused at render time when empty, the way `vertex.projectId` is; the env var beside `VERTEXAI_PROJECT`; one egress rule. |
| Installer                       | `hosted_vllm` accepted, with no default model; a fifth entry in both provider menus prompting for the three values; the two new keys round-trip through `install.env` and `terraform.tfvars`.                                                                           |
| Terraform                       | `hosted_vllm` in the `model_provider` validation; `hosted_vllm_api_base` and `hosted_vllm_target_port` passed through to the chart, no default.                                                                                                                         |
| `examples/litellm-hosted-vllm/` | `examples/litellm-gemini/` with the Gemini key replaced by the provider line and the env var: the gateway half of the pair with `examples/vllm-gemma/`.                                                                                                                 |

Left out on purpose, to keep the change small: a kustomize overlay for the dev path and a
`providers.json` row for the admin console. Both are the `vertex_ai` shape again and can follow.

The model server is `examples/vllm-gemma/`, applied by hand on a GPU node pool the user provides,
as the site's inference-gateway page already describes. Automating the node pool and the server
(node auto-provisioning in the `gke-cluster` module, the vLLM project's chart as a second
`helm_release`) was considered and left out of this change: it is where the complexity lives, and
the provider is useful without it.

## 4. Egress

The gateway's NetworkPolicy allows 443 to public addresses and nothing to private ones. The new
rule admits pods in the gateway's own namespace on the server pod's port, 8000. Not every pod in
the cluster, and not the Service port: NetworkPolicy matches after Service translation.

## 5. Testing

- **Unit, every PR** (`tests/test_hosted_vllm_provider.py`): the chart render for `hosted_vllm`
  carries the provider line, the env var, and exactly one same-namespace egress rule on the
  configured port; each missing value fails the render naming it; the other providers render none
  of it; the example's manifests carry the same provider line, env var, and egress rule.
- **Manual:** the live run recorded in the pull request. CI has no GPU.
- **No new eval cases.** The agent's behaviour does not change.

## 6. What the live run found

On a 2×L4 node (`g2-standard-24`), `examples/vllm-gemma/` as #608 left it does not fit a 31B model:
with FP8 weights at 16.5 GiB per GPU, `--max-model-len 32768` plus multimodal profiling fails
engine initialization with `CUDA error: out of memory`; two sequences and vision profiling
disabled fit.

The agent then failed every turn with an HTTP 500 whatever the context, because it requests
65536 output tokens on each call (the Hermes `custom` provider default; the agent's config sets
no override) and vLLM rejects a request whose output budget exceeds `--max-model-len`. The
server's context therefore has to exceed 65536 plus the agent's ~20k-token prompt. That is why
the example now serves `google/gemma-4-E4B-it` at 131072 on one L4, the configuration under
which the gateway request and a full agent turn with a tool call succeeded, and why the larger
Gemma 4 checkpoints are out of reach on L4-class hardware. A harness-side cap would be a
`max_tokens` knob in the agent's rendered config; the review of #608 asked for the operator to
stay untouched, so the requirement is documented in the example instead.
