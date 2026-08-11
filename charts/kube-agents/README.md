# kube-agents Helm Chart

Canonical GKE-oriented Helm chart for deploying the Kube-Agents Kubernetes Operator and Platform Agent Custom Resource.

## Prerequisites

- Kubernetes 1.28+ (GKE Autopilot or Standard)
- A Google Service Account (GSA) with a Workload Identity binding to the agent's
  Kubernetes ServiceAccount — `kubeagents-platform-agent` in the release
  namespace by default (`platformAgent.security.serviceAccountName`):

  ```bash
  gcloud iam service-accounts add-iam-policy-binding <GSA>@<PROJECT>.iam.gserviceaccount.com \
    --role roles/iam.workloadIdentityUser \
    --member "serviceAccount:<PROJECT>.svc.id.goog[kubeagents-system/kubeagents-platform-agent]"
  ```

  Then set the KSA annotation via
  `--set platformAgent.security.serviceAccountAnnotations."iam\.gke\.io/gcp-service-account"=<GSA>@<PROJECT>.iam.gserviceaccount.com`.

- A Secret with the agent's credentials in the release namespace (name from
  `platformAgent.credentials.secretName`, default `platform-agent-secrets`),
  holding `API_SERVER_KEY` plus your model-provider key (`ANTHROPIC_API_KEY`,
  `GEMINI_API_KEY`, or `OPENAI_API_KEY` — `vertex_ai` needs none, it authenticates
  with Workload Identity) and optional `SLACK_BOT_TOKEN` /
  `SLACK_APP_TOKEN`. For dev installs the chart can create it from values
  (`platformAgent.credentials.create=true` + `platformAgent.credentials.data`).

## Usage

Helm installs OCI charts directly (there is no `helm repo add` for OCI
registries):

```bash
helm install kube-agents oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents \
  --version X.Y.Z \
  --namespace kubeagents-system --create-namespace \
  --set platformAgent.harness.clusterName=my-cluster \
  --set platformAgent.harness.location=us-central1 \
  --set platformAgent.harness.projectId=my-gcp-project
```

`platformAgent.harness.{clusterName,location,projectId}` are required and have
no defaults — rendering fails until they are set.

### Installing from a repository checkout

The `appVersion` in a checkout's `Chart.yaml` is a placeholder that never
corresponds to a published image tag, so checkout installs must override
**both** image tags with tags that exist (`latest` or a commit SHA — published
on every push to `main`):

```bash
helm install kube-agents ./charts/kube-agents \
  --namespace kubeagents-system --create-namespace \
  --set platformAgent.harness.clusterName=my-cluster \
  --set platformAgent.harness.location=us-central1 \
  --set platformAgent.harness.projectId=my-gcp-project \
  --set operator.image.tag=latest \
  --set platformAgent.deployment.image.tag=latest
```

### LiteLLM gateway

The agent's baked default model endpoint is
`http://litellm.<namespace>.svc.cluster.local/v1`, so the chart deploys the
LiteLLM gateway by default (`litellm.enabled=true`), mirroring
`k8s-operator/config/integrations/litellm/base`. `litellm.modelProvider`
(gemini/anthropic/openai/vertex_ai) picks which provider `model-default` routes to
— the matching API key must be in the credentials Secret, except `vertex_ai`, which
uses Workload Identity (below); `litellm.modelDefaultName`
overrides the per-provider default model. `chatgpt` mode is rejected (it needs
the OAuth-token PVC from the kustomize overlay). Set `litellm.enabled=false`
only if you operate your own gateway at that address. LLM-call telemetry to
the GKE Managed OpenTelemetry collector is opt-in (`litellm.otel=true`) —
enable it only on clusters that run the managed collector, since without it
the otel callback aborts every LLM request on DNS failure.

#### Vertex AI (`litellm.modelProvider=vertex`)

Vertex AI has no API key. The gateway calls
`projects/<litellm.vertex.projectId>/locations/<litellm.vertex.location>`
(both default to `platformAgent.harness.projectId`/`.location`) as a Google
Service Account reached through Workload Identity. That GSA, its
`roles/aiplatform.user` grant, and its binding to the gateway's KSA are not
chart resources — see
[Security & IAM](https://gke-labs.github.io/kube-agents/reference/security-and-iam/).

The chart does create the gateway KSA whenever `modelProvider=vertex_ai`, since no
operator reconciles this one. Pass the Workload Identity annotation so it
resolves to that GSA:

```bash
--set litellm.modelProvider=vertex_ai \
--set litellm.modelDefaultName=<publisher-model-id> \
--set litellm.vertex.serviceAccountAnnotations."iam\.gke\.io/gcp-service-account"=<LITELLM_GSA>@<PROJECT>.iam.gserviceaccount.com
```

`terraform/examples/full-install` wires all of this up when
`model_provider = "vertex_ai"`. The provisioning-script path does the same via
`make gcp-provision-04-iam` (identity and roles) plus
`make gcp-provision-09-litellm` (the annotated KSA), both run from
`k8s-operator/`.

### Integrations

- **Google Chat** — `platformAgent.integration.googleChat.enabled=true` plus the
  topic/subscription names (defaults match the provisioning scripts and the
  `chat-pubsub` Terraform module). Requires the Chat Pub/Sub backend to exist
  (`provision_05_gcp_gchat.sh` or `terraform/modules/chat-pubsub`); `projectId`
  is taken from `platformAgent.harness.projectId`. Restrict access via
  `allowedUsers` (empty = everyone).
- **Slack** — `platformAgent.integration.slack.enabled=true`; the bot/app
  tokens are read from the credentials Secret's `SLACK_BOT_TOKEN` /
  `SLACK_APP_TOKEN` keys (the CRD requires both refs when Slack is enabled).
- **GitHub** — `platformAgent.integration.github.gitRepo` sets the agent's
  GitOps target repository.

Chat and Slack each need a one-time manual registration that no install
automation can perform (the Chat app on the Chat API console page pointed at
the Pub/Sub topic; Socket Mode + bot scopes in the Slack app console) —
[INSTALL.md § Enable Google Chat & Slack Integrations](../../INSTALL.md#step-5-enable-google-chat--slack-integrations-manual-required-steps)
is the canonical walkthrough, including the pairing-code approval.

### ServiceAccount ownership

Exactly one owner creates the agent's KSA, depending on
`platformAgent.security.serviceAccountAnnotations`:

- **Annotations set** (the Workload Identity case): the **operator** creates
  and manages the KSA with those annotations.
- **No annotations**: the operator treats the named KSA as user-managed and
  does not create it — the **chart** renders it instead, so a default install
  still starts.

## Uninstalling

The `PlatformAgent` resource carries a finalizer that only the operator can
clear. Delete the CR and wait for it to disappear **before** uninstalling the
release (which removes the operator), otherwise the CR strands:

```bash
kubectl delete platformagent platform-agent -n kubeagents-system --wait
helm uninstall kube-agents -n kubeagents-system
```

## Notes

- **Admission webhooks are not part of chart installs** (deliberate follow-up
  scope, not an oversight: they need cert-manager wiring and carry
  `failurePolicy: Fail` risk, so they warrant their own change). The chart
  ships no webhook Service, certificate, or `*WebhookConfiguration`, and pins
  `ENABLE_WEBHOOKS=false` on the manager; the webhooks' validation, defaulting,
  and delete-protection therefore don't apply (CRD-level CEL validation and
  OpenAPI defaulting still do). The provisioning-script / kustomize install
  path provides them.
- **CRDs** live in `crds/` and are installed by Helm on first install but never
  upgraded (a Helm limitation) — apply `k8s-operator/config/crd/bases/`
  manually when upgrading across CRD changes. Automating this (pre-upgrade
  hook) is deliberate follow-up scope; it first matters when upgrading between
  two published releases.
- The CRD and RBAC manifests under this chart are generated copies of
  `k8s-operator/config/` — edit the source and run `make chart-sync` (CI
  enforces this via `make chart-check`).

See [docs/site/src/content/docs/deploy/release-versioning.md](../../docs/site/src/content/docs/deploy/release-versioning.md) for versioning rules.
