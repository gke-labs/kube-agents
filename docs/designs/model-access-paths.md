# Hosting the model in your cluster

> **STATUS: proposal.** Real today: the hosted-provider path (`gemini`, `anthropic`, `openai`,
> `vertex_ai`) and the ChatGPT-subscription path (`chatgpt`, dev overlay plus example), each a row
> in `k8s-operator/config/integrations/litellm/providers.json`; and a hand-applied vLLM recipe
> under `examples/vllm-gemma/`. Everything else here is the plan. It supersedes the installer,
> chart, and Terraform half of #608 and keeps that PR's example. Tracking issue: #1418.

## 1. Goal

A third way for a kube-agents user to give the agent a model, beside an API key and a
subscription: **the install hosts the model itself**, on GPUs in the same cluster, with an open
model as the default and any model the server can load allowed. One `install.env` line selects it:

```bash
MODEL_PROVIDER=hosted_vllm
# MODEL_DEFAULT_NAME=<any id the server can load>; the default lives in install.defaults.env
```

The agent is untouched. It speaks the OpenAI wire to `model-default` at the gateway, and only the
gateway's `model_list` knows where that alias goes
([inference gateway](../site/src/content/docs/concepts/inference-gateway.md)). The end-state
architecture already names the split: "LiteLLM proxy for hosted models, vLLM for local GPU
models" ([05-system-architecture](../architecture/05-system-architecture.md) §5, C5).

## 2. Two facts that shape the design

**LiteLLM routes; it does not run a model.** It has no inference engine, so a GPU on the LiteLLM
pod does nothing unless a model server shares the pod. LiteLLM's own provider for a vLLM server
is `hosted_vllm/<model>`; it needs no key, and it reads the server address from the
`HOSTED_VLLM_API_BASE` environment variable when the config carries no `api_base`. That last
point is what keeps the gateway change small: the base config already renders
`${MODEL_PROVIDER}/${MODEL_DEFAULT_NAME}`, so `hosted_vllm/<model>` comes out of the template
that exists, and the address travels as one environment variable, the way `VERTEXAI_PROJECT` does.

**The model server is its own workload, not a sidecar.** The gateway runs two replicas behind a
PodDisruptionBudget at 100m CPU; a sidecar would mean two GPUs and two copies of the weights for
one model's throughput, and every gateway rollout would reload the model for minutes.

## 3. Constraint: minimal insertion

Every piece below reuses something in the tree or upstream rather than adding a copy. The
budget is stated per file so a reviewer can hold the change to it. Three reuse decisions do most
of the work:

1. **No new LiteLLM config file.** The provider rides the existing base `config.yaml` and one
   environment variable. #608 added a 30-line config overlay and a 30-line chart helper branch for
   the same outcome.
2. **No in-tree model-server manifests beyond the example that already exists.** The kustomize
   dev path builds `examples/vllm-gemma/` as a base and patches two fields; the Terraform install
   runs the vLLM project's own chart as a second `helm_release`, exactly as cert-manager is
   installed today. Nothing is copied into `charts/kube-agents/templates/`.
3. **No node pool resource.** GPU capacity on Standard comes from GKE node auto-provisioning, a
   block on the cluster resource the module already owns; Autopilot needs nothing. #608 carried a
   300-line node pool script; the gvisor pool pattern would be about 60 lines of Terraform.

## 4. Build plan: one pull request, four parts

The whole change lands as one pull request against #1418, with this document in it, so a
reviewer sees the provider, the server, the capacity, and the example together and the
behavioural gate runs once. The parts below are its commits, in order, each self-contained so the
review can proceed part by part.

### Part 1: the `hosted_vllm` provider at the gateway (about 90 lines, plus tests)

Mirrors `vertex_ai` file for file. The address of the server is one value,
`litellm.hostedVllm.apiBase`, required when the provider is `hosted_vllm` and rejected at render
time otherwise, the way `vertex.projectId` is.

| File                                                      | Change                                                                                                                                                                                    | Lines |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `k8s-operator/config/integrations/litellm/providers.json` | One row: `id: hosted_vllm`, `overlay: hosted_vllm`, `authentication.type: none`, one setting `api_base` bound to `HOSTED_VLLM_API_BASE`. The admin console reads it.                      | 12    |
| `…/litellm/overlays/hosted_vllm/kustomization.yaml`       | `resources: [../../base]`, two patches, the label block the other overlays carry.                                                                                                         | 14    |
| `…/litellm/overlays/hosted_vllm/deployment-patch.yaml`    | `env: HOSTED_VLLM_API_BASE=${HOSTED_VLLM_API_BASE}` on `litellm-container`. Same shape as the `vertex_ai` patch.                                                                          | 12    |
| `…/litellm/overlays/hosted_vllm/networkpolicy-patch.yaml` | One egress rule: `podSelector: {}` in the release namespace, port `${HOSTED_VLLM_PORT}`. Not every pod in the cluster.                                                                    | 12    |
| `charts/kube-agents/templates/litellm.yaml`               | `hosted_vllm` in `$defaultModels` and the `fail` text; a `fail` when `hostedVllm.apiBase` is empty; the env var beside `VERTEXAI_PROJECT`; the egress rule with the port from `urlParse`. | 20    |
| `charts/kube-agents/values.yaml`                          | `litellm.hostedVllm.apiBase: ""` with a two-line comment.                                                                                                                                 | 4     |
| `k8s-operator/Makefile`                                   | `LITELLM_HOSTED_VLLM_VARS`; one `elif` arm in `deploy-litellm` and in `undeploy-litellm`.                                                                                                 | 9     |
| `install.defaults.env`                                    | `DEFAULT_MODEL_VLLM`.                                                                                                                                                                     | 1     |
| `scripts/installer/installer_common.sh`                   | One `case` arm in `default_model_for_provider`, one alternation in `is_valid_model_provider`, `hosted_vllm_api_base` in `write_tfvars_from_state`.                                        | 4     |
| `install.sh`                                              | Fifth menu entry and `case` arm in the Day-1 wizard and the Day-2 panel; the summary line.                                                                                                | 16    |
| `terraform/examples/full-install/variables.tf`, `main.tf` | `hosted_vllm` in the `model_provider` validation; `hosted_vllm_api_base` variable; one entry in the `litellm` values merge.                                                               | 10    |
| `install.env.example`, `terraform.tfvars.example`         | A commented block each.                                                                                                                                                                   | 8     |

Tests, all on every pull request: the chart render for `hosted_vllm` (env var present, egress rule
present with the URL's port, `gemini` byte-identical to `main`, empty `apiBase` fails with a
message naming the value), following `tests/test_vertex_location_defaults.py`; `hosted_vllm` added
to the loop in `tests/test_installer_common.py`, whose pin against `install.defaults.env` then
covers the default; the provider added to the tuple in `tests/test_operator_makefile.py` so the
overlay builds and the recipe parses.

Live: the gateway on the dev cluster re-rendered with `hosted_vllm` against the vLLM pod from
`examples/vllm-gemma/`, one chat completion through it, one agent turn.

### Part 2: the model server (about 110 lines, plus `images.json`)

**Dev path: build the example.** `examples/vllm-gemma/` gains a `kustomization.yaml` listing its
four files, which changes nothing about applying it by hand. A new
`k8s-operator/config/integrations/vllm/kustomization.yaml` names the example as its base (a
kustomize base may live outside the kustomization's directory; only raw files may not) and
patches two fields so the copy people paste from keeps its literal values while the dev path gets
`${VLLM_IMAGE}` and `${MODEL_DEFAULT_NAME}`. `deploy-vllm` and `undeploy-vllm` in the Makefile
follow `deploy-hindsight`.

| File                                                        | Change                                                                                        | Lines |
| ----------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ----- |
| `examples/vllm-gemma/kustomization.yaml`                    | The resource list.                                                                            | 7     |
| `k8s-operator/config/integrations/vllm/kustomization.yaml`  | Base plus two JSON patches (container image, `MODEL_ID` env).                                 | 22    |
| `k8s-operator/config/integrations/vllm/README.md`           | Ten lines: what it is, the two variables, the example it builds.                              | 10    |
| `k8s-operator/Makefile`                                     | `VLLM_VARS`, `deploy-vllm`, `undeploy-vllm`.                                                  | 10    |
| `images.json`                                               | The vLLM image the example already pins, with `override: VLLM_IMAGE` and `mirrorGroup: vllm`. | 9     |
| `scripts/mirror_images.sh`, `hack/check-image-inventory.sh` | `mirrorGroup`: skipped unless named in `INCLUDE_GROUPS`; recognised as a key.                 | 14    |

**Install path: the vLLM project's chart.** `terraform/examples/full-install/main.tf` gains a
`helm_release` of `vllm-stack` from `https://vllm-project.github.io/production-stack`, shaped
like `helm_release.cert_manager`: `count` on `var.model_provider == "hosted_vllm" &&
var.host_model_server`, `depends_on` the cluster, mirrored image values from the same local
cert-manager uses. Its values are the chart's own keys, filled from ours:
`servingEngineSpec.modelSpec[0]` with `modelURL = var.model_default_name`, `requestGPU`,
`vllmConfig.tensorParallelSize`, `vllmConfig.extraArgs` for quantization and max model length,
`nodeSelectorTerms` on `cloud.google.com/gke-accelerator`, `hf_token` from a secret when given,
`pvcStorage` sized from a variable; `routerSpec.enableRouter = true` so the Service is the
router's on port 80. `hosted_vllm_api_base` then defaults to that Service's cluster address and
Part 1's gateway change needs nothing new.

| File                                                  | Change                                                                                                                                                                    | Lines |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `terraform/examples/full-install/main.tf`             | The `helm_release`; the `hosted_vllm_api_base` default local.                                                                                                             | 40    |
| `terraform/examples/full-install/variables.tf`        | `host_model_server`, `vllm_chart_version`, `gpu_accelerator`, `gpu_count`, `vllm_quantization`, `vllm_max_model_len`, `hf_token` (sensitive), `model_weights_storage_gb`. | 40    |
| `scripts/installer/installer_common.sh`, `install.sh` | Write the new tfvars; prompt for the weights source when the provider is `hosted_vllm`.                                                                                   | 14    |
| `install.defaults.env`                                | Defaults for the five knobs.                                                                                                                                              | 5     |
| `images.json`                                         | The chart's engine and router images, `mirrorGroup: vllm`, so an air-gapped mirror can opt in.                                                                            | 16    |

Weights: `hf_token` set means the chart pulls from the Hub; unset, the install expects the model
on the PVC, and the README shows the one `gcloud storage cp` from a bucket (Model Garden publishes
the default there, readable without a token) or from the user's own bucket in an air-gapped
project. No init container of ours; the chart's `initContainer` value carries it when wanted.

Tests: `terraform validate`; a render test that `hosted_vllm` with `host_model_server = false`
emits no release; kustomize build of the integration; `tests/test_deployments_rollout_quota.py`
already names the example's Deployment. The chart's images join the inventory check, which
renders every toggle.

Live: `terraform apply` on the dev cluster with the provider set, the router Service answering,
the agent turn from Part 1 repeated through the install's own gateway.

### Part 3: GPU capacity by node auto-provisioning (about 35 lines)

On Standard, the `gke-cluster` module's `google_container_cluster.standard` gains a
`cluster_autoscaling` block behind `enable_gpu_autoprovisioning`: `resource_limits` for CPU,
memory, and the accelerator type, and `auto_provisioning_defaults` carrying the module's existing
OAuth scopes and `GKE_METADATA`. GKE then creates and removes GPU nodes from the model server's
own `nvidia.com/gpu` request and accelerator selector, picks the machine shape, and needs no zone
list, machine type, or pool name from us. Autopilot already does this. The installer sets the
variable from `MODEL_PROVIDER=hosted_vllm` on a Standard cluster it created; for an adopted
cluster it prints the one `gcloud container clusters update --enable-autoprovisioning` line, as it
does for other adopted-cluster prerequisites.

| File                                                      | Change                                                             | Lines |
| --------------------------------------------------------- | ------------------------------------------------------------------ | ----- |
| `terraform/modules/gke-cluster/variables.tf`              | `enable_gpu_autoprovisioning`, `gpu_autoprovisioning_accelerator`. | 12    |
| `terraform/modules/gke-cluster/main.tf`                   | The `cluster_autoscaling` block, `dynamic` on the variable.        | 18    |
| `terraform/examples/full-install/main.tf`, `variables.tf` | Pass-through and derivation.                                       | 6     |
| `scripts/installer/installer_common.sh`                   | The tfvar line; the adopted-cluster hint.                          | 4     |

Spot capacity stocks out (both `us-central1` zones did on 2026-09-10), so the default asks for
on-demand and the knob to prefer spot is a later addition if wanted.

### Part 4: the example and the journey (copies by design)

`examples/litellm-hosted-vllm/` is `examples/litellm-gemini/` with two edits: the ConfigMap's
model line reads `hosted_vllm/<model>` and the Deployment carries `HOSTED_VLLM_API_BASE`. Six
files, the same README sections as the subscription example (prerequisites, numbered `kubectl
apply` steps, verification with one `curl` through the gateway, when to use), its NetworkPolicy on
the `check_iac_parity.py` roster, a row in the site's examples page, one line in `docs/README.md`.
`examples/vllm-gemma/`'s README links to it as the gateway half of the pair.

`bench/cuj/` gains one journey for the hosted path, manual because CI has no GPU: server Ready,
one completion through the gateway, one agent turn that calls a tool, and three numbers (load
time, first token, VRAM headroom at the default max model length). It runs once against the finished branch and is recorded in the pull request's Live validation.

### Documentation, across the parts

The site's inference-gateway page: one row in "Choosing a provider" and the `MODEL_PROVIDER`
table, and the "vLLM (local models)" section gains the install-path paragraph. `INSTALL.md`
Method 1 gets the `install.env` lines, Method 2 the `deploy-vllm` line. No new page.

## 5. Testing summary

- **Unit, every PR:** chart renders (provider, egress, byte-identical `gemini`, the empty-value
  failure), the default-model pin, kustomize builds and recipe syntax, Terraform validation, the
  inventory and parity checks.
- **Integration, every PR:** `deploy-litellm MODEL_PROVIDER=hosted_vllm` in kind against a stub
  OpenAI-compatible server, asserting `model-default` routes to it and the ConfigMap carries no
  key. Home: `tests/integration/`.
- **Manual:** the `bench/cuj/` journey against a real GPU node, recorded in the pull request's
  Live validation.
- **No new eval cases.** The agent's behaviour does not change.

## 6. On the #608 review

The reviewer asked that the operator not deploy models or model servers. This plan keeps that
line: the chart renders no model server; the composition installs the vLLM project's own chart on
request, as it installs cert-manager; the dev path builds the example that already exists; and
nothing in the harness names a model except a default value in `install.defaults.env`.

## 7. Open questions

1. **Default tier versus cost.** The bench harness can say which model tier passes the presubmit
   cases on one L4 versus two.
2. **The upstream chart's pace.** `vllm-stack` moves quickly; the version is pinned in a variable
   and the inventory check will flag image drift, but someone owns the bump.
3. **`examples/vllm-gemma/` naming.** The model-server half of the pair is named for a model; a
   rename to `examples/vllm/` is cheap now and dearer later.

<!-- Live-test results from the 2026-09-10 run (2×L4, FP8, TP 2): fill in load time, VRAM
     headroom at max-model-len 32768, and the agent-turn outcome once the node lands. -->
