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
  by default, or `cluster_mode = "standard"` for an e2-standard-4 node pool
  (with an optional gVisor node pool) — with Workload Identity, Cloud KMS
  database encryption (CMEK), the Backup for GKE agent enabled, and the
  `kube-agents-host=true` discovery label applied. Setting
  `create_cluster = false` instead makes the module read an existing cluster:
  it enables none of those, and its postconditions refuse the plan unless the
  cluster already has Workload Identity and NetworkPolicy enforcement (see
  [Prerequisites](#prerequisites)).
- Optionally (`enable_gke_backup_plan = true`) a scheduled
  [`gke-backup-plan`](../../modules/gke-backup-plan) for the release namespace.
- The agent's GCP identity ([`kube-agents-iam`](../../modules/kube-agents-iam)
  module): a service account (`kubeagents-platform-gsa` by default; a second
  install in the same project sets `agent_service_account_id` — see
  [Remote state](#remote-state)), its read-only project roles, and the Workload
  Identity binding to the agent KSA (`agent_ksa_name`,
  `kubeagents-platform-agent` by default; see
  [IAM roles](#iam-roles-permission_set-and-project_roles) below), and, when
  `scope` names a Shared VPC host or Metrics Scope, the read grants in the projects
  the [`kube-agents-scope-resolver`](../../modules/kube-agents-scope-resolver)
  module resolves them to at plan time.
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
  `model_default_name` overrides the per-provider default model;
  `model_max_tokens` (default `0`, meaning none) sets the output-token budget
  the gateway asks for on a request that names none; `litellm_redaction`
  (off by default) redacts every request body the gateway forwards to the
  provider, and `install.sh` sets it from the `LITELLM_REDACTION_*` keys in
  `install.env`.
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
- Optionally (`enable_drift_pubsub = true`) the drift detector's audit-log
  ingress ([`drift-pubsub`](../../modules/drift-pubsub) module): a Log Router
  sink exporting GKE audit logs (`drift_pubsub_sink`), the drift-audit Pub/Sub
  topic (`drift_pubsub_topic`) and pull subscription
  (`drift_pubsub_subscription`), the sink-writer IAM on the topic, and
  publisher on it for anything `drift_pubsub_topic_publishers` names — plus
  the agent GSA's subscription grants, which this composition makes beside the
  module rather than inside it; and, with
  `enable_drift_detector = true` alongside it, the
  `spec.harness.driftDetector.enabled` field that starts the consumer. See
  [Drift audit-log ingress](#drift-audit-log-ingress).
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
> marked `sensitive`, which redacts them where Terraform prints the variables
> themselves — but not in `helm_release.kube_agents`'s `metadata` attribute,
> which repeats every chart value and which the helm provider does not mark
> sensitive. `lifecycle.sh` hides that block from `plan`, `apply` and
> `destroy`, except for an `apply` that will ask for approval at a terminal, so
> that its prompt shows; a raw `terraform plan`, `apply`, `destroy` or `show`
> prints it.
> Like every secret passed through Terraform, they are also stored **in
> plaintext in the Terraform state**.
> The two generated `SESSION_KV_*` values live in state for the same reason.
> Keep the state in a protected backend (e.g. a GCS bucket with tight IAM),
> not on a shared disk or in version control.

## Prerequisites

- A GCP project you can administer.
- Terraform `~> 1.5`.
- With `create_cluster = false`, a cluster that meets the
  [cluster requirements](../../../docs/site/src/content/docs/install/prerequisites.md#cluster-requirements):
  GKE 1.29+, Workload Identity with `GKE_METADATA` on every node pool,
  NetworkPolicy enforcement, a control plane reachable from here, and cert-manager
  either present (`enable_cert_manager = false`) or absent. The module refuses the
  plan on two of these, the Workload Identity pool and NetworkPolicy enforcement,
  and checks none of the others; `install.sh` changes an adopted cluster to meet
  those two instead, and this composition on its own never does. The NetworkPolicy
  refusal alone can be waived: `accept_no_network_policy = true` installs onto a
  cluster that enforces none, leaves it as it is, and stamps the
  `kubeagents.x-k8s.io/network-policy-enforcement: absent-accepted` annotation onto
  the `PlatformAgent` so the choice is readable later. Every NetworkPolicy the
  install ships is then inert, including the ones that confine the agent's shell
  sandbox.
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
`upgrade.sh` may run from a fresh clone, a release bundle, or the install
checkout in `$HOME/kube-agents` — set `KUBE_AGENTS_STATE_BUCKET` before any
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
installs in one project keep separate state. State is only half of the
second-install story: every service account the composition creates has one
fixed default name per project, so the second install must name its own —
`agent_service_account_id`, and `github_minter_service_account_id` or
`litellm_service_account_id` when it enables the minter or serves from Vertex —
or its first apply stops on the account the first install owns. Through the
installer front doors that means `PLATFORM_AGENT_GSA_NAME`,
`GITHUB_MINTER_GSA_NAME` and `LITELLM_GSA_NAME` in `install.env`, which every
front door regenerates `terraform.tfvars` from; every front door that applies
(`install.sh`, its Day-2 menu, `upgrade.sh`) checks for the collision first and
names the key to set. Do not rely on a shell
`export` or a hand-edited `terraform.tfvars` instead: the export dies with the
shell and the file is regenerated on every run, and either way the next run
resolves the name back to the default and plans the GSA's destroy-and-recreate
under `-auto-approve` — which `lifecycle.sh`'s `guard_gsa_identity` refuses. The
release namespace (`NAMESPACE` in `install.env`) has the same guard,
`guard_release_namespace`, because `helm_release` treats it as ForceNew too, and
so do the CMEK key ring and key names (`GKE_DB_KMS_KEYRING` / `GKE_DB_KMS_KEY`,
`guard_kms_identity`): on a cluster this state created, a renamed key would be
destroyed and recreated, which schedules the live key's versions for destruction.
A key is rotated in Cloud KMS, not by renaming it here.
And a distinct GSA name un-collides creation, not identity: the Workload
Identity principal names a namespace and KSA project-wide, no cluster, so two
installs that share the namespace and the default KSA name bind the same
principal and each agent can mint the other's GSA tokens however differently
the GSAs are named. The second install names its own `agent_ksa_name` too. One
variable feeds both the module's binding and the chart's `serviceAccountName`,
so the pod and the binding move together, and it must end in `-agent`: the
`kube-agents-agent-binding-scope` admission policy the chart ships selects the
bindings it governs by that suffix on the bound ServiceAccount, so a name
outside it would leave this install's agent bindings unselected by that policy
and by any validation it gains. The validation in `variables.tf` refuses the
plan instead, and the variable's description says what the policy does and does
not deny today. `agent_ksa_name` has no `install.env` key of its own yet, so
through the front doors it is a `TF_VAR_agent_ksa_name=...` line in that file:
every front door sources it with `set -a`, and the generator never writes
`agent_ksa_name` into `terraform.tfvars`, so the passthrough is what Terraform
reads. The caution above applies to it with one gap — a lost line resolves the
KSA back to the default and re-shares the identity silently, and there is no
`guard_ksa_identity` to refuse that the way `guard_gsa_identity` refuses the
GSA's destroy-and-recreate. The `agent_service_account_id` description in
`variables.tf` carries the limits to read before relying on any of this.
The drift audit-log ingress has the same one-default-per-project shape, and
this one every second install meets, because `ENABLE_DRIFT_DETECTOR` defaults
to `true`: `drift_pubsub_topic`, `drift_pubsub_subscription` and
`drift_pubsub_sink` each default to one name, so the second install reaches its
first apply with all three already in the project and none of them in its
state. `lifecycle.sh apply` refuses it — `guard_drift_adoption`, before
Terraform runs — rather than importing them, because an import cannot tell a
trio an earlier install left behind from one another live install owns, and
adopting the second takes it over: this install's teardown then deletes all
three, retained records included, and until then both detectors pull the one
subscription, which Pub/Sub splits between them, so each install reports about
half the project's drift while both stay Ready. The guard names four ways out —
give this install its own three names (through the front doors,
`TF_VAR_drift_pubsub_topic=...` and the other two as lines in `install.env`,
since the generator writes none of them); import the trio back if it is this
install's own and the state is what went missing, a repointed
`KUBE_AGENTS_STATE_BUCKET` or a `terraform state rm` (it prints the
`terraform import` commands, and running one asserts the ownership the guard
could not establish); delete the leftovers if that is what they are (it prints
the `gcloud` commands); or leave the detector off for this install with
`ENABLE_DRIFT_DETECTOR=false`. The stockout trio
(`stockout_pubsub_*`) has the same shape but is still adopted, because
`enable_stockout_investigator` stays off unless asked for: a second install
that turns it on names its own three. Renaming a subscription already in state is a
different failure, and `guard_pubsub_subscription` refuses it for the Chat,
drift and stockout subscriptions alike, while the feature's flag is on: `name`
and `topic` are ForceNew on `google_pubsub_subscription`, so the apply would
destroy and recreate the subscription — and the topic with it, where the topic
name is what changed — under `-auto-approve`, dropping whatever it had not
acknowledged: the Chat events, the stockout alerts, or the GKE audit records
the drift detector exists to report.
Versioning is the recovery story:
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

If the state is gone entirely, import the cluster back before anything else —
`terraform import 'module.gke_cluster.google_container_cluster.<autopilot|standard>[0]' projects/<project>/locations/<location>/clusters/<cluster_name>`,
with the two overrides the BackupPlan recipe below writes — and then re-run
`lifecycle.sh apply` against the same tfvars: KMS adoption is automatic, and
`terraform import` covers the rest. Without that import the apply is refused up
front (`guard_cluster_ownership`, [below](#recovering-from-an-interrupted-apply))
rather than 409ing on the cluster halfway through. Through `install.sh` the
probe derives `create_cluster = false` instead and adopts the cluster.

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

  The import itself needs the two overrides `adopt-kms` writes for its own
  imports, and `lifecycle.sh` exposes no generic import subcommand to borrow —
  so write them yourself. `terraform import` configures every provider before
  it does anything, and the `helm` provider here is built from
  `module.gke_cluster.cluster_endpoint`; that override was needed in practice
  even with the cluster already in state. The same walk leaves every resource
  not in state unknown, so the scope resolver module's monitored-project
  lookup, whose `for_each` is keyed on a read the walk never makes, refuses the
  import (`Invalid for_each argument`) until it is pinned to an empty set, and
  the IAM module keys its scope bindings on that module's `members` output,
  unknown for the same reason once a Shared VPC host or Metrics Scope is
  declared. The second file pins both, the lookup to no instances and
  `members` to an empty list under each declared selector's name (the IAM
  module's precondition wants an entry per selector, so a bare `{}` would warn
  on every import), and goes into the module's own directory because
  Terraform merges override files per module. The filename suffix is what
  makes Terraform treat each as an override, so keep it:

  ```bash
  cat > providers_lifecycle_override.tf <<'EOF'
  provider "helm" {
    kubernetes = {
      host  = "https://127.0.0.1"
      token = "placeholder"
    }
  }
  EOF
  cat > ../../modules/kube-agents-scope-resolver/scope_resolver_lifecycle_override.tf <<'EOF'
  data "http" "scope_monitored_project" {
    for_each = toset([])
  }

  output "members" {
    value = merge(
      { for host in var.shared_vpc_hosts : "sharedVpcHosts/${host}" => [] },
      { for scope in var.metrics_scopes : "metricsScopes/${scope}" => [] },
    )
  }
  EOF
  terraform import 'module.gke_backup_plan[0].google_gke_backup_backup_plan.this' \
    "projects/<project>/locations/<region>/backupPlans/<cluster_name>-backup-plan"
  rm -f providers_lifecycle_override.tf \
    ../../modules/kube-agents-scope-resolver/scope_resolver_lifecycle_override.tf
  ```

  Remove both overrides before the next apply — they are never meant to
  survive an import, which is why `lifecycle.sh` deletes them on an `EXIT` trap
  and again at the start of every subcommand, and `install.sh --dry-run` deletes
  them before its own validate and plan. A plan or apply that merged the
  scope override would resolve every declared selector to no members and plan
  the removal of the bindings those members hold, and the resolver module's
  own `terraform test` suite would assert against the pin instead of the
  module, which is why `make terraform-test` refuses to run beside the file
  and names it.

- **A retry that would create a cluster that already exists.** State left by an
  apply that died before the cluster finished creating can hold a managed
  cluster entry that manages nothing, and a retry against it would plan a
  create over the live cluster and 409 halfway through. `lifecycle.sh apply`
  refuses that before Terraform runs (`guard_cluster_ownership`, in both
  directions) and names a way out per caller: `create_cluster = false` if the
  cluster is somebody else's to install onto; a `terraform import` of the
  cluster address if this state created it (a hand-written tfvars keeps
  `create_cluster = true`, so clearing the state alone reproduces the
  refusal); or, through `install.sh`, `uninstall.sh` or clearing the state
  under `gs://<bucket>/<prefix>/` and re-running `install.sh`, whose probe
  derives `create_cluster = false` for a live cluster outside state and adopts
  it. `install.sh` itself reaches this refusal only through a race. The other thing such a state holds is the cluster's CMEK key ring
  and key, adopted on the retry; with `create_cluster = false` the module no
  longer manages them, so `lifecycle.sh apply` forgets them from state
  (`forget_unmanaged_cluster_kms`) rather than let the apply schedule the key's
  versions for destruction under the live cluster.

### Applying over a Pub/Sub topic that already exists

`lifecycle.sh apply` adopts a pre-existing Google Chat Pub/Sub topic and
subscription rather than failing with `Error 409: Resource already exists`, the
way it already adopts KMS key rings (`adopt_pubsub`, beside `adopt_kms`).
Configuring the Chat app in the Cloud console creates the topic before the
installer runs, so this is reachable on a first install, not only on a
re-apply. Every Pub/Sub IAM binding in the
[`chat-pubsub`](../../modules/chat-pubsub/main.tf) module is keyed on its
parent's `.id`, never its `.name`, so a replaced topic takes its bindings into
the plan with it instead of leaving a green apply over an empty policy.

### The `image_tag` rule

`image_tag` (default `latest` on `main`) overrides both the operator and platform-agent
image tags. In CI/CD pipelines and automated testing, it is passed explicitly with the
commit SHA on which container images were built for testing, ensuring the deployment pulls
the exact matching artifacts. In official release bundles and release-tag checkouts, release
automation stamps this default directly with the released SemVer version (e.g. `0.4.0`).
It exists because the chart is installed from this checkout, and a checkout's `Chart.yaml`
carries an `appVersion` placeholder that never matches a published image tag — so the
chart's usual tag defaulting cannot work here (see the [chart README](../../../charts/kube-agents/README.md)).
For production, always pin a validated numeric SemVer release tag or full commit SHA.

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

### Projects, folders, organisations and selectors in scope (`scope`)

`scope` is the `PlatformAgent`'s `spec.scope`, declared once and reaching both halves of the
install from this one value: the `kube-agents-iam` module binds its read allowlist (the read
subset of `read_only_roles`, intersected with the roles the host project got) in every project
`scope.projects` names, and the chart renders the same object into the CR, so the IAM and the
declaration cannot name different projects, and the release waits for the bindings. The block is
rendered on every apply, empty lists included: an emptied `projects` list is the declaration that
drops projects (their read roles are revoked and their Cluster Agent profiles retire over the
reconcile's next two clean runs), and a missing block would declare nothing. `exclude.projects`
takes project IDs or shell-style globs, `exclude.clusters` the full `project_id`, `location`,
`cluster_name` triple; neither changes IAM, except that an entry naming a Shared VPC service project
by ID, or a monitored project by number, withholds its grant (below). Through the installer the value comes from
`SCOPE_PROJECTS`, `SCOPE_FOLDERS`, `SCOPE_ORGANIZATIONS`, `SCOPE_SHARED_VPC_HOSTS`,
`SCOPE_METRICS_SCOPES`, `SCOPE_MAX_PROJECTS`,
`SCOPE_EXCLUDE_PROJECTS` and `SCOPE_EXCLUDE_CLUSTERS` in `install.env`
([`scripts/installer/README.md`](../../../scripts/installer/README.md), which also says how to
forget the bindings of a project that became unreachable). If the running `PlatformAgent` already
declares `spec.scope` by hand, copy it into `scope` before the first apply of a composition that
has the variable: that apply renders the block for the first time and replaces the live lists
with `scope`'s, empty by default, after which the reconcile retires the dropped projects over two
clean runs. `upgrade.sh --upgrade-mode=full` refuses that apply until `install.env` records the
declaration; the composition run directly does not. A composition applied directly to an existing
install also applies no CRDs: run `kubectl --context gke_<project>_<region>_<cluster> apply
--server-side --force-conflicts -f charts/kube-agents/crds/` first, through the install's own context
as `upgrade.sh` does, or a `spec.scope` the served schema does not know is pruned on write and, the
release record then carrying it, never re-sent. The identity running the apply needs
to set IAM policy in each project named. The release's dependency on the module orders creation,
not IAM propagation: a first install's one-shot inventory sweep may name a scoped project as
`denied`, and the hourly reconcile creates its profiles once the grant has propagated.

`scope.folders` and `scope.organizations` take numeric Resource Manager IDs. Each is bound on the
container itself with the same allowlist plus `roles/cloudasset.viewer`, so every project beneath
it inherits the grant and the reconcile resolves its members with one Cloud Asset Inventory search;
a project created under a declared folder after the apply is discovered and readable with no
change here. Declaring one adds `cloudasset.googleapis.com` to the APIs the composition enables in
`project_id`; an install that names explicit projects alone never enables it. The identity running
the apply needs `resourcemanager.folders.setIamPolicy` on each folder or
`resourcemanager.organizations.setIamPolicy` on the organisation, which the installer front doors
check before the apply and the composition run directly does not. An organisation binding reaches
every project in the organisation; the design recommends folders until the scoped service account
pool grants authority ([`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md)
§9).

`scope.shared_vpc_hosts` and `scope.metrics_scopes` take project IDs: a Shared VPC host project,
whose attached service projects are in scope, and the scoping project of a Cloud Monitoring
Metrics Scope, whose monitored projects are. Neither is a Resource Manager container, so nothing
is inherited through them. The composition resolves each at plan time through the
[`kube-agents-scope-resolver`](../../modules/kube-agents-scope-resolver/README.md) module, with the
same three selector reads the reconcile makes each run (the Compute API for a host's service projects, the
Monitoring API for a scope's monitored projects, Resource Manager to name each of those, which the
Monitoring API returns by number), made with the google provider's own token so they are answered
for the identity that applies, and hands the members to the IAM module, which binds the allowlist
in every project resolved and in each scoping project, and `roles/compute.viewer` alone in a host
not otherwise in scope, which is all the reconcile's lookup of its service projects reads. The resolver is a module of its own, called without a `depends_on`, because the IAM
module's module-level `depends_on` would defer a read inside it to apply time and fail the plan on
a first install. A read that identity cannot make fails the plan before anything is applied,
naming the selector and the API's answer: it needs
`compute.projects.get` on a host, to read the Metrics Scope in its scoping project
(`roles/monitoring.metricsScopesViewer` is the narrowest role) with `monitoring.googleapis.com`
enabled there, and `resourcemanager.projects.get` on each monitored project; a monitored project
it cannot name, or one whose ID the scope cannot carry, is left out by naming its project number
in `exclude.projects`. The reads are billed to `project_id`, whose `cloudresourcemanager` and
`monitoring` APIs a Metrics Scope's use and whose `compute` API a Shared VPC host's does; the
composition enables them in the apply, per selector, so `install.sh` enables whichever the declared
selectors read is off before an apply that carries one; a 403 that names a disabled
API is reported with that remedy, and one that refuses the identity the consumer project
(`USER_PROJECT_DENIED`) with the `serviceusage.services.use` it needs there. The reconcile lists at
most `scope.max_projects` projects of the resolved set (100 by default; `spec.scope.maxProjects` on the CR), the management project included, so a declaration whose
management project, `projects` and selector members together exceed that (once each, less an exact
`exclude.projects` entry; a project both in `projects` and excluded by its number stays counted, so drop
it from `projects`) is refused at plan rather than bound in full while a selector is declared or the cap is below its default (without a selector and at the default the count is the CRD's own, and such a plan is not refused for it), and a single selector past it is refused at its read. A project that is not a Shared VPC host resolves to no members, as it does
at runtime. An exclude entry that names a Shared VPC service project by ID, or a monitored project
by number, keeps it out of the bindings, the one place `exclude` reaches IAM, because a selector's
member has no list to be dropped from; a monitored project excluded by ID keeps its grant, which the
reconcile's naming call needs before the exclusion can match; a glob is the reconcile's alone. What the
selectors do not have is a container's zero-touch onboarding: a service project attached, or a
project added to the scope, after the last apply reads `denied` in the reconcile's snapshot until
the next apply binds it. `scope_selector_members` outputs what each resolved to, under the name
the snapshot's `containers` array uses.

`scoped_pool_enabled` arms the scoped service account pool from the same `scope` object: the
IAM module provisions one reader service account in `project_id` per project the plan listed
(the management project, `scope.projects` less an exact `exclude.projects` entry, each
selector's members, and each declared folder's and organisation's members, which the resolver
lists at plan time with the reconcile's own Cloud Asset Inventory search while the pool is armed
and never while it is off), keyed on the project id,
and the chart renders the mapping into the CR as `spec.security.scopedServiceAccountPool` with
`enabled` set from the same variable, so the broker is armed by this switch alone and never by
declaring projects. Two clusters in one project share an account by design
([`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md) §6).
A container's member gets a pool account on that apply and nothing else: its grant is the
container's, inherited, and it is not counted toward `scope.max_projects`. Pool membership under a
container therefore lags where the grant and discovery do not: a project created beneath a declared
folder since the last apply is discovered and readable, but refused by the broker until the next
apply lists it. Listing needs `cloudasset.googleapis.com` on in `project_id` before the first plan
that arms the pool beside a container, which `install.sh` enables, and
`roles/cloudasset.viewer` on the container for the planning identity. An exact `exclude.projects`
entry drops a member from the pool; a glob is the reconcile's alone.
`scoped_pool_max_accounts` bounds how many the plan may create and refuses a pool past it at plan;
its default of 100 is GCP's default service-account quota, which the agent's own accounts share, so
set it to the headroom the project has free rather than leaving a large pool at the default. Off by default, and it should stay off
until per-cluster RBAC lands: a member holds no IAM grant, so an armed pool turns every cluster
read into a `Forbidden`. Through the installer the two come from `SCOPED_SA_POOL_ENABLED` and
`SCOPED_SA_POOL_MAX_ACCOUNTS` in `install.env`. `scoped_service_accounts` outputs the mapping,
project id to email.

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

With `gitops_forge = "gitlab"`, set `gitlab_repo` to the GitLab project's full path (`group/project`, nested groups allowed) and, for a self-managed instance, `gitops_host` to its hostname. The CR then declares one `gitlab` forge and the project as its `gitops` repository, with `credentialsRef` naming `gitlab_token_secret_name` (default `gitlab-forge-token`). Terraform never sees the token: create that Secret, with the token under the key `token`, in the agent's namespace yourself (`install.sh` does it for you). A GitLab install has no GitHub App, so `github_repo` and `enable_github_minter` must stay unset; the plan refuses otherwise. See [GitLab as the GitOps forge](../../../docs/site/src/content/docs/install/gitlab.md).

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

### Drift audit-log ingress

`enable_drift_pubsub = true` (default `false`) instantiates the
[`drift-pubsub`](../../modules/drift-pubsub) module: a Log Router sink that
exports mutating GKE audit-log calls (`drift_pubsub_sink`, default
`platform-agent-drift-audit-sink`), the topic it publishes to
(`drift_pubsub_topic`, default `platform-agent-drift-audit`), the pull
subscription (`drift_pubsub_subscription`, default
`platform-agent-drift-audit-sub`), and `roles/pubsub.publisher` on the topic
for the sink's writer identity. It also adds `pubsub.googleapis.com` to the
enabled APIs.

`roles/pubsub.subscriber` and `roles/pubsub.viewer` on the subscription for the
agent's GSA are granted here rather than inside the module, next to the module
call. That is what keeps the module clear of `kube-agents-iam`, and so clear of
the cluster: the cluster module depends on the ingress, which makes Terraform
destroy the cluster first and delete the topic last. Without that ordering the
topic goes while the control plane is still emitting matching audit records,
and each one mails every project owner. The module README's
[ordering section](../../modules/drift-pubsub/README.md#why-the-sink-is-created-last-and-destroyed-first)
is canonical; `moved` blocks beside the two grants keep an install created
by an earlier version, which had them inside the module, from planning a
destroy and create of the detector's access on its next apply.

`drift_pubsub_topic_publishers` (default `[]`) grants `roles/pubsub.publisher`
on the topic to each member it lists, on top of the sink's writer identity.
Leave it empty unless a test harness has to inject synthetic audit records: the
detector classifies on the `principalEmail` inside each record and Pub/Sub does
not attach the publisher's identity to the message, so anything that can
publish here can make the detector report a change nobody made, under any
principal it names. Never list the agent's own GSA.

Beyond the three names and that list, the module's one required input is
passed and three more of its optional ones:
`drift_pubsub_sink_writer_identity_override`, which the module's own
postcondition tells an operator to set when a project's sink reports a writer
identity the module did not derive, and the module's two timers —
`drift_pubsub_sink_drain_duration`, the destroy-time wait below, and
`drift_pubsub_logging_identity_propagation_duration`, the apply-time one. None
has an installer key, so through the front doors all three are passthrough
lines in `install.env`
(`TF_VAR_drift_pubsub_sink_writer_identity_override`,
`TF_VAR_drift_pubsub_sink_drain_duration`,
`TF_VAR_drift_pubsub_logging_identity_propagation_duration`) rather than
entries in `terraform.tfvars`, which `write_tfvars_from_state` regenerates
wholesale on every `install.sh` and `upgrade.sh` run — a hand-added key there
is gone on the next one, and for the override that means the failure it
cleared comes back. A hand-driven apply sets them in `terraform.tfvars`.
Everything else is left to the module's defaults,
which decide the 31-day retention and the cluster scope, every GKE cluster in
the project; a caller that needs the module's remaining knobs instantiates it
directly.

The module creates the sink after its publish grant and holds a wait between
deleting the sink and deleting the topic — `drift_pubsub_sink_drain_duration`,
two minutes by default — so that Cloud Logging never routes to a topic it
cannot reach and mails every project owner about it. That wait is why a
destroy of this configuration pauses once the sink is gone. Raising it takes
an apply to land before the destroy that should honour it: `time_sleep` reads
`destroy_duration` from state, since a provider's delete is handed prior state
and no configuration, and `uninstall.sh` runs no apply of its own. Setting the
variable and going straight to `uninstall.sh` waits whatever an earlier apply
recorded, so run `upgrade.sh` in between.

An apply pauses too, for
`drift_pubsub_logging_identity_propagation_duration` (60s by default), between
minting the project's Logging service agent and granting it publisher on the
topic. GCP cannot bind a service agent the instant it is minted, and without
that wait the apply fails on about one project in five with "Service account
… does not exist", leaving the topic behind and no sink. It is paid on
the first apply that carries the wait — the first apply with
`enable_drift_pubsub` on, or, on an install that already had the ingress, the
next apply of any kind after this version lands — and after that only on an
apply that re-mints the Logging identity or changes this value, the two things
the wait is keyed on. A project whose Logging agent
already exists gains nothing from it, and lowering the value before that first
apply is how it keeps the minute. Lowering it afterwards does not refund the
wait already paid — the wait is keyed on this value, so a change re-creates it
and pays the new, lower figure once — it only shortens any later one.

[The module's README](../../modules/drift-pubsub/README.md#why-the-sink-is-created-last-and-destroyed-first)
is canonical for all four orderings — the three inside the module, and the
cluster-before-ingress edge this composition adds on top of them.

Three outputs, each `null` while the flag is off: `drift_pubsub_topic`,
`drift_pubsub_subscription`, and `drift_pubsub_subscription_id`, the
fully-qualified path the drift detector's `--subscription` flag takes.

The subscription is the input to the drift detector of
[`docs/designs/drift-detection.md`](../../../docs/designs/drift-detection.md).
Its consumer,
[`k8s-operator/cmd/drift-detector`](../../../k8s-operator/cmd/drift-detector/README.md),
ships in the platform-agent images and starts inside the gateway pod when the
`PlatformAgent` sets `spec.harness.driftDetector.enabled`, which is what
`enable_drift_detector = true` (default `false`) writes. With
`enable_drift_pubsub` on, the composition also writes the subscription's name
into that block (`platformAgent.harness.driftDetector.subscription`), so a
renamed `drift_pubsub_subscription` is the one the detector pulls from; the
detector's compiled-in default is the module's default name, which is why an
install that leaves the name alone would work without that wire and one that
renames it would not.

The two are separate variables so that a hand-driven apply can provision the
ingress on its own, and that is the only order allowed. Turned on without the
detector, the sink publishes every mutating call on every GKE cluster in the
project (about 60k messages a day after the module's lease filter, per its
README) into a subscription that retains them for 31 days and never expires:
Pub/Sub storage cost and a backlog until the detector is enabled. The reverse
is refused — a `helm_release` precondition fails the apply when
`enable_drift_detector` is set without `enable_drift_pubsub`. The block
carrying `enabled` is written only when the ingress is on, so the failure that
precondition catches is quieter than the Ready-forever detector the CRD
field's description warns about: an apply that succeeds, renders no
`driftDetector` block, provisions nothing, starts nothing, and leaves a
variable that did nothing as the only evidence. Both preconditions test
`local.drift_detector_requested` rather than the variable, because
`extra_helm_values` reaches the same field: Helm deep-merges it over the values
computed here, so `platformAgent.harness.driftDetector.enabled = true` set
there would otherwise pass the apply and produce the Ready-forever detector
itself, pulling a subscription that was never created. A second precondition
refuses the detector when `project_id` is the project number rather than
the project ID, which the rest of the composition accepts and the operator
does not (`driftDetectorEnabled` in
[`k8s-operator`](../../../k8s-operator/internal/controller/platformagent_manifests.go)
matches it against each audit record's `project_id`, which is always the ID) —
without it the ingress bills for a stream nothing reads.

`lifecycle.sh apply` refuses rather than adopting when a topic, subscription
or sink of those names already exists outside its state — `guard_drift_adoption`,
before Terraform runs. Adoption by name cannot tell a leftover from another
install's live trio, and since `ENABLE_DRIFT_DETECTOR` became an install
default it is every second install in a project that arrives at that
ambiguity rather than only one that asked for the feature. The refusal names
the four ways out; [Remote state](#remote-state) has them, and the cost of
getting it wrong.

Through the installer front doors the two variables are one `install.env` key,
`ENABLE_DRIFT_DETECTOR`, and it defaults to `true`: an install that says nothing
about it writes both into the generated `terraform.tfvars`, which is the only
order the precondition accepts. `ENABLE_DRIFT_DETECTOR=false` writes neither —
the one boolean in that generated file omitted rather than written `false`. `enable_drift_pubsub` is
reachable on its own as a `TF_VAR_enable_drift_pubsub=true` line in
`install.env`, the same channel `agent_ksa_name` uses (every front door sources
that file with `set -a`, and Terraform reads `TF_VAR_*` where the generated file
is silent), and a tfvars key beats `TF_VAR_`: a written `false` would override
such an install and plan its sink, topic and subscription — with up to 31 days
of retained audit records nothing has acknowledged — for removal under
`-auto-approve`, from a release note nobody read. Omission is what leaves it
alone.

Which makes turning the key off two different things — and note that dropping
the line is not one of them. Absence resolves to the shipped `true`, so a
deleted `ENABLE_DRIFT_DETECTOR` line provisions the trio rather than destroying
it, the opposite of every other key in that file. Only an explicit
`ENABLE_DRIFT_DETECTOR=false` turns the feature off. On an install that has only
ever had the key, writing that `false` returns both variables to their `false`
defaults and the next apply destroys the sink, topic and subscription, retained
messages included — the ordinary teardown the other flags get, and the same
`-auto-approve` destroy the paragraph above describes, arriving this time
because it was asked for. Nothing refuses it: `guard_pubsub_subscription`
checks the name only while the flag is on, because switching the feature off is
a teardown it reads as deliberate rather than the rename it guards against. On
an install carrying the `TF_VAR_` line, that same `false` stops the detector and
leaves the ingress running, still exporting and still billing.

**Manual steps that no IaC can perform** — canonical walkthrough:
[INSTALL.md § Enable Google Chat & Slack Integrations](../../../INSTALL.md#step-5-enable-google-chat--slack-integrations-manual-required-steps):

- **Google Chat:** register the Chat app on the Chat API configuration page —
  select Cloud Pub/Sub and enter the created topic (the `chat_topic_name`
  output, as `projects/<project>/topics/<topic>`), set visibility, and verify
  a **Service account email** appears under Connection settings after saving
  (if it stays blank, Chat silently delivers no events). That address is the
  Workspace Add-ons service agent,
  `service-<PROJECT_NUMBER>@gcp-sa-gsuiteaddons.iam.gserviceaccount.com`, not
  the agent's own GSA. Then DM the bot; on
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

(and likewise for `kube-agents-iam`, `kube-agents-scope-resolver`, `chat-pubsub`, `github-minter`,
`gke-backup-plan`, and `drift-pubsub`), and
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

| Asymmetry                                                                                                | Handled by                                                                                                                                                                                                                                                                         |
| -------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| KMS key rings and keys can never be deleted, so the next apply 409s                                      | `tf-apply` imports the survivors before applying (`lifecycle.sh adopt-kms`)                                                                                                                                                                                                        |
| The `PlatformAgent` finalizer strands the CR and hangs the namespace                                     | `tf-destroy` deletes the CR and waits, force-clearing the finalizer if wedged                                                                                                                                                                                                      |
| A `BackupPlan` cannot be deleted while it owns backups                                                   | `tf-destroy` purges the plan's backups first                                                                                                                                                                                                                                       |
| `deletion_protection = true` cannot be overridden by a destroy alone                                     | `tf-destroy` applies it as `false`, then destroys                                                                                                                                                                                                                                  |
| The helm provider reports a release destroyed when it cannot reach the cluster                           | On a cluster this composition did not create, `tf-destroy` uninstalls the releases this state installed with the `helm` CLI first, and stops if `helm` cannot list or uninstall them; skipped with a warning when `helm` is missing or the cluster's credentials cannot be fetched |
| A Pub/Sub topic or subscription that already exists makes the create 409                                 | `tf-apply` imports it first (`adopt_pubsub`), so a topic created in the Cloud console while wiring up Google Chat does not block the install                                                                                                                                       |
| The stockout topic, subscription and sink survive a partial teardown and 409 the same way                | `tf-apply` imports whichever of them exist by name when the flag is on (`adopt_kms`, alongside the KMS resources)                                                                                                                                                                  |
| The drift trio does too, but the detector is on by default, so every second install in a project hits it | `tf-apply` refuses instead of importing (`guard_drift_adoption`): an import cannot tell a leftover from another live install's trio                                                                                                                                                |

The chart also carries a `pre-delete` hook that removes the CR and waits for
its finalizer, so a plain `helm uninstall` is safe on its own; `tf-destroy`
does it up front anyway, which turns the hook into a no-op. Disable it with
`platformAgent.cleanupHook.enabled=false`.

Running `terraform destroy` directly still works, but you own the destroy steps
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
