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

  Two further keys are read from the same Secret but generated rather than
  asked for, since no value an operator could choose is better than a random
  one: `SESSION_KV_API_KEY` (bearer token for the pod-local Session KV server)
  and `SESSION_KV_SALT` (HMAC salt for pseudonymising chat identities). With
  `create=true` the chart generates them on install and carries the existing
  values forward on upgrade — rotating the salt would re-anonymise every user,
  severing their past sessions from their future ones. With `create=false`,
  whatever created the Secret supplies them; `provision_07_gcp_k8s_secrets.sh`
  and the Terraform example both do.

  Absent, the pod starts anyway — but the in-pod `k8s-event-watcher`
  authenticates with `SESSION_KV_API_KEY`, treats an empty value as fatal, and
  exits on every start, so **no cluster events are watched at all**; the
  container stays Ready and its log is the only place that says so. The Session
  KV server also answers `503` to every request, and identity hashing falls back
  to a per-pod salt with a warning. Add the keys to the Secret before upgrading
  an installation that predates them.

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
only if you operate your own gateway at that address. LLM-call telemetry is
opt-in (`litellm.otel=true`) — enable it only on clusters that run a reachable
collector, since without one the otel callback aborts every LLM request on DNS
failure.

### Telemetry

`telemetry.otlpEndpoint` (default `""`) is the OTLP/HTTP collector base URL.
Empty means "do not decide here": the LiteLLM exporter and NetworkPolicy keep
the GKE Managed OpenTelemetry collector, and the `telemetry` block is omitted
from the PlatformAgent CR so the operator discovers an in-cluster collector at
reconcile time. Setting it moves the agent and the policy's egress namespace
together, and pins the agent so a release can't be internally split. It also
moves the LiteLLM exporter, but that variable only exists when `litellm.otel=true`
— off by default, and not turned on by naming a collector.

The egress namespace is read off the endpoint host when it names an in-cluster
Service. An external endpoint has none to read: with `litellm.otel=true` that
fails the render, so set `telemetry.collectorNamespace` (or
`litellm.networkPolicy=false`); with the callback off the rule keeps
`gke-managed-otel`, since nothing exports through it. Full precedence
ladder and discovery rules: [Deploy → Telemetry](https://gke-labs.github.io/kube-agents/deploy/telemetry/#pointing-at-your-own-collector).

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

### Turning telemetry off

The operator's endpoint ladder always resolves to something — a collector it
discovers in the cluster, otherwise the GKE Managed OpenTelemetry collector — so
`telemetry.otlpEndpoint` can move the exporter but cannot switch it off. On a
cluster running neither (a plain `gke-cluster` module cluster has no
`gke-managed-otel` namespace) the exporter then retries a hostname that never
resolves, for the life of the pod.

`platformAgent.deployment.env` is the off switch. The operator applies it after
its own container environment, so it wins:

```yaml
platformAgent:
  deployment:
    env:
      - name: OTEL_SDK_DISABLED
        value: "true"
```

Use `telemetry.otlpEndpoint` instead when you do have a collector to point at.

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

### Agent runtime knobs

`platformAgent.harness.hermes`, `platformAgent.harness.memory`, and
`platformAgent.deployment.availability` expose the remaining fields the
provisioning scripts substitute into
[`platform-agent.yaml.template`](../../k8s-operator/scripts/platform-agent.yaml.template),
so a chart install can reach the same CR as a script install. Each one defaults
to `null`/`""`, which **omits** the field and lets the CRD's own default apply
— setting `false` is therefore distinct from leaving it unset, and `replicas: 0`
means zero rather than unset.

`platformAgent.deployment.image.pullPolicy` defaults to `Always`, matching the
same template. Under `IfNotPresent` a node that has already cached the tag never
picks up a rebuild, which is the normal case for the Terraform composition's
default `image_tag = "latest"`.

**Consider `IfNotPresent` when you pin the tag.** The chart's own default tag is
`.Chart.AppVersion`, which the release workflow overwrites with the git tag — an
immutable tag, where `Always` buys nothing and costs a registry round-trip on
every pod start. It also removes a fallback: if the agent pod is rescheduled
while ghcr.io is unreachable or rate-limiting, `Always` fails the pull and the
pod sits in `ImagePullBackOff` where `IfNotPresent` would have started from the
node's cache. The two install surfaces agree on `Always` for the mutable-tag
case they were both written for, and `make iac-parity-check` holds them there;
an install at a pinned release tag is the case that wants the override.

Two knobs have no Terraform or chart-side infrastructure behind them:

- `deployment.availability.runtimeClassName: gvisor` needs the GKE Sandbox node
  pool that [`provision_02_gvisor_nodepool.sh`](../../k8s-operator/scripts/provision_02_gvisor_nodepool.sh)
  creates; the Autopilot `gke-cluster` module has no equivalent.
- `harness.hermes.dashboardEnabled` is the one field where the two install paths
  disagree by default: the CRD defaults it to `true`, the script path to
  `false`. Set it explicitly when the two installs must match.

### ServiceAccount ownership

Exactly one owner creates the agent's KSA, depending on
`platformAgent.security.serviceAccountAnnotations`:

- **Annotations set** (the Workload Identity case): the **operator** creates
  and manages the KSA with those annotations.
- **No annotations**: the operator treats the named KSA as user-managed and
  does not create it — the **chart** renders it instead, so a default install
  still starts.

## Uninstalling

```bash
helm uninstall kube-agents -n kubeagents-system
```

The `PlatformAgent` resource carries a finalizer that only the operator can
clear, and Helm deletes the CR and the operator in the same pass — so nothing
would be left to clear it, the CR would strand, and the namespace would hang in
`Terminating`. `platformAgent.cleanupHook` (on by default) prevents that with a
`pre-delete` hook that deletes the CR and waits for the finalizer while the
operator is still running.

The hook runs `kubectl` from `alpine/k8s`, because the operator image is
distroless and carries no client — and because the hook needs a shell: it is
best-effort on purpose, exiting 0 (`|| true`) even when the wait times out,
since a failed `pre-delete` hook aborts the entire uninstall — worse than the
stranded CR it prevents. Use `platformAgent.cleanupHook.image` to point at your
own mirror; any image with `kubectl` and `/bin/sh` works.

With `platformAgent.cleanupHook.enabled=false`, the ordering is yours to keep:

```bash
kubectl delete platformagent platform-agent -n kubeagents-system --wait
helm uninstall kube-agents -n kubeagents-system
```

## Notes

- **Admission webhooks are off by default** (`operator.webhooks.enabled=false`)
  and the chart renders the full wiring when you turn them on: the webhook
  Service, a self-signed `Issuer` and `Certificate`, both
  `*WebhookConfiguration`s with cert-manager's `inject-ca-from` annotation, and
  the manager's cert mount on `:10250`. Left off, the webhooks' validation,
  defaulting, and delete-protection don't apply (CRD-level CEL validation and
  OpenAPI defaulting still do).

  They are off by default only because they need **cert-manager** and the chart
  cannot install it for you — a default-on chart would fail at apply time on
  every cluster without the CRDs. Install cert-manager, then:

  ```bash
  helm upgrade kube-agents … --set operator.webhooks.enabled=true
  ```

  `terraform/examples/full-install` does both in one apply.

  Two behaviours worth knowing before you enable them:

  - **`failurePolicy` defaults to `Ignore`, where the kustomize path uses
    `Fail`.** Helm applies the webhook configurations before both the
    `Certificate` and the `PlatformAgent` CR, so under `Fail` the API server
    rejects this chart's own CR on a fresh install and the release never
    completes. The chart refuses that combination at render time rather than
    letting you discover it half-applied. `Fail` is available and correct once
    the operator is serving — set it on a later upgrade, or on a release
    installed with `platformAgent.enabled=false`.
  - **The configurations are cluster-scoped and match every namespace**, as
    they do under kustomize, because the manager reconciles PlatformAgents
    cluster-wide. Two releases with webhooks on therefore both intercept every
    PlatformAgent in the cluster; under `Fail` an outage of either one blocks
    writes for both. Run webhooks from one release.

- **CRDs** live in `crds/` and are installed by Helm on first install but never
  upgraded (a Helm limitation) — apply `k8s-operator/config/crd/bases/`
  manually when upgrading across CRD changes. Automating this (pre-upgrade
  hook) is deliberate follow-up scope; it first matters when upgrading between
  two published releases.
- The CRD and RBAC manifests under this chart are generated copies of
  `k8s-operator/config/` — edit the source and run `make chart-sync` (CI
  enforces this via `make chart-check`).

See [docs/site/src/content/docs/deploy/release-versioning.md](../../docs/site/src/content/docs/deploy/release-versioning.md) for versioning rules.
