# Model access paths: API key, subscription, and hosting the model in your cluster

> **STATUS: proposal.** Real today: the API-key and Workload Identity path (`gemini`, `anthropic`,
> `openai`, `vertex_ai`) through the chart, the installer, and the Terraform composition; a
> hand-applied ChatGPT-subscription proxy under `examples/litellm-chatgpt-subscription/`; and a
> hand-applied vLLM recipe under `examples/vllm-gemma/`. Everything else here is the plan. It
> supersedes the installer, chart, and Terraform half of #608 and keeps that PR's example.

## 1. Goal

A kube-agents user chooses how the agent reaches a model, and the harness does not care which
model it is. Three ways to get there, each one setting in `install.env`:

| Path                     | The user brings                                  | Today                                  |
| ------------------------ | ------------------------------------------------ | -------------------------------------- |
| **API key**              | A hosted provider key, or a GCP project (Vertex) | Shipped                                |
| **Subscription**         | A consumer ChatGPT Plus/Pro login                | Example only; the chart refuses it     |
| **Host in your cluster** | GPUs, and optionally a Hugging Face token        | Example only; nothing in the installer |

The third path has an opinionated default, Gemma 4, and no restriction: any model a supported
model server can load is valid. The agent is untouched throughout. It speaks the OpenAI wire to
`model-default` at the gateway, and only the gateway's `model_list` knows where that alias goes
([inference gateway](../site/src/content/docs/concepts/inference-gateway.md)). The end-state
architecture already names this split: "LiteLLM proxy for hosted models, vLLM for local GPU models"
([05-system-architecture](../architecture/05-system-architecture.md) §5, C5).

## 2. What the gateway can and cannot do

LiteLLM routes requests. It has no inference engine, so **a GPU on the LiteLLM pod does nothing by
itself**: weights have to be loaded by a model server, and LiteLLM forwards to that server over
HTTP. The question "can we give the LiteLLM pod a GPU" therefore becomes "should the model server
run as a sidecar in the gateway pod, or as its own workload". §4.3 answers it: its own workload,
for reasons that are about rollouts and cost rather than possibility.

What LiteLLM does have is a provider for every way of hosting a model locally, each with the same
shape, a prefix on the model name plus an `api_base`:

| Server                 | LiteLLM prefix        | Key required       | Environment variables LiteLLM reads           |
| ---------------------- | --------------------- | ------------------ | --------------------------------------------- |
| vLLM                   | `hosted_vllm/<model>` | No                 | `HOSTED_VLLM_API_BASE`, `HOSTED_VLLM_API_KEY` |
| Ollama                 | `ollama_chat/<model>` | No                 | none; `api_base` in config                    |
| Anything OpenAI-shaped | `openai/<model>`      | Yes, a placeholder | `OPENAI_API_KEY`                              |

The harness already renders `${MODEL_PROVIDER}/${MODEL_DEFAULT_NAME}` into the gateway's config, so
every prefix above works there as soon as the config can also carry an `api_base`. That is the
whole mechanism. Nothing about it is Gemma-specific or vLLM-specific, which is what #608's review
asked for: the harness integrates OpenAI-compatible endpoints and does not own a model server.

## 3. Principles

1. **The gateway routes; it never runs a model.** Whatever hosts the weights is a separate
   workload with its own scaling, disk, and GPU.
2. **Use LiteLLM's provider names.** No `custom` pseudo-provider: `hosted_vllm`, `ollama_chat`,
   and `openai` are the names, and the chart's typo guard learns to accept any prefix once an
   `api_base` is set.
3. **Model-agnostic mechanism, opinionated default.** Defaults name a real model id and a real
   Service. Nothing in the installer, chart, or Terraform mentions a model family except as a
   default value in `install.defaults.env`.
4. **The model server comes from an upstream recipe.** The composition installs a maintained
   chart the way it installs cert-manager today; this repository keeps no vLLM Deployment of its
   own outside `examples/`.
5. **One `install.env` choice per path.** A user picks the path; the installer fills in what
   follows from it, and every derived value is visible in `terraform.tfvars`.

## 4. Design

### 4.1 Layer 0: a generic endpoint (delivers "bring your own endpoint")

Two new install keys, both optional:

| Key              | Chart value                     | Rendered as                                                                                             |
| ---------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `MODEL_API_BASE` | `litellm.apiBase`               | `api_base: <url>` on every `model_list` entry                                                           |
| `MODEL_API_KEY`  | key in `platform-agent-secrets` | `api_key: os.environ/MODEL_API_KEY`, with the env var sourced from the Secret like the other three keys |

Rules the chart enforces at render time, the way it already enforces `vertex.projectId`:

- `MODEL_PROVIDER` outside the four hosted providers **requires** `MODEL_API_BASE` and
  `MODEL_DEFAULT_NAME`. There is no default model for a self-hosted server, because the name is
  whatever the server was started with.
- `MODEL_API_BASE` set with a hosted provider is allowed (Vertex private endpoints, corporate
  proxies) and renders the same way.
- The key is never written into the ConfigMap. `os.environ/` is LiteLLM's own indirection; #608
  wrote the literal key in the kustomize path and dropped it entirely in the chart path.

**Egress.** The gateway's NetworkPolicy allows 443 to public addresses today and nothing to
private ones. With `MODEL_API_BASE` set the chart parses the URL (`urlParse`) and adds one rule
for exactly that port:

- Host ending in `.svc` or `.svc.cluster.local`: a `namespaceSelector` on the named namespace,
  port from the URL. Not the #608 rule, which opened 8000, 8080, and 80 to every pod in the
  cluster.
- Any other host: an `ipBlock` when the host is an IP literal, else a `FQDNNetworkPolicy`, which
  the `gke-cluster` module can already turn on (`enable_fqdn_network_policy`). On-prem endpoints
  behind private DNS need this; a public HTTPS endpoint needs nothing new.

**Kustomize dev path.** One overlay, `config/integrations/litellm/overlays/endpoint/`, adds the
`api_base` and `api_key` lines by `envsubst`. It replaces #608's `custom` overlay and names no model.

This layer alone serves the air-gapped and policy-restricted users #608 describes, plus GKE
Inference Gateway, Vertex private endpoints, and any on-prem server. It is a small PR.

### 4.2 Layer 1: host the model in your cluster

`MODEL_HOSTING=in-cluster` in `install.env` (default `external`). It implies
`MODEL_PROVIDER=hosted_vllm` and fills `MODEL_API_BASE` from the Service the composition creates,
so Layer 0 does the gateway side unchanged. The composition adds two things.

**GPU capacity.** `terraform/modules/gke-cluster` gains `enable_gpu_node_pool`, modelled on the
gvisor pool that is already there (Standard only, attaches to an adopted cluster, fails at plan
time on Autopilot). Inputs: accelerator type, count per node, machine type, spot, disk size, and
node locations, because L4 is zonal. Autopilot needs no pool: the model server's `nodeSelector`
on `cloud.google.com/gke-accelerator` is enough, and the installer skips the pool there.

**The model server.** Four candidates:

| Option                                             | Maintains the manifest | GPU/TP/quantization knobs | Fits the composition as                    | Verdict                                    |
| -------------------------------------------------- | ---------------------- | ------------------------- | ------------------------------------------ | ------------------------------------------ |
| vLLM production-stack chart (`vllm/vllm-stack`)    | vLLM project           | Yes, plus a router        | A second `helm_release`, like cert-manager | **Recommended**                            |
| Ollama chart (`otwld/ollama`)                      | Community              | GPU on/off, model pull    | A second `helm_release`                    | Alternative for small models and dev       |
| GKE Inference Quickstart manifests                 | Google, generated      | Chosen at generation time | Generated files applied by Terraform       | Good source of sizing; awkward to template |
| Our own Deployment (promote `examples/vllm-gemma`) | This repository        | Whatever we write         | A chart template                           | Rejected: the reviewer's point on #608     |

`vllm-stack` is the default because vLLM is what the recommended models are benchmarked on, the
chart exposes tensor parallelism and quantization as values, and it is one `helm_release` with
values the installer already knows how to write. Ollama stays a supported provider through Layer 0
for anyone who prefers it, and is worth a second `MODEL_HOSTING` value later if a dev-sized
default is wanted.

**Defaults.** Model ids must exist; #608 defaulted to `google/gemma-4-27B-it`, which does not.
The Gemma 4 instruction-tuned family on Hugging Face and in the public Model Garden bucket is
`E2B`, `E4B`, `12B`, `26B-A4B`, and `31B`. The proposed default is `google/gemma-4-31B-it` with
FP8 on 2×L4 (`g2-standard-24`, spot), the smallest configuration that fits it; §7 lists the
alternatives and what the live test measured. Weights come from one of two sources the installer
asks about:

- `HF_TOKEN`, stored in a Secret the model server reads. Gemma is gated, so an online pull needs it.
- A GCS path. Model Garden publishes the weights at
  `gs://vertex-model-garden-public-us/gemma4/<model>/`, readable without a token, and GCS is the
  natural store for an air-gapped GCP project's own copy. The chart values point the server at a
  path on a volume the composition fills, and no token is involved.

**Install surface after Layers 0 and 1:**

```bash
# API key (today, unchanged)
MODEL_PROVIDER=gemini
GEMINI_API_KEY=...

# Bring your own endpoint (Layer 0)
MODEL_PROVIDER=hosted_vllm
MODEL_DEFAULT_NAME=<served model name>
MODEL_API_BASE=http://<service>.<namespace>.svc.cluster.local:<port>/v1
# MODEL_API_KEY=            # only if the server checks one

# Host in your cluster (Layer 1)
MODEL_HOSTING=in-cluster
# MODEL_DEFAULT_NAME=google/gemma-4-31B-it   # the default; any vLLM-loadable id works
# GPU_ACCELERATOR=nvidia-l4  GPU_COUNT=2  GPU_SPOT=true
# HF_TOKEN=...   or   MODEL_WEIGHTS_GCS=gs://.../gemma-4-31B-it/
```

### 4.3 Why not a GPU sidecar in the gateway pod

It is possible: a vLLM container beside LiteLLM, `api_base: http://localhost:8000/v1`, no
NetworkPolicy change. It is the wrong default:

- The gateway runs at `replicaCount: 2` with a PodDisruptionBudget. Two replicas means two GPUs
  and two copies of the weights for one model's worth of throughput.
- Every gateway rollout reloads the model. A config change today is a seconds-long restart; with
  weights in the pod it is minutes, and the PDB's `maxUnavailable: 1` fencepost stops protecting
  anything useful.
- The gateway is deliberately small (100m CPU, 512Mi). A GPU pod is spot-preemptible, disk-heavy,
  and slow to schedule; coupling the two makes the agent's only model path inherit all of that.
- Scaling the two independently is the point of having a gateway at all.

A single-GPU all-in-one for laptops-in-the-cloud is a reasonable later addition as a third
`MODEL_HOSTING` value. It is not the shape to build first.

### 4.4 Layer 2: subscription

The chart refuses `chatgpt` today because the OAuth device flow needs a token store and a human
step. Making it a first-class path means:

- `MODEL_PROVIDER=chatgpt` renders a PVC for LiteLLM's auth state, mounted at the path the
  existing example uses, and drops the `fail`.
- The installer, after `apply`, tails the gateway log for the device code and prints the same
  instructions the example's README gives, then waits for the login to complete.
- It stays behind an explicit flag with a warning: consumer subscription terms may not permit
  automated use, and that is the user's call, not the installer's.

This is the smallest of the three and the least urgent; it is here so the goal statement is
complete and the provider enum has a home for it.

## 5. Security

- Keys reach the gateway through `platform-agent-secrets` and `os.environ/`, never a ConfigMap.
- The egress rule is derived from `MODEL_API_BASE`, one port, one namespace or host. Nothing opens
  to every pod.
- The in-cluster model server's ingress admits the gateway's pods only. The agent never talks to
  it directly.
- A Hugging Face token is a Secret the model server mounts; the installer treats it like the other
  provider keys for `PERSIST_SECRETS_ON_DISK`.
- Nothing here changes what the agent can do; the permission boundary is unchanged
  ([security and IAM](../site/src/content/docs/reference/security-and-iam.md)).

## 6. Testing

- **Unit, every PR:** chart renders for each path (`hosted_vllm` with and without `api_base`,
  `ollama_chat`, `openai` with a placeholder key, `gemini` unchanged byte-for-byte); the pinned
  default table stays equal between `install.defaults.env` and the chart; the egress rule matches
  the URL for a `.svc` host, an IP literal, and a DNS name; `terraform validate` for the pool.
- **Integration:** the kustomize `endpoint` overlay against a fake OpenAI-compatible server in
  kind, asserting the model alias, the header, and the absence of the key from the ConfigMap.
- **CUJ, manual:** `bench/cuj/` gains an in-cluster hosting journey, because it needs a GPU and
  CI has none. It records load time, first-token latency, and one full agent turn.
- **No new eval cases.** The agent's behaviour is not what changes.

## 7. Phased plan

| Phase | Delivers                                                                                                     | Size           | Notes                                                               |
| ----- | ------------------------------------------------------------------------------------------------------------ | -------------- | ------------------------------------------------------------------- |
| 1     | Layer 0: `MODEL_API_BASE`, `MODEL_API_KEY`, relaxed provider guard, derived egress, `endpoint` overlay, docs | One PR         | Supersedes #608's installer/chart/Terraform diff; keeps its example |
| 2a    | `enable_gpu_node_pool` in `gke-cluster`, Autopilot passthrough                                               | One PR         | Independent of 2b; useful on its own                                |
| 2b    | `vllm-stack` `helm_release`, `MODEL_HOSTING=in-cluster`, weights source, Gemma default                       | One or two PRs | Depends on 1 and 2a                                                 |
| 3     | Subscription path                                                                                            | One PR         | Independent; lowest priority                                        |

**Sizing data for the default.** A live run on 2026-09-10 against a Standard cluster in
`us-central1` with a spot `g2-standard-24` (2×L4) node exercised `examples/vllm-gemma` with
`google/gemma-4-31B-it`, FP8, tensor parallelism 2, weights copied from the Model Garden bucket by
an init container. Results are recorded in the section below as they land; a model that does not
fit or does not answer tool calls correctly on that hardware is not a default.

<!-- Live-test results: fill in load time, VRAM headroom at max-model-len 32768, and the outcome of
     one agent turn through LiteLLM's hosted_vllm route. -->

## 8. Open questions

1. **Default size versus cost.** `31B` FP8 on 2×L4 is the most capable fit; `26B-A4B` or `12B`
   on one L4 is far cheaper. The bench harness can answer which tier passes the presubmit cases.
2. **Weights source default.** Ask for `HF_TOKEN`, or default to the public GCS bucket on GCP and
   ask only when a model is not there?
3. **Autopilot.** Whether the composition should pick an accelerator class or leave the model
   server's `nodeSelector` to the chart values.
4. **`examples/vllm-gemma` after Phase 2b.** Keep as the hand-applied reference, or retire it in
   favour of the composition's values file.
5. **#608.** Keep the example half open as its own PR against `examples/`, and close the rest in
   favour of Phase 1.
