---
name: review-iac-parity
description: Reviews a pull request for drift between the three install surfaces — the provisioning scripts and kustomize manifests, the Terraform modules, and the Helm chart — and reconciles the lagging surfaces when asked.
---

# Task

kube-agents can be installed three ways, and each states the same install in its own language:

| Surface              | Lives in                                                                      |
| -------------------- | ----------------------------------------------------------------------------- |
| Provisioning scripts | `k8s-operator/scripts/` plus the manifests they apply, `k8s-operator/config/` |
| Terraform            | `terraform/modules/*` and `terraform/examples/full-install/`                  |
| Helm chart           | `charts/kube-agents/`                                                         |

Nothing in the language forces them to move together. A change to a script does not fail a Terraform plan; a bumped image in a kustomize base does not fail a chart render. So they drift silently, and the drift is only ever noticed by whoever installs the other way.

Given a branch diff, your job is to determine whether a change to one surface left the other two behind, and to say exactly what has to change where.

**The provisioning scripts and `k8s-operator/config/` are the source of truth.** When surfaces disagree, they win — unless the divergence is on the deliberate list below, in which case the chart or Terraform wins and the scripts are not "wrong".

This skill holds no repository facts of its own — no version numbers, no role lists, no identifier values. Every fact lives in the sources named here; when this skill and a source disagree, the source wins and this skill needs fixing.

# The deliberate divergences

These are **not** drift. Do not "fix" them; if a diff changes one, that is what needs discussing.

- **Admission webhooks default off in the chart** and on in the script/kustomize path. Both surfaces ship the wiring; what the chart cannot ship is the cert-manager it needs, so a default-on chart would fail at apply time on every cluster without it. `terraform/examples/full-install` installs cert-manager and sets `operator.webhooks.enabled=true`. The cert-manager version and the admission paths are compared mechanically by `make iac-parity-check`.
- **The webhooks' `failurePolicy`** is `Ignore` in the chart and `Fail` in the kustomize copy. Helm applies the webhook configurations before both the Certificate and the PlatformAgent CR, so `Fail` would have the API server reject the chart's own CR on a fresh install; the script path is not exposed to this because `provision_03` waits for the operator and a later step applies the CR. The chart refuses the deadlocking combination at render time.
- **`modelProvider: chatgpt`** is rejected by the chart: it needs the OAuth-token PVC from the kustomize overlay.
- **Autopilot vs Standard.** The `gke-cluster` module builds an Autopilot cluster; `provision_01_gcp_cluster.sh` builds a Standard one. Everything node-level therefore has no Terraform counterpart — machine type, the gVisor node pool (`provision_02`), the managed-OTel scope. The `--addons=GcpFilestoreCsiDriver` half of `provision_01`'s addons flag sits here too: nothing in the harness mounts a Filestore volume, and `gcloud container clusters create-auto` has no `--addons` flag at all, so the script's own Autopilot path could not pass it either. The `BackupRestore` half **is** mirrored, as `gke_backup_agent_config`.
- **LiteLLM's OTel callback** is unconditional in the kustomize base and gated behind `litellm.otel` in the chart, because a chart install may target a cluster with no managed collector.
- **`harness.hermes.dashboardEnabled`** defaults to `true` in the CRD and `false` on the script path. This one is a real inconsistency rather than a designed difference; it is tracked in the chart README, and closing it is a change in its own right.
- **The GitHub minter's Kubernetes surface is script-only.** The `github-minter` module creates IAM and KMS only; the minter Deployment, KSA, and NetworkPolicy (`k8s-operator/config/integrations/github/`), the `github-app-credentials` Secret (written by `provision_07`), and the App PEM import (`provision_10`) have no Terraform or chart counterpart.
- **cert-manager is installed differently by design.** On Autopilot the script patches the deployments to `--leader-elect=false`; the composition keeps leader election and moves the lease into the cert-manager namespace, which clears the same kube-system restriction without giving up the lock. The script also skips an existing install (`verify_cert_manager`); Terraform cannot detect one and fails on the existing CRDs — `enable_cert_manager = false` is the documented escape. Both live in `terraform/examples/full-install/main.tf`'s comments.
- **The Hindsight memory store (`provision_13`) is script-only.** Hindsight-backed memory providers (`kube_agents_memory`, `hindsight`) need the API server and Postgres from `k8s-operator/config/integrations/hindsight/`, which only `provision_13_deploy_hindsight.sh` deploys. The chart accepts those providers in `platformAgent.harness.memory.provider` and the operator will point the agent at a `hindsight-api` Service that a chart/Terraform install never created — the values comment warns about this. The default `multiuser_memory` needs none of it.
- **`full-install` enables a superset of APIs** (`iam`, `monitoring`, `logging` on top of the ones the scripts enable): Terraform must enable everything its own resources call, where gcloud enables APIs implicitly.
- **`googleChat.homeChannel` is settable from the chart and Terraform only**; `platform-agent.yaml.template` hardcodes it empty (Slack's equivalent is script-settable). Closing it means a new init_var on the script path — a follow-up, not silent drift.
- **No Terraform or chart counterpart** exists for the inference-replay proxy (`provision_11`) or the gVisor node pool (`provision_02`). Absence here is expected; note it only if a diff starts building one halfway.

Anything else that differs is drift until someone documents otherwise — and documenting it means editing this list **and** the exemption list in `scripts/check_iac_parity.py`, together.

# Procedure

## 1. Run the mechanical gate first

```bash
make iac-parity-check
```

`scripts/check_iac_parity.py` compares the scalar values two surfaces must literally agree on: image tags and replica counts, the agent's `imagePullPolicy`, LiteLLM model aliases and per-provider default models, the Vertex gateway's KSA/GSA names, the registry prefix, the IAM role bundles, GSA/KSA/namespace/topic identifiers, KMS key names, the backup-plan defaults, the cert-manager version and resource quotas, the webhook admission paths, and the host-cluster discovery label. Its failures are always Blocking and always precise — start there, and do not re-derive by hand what it already told you.

Its extractors have their own tests (`scripts/test_check_iac_parity.py`, run by `make test-python`). Extend them alongside any new check: the failure that matters is not a parser that stops matching — that exits loudly — but one that matches the wrong text and reports parity across surfaces that have drifted.

It is a floor, not a ceiling. It knows nothing about structure: a resource one surface creates and another does not, a variable that exists but is never wired into the chart values, a flag added to a `gcloud` call. That is the rest of this procedure.

## 2. Classify the diff

`git diff --name-only main...HEAD`, then for each changed path ask which surface it belongs to and which peers own the same concern:

| Changed                                                    | Peers to check                                                                   |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `k8s-operator/scripts/provision_01_*` (cluster, KMS, APIs) | `terraform/modules/gke-cluster`, the API list in `full-install/main.tf`          |
| `provision_04_*` (IAM, Workload Identity)                  | `terraform/modules/kube-agents-iam`, the role bundles in `full-install/main.tf`  |
| `provision_05_*` (Chat, Pub/Sub)                           | `terraform/modules/chat-pubsub`, the chart's `googleChat` values                 |
| `provision_06_*` (Slack)                                   | the chart's `slack` values, `full-install`'s Slack variables and Secret keys     |
| `provision_07_*` (Secrets)                                 | `platform-agent-secret.yaml`, `local.credentials` in `full-install/main.tf`      |
| `provision_08_*`, `platform-agent.yaml.template`           | `charts/kube-agents/templates/platform-agent-cr.yaml` and its values             |
| `provision_10_*` (minter)                                  | `terraform/modules/github-minter`                                                |
| `provision_12_*` (backups)                                 | `terraform/modules/gke-backup-plan`, `enable_backup_agent` on the cluster module |
| `k8s-operator/config/**` (kustomize)                       | the chart template that mirrors it                                               |
| `k8s-operator/scripts/common.sh`                           | every surface — this file owns the shared defaults                               |
| `charts/**` or `terraform/**` alone                        | the script that owns the same concern; a one-sided change is the common case     |

Read the peer, not your memory of it.

## 3. Test the parity claims

For each pair the diff touches, the questions are always the same four:

1. **Same resource?** Does one surface now create, delete, or rename something the other does not? A new `gcloud ... create` in a script, a new `resource` block in a module, a new template in the chart.
2. **Same value?** Names, image tags, IAM roles, ports, schedules, retention, ack deadlines, addon flags. If it is a scalar both sides state, it belongs in `check_iac_parity.py` — say so in your finding when it is not there yet.
3. **Same knob?** A configuration option one surface exposes and another cannot express is drift even when the defaults happen to agree, because the two installs can no longer be made identical. Check the `PlatformAgent` CRD (`charts/kube-agents/crds/`) for what the field actually permits before proposing a chart value.
4. **Same wiring?** A Terraform variable that exists but never reaches the chart's `values`, or a chart value the composition never sets, is a knob in name only.

## 4. Check the documentation of the seam

The parity intent is stated in prose in several places, and prose drifts with the code:

- each Terraform module README's "Relationship to the provisioning scripts" section,
- `terraform/examples/full-install/README.md` (what it provisions, the IAM bundle table),
- the chart README (LiteLLM, Integrations, Agent runtime knobs, Notes),
- `k8s-operator/scripts/README.md` for what the scripts themselves claim.

A new module needs its README, an entry in the documentation map (`docs/README.md`), and a line in the `AGENTS.md` repository layout if it changes what `terraform/` contains.

## 5. Run the rest of the gates

- `make iac-parity-check` (again, at the final state)
- `terraform fmt -check -recursive terraform/`, then `terraform init -backend=false && terraform validate` in each changed module and example
- `helm lint` and `helm template` for the chart, with `platformAgent.harness.{clusterName,location,projectId}` set — and render again with the new values set, not just defaulted, or the new branch is never exercised
- `make chart-check` if `k8s-operator/config/crd` or `config/rbac` moved
- `make docs-check` and `prettier --check` on changed Markdown/YAML

# Output

Report a triage table: **finding → the two surfaces that disagree (file:line each) → severity → required action**. Separate:

- **Blocking:** the mechanical check fails; the surfaces would produce materially different installs (different image, different IAM, a resource only one creates); a knob one surface cannot express at all; a new module with no README or map entry.
- **Advisory:** cosmetic or defaulted-the-same differences; a scalar pair that ought to be added to `check_iac_parity.py`; prose that describes the seam loosely.

State explicitly which surface you consider correct for each finding and why — "the scripts are the source of truth" is the default, and any other answer needs its reason.

# Reconciling

Reporting is the deliverable unless you were asked to fix. When you are asked to bring the surfaces back into line:

1. **Change the lagging surface, never the source of truth**, unless the finding is that the script itself is wrong — in which case say so and get agreement first.
2. **Extend `scripts/check_iac_parity.py`** with any scalar pair you just had to reconcile by hand. A drift that recurred once will recur again, and a check is cheaper than the next review.
3. **Add a divergence to both lists** — this file and the script's docstring — when the right answer is "these two are allowed to differ". A divergence documented in only one place is how the exemption list rots.
4. **Live-test the reconciled path**, per `AGENTS.md`. A chart value that renders is not a chart value that works; a Terraform variable that plans is not one that applies.
