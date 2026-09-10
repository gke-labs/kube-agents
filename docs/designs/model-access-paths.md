# Hosting the model in your cluster

> **STATUS: proposal.** Real today: the hosted-provider path (`gemini`, `anthropic`, `openai`,
> `vertex_ai`) and the ChatGPT-subscription path (`chatgpt`, dev overlay plus example), each a row
> in `k8s-operator/config/integrations/litellm/providers.json`; and a hand-applied vLLM recipe
> under `examples/vllm-gemma/`. Everything else here is the plan. It supersedes the installer,
> chart, and Terraform half of #608 and keeps that PR's example.

## 1. Goal

A third way for a kube-agents user to give the agent a model, beside an API key and a
subscription: **the install hosts the model itself**, on GPUs in the same cluster, with Gemma 4 as
the default and any model the server can load allowed. One `install.env` line selects it:

```bash
MODEL_PROVIDER=hosted_vllm
# MODEL_DEFAULT_NAME=google/gemma-4-31B-it   # the default; any vLLM-loadable id works
```

The agent is untouched. It speaks the OpenAI wire to `model-default` at the gateway, and only the
gateway's `model_list` knows where that alias goes
([inference gateway](../site/src/content/docs/concepts/inference-gateway.md)). The end-state
architecture already names the split: "LiteLLM proxy for hosted models, vLLM for local GPU
models" ([05-system-architecture](../architecture/05-system-architecture.md) §5, C5).

## 2. Two facts that shape the design

**LiteLLM routes; it does not run a model.** It has no inference engine, so a GPU on the LiteLLM
pod does nothing unless a model server shares the pod. LiteLLM's own provider for a vLLM server
is `hosted_vllm/<model>` plus an `api_base`; it needs no key and reads `HOSTED_VLLM_API_BASE`
from the environment. That provider name is the one this design adds, exactly as `vertex_ai` is
a provider name today.

**The model server is its own workload, not a sidecar.** The gateway runs two replicas behind a
PodDisruptionBudget at 100m CPU; a sidecar would mean two GPUs and two copies of the weights for
one model's throughput, and every gateway rollout would reload the model for minutes. A separate
Deployment is what Hindsight already is: a third-party service the chart renders when a provider
setting asks for it.

## 3. Fit to the existing structure

Everything below is a copy of a pattern already in the tree. The left column is what exists; the
right column is the new file or edit.

### 3.1 The provider: mirror `vertex_ai`

| Exists today                                                         | Add for `hosted_vllm`                                                                                                                                                                                                                                                   |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `providers.json` row `vertex_ai` with `overlay` and `settings`       | Row `hosted_vllm`: `label` "vLLM in this cluster", `defaultModel`, `overlay: hosted_vllm`, `authentication.type: none`. The admin console's LLM gateway page reads this file and needs nothing else.                                                                    |
| `litellm/overlays/vertex_ai/` (kustomization, deployment patch, KSA) | `litellm/overlays/hosted_vllm/`: a `config.yaml` whose entries are `hosted_vllm/${MODEL_DEFAULT_NAME}` with `api_base: http://vllm.${NAMESPACE}.svc.cluster.local:8000/v1`, and a NetworkPolicy patch adding one egress rule to the vLLM pods on 8000 by `podSelector`. |
| `litellm.yaml` `$defaultModels` and the `vertex_ai` render branch    | `hosted_vllm` in the table and in the `fail` message; the config helper takes an `apiBase` and renders it; the egress rule selects the chart's own vLLM pods, not every pod in the cluster.                                                                             |
| `install.defaults.env` `DEFAULT_MODEL_GEMINI/OPENAI/ANTHROPIC`       | `DEFAULT_MODEL_VLLM`; `tests/test_installer_common.py` already pins the chart table equal to these.                                                                                                                                                                     |
| `default_model_for_provider`, `is_valid_model_provider`              | One `case` arm and one alternation each.                                                                                                                                                                                                                                |
| `install.sh` provider menus (Day-1 wizard and Day-2 control panel)   | A fifth entry, "Host the model in this cluster (vLLM on a GPU node pool)"; it prompts for the model id and the weights source (§3.3) and nothing else.                                                                                                                  |
| `variables.tf` `model_provider` validation                           | `hosted_vllm` in the `contains` list.                                                                                                                                                                                                                                   |
| `k8s-operator/Makefile` `deploy-litellm` `vertex_ai` branch          | A `hosted_vllm` branch building the overlay with `LITELLM_VARS`.                                                                                                                                                                                                        |

No `custom` pseudo-provider, no default endpoint URL: the `api_base` is the Service the chart
renders in §3.2, and the model id is whatever the server was started with.

### 3.2 The model server: mirror Hindsight

| Exists today                                                                                          | Add for vLLM                                                                                                                                                                                                                                                                                      |
| ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `k8s-operator/config/integrations/hindsight/` (api, postgres, netpol, pdb, podmonitoring, README)     | `k8s-operator/config/integrations/vllm/`: `deployment.yaml`, `service.yaml`, `networkpolicy.yaml`, `pdb.yaml`, `podmonitoring.yaml`, `kustomization.yaml`, `README.md`. Promoted from `examples/vllm-gemma/`, with `${VLLM_IMAGE}`, `${MODEL_DEFAULT_NAME}`, and `${VLLM_GPU_COUNT}` substituted. |
| `charts/kube-agents/templates/hindsight.yaml`, "mirroring k8s-operator/config/integrations/hindsight" | `charts/kube-agents/templates/vllm.yaml`, the same objects from values.                                                                                                                                                                                                                           |
| `kube-agents.hindsightEnabled`: `hindsight.enabled: null` follows the memory provider                 | `kube-agents.vllmEnabled`: `vllm.enabled: null` follows `litellm.modelProvider == "hosted_vllm"`; `true` or `false` overrides, so a user can host the server for another consumer or point the provider at their own.                                                                             |
| `values.yaml` `hindsight:` block                                                                      | `vllm:` block: `image`, `model`, `gpu.accelerator`, `gpu.count`, `tensorParallelSize`, `quantization`, `maxModelLen`, `weights` (§3.3), `resources`, `rollingUpdate` (surge-first, per `tests/test_deployments_rollout_quota.py`).                                                                |
| `images.json` `hindsight-api`, `hindsight-postgresql`                                                 | `vllm`, third-party, digest-pinned. See §3.4 for the mirror-size question.                                                                                                                                                                                                                        |
| `deploy-hindsight` / `undeploy-hindsight`                                                             | `deploy-vllm` / `undeploy-vllm`, `VLLM_VARS`, `require-var VLLM_IMAGE`.                                                                                                                                                                                                                           |
| `STATIC_NETWORK_POLICIES` roster in `scripts/check_iac_parity.py`                                     | The two new NetworkPolicy copies (chart template and kustomize dir); the example's is already there.                                                                                                                                                                                              |
| `examples/vllm-gemma/`                                                                                | Stays as the hand-applied reference, its README pointing at the integration for the managed path. Same relationship as `examples/litellm-chatgpt-subscription/` to `overlays/chatgpt/`.                                                                                                           |

The server's NetworkPolicy admits ingress on 8000 from the gateway's pods and `gke-gmp-system`
only. The agent never reaches it directly.

### 3.3 Weights

Gemma is gated on Hugging Face, so an online pull needs a token; an air-gapped cluster has no
Hugging Face at all. `vllm.weights` takes one of:

- `hfTokenSecret`: a Secret holding `HF_TOKEN`, created by the composition from a new `HF_TOKEN`
  install key handled like the provider keys (`write_secret_env_var`, `PERSIST_SECRETS_ON_DISK`).
  vLLM pulls from the Hub into an emptyDir sized for the model.
- `gcsPath`: a `gs://` prefix an init container copies onto the volume before vLLM starts. Model
  Garden publishes Gemma 4 at `gs://vertex-model-garden-public-us/gemma4/<model>/`, readable
  without a token, and a private bucket is the natural store for an air-gapped GCP project's own
  copy. The default on GCP.

Either way the volume is sized from the model: a 31B bf16 checkpoint is 62 GB, which is why
#608's 50Gi ephemeral-storage limit could not have loaded the model it named.

### 3.4 Capacity: mirror the gvisor node pool

| Exists today                                                                                   | Add                                                                                                                                                                                                                                                                                                               |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `gke-cluster` module `enable_gvisor_node_pool`, `gvisor_pool_name`, Standard-only precondition | `enable_gpu_node_pool`, `gpu_pool_name`, `gpu_accelerator_type`, `gpu_accelerator_count`, `gpu_machine_type`, `gpu_spot`, `gpu_disk_size_gb`, `gpu_node_locations`. Same precondition: Autopilot fails the plan, because there the server's `nodeSelector` on `cloud.google.com/gke-accelerator` is all it takes. |
| `full-install` passes `enable_gvisor_node_pool` through; installer derives `ENABLE_GVISOR`     | `enable_gpu_node_pool` defaults to `model_provider == "hosted_vllm" && cluster is Standard`; the installer writes the `gpu_*` tfvars from `GPU_ACCELERATOR`, `GPU_COUNT`, `GPU_SPOT` install keys with defaults in `install.defaults.env`.                                                                        |

L4 is zonal, and spot L4 stocks out (both `us-central1` zones did on 2026-09-10), so
`gpu_node_locations` is explicit and `gpu_spot` defaults to `false`.

**Image size.** The vLLM image is about 11 GB and `scripts/mirror_images.sh` selects by `origin`
alone, so a plain third-party entry makes it mandatory in every mirror, the point #608's review
raised. The fix is one optional field, `mirrorGroup: "vllm"`, honoured by `mirror_images.sh` as an
`INCLUDE_GROUPS` filter defaulting to none, and by `hack/check-image-inventory.sh` as a recognised
key. Small, and it gives the next large optional image somewhere to go.

### 3.5 Defaults

Model ids must exist. #608 defaulted to `google/gemma-4-27B-it`, which does not; the Gemma 4
instruction-tuned family is `E2B`, `E4B`, `12B`, `26B-A4B`, and `31B`. Proposed defaults:

| Value                             | Default                   | Why                                                                                             |
| --------------------------------- | ------------------------- | ----------------------------------------------------------------------------------------------- |
| `DEFAULT_MODEL_VLLM`              | `google/gemma-4-31B-it`   | The tier the agent's tool use needs; open question 1 is whether a cheaper one passes the bench. |
| `gpu.accelerator`                 | `nvidia-l4`               | Widest GKE availability with native bf16 and FP8.                                               |
| `gpu.count`, `tensorParallelSize` | `2`                       | 31B fits 2×L4 (48 GB) with FP8 and not without it.                                              |
| `quantization`                    | `fp8`                     | Same reason; `""` for a card with room for bf16.                                                |
| `gpu_machine_type`                | `g2-standard-24`          | The 2×L4 shape.                                                                                 |
| `weights`                         | `gcsPath` to Model Garden | No token, works air-gapped from a private copy.                                                 |

## 4. Testing

- **Unit, every PR:** chart renders for `hosted_vllm` (config, egress, the vLLM objects present)
  and for `gemini` byte-identical to today; `vllm.enabled` override both ways; the default-model
  pin test covers the new row; kustomize builds of the overlay and the integration; the rollout
  test's expectations for the new Deployment; `terraform validate` for the pool variables.
- **Integration:** `deploy-litellm MODEL_PROVIDER=hosted_vllm` against a stub OpenAI server in
  kind, asserting the alias routes and the ConfigMap carries no key.
- **CUJ, manual:** a `bench/cuj/` journey for the hosted path, because it needs a GPU and CI has
  none: load time, first token, one agent turn.
- **No new eval cases.** The agent's behaviour is not what changes.

## 5. Delivery, three pull requests

| PR  | Contents                                                                                                                                  | Testable how                                                                              |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| 1   | §3.2 and §3.3: the `vllm` integration and chart template, opt-in via `vllm.enabled: true`, `images.json` with `mirrorGroup`, roster, docs | Live on a GPU node with `helm upgrade --set vllm.enabled=true`; no provider change needed |
| 2   | §3.1: the `hosted_vllm` provider end to end, `vllm.enabled: null` derivation, installer menus, defaults                                   | Chart render tests; live agent turn through the gateway                                   |
| 3   | §3.4: the GPU node pool and its installer derivation                                                                                      | `terraform validate`; a fresh Standard install                                            |

PR 1 first because it is the half that can be exercised on the GPU node already provisioned for
this design, and because it stands alone: a user with a GPU pool can turn it on today and point
`examples/litellm-gemini`-style config at it by hand.

## 6. On the #608 review

The reviewer asked that the operator not be "in the business of deploying models and model
servers". This design does put a vLLM Deployment in the chart, deliberately, and the difference
from #608 is the terms: it is rendered only when a provider setting asks for it, exactly as the
Hindsight store is; it is a copy of an upstream recipe, not a fork of vLLM; it carries no model
in the image and no model-specific code; and the model id is a value with a default, not a
constant. The alternative, a second `helm_release` of the vLLM production-stack chart the way
cert-manager is installed, was considered and set aside for now: it brings a router and
observability stack this install already has, and it would be the first component whose
manifests this repository cannot read. It remains the fallback if the in-tree copy proves costly
to keep current.

## 7. Open questions

1. **Default tier versus cost.** `31B` FP8 on 2×L4 against `26B-A4B` or `12B` on one L4; the bench
   harness can say which passes the presubmit cases.
2. **Autopilot defaults.** Whether the chart should pick an accelerator class or leave
   `nodeSelector` to values.
3. **`examples/vllm-gemma/` after PR 1.** Reference kept, or reduced to a pointer.
4. **#608.** Keep its example half open against `examples/`, close the rest in favour of PR 2.

<!-- Live-test results from the 2026-09-10 run (2×L4, gemma-4-31B-it, FP8, TP 2): fill in load
     time, VRAM headroom at max-model-len 32768, and the agent-turn outcome once the node lands. -->
