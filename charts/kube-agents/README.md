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
  `GEMINI_API_KEY`, or `OPENAI_API_KEY`) and optional `SLACK_BOT_TOKEN` /
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

### Installing from a mirrored registry

Clusters that may only pull from an approved registry need every image copied
there first — `make mirror-images MIRROR_PREFIX=<prefix>` from the repository
root does that, driven by `images.json`. Then point the chart at the copy:

```bash
helm install kube-agents ./charts/kube-agents \
  --namespace kubeagents-system --create-namespace \
  --set global.imageRegistry=registry.example.com/kube-agents \
  --set platformAgent.harness.clusterName=my-cluster \
  --set platformAgent.harness.location=us-central1 \
  --set platformAgent.harness.projectId=my-gcp-project \
  --set operator.image.tag=latest \
  --set platformAgent.deployment.image.tag=latest
```

This example installs from a checkout, so the two tag overrides above still
apply — and they have to name the tag the mirror was populated with, which is
whatever `IMAGE_TAG` `make mirror-images` copied (`latest` by default). From a
published chart, drop them and let `appVersion` pick the release.

`global.imageRegistry` rewrites each image onto the prefix keeping the trailing
name only, matching the flat layout `mirror-images` writes. Set
`global.thirdPartyImageRegistry` as well if the mirror keeps LiteLLM and
fluent-bit under a different path; it defaults to `global.imageRegistry`.

It reaches more than the containers the chart renders. The operator resolves
two images at reconcile time that appear in no chart template — the agent image
for a `PlatformAgent` that omits `spec.deployment.image`, and the fluent-bit
logging sidecar it injects into every agent pod — so the chart passes both to
the operator as `PLATFORM_AGENT_IMAGE` and `FLUENT_BIT_IMAGE`. Without that a
mirrored install reaches `ghcr.io` and Docker Hub minutes after `helm install`
reported success. `CREDENTIAL_PROXY_IMAGE` is deliberately not passed: the
operator derives that sidecar from the agent image by swapping the trailing
name, so it follows the mirror on its own.

The prefix is not a per-image default — it replaces every image's registry and
path, keeping the trailing name, because that is the flat layout
`make mirror-images` writes. Setting `litellm.image.repository` while
`global.imageRegistry` is set therefore changes only the name the prefix is
joined to, not where the image is pulled from. To place images individually —
most on the mirror, one somewhere else — leave `global.imageRegistry` empty and
give each `*.image.repository` its full mirrored path instead; the operator's
`PLATFORM_AGENT_IMAGE` and `FLUENT_BIT_IMAGE` are rendered from those values
either way.

Anything in `operator.extraEnv` is appended after the env vars above and
therefore wins.

Registry authentication is out of scope — the mirror must be readable with the
nodes' own credentials (an in-project Artifact Registry, or a pull-through
cache). The chart renders no `imagePullSecrets`.

### LiteLLM gateway

The agent's baked default model endpoint is
`http://litellm.<namespace>.svc.cluster.local/v1`, so the chart deploys the
LiteLLM gateway by default (`litellm.enabled=true`), mirroring
`k8s-operator/config/integrations/litellm/base`. `litellm.modelProvider`
(gemini/anthropic/openai) picks which provider `model-default` routes to — the
matching API key must be in the credentials Secret; `litellm.modelDefaultName`
overrides the per-provider default model. `chatgpt` mode is rejected (it needs
the OAuth-token PVC from the kustomize overlay). Set `litellm.enabled=false`
only if you operate your own gateway at that address. LLM-call telemetry to
the GKE Managed OpenTelemetry collector is opt-in (`litellm.otel=true`) —
enable it only on clusters that run the managed collector, since without it
the otel callback aborts every LLM request on DNS failure.

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
