# Full install (Terraform root composition)

A single `terraform apply` that provisions everything a running Platform Agent
needs — the IaC counterpart of the interactive
[`k8s-operator/scripts/provision.sh`](../../../k8s-operator/scripts/provision.sh)
flow. Use one or the other per project, not both: they would fight over the
same cluster, service accounts, and IAM bindings.

## What it provisions

- The required Google APIs (`google_project_service`, never disabled on
  destroy), including the Cloud KMS API for GKE database encryption and the Chat
  API when Google Chat is enabled.
- A GKE Autopilot cluster ([`gke-cluster`](../../modules/gke-cluster) module)
  with Workload Identity, Cloud KMS database encryption (CMEK), and the Backup
  for GKE agent enabled, and the `kube-agents-host=true` discovery label
  applied.
- Optionally (`enable_gke_backup_plan = true`) a scheduled
  [`gke-backup-plan`](../../modules/gke-backup-plan) for the release namespace.
- The agent's GCP identity ([`kube-agents-iam`](../../modules/kube-agents-iam)
  module): the `kubeagents-platform-gsa` service account, its read-only
  project roles, and the Workload Identity binding to the
  `kubeagents-platform-agent` KSA (see
  [IAM roles](#iam-roles-permission_set-and-project_roles) below).
- Optionally (`enable_google_chat = true`) the Google Chat backend
  ([`chat-pubsub`](../../modules/chat-pubsub) module): Pub/Sub topic,
  subscription, and Chat integration wiring.
- Optionally (`enable_github_minter = true`) the GitHub token minter backend
  ([`github-minter`](../../modules/github-minter) module): minter service
  account plus a KMS key ring and signing key.
- Unless `enable_cert_manager = false`, [cert-manager](https://cert-manager.io)
  via `helm_release`, pinned to the same version
  [`provision_03_gcp_gke_operator.sh`](../../../k8s-operator/scripts/provision_03_gcp_gke_operator.sh)
  installs. It issues the serving certificate for the operator's admission
  webhooks, which this composition turns on (`enable_webhooks`, default true)
  because it can guarantee the dependency — a bare `helm install` of the chart
  cannot, and leaves them off. See [cert-manager](#cert-manager) below.
- The [`kube-agents` Helm chart](../../../charts/kube-agents) (operator +
  `PlatformAgent` CR + the LiteLLM gateway the agent's default model endpoint
  requires) via `helm_release`, installed straight from this repository
  checkout with Workload Identity annotations and the credentials Secret
  composed from your variables. `model_provider` selects which provider
  LiteLLM routes `model-default` to (set the matching `*_api_key` variable);
  `model_default_name` overrides the per-provider default model.
- Two `random_password` values added to that Secret rather than asked for:
  `SESSION_KV_API_KEY`, the bearer token for the pod-local Session KV server,
  and `SESSION_KV_SALT`, the HMAC salt that pseudonymises chat identities.
  Generated here rather than left to the chart so `terraform apply` stays
  idempotent without reading the cluster — and because rotating the salt
  re-anonymises every user, severing their past sessions from their future
  ones.
- Optionally (`model_provider = "vertex_ai"`) the Vertex AI / Model Garden path:
  a second [`kube-agents-iam`](../../modules/kube-agents-iam) instantiation for
  the gateway's service account, `roles/aiplatform.user` on
  `vertex_project_id`, and the Workload Identity annotation the chart needs.
  Vertex takes no API key, so no `*_api_key` variable applies.

> [!WARNING]
> The credential variables (`api_server_key`, `*_api_key`, Slack tokens) are
> marked `sensitive`, which redacts plan output — but like every secret passed
> through Terraform they are stored **in plaintext in the Terraform state**.
> The two generated `SESSION_KV_*` values live in state for the same reason.
> Keep the state in a protected backend (e.g. a GCS bucket with tight IAM),
> not on a shared disk or in version control.

## Prerequisites

- A GCP project you can administer.
- Terraform `~> 1.5`.
- Application Default Credentials for the Google, Kubernetes, and Helm
  providers:

  ```bash
  gcloud auth application-default login
  ```

## Usage

```bash
cd terraform/examples/full-install
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform apply
```

A first apply into an empty project needs nothing else. Once the project has
been destroyed and re-applied even once, use `make tf-apply` instead — it
adopts the Cloud KMS resources GCP refuses to delete, which a bare
`terraform apply` fails on. See [Teardown and re-apply](#teardown-and-re-apply).

### The `image_tag` rule

`image_tag` (default `latest`) overrides both the operator and platform-agent
image tags. It exists because the chart is installed from this checkout, and a
checkout's `Chart.yaml` carries an `appVersion` placeholder that never matches
a published image tag — so the chart's usual tag defaulting cannot work here
(see the [chart README](../../../charts/kube-agents/README.md)). `latest` is
fine for evaluation; pin an `X.Y.Z` release tag for production.

### IAM roles (`permission_set` and `project_roles`)

`permission_set` names one of the bundles
[`k8s-operator/scripts/provision_04_gcp_iam.sh`](../../../k8s-operator/scripts/provision_04_gcp_iam.sh)
grants, using the same vocabulary as the scripts' `PLATFORM_AGENT_PERMISSION_SET`:

| `permission_set`      | Roles granted                                           |
| --------------------- | ------------------------------------------------------- |
| `read-only` (default) | `local.read_only_roles` in [`main.tf`](main.tf)         |
| `gke-admin`           | `local.gke_admin_roles` in [`main.tf`](main.tf)         |
| `custom`              | whatever `project_roles` lists — setting it is required |

Both lists are copied verbatim from the script and are the values the parity
check compares; read them there rather than from this page.

`project_roles` still wins when set, whatever `permission_set` says, so an
existing configuration keeps the roles it had. `project_roles = []` grants
nothing and leaves IAM to you (the agent fails every GCP call until an
equivalent set exists). Deliberately no admin list is pre-staged in
`terraform.tfvars.example` — widening access should be an explicit, reviewed
choice.

### Backups

`enable_backup_agent` (default `true`) turns on the Backup for GKE addon,
matching the cluster `provision_01_gcp_cluster.sh` creates. It costs nothing on
its own. `enable_gke_backup_plan = true` then adds the scheduled plan that
`provision_12_gke_backup_plan.sh` creates — opt-in in both paths, because
backups are billed per backed-up pod and per GB of snapshot storage.

Backups include Kubernetes Secrets and persistent volume data, so the agent's
credentials are inside every snapshot: restrict backup/restore IAM to
administrators already allowed to read them, and set `backup_encryption_key`
for CMEK.

Turning the plan back off is not symmetric with turning it on: a BackupPlan
cannot be deleted while it still owns backups, so `terraform destroy` — and
setting `enable_gke_backup_plan = false` again, and changing
`backup_encryption_key` — fails on that resource until the backups are purged.
`make tf-destroy` purges them for you; the
[module README](../../modules/gke-backup-plan/README.md#teardown-is-not-symmetric)
has the commands for the other two cases, which no teardown script covers.

### cert-manager

The operator's admission webhooks — defaulting, validation, and the
delete-protection tripwire on the `PlatformAgent` CR — need a serving
certificate, and cert-manager is what issues it. `enable_cert_manager`
(default `true`) installs it as its own `helm_release` at
`cert_manager_version`, the version `provision_03_gcp_gke_operator.sh` applies;
`enable_webhooks` (default `true`) then turns the webhooks on in the chart.

Three differences from the script path are worth knowing:

- **This is not idempotent against an existing install.** `provision_03` skips
  cert-manager when a `cert-manager-webhook` Deployment is already available;
  Terraform does not look, and the apply fails on the CRDs that are already
  there. Set `enable_cert_manager = false` on such a cluster — the webhooks
  keep working, they just use the cert-manager that is already installed.
- **Destroying takes the CRDs with it**, and therefore every `Certificate`,
  `Issuer`, and `ClusterIssuer` in the cluster — not only the ones this
  composition created. On any cluster that shares cert-manager with another
  workload, install it separately and set `enable_cert_manager = false`.
- **Leader election moves rather than switching off.** The script patches
  `--leader-elect=false` onto the Autopilot deployments because cert-manager's
  leases default to `kube-system`, which Autopilot restricts. This sets
  `global.leaderElection.namespace = "cert-manager"`, which clears the same
  restriction without giving up the lock.

The chart's `failurePolicy` stays at its default of `Ignore` here. Helm applies
the webhook configurations before both the `Certificate` and the
`PlatformAgent` CR, so `Fail` would have the API server reject this
composition's own CR on the first apply. See the
[chart README](../../../charts/kube-agents/README.md) for switching it to
`Fail` afterwards.

### Google Chat and GitHub integrations

With `enable_google_chat = true` the composition provisions the GCP backend
(topic, subscription, IAM) **and** enables the CR's `googleChat` integration
with the created topic/subscription — restrict access with
`google_chat_allowed_users` (empty = everyone).

Set `github_repo` to wire the agent's GitOps target repository
(`spec.integration.github.gitRepo`).

`enable_slack = true` writes `slack_bot_token` / `slack_app_token` into the
credentials Secret and turns on the CR's `slack` section, the same pair
`provision_06_slack.sh` collects. Slack needs no GCP resources, so this is
purely configuration — the Slack app itself is a manual step (below).

**Manual steps that no IaC can perform** — canonical walkthrough:
[INSTALL.md § Enable Google Chat & Slack Integrations](../../../INSTALL.md#step-5-enable-google-chat--slack-integrations-manual-required-steps):

- **Google Chat:** register the Chat app on the Chat API configuration page —
  select Cloud Pub/Sub and enter the created topic (the `chat_topic_name`
  output, as `projects/<project>/topics/<topic>`), set visibility, and verify
  a **Service account email** appears under Connection settings after saving
  (if it stays blank, Chat silently delivers no events). Then DM the bot; on
  first contact, optionally approve the pairing code via
  `hermes pairing approve google_chat <CODE>` in the gateway pod.
- **Slack:** in the Slack app console enable Socket Mode and grant the bot
  scopes listed in the walkthrough, then pass the resulting tokens as
  `slack_bot_token` / `slack_app_token`; pairing approval works the same way
  (`hermes pairing approve slack <CODE>`).

## Standalone use outside this repository

This example sources the modules by relative path because it lives in the same
repository. A standalone consumer would pin a release instead:

```hcl
module "gke_cluster" {
  source = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=1.2.0"
  # ...
}
```

(and likewise for `kube-agents-iam`, `chat-pubsub`, `github-minter`, and
`gke-backup-plan`), and
would install the chart from the OCI registry rather than a local path — see
the [chart README](../../../charts/kube-agents/README.md).

## Teardown and re-apply

Use Terraform for teardown; do not mix this install path with the shell provisioner's
`teardown_*.sh` scripts. In particular, `teardown_08_deploy_platform_agent.sh` removes the
Terraform-managed `kube-agents-host` label out of band and causes plan drift.

Four things in this stack are not symmetric — applying them is not the inverse
of destroying them — and each one breaks a plain `terraform destroy`, or the
`terraform apply` that follows it. [`lifecycle.sh`](lifecycle.sh) handles all
four, so the cycle is repeatable:

```bash
make tf-destroy     # or: ./terraform/examples/full-install/lifecycle.sh destroy
make tf-apply       # or: ./terraform/examples/full-install/lifecycle.sh apply
```

What each one does that raw Terraform cannot:

| Asymmetry                                                            | Handled by                                                                    |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| KMS key rings and keys can never be deleted, so the next apply 409s  | `tf-apply` imports the survivors before applying (`lifecycle.sh adopt-kms`)   |
| The `PlatformAgent` finalizer strands the CR and hangs the namespace | `tf-destroy` deletes the CR and waits, force-clearing the finalizer if wedged |
| A `BackupPlan` cannot be deleted while it owns backups               | `tf-destroy` purges the plan's backups first                                  |
| `deletion_protection = true` cannot be overridden by a destroy alone | `tf-destroy` applies it as `false`, then destroys                             |

The chart also carries a `pre-delete` hook that removes the CR and waits for
its finalizer, so a plain `helm uninstall` is safe on its own; `tf-destroy`
does it up front anyway, which turns the hook into a no-op. Disable it with
`platformAgent.cleanupHook.enabled=false`.

Running `terraform destroy` directly still works, but you own the four steps
above yourself — starting with `kubectl delete platformagent <name> -n
kubeagents-system --wait` while the operator is still running, and setting
`deletion_protection = false` and applying before the cluster can be removed.

> [!WARNING]
> Destroying also uninstalls cert-manager when this composition installed it,
> and that removes its CRDs — deleting every `Certificate`, `Issuer`, and
> `ClusterIssuer` in the cluster, including any another workload owns. Only the
> cluster this composition created is normally affected, since it is destroyed
> too; the case to watch is `enable_cert_manager = true` pointed at a cluster
> you did not create here.

> [!NOTE]
> Cloud KMS key rings and crypto keys (for GKE CMEK and optional GitHub minter)
> cannot be deleted from GCP — `terraform destroy` only removes them from state,
> and they stay in the project forever. `make tf-apply` imports them back
> automatically. Applying with bare `terraform apply` after a destroy fails with
> a 409 until you either import them yourself or choose new key/keyring names.
