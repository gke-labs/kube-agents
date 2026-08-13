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
  with Workload Identity and Cloud KMS database encryption (CMEK) enabled.
- The agent's GCP identity ([`kube-agents-iam`](../../modules/kube-agents-iam)
  module): the `kubeagents-platform-gsa` service account, its read-only
  project roles, and the Workload Identity binding to the
  `kubeagents-platform-agent` KSA (see [IAM roles](#iam-roles-project_roles)
  below).
- Optionally (`enable_google_chat = true`) the Google Chat backend
  ([`chat-pubsub`](../../modules/chat-pubsub) module): Pub/Sub topic,
  subscription, and Chat integration wiring.
- Optionally (`enable_github_minter = true`) the GitHub token minter backend
  ([`github-minter`](../../modules/github-minter) module): minter service
  account plus a KMS key ring and signing key.
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

### The `image_tag` rule

`image_tag` (default `latest`) overrides both the operator and platform-agent
image tags. It exists because the chart is installed from this checkout, and a
checkout's `Chart.yaml` carries an `appVersion` placeholder that never matches
a published image tag — so the chart's usual tag defaulting cannot work here
(see the [chart README](../../../charts/kube-agents/README.md)). `latest` is
fine for evaluation; pin a `vX.Y.Z` release tag for production.

### IAM roles (`project_roles`)

When `project_roles` is not set, the agent's service account gets the
**read-only permission set** — verify the exact list in the
`project_roles` variable default in
[`terraform/modules/kube-agents-iam/variables.tf`](../../modules/kube-agents-iam/variables.tf),
which mirrors the provisioning scripts' `read-only` set (the scripts' own
default; source: `read_only_roles` in
[`k8s-operator/scripts/provision_04_gcp_iam.sh`](../../../k8s-operator/scripts/provision_04_gcp_iam.sh)).

To grant a different set, set `project_roles` explicitly in your
`terraform.tfvars` — for the scripts' `gke-admin` equivalent, copy the
`gke_admin_roles` list from `provision_04_gcp_iam.sh` rather than typing it
from memory. `project_roles = []` grants nothing and leaves IAM to you (the
agent fails every GCP call until an equivalent set exists). Deliberately no
admin list is pre-staged in `terraform.tfvars.example` — widening access
should be an explicit, reviewed choice.

### Google Chat and GitHub integrations

With `enable_google_chat = true` the composition provisions the GCP backend
(topic, subscription, IAM) **and** enables the CR's `googleChat` integration
with the created topic/subscription — restrict access with
`google_chat_allowed_users` (empty = everyone).

Set `github_repo` to wire the agent's GitOps target repository
(`spec.integration.github.gitRepo`). Slack can be enabled directly through
chart values (`platformAgent.integration.slack.*`) once the Slack tokens are
present in the credentials Secret.

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
  scopes listed in the walkthrough, then put `SLACK_BOT_TOKEN` /
  `SLACK_APP_TOKEN` into the credentials Secret; pairing approval works the
  same way (`hermes pairing approve slack <CODE>`).

## Standalone use outside this repository

This example sources the modules by relative path because it lives in the same
repository. A standalone consumer would pin a release instead:

```hcl
module "gke_cluster" {
  source = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=vX.Y.Z"
  # ...
}
```

(and likewise for `kube-agents-iam`, `chat-pubsub`, and `github-minter`), and
would install the chart from the OCI registry rather than a local path — see
the [chart README](../../../charts/kube-agents/README.md).

## Teardown

`terraform destroy` removes the Helm release, but that also removes the
operator — and the `PlatformAgent` CR carries a finalizer only the operator
can clear, so destroying first strands the CR and hangs the namespace
deletion. Delete the CR and wait for it to disappear **before** destroying:

```bash
# Use your namespace value if you overrode the kubeagents-system default.
kubectl delete platformagent platform-agent -n kubeagents-system --wait
terraform destroy
```

The cluster is created with `deletion_protection = true` by default; set the
variable to `false` (and apply) before a destroy can remove the cluster.

> [!NOTE]
> Cloud KMS key rings and crypto keys (for GKE CMEK and optional GitHub minter)
> cannot be deleted from GCP. `terraform destroy` removes them from state, but
> re-applying with the same names requires either importing them back into state
> (`terraform import module.gke_cluster.google_kms_key_ring.gke_keyring[0] ...`)
> or choosing new key/keyring names.
