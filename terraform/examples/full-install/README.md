# Full install (Terraform root composition)

A single `terraform apply` that provisions everything a running Platform Agent
needs. This composition **is** the install engine: the repository-root
`install.sh` generates its `terraform.tfvars` and drives it through
`lifecycle.sh`, and applying it by hand with your own tfvars is the same
install without the interview.

## What it provisions

- The required Google APIs (`google_project_service`, never disabled on
  destroy), including the Cloud KMS API for GKE database encryption and the Chat
  API when Google Chat is enabled.
- A GKE cluster ([`gke-cluster`](../../modules/gke-cluster) module) — Autopilot
  by default, `cluster_mode = "standard"` for an e2-standard-4 node pool
  (with an optional gVisor node pool), or `create_cluster = false` to
  install onto an existing one — with Workload Identity, Cloud KMS database
  encryption (CMEK), the Backup for GKE agent enabled, and the
  `kube-agents-host=true` discovery label applied.
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
  via `helm_release`, pinned in `cert_manager_version`.
  It issues the serving certificate for the operator's admission
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
- Optionally (`enable_pubsub_platform = true`) the Cloud Pub/Sub platform adapter
  `AgentPlugin/pubsubplatform` for message and alert ingress.
- Optionally (`enable_stockout_investigator = true`) the GKE Stockout Investigator
  backend and `AgentPlugin/gkestockoutinvestigator`: Pub/Sub topic (`stockout_pubsub_topic`),
  subscription (`stockout_pubsub_subscription`), Cloud Logging project sink
  (`stockout_pubsub_sink`), and publisher IAM binding.
- Optionally (`model_provider = "vertex_ai"`) the Vertex AI / Model Garden path:
  a second [`kube-agents-iam`](../../modules/kube-agents-iam) instantiation for
  the gateway's service account, `roles/aiplatform.user` on
  `vertex_project_id`, and the Workload Identity annotation the chart needs.
  Vertex takes no API key, so no `*_api_key` variable applies. The API
  enablement and the role grant are the only two resources that live in
  `vertex_project_id` rather than `project_id`; when that is a project the
  applying identity cannot administer, set
  `vertex_manage_serving_project = false` and enable the API and make the
  grant by hand — the service account and its Workload Identity binding are
  still created here. Decide it at the first apply: turning it off later, on
  an install whose earlier apply created the two, destroys them on the next
  apply and revokes the grant. To hand them over instead, remove both from
  state first:

  ```bash
  terraform state rm 'google_project_service.vertex_ai[0]' \
    'google_project_iam_member.litellm_vertex_user[0]'
  ```

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

### Remote state

The composition ships no backend block, so a hand-driven apply uses local
state in this directory. For an install whose state must outlive the checkout
— anything driven by `install.sh`, whose companion `uninstall.sh` and
`upgrade.sh` run from fresh clones — set `KUBE_AGENTS_STATE_BUCKET` before any
`lifecycle.sh` subcommand:

```bash
KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh apply
```

`auto` derives the bucket name `<project_id>-kube-agents-tfstate`; any other
value is used verbatim. `apply` and `destroy` create the bucket on first use —
versioned, with uniform bucket-level access, in the cluster's region. `plan`
does not: it stops instead, because a plan that creates the backend has both
changed the project and answered the wrong question, since an empty bucket
plans the whole composition as new and reads as total drift. A gitignored
`backend_override.tf` points Terraform at
`gs://<bucket>/<prefix>`, where the prefix defaults to
`kube-agents/<cluster_name>` (override with `KUBE_AGENTS_STATE_PREFIX`) so two
installs in one project keep separate state. Versioning is the recovery story:
a corrupted or mistakenly-overwritten state file can be rolled back to a prior
generation by copying it over the live object (`gcloud storage ls -a` lists the
generations; `gcloud storage restore` is for soft-deleted objects, which is a
different feature — with versioning on, a previous generation is a noncurrent
version rather than a soft-deleted object):

```bash
gcloud storage ls -a gs://<bucket>/<prefix>/default.tfstate
gcloud storage cp gs://<bucket>/<prefix>/default.tfstate#<generation> \
  gs://<bucket>/<prefix>/default.tfstate
```

If the state is gone entirely,
re-run `lifecycle.sh apply` against the same tfvars — KMS adoption is
automatic, and `terraform import` covers the rest.

### Asking what an apply would change

```bash
KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh plan -detailed-exitcode
```

Read-only, and every part of that is deliberate: no state lock, so it can
neither block nor be blocked by the apply it is reporting on; no bucket
creation; and none of the adoption imports `apply` runs, since those write
state. An install that needs adoption therefore shows those resources as "to
create", which is what a plan alone can honestly say about them.

`-detailed-exitcode` makes the answer machine-readable — 0 for in sync, 2 for
there are changes, 1 for a plan that failed — which is what the scheduled drift
report reads. `./upgrade.sh --plan`, run from the install's own checkout, is the
front door: it renders the tfvars an upgrade would apply and then calls this.

### Recovering from an interrupted apply

An apply killed part-way — Ctrl-C, a dropped connection, a laptop lid — leaves
state locked, and the next command fails with `Error acquiring the state lock`.
`terraform force-unlock` is the release, and it works here; what trips people up
is which identifier it wants. On the `gcs` backend the lock ID is the **GCS
generation number** of the lock object, not a UUID, and it is the `ID:` in the
error Terraform just printed:

```text
Lock Info:
  ID:        1787242876096737
  Path:      gs://<bucket>/<prefix>/default.tflock
```

```bash
terraform force-unlock 1787242876096737
```

Run it from a directory whose backend is already configured, or it will not
reach the lock at all. `force-unlock` acts on whatever backend the working
directory has, this composition ships no backend block, and `backend_override.tf`
is gitignored and written only by `lifecycle.sh` — so in a checkout that has
never been through `lifecycle.sh`, Terraform is on local state and refuses with a
local-state error that never mentions GCS. That matters here more than it looks:
`install.sh`, `uninstall.sh` and `upgrade.sh` all drive the apply from a
disposable clone, so the directory that took the lock is routinely gone by the
time anyone goes looking for it. The fix is the same one the `BackupPlan` import
below needs — `KUBE_AGENTS_STATE_BUCKET=<same value> KUBE_AGENTS_STATE_PREFIX=<same value> ./lifecycle.sh adopt-kms`
first, or work in a directory `lifecycle.sh` has already initialised.

Pass a UUID from anywhere else — the `"ID"` field inside the lock object's own
JSON, for instance — and it refuses with `Lock ID should be numerical value`,
which reads like the backend not supporting `force-unlock` at all. It does.

Establish that nothing is still applying before you run it. The ID in the error
is the generation of the lock as it exists at that moment, so `force-unlock`
matches it and releases it whether the holder is a dead process or a colleague's
apply still running — the interactive confirmation is the only thing in the way,
and `-force` removes that too. What the generation check does catch is a
**stale** ID: one carried over from an earlier error, after which the lock was
released and re-taken. Pass that and the command refuses rather than breaking a
lock you were not looking at. That is the reason to prefer it to
`gcloud storage rm gs://<bucket>/<prefix>/default.tflock`, which deletes the
object whatever its generation and is the fallback for a lock whose ID you no
longer have. `<bucket>` and `<prefix>` are the ones from
[Remote state](#remote-state) — `<project_id>-kube-agents-tfstate` and
`kube-agents/<cluster_name>` unless `KUBE_AGENTS_STATE_PREFIX` overrode it.

A connectivity drop mid-apply produces two failures worth recognising, because
neither means what it looks like:

- **`http2: client connection lost` on the state upload.** Terraform retries this
  itself and the retry usually succeeds, so the run can report a state-write
  failure it then recovered from. Check the bucket before concluding state was
  lost — and remember versioning is on, so a bad write can be rolled back to the
  previous generation as above.
- **A `BackupPlan` create that fails while polling its operation.** The
  long-running operation keeps going server-side, so the plan is often `READY`
  even though Terraform recorded a failure and holds nothing in state. Check with
  `gcloud beta container backup-restore backup-plans describe`, then import it and
  re-apply rather than deleting the plan to let Terraform recreate it.

  Import into the same state the apply used, which on a remote-state install
  means the backend has to be configured first. `backend_override.tf` is
  gitignored and written only by `lifecycle.sh`, so in a checkout that has
  `terraform.tfvars` but no override — one `install.sh` wrote into and something
  later cleaned, say — a bare `terraform import` silently writes a **local**
  `terraform.tfstate`, reports success, and leaves the next apply still trying to
  create the plan. Run
  `KUBE_AGENTS_STATE_BUCKET=<same value> ./lifecycle.sh adopt-kms` first — it is
  the cheapest subcommand that initialises the backend — or work in a directory
  `lifecycle.sh` has already initialised. Carry `KUBE_AGENTS_STATE_PREFIX` across
  as well if the install set one. `ensure_backend` derives the prefix
  independently of the bucket and inits with `-reconfigure`, so an omitted
  override points the backend at the default `kube-agents/<cluster_name>` object,
  and the import lands in an empty state that reports success while the real one
  still has no plan.

  `terraform.tfvars` is gitignored too, so a literally fresh clone has neither
  file and fails earlier and more loudly: `ensure_backend` evaluates
  `var.project_id` before it can write the override, and `lifecycle.sh` exits
  with "could not evaluate var.project_id". Restore the tfvars the install used
  before either recipe.

  The import itself needs the placeholder Helm provider `adopt-kms` writes for
  its own imports, and `lifecycle.sh` exposes no generic import subcommand to
  borrow — so write it yourself. `terraform import` configures every provider
  before it does anything, and the `helm` provider here is built from
  `module.gke_cluster.cluster_endpoint`; the override was needed in practice even
  with the cluster already in state. The filename suffix is what makes Terraform
  treat it as an override, so keep it:

  ```bash
  cat > providers_lifecycle_override.tf <<'EOF'
  provider "helm" {
    kubernetes = {
      host  = "https://127.0.0.1"
      token = "placeholder"
    }
  }
  EOF
  terraform import 'module.gke_backup_plan[0].google_gke_backup_backup_plan.this' \
    "projects/<project>/locations/<region>/backupPlans/<cluster_name>-backup-plan"
  rm -f providers_lifecycle_override.tf
  ```

  Remove the override before the next apply — it is never meant to survive an
  import, which is why `lifecycle.sh` deletes it on an `EXIT` trap.

### The `image_tag` rule

`image_tag` (default `latest`) overrides both the operator and platform-agent
image tags. It exists because the chart is installed from this checkout, and a
checkout's `Chart.yaml` carries an `appVersion` placeholder that never matches
a published image tag — so the chart's usual tag defaulting cannot work here
(see the [chart README](../../../charts/kube-agents/README.md)). `latest` is
fine for evaluation; pin an `X.Y.Z` release tag for production.

### Installing from a mirrored registry

For a cluster that may only pull from an approved registry, copy the images
there first — `make mirror-images MIRROR_PREFIX=<prefix> IMAGE_TAG=<tag>` from
the repository root, driven by `images.json` — then set `image_registry` to the
same prefix. It reaches the two images the chart never renders as well (the
agent Deployment and the fluent-bit sidecar the operator resolves at reconcile
time); the [chart README](../../../charts/kube-agents/README.md) explains how.
Add `third_party_image_registry` only if the mirror keeps LiteLLM and
fluent-bit under a different path.

`IMAGE_TAG` is not optional here. The four first-party images take whatever tag
the mirror step was given (`latest` if it was given none), while Terraform asks
for `image_tag` — so a mirror populated at `latest` against an `image_tag` of
`v1.2.3` holds no reference the install will ever request. `terraform apply`
reports success and the pods sit in ImagePullBackOff. Pass the same value to
both.

A mirror the nodes cannot read on their own — Harbor or Artifactory with token
auth, rather than an Artifact Registry in the same project — needs
`image_pull_secrets` as well. It takes Secret names, and the Secrets are
referenced rather than created, which is what keeps registry credentials out of
Terraform state. Create them before applying, and create the namespace first:
`create_namespace = true` on the release means Helm has not made it yet.

```bash
kubectl create namespace kubeagents-system
kubectl create secret docker-registry regcred \
  --namespace kubeagents-system \
  --docker-server=harbor.example.com \
  --docker-username=robot\$kube-agents \
  --docker-password="$TOKEN"
```

Both are idempotent against what Helm then finds. The names reach every pod the
chart renders and, through `IMAGE_PULL_SECRETS` on the operator and
`spec.deployment.imagePullSecrets` on the `PlatformAgent`, the agent pods the
operator renders too.

**cert-manager images follow the same prefix, but not the same credentials.**
`helm_release.cert_manager` is a separate release of an upstream chart and
never sees the `helm_release.kube_agents` values, but the composition passes
it the same registry through its own image overrides
(`local.cert_manager_mirror_values`), so its five images
(`cert-manager-controller`, `-cainjector`, `-webhook`, `-acmesolver`,
`-startupapicheck`) are pulled as `<prefix>/<name>:<tag>` — the layout
`make mirror-images` writes from `images.json`, which carries all five
entries. `image_pull_secrets` does **not** reach it, so a mirror that needs
credentials means installing cert-manager yourself (below). Also not covered
is the chart itself: it is fetched over the network from
`https://charts.jetstack.io`, which an air-gapped runner cannot
reach at all. On such a runner, set `enable_cert_manager = false` and install
cert-manager yourself from the mirror before applying. `enable_webhooks`
needs cert-manager present either way; the composition's `depends_on` only
orders the release it manages, so with `enable_cert_manager = false` it is on
you to have cert-manager serving first.

### IAM roles (`permission_set` and `project_roles`)

`permission_set` names one of the agent's GCP IAM role bundles (the same
vocabulary the installer's `--permission-set` flag uses):

| `permission_set`      | Roles granted                                           |
| --------------------- | ------------------------------------------------------- |
| `read-only` (default) | `local.read_only_roles` in [`main.tf`](main.tf)         |
| `custom`              | whatever `project_roles` lists — setting it is required |

There is no admin bundle. `roles/container.admin` authorizes the agent
through IAM regardless of its Kubernetes RBAC, and the
`container.clusters.impersonate` it carries applies to every cluster in the
project, so it is not something a one-word setting should hand out; see
[Security & IAM](../../../docs/site/src/content/docs/reference/security-and-iam.md).
Widening access means naming the roles in `project_roles`, where the grant
is explicit and reviewed.

The list lives in [`main.tf`](main.tf); read it there rather than from this
page.

`project_roles` still wins when set, whatever `permission_set` says, so an
existing configuration keeps the roles it had. `project_roles = []` grants
nothing and leaves IAM to you (the agent fails every GCP call until an
equivalent set exists). Deliberately no admin list is pre-staged in
`terraform.tfvars.example` — widening access should be an explicit, reviewed
choice.

### Backups

`enable_backup_agent` (default `true`) turns on the Backup for GKE addon. It
costs nothing on its own. `enable_gke_backup_plan = true` then adds the
scheduled plan — opt-in, because backups are billed per backed-up pod and per
GB of snapshot storage.

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
has the commands for the other two cases, which nothing automates.

### cert-manager

The operator's admission webhooks — defaulting, validation, and the
delete-protection tripwire on the `PlatformAgent` CR — need a serving
certificate, and cert-manager is what issues it. `enable_cert_manager`
(default `true`) installs it as its own `helm_release` at
`cert_manager_version`;
`enable_webhooks` (default `true`) then turns the webhooks on in the chart.

Three behaviours are worth knowing:

- **This is not idempotent against an existing install.** install.sh probes an
  existing cluster for a `cert-manager` Deployment in the `cert-manager`
  namespace and turns this off when it finds one; a hand-written tfvars does
  not get that probe, and the apply fails on the CRDs that are already there.
  Set `enable_cert_manager = false` on such a cluster — the webhooks keep
  working, they just use the cert-manager that is already installed.
- **Destroying takes the CRDs with it**, and therefore every `Certificate`,
  `Issuer`, and `ClusterIssuer` in the cluster — not only the ones this
  composition created. On any cluster that shares cert-manager with another
  workload, install it separately and set `enable_cert_manager = false`.
- **Leader election moves rather than switching off.** cert-manager's leases
  default to `kube-system`, which Autopilot restricts. This sets
  `global.leaderElection.namespace = "cert-manager"`, which clears the
  restriction without giving up the lock.

The chart's `failurePolicy` stays at its default of `Ignore` here. Helm applies
the webhook configurations before both the `Certificate` and the
`PlatformAgent` CR, so `Fail` would have the API server reject this
composition's own CR on the first apply. See the
[chart README](../../../charts/kube-agents/README.md) for switching it to
`Fail` afterwards.

### Reaching the control plane (`allow_external_dns_traffic`)

`allow_external_dns_traffic` (default `false`) is passed to the `gke-cluster`
module and decides whether the cluster's DNS-based control plane endpoint
serves traffic from outside the VPC. Set it to `true` for a cluster a Platform
Agent running elsewhere has to reach; leave it alone for a cluster that should
stay VPC-only. The default is `false` so that applying an existing root after
upgrading does not publish an endpoint on a cluster that has none — see the
[module README](../../modules/gke-cluster/README.md) for why that endpoint is
not covered by master-authorized-networks.

### Google Chat, Slack, and GitHub integrations

With `enable_google_chat = true` the composition provisions the GCP backend
(topic, subscription, IAM) **and** enables the CR's `googleChat` integration
with the created topic/subscription — restrict access with
`google_chat_allowed_users` (empty = everyone).

With `enable_github_minter = true`, set `github_repo` to your primary GitOps repository (in `owner/repo` or GitHub URL format). Additional GitOps repositories within the same organization can also be registered in the ConfigMap by cluster administrators.

`enable_slack = true` writes `slack_bot_token` / `slack_app_token` into the
credentials Secret and turns on the CR's `slack` section, the same pair
install.sh collects. Slack needs no GCP resources, so this is
purely configuration — the Slack app itself is a manual step (below).

### Pub/Sub Platform and Stockout Investigator Plugins

- `enable_pubsub_platform = true` deploys the `pubsub-platform` adapter
  `AgentPlugin/pubsubplatform` via Helm, giving the Platform Agent Cloud Pub/Sub
  ingress capabilities.
- `enable_stockout_investigator = true` provisions the GCP Pub/Sub topic
  (`stockout_pubsub_topic`), pull subscription (`stockout_pubsub_subscription`),
  and Log Router sink (`stockout_pubsub_sink`), and deploys the
  `AgentPlugin/gkestockoutinvestigator` resource targeting the `platform` profile
  with tuned execution limits. Requires `enable_pubsub_platform = true`.

If a plugin was previously installed using the standalone `agentplugins/*/install.sh` scripts,
uninstall its standalone release before setting these variables (`helm uninstall pubsubplatform -n <namespace>`,
`helm uninstall gkestockoutinvestigator -n <namespace>`). Helm checks object ownership metadata
(`meta.helm.sh/release-name`) and refuses to adopt existing resources owned by another release.

**Manual steps that no IaC can perform** — canonical walkthrough:
[INSTALL.md § Enable Google Chat & Slack Integrations](../../../INSTALL.md#step-4-enable-google-chat--slack-integrations-manual-required-steps):

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

Use `lifecycle.sh destroy` for teardown; anything that mutates the
Terraform-managed resources out of band (for instance removing the
`kube-agents-host` label by hand) causes plan drift the next apply reverts.

Several things in this stack are not symmetric — applying them is not the
inverse of destroying them — and each one breaks a plain `terraform destroy`, or
the `terraform apply` that follows it. [`lifecycle.sh`](lifecycle.sh) handles
them, so the cycle is repeatable:

```bash
make tf-destroy     # or: ./terraform/examples/full-install/lifecycle.sh destroy
make tf-apply       # or: ./terraform/examples/full-install/lifecycle.sh apply
```

What each one does that raw Terraform cannot:

| Asymmetry                                                                | Handled by                                                                                                                                   |
| ------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------- |
| KMS key rings and keys can never be deleted, so the next apply 409s      | `tf-apply` imports the survivors before applying (`lifecycle.sh adopt-kms`)                                                                  |
| The `PlatformAgent` finalizer strands the CR and hangs the namespace     | `tf-destroy` deletes the CR and waits, force-clearing the finalizer if wedged                                                                |
| A `BackupPlan` cannot be deleted while it owns backups                   | `tf-destroy` purges the plan's backups first                                                                                                 |
| `deletion_protection = true` cannot be overridden by a destroy alone     | `tf-destroy` applies it as `false`, then destroys                                                                                            |
| A Pub/Sub topic or subscription that already exists makes the create 409 | `tf-apply` imports it first (`adopt_pubsub`), so a topic created in the Cloud console while wiring up Google Chat does not block the install |

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
