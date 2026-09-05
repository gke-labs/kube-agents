# kube-agents Helm Chart

Canonical GKE-oriented Helm chart for deploying the Kube-Agents Kubernetes Operator and Platform Agent Custom Resource.

## Prerequisites

- Kubernetes 1.29+ (GKE Autopilot or Standard) — the credential proxy is a native sidecar, and `SidecarContainers` is beta and on by default from 1.29 (alpha and off in 1.28, GA in 1.33)
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
  whatever created the Secret supplies them; the Terraform full-install
  composition does.

  Two more, `SANDBOX_SSH_PRIVATE_KEY` and `SANDBOX_SSH_PUBLIC_KEY`, are the
  agent's keypair for the shell sandbox, and they are the one pair the chart
  cannot generate: sprig can make an ed25519 private key but has no function
  that encodes the public half in `authorized_keys` form. Supply both or
  neither. The Terraform full-install composition generates them and
  `upgrade.sh` backfills them, so only a bare `helm install` has to supply them
  by hand. Given the public half, the chart
  also renders `<platformAgent.name>-shell-authorized-keys`, the single-entry
  Secret the sandbox mounts — the sandbox never mounts the credential Secret
  itself. Without the pair nothing breaks on an install that leaves
  `harness.experimental.shellSandbox` off, which is the default; with it on, the
  agent has no key to dial the sandbox with. See
  [`docs/designs/agent-shell-sandboxing.md`](../../docs/designs/agent-shell-sandboxing.md).

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

These commands also sandbox the agent under the `gvisor` RuntimeClass, which the
chart enables by default. On a cluster that has no such RuntimeClass the
operator reports `RuntimeClassNotFound` and never writes the agent Deployment;
add `--set platformAgent.deployment.availability.runtimeClassName=""` to run on
the standard container runtime. See
[Agent runtime knobs](#agent-runtime-knobs) for what the sandbox needs.

**Upgrading an existing release picks this up too.** Helm applies the new
chart's defaults for any key your release does not already set, so a release
installed before this default and upgraded without pinning the value starts
asking for the sandbox. On a cluster with no `gvisor` RuntimeClass that upgrade
is quiet rather than loud: the operator stops at its RuntimeClass check before
touching the workload, so the agent Deployment from the previous reconcile keeps
running on the standard runtime — and every later change to the CR goes
unapplied — while `.status` reports `Degraded` with `RuntimeClassNotFound`.
`helm upgrade` itself reports success. Pass the same `--set …runtimeClassName=""`
to stay on the standard runtime, or check
`kubectl get platformagent -n kubeagents-system -o jsonpath='{.items[0].status}'`
after the upgrade.

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
three images at reconcile time that appear in no chart template — the agent
image for a `PlatformAgent` that omits `spec.deployment.image`, the shell
sandbox StatefulSet it renders beside every agent pod, and the fluent-bit
logging sidecar it injects into that pod — so the chart passes all three to the
operator as `PLATFORM_AGENT_IMAGE`, `AGENT_SANDBOX_IMAGE`, and
`FLUENT_BIT_IMAGE`. Without that a mirrored install reaches `ghcr.io` and Docker
Hub minutes after `helm install` reported success. `CREDENTIAL_PROXY_IMAGE` is
deliberately not passed: the operator derives the broker image from the agent
image by swapping the trailing name, so it follows the mirror on its own. The
sandbox image cannot be derived that way — it is a separate repository — which
is why it has to be named.

The prefix is not a per-image default — it replaces every image's registry and
path, keeping the trailing name, because that is the flat layout
`make mirror-images` writes. Setting `litellm.image.repository` while
`global.imageRegistry` is set therefore changes only the name the prefix is
joined to, not where the image is pulled from. To place images individually —
most on the mirror, one somewhere else — leave `global.imageRegistry` empty and
give each `*.image.repository` its full mirrored path instead; the operator's
`PLATFORM_AGENT_IMAGE`, `AGENT_SANDBOX_IMAGE`, and `FLUENT_BIT_IMAGE` are
rendered from those values either way.

Anything in `operator.extraEnv` is appended after the env vars above and
therefore wins.

`global.imagePullSecrets` is the pull identity for a mirror the nodes' own
credentials cannot read — Harbor or Artifactory with token auth, rather than an
in-project Artifact Registry. An entry is either a bare Secret **name**, so a
single one is reachable with `--set global.imagePullSecrets[0]=regcred`, or the
`{name: <secret>}` map a `PodSpec` takes; any other shape fails the render. It
reaches the same two populations `global.imageRegistry` does: every pod the chart renders
(operator, LiteLLM, the pre-delete cleanup Job) and the agent pods the operator
renders, via `IMAGE_PULL_SECRETS` on the manager and `spec.deployment.imagePullSecrets`
on the `PlatformAgent`. A hand-written `PlatformAgent` that sets that field
replaces the operator's default rather than adding to it.

The Secrets are referenced, never created: keeping registry credentials out of
Helm release data is the point, and the chart has no way to write one that would
not end up there. Create them yourself before installing, which for the usual
case means creating the namespace first, since Helm has not made it yet:

```bash
kubectl create namespace kubeagents-system
kubectl create secret docker-registry regcred \
  --namespace kubeagents-system \
  --docker-server=harbor.example.com \
  --docker-username=robot\$kube-agents \
  --docker-password="$TOKEN"
```

Both commands are idempotent against what Helm then finds. The cleanup Job is
the one to get right: it is a `pre-delete` hook, so a pull it cannot
authenticate fails `helm uninstall` at the one moment the operator is still
running to clear the CR's finalizer.

It does not reach cert-manager, which this chart never renders and which
`operator.webhooks.enabled` requires you to have installed already. Pull that
one from the mirror through its own chart's values.

### LiteLLM gateway

The agent's baked default model endpoint is
`http://litellm.<namespace>.svc.cluster.local/v1`, so the chart deploys the
LiteLLM gateway by default (`litellm.enabled=true`), mirroring
`k8s-operator/config/integrations/litellm/base`. `litellm.modelProvider`
(gemini/anthropic/openai/vertex_ai) picks which provider `model-default` routes to
— the matching API key must be in the credentials Secret, except `vertex_ai`, which
uses Workload Identity (below); `litellm.modelDefaultName`
overrides the per-provider default model. Set `litellm.enabled=false`
only if you operate your own gateway at that address. LLM-call telemetry is
opt-in (`litellm.otel=true`) — enable it only on clusters that run a reachable
collector, since without one the otel callback aborts every LLM request on DNS
failure.

`litellm.rollingUpdate.maxUnavailable` defaults to `1` so that a rollout can
replace a Pod in place. Set it to `0` for a zero-downtime rollout, but only
where the namespace has quota headroom for one more LiteLLM Pod: at `0` the
surge Pod is mandatory, and a `ResourceQuota` with no room for it stalls the
rollout instead of completing it. At the default `litellm.replicaCount` of 2 one
replica keeps serving either way; at `replicaCount: 1` the default of `1` means
a rollout drops the only Pod before its replacement is ready, so LiteLLM is
unreachable for up to the three minutes its `startupProbe` allows. `values.yaml`
states the trade in full.

### Hindsight memory store

`hindsight.*` renders the agents' long-term memory store — the Hindsight API
Deployment, the Postgres/pgvector StatefulSet behind it, an ingress-only
NetworkPolicy standing in for the database's deliberate lack of a password,
and a PodMonitoring. `hindsight.enabled` is a tri-state: `null` (the default)
follows `platformAgent.harness.memory.provider`, so selecting a
Hindsight-backed provider (`kube_agents_memory`, `hindsight`) brings the store
with it and everything else renders nothing; `true`/`false` override. The
image pins mirror `images.json`; `hindsight.postgresql.storage` sizes the
volumeClaimTemplate (immutable once the StatefulSet exists), and the PVC —
which **is** the memory — survives uninstall.

### GitHub token minter

`githubMinter.*` renders the minty Deployment, Service, NetworkPolicy,
Workload Identity KSA, and rule ConfigMap, plus the `github-app-credentials`
Secret when `githubMinter.appId` is set (leave it empty to manage that Secret
yourself). `org` and `repo` are required when enabled. This is the Kubernetes
half only: the minter GSA, its Workload Identity binding, and the import-only
KMS signing key come from `terraform/modules/github-minter`, and the App
private key must be imported into that key (see the module README) before the
Deployment passes its readiness probe.

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

#### Vertex AI (`litellm.modelProvider=vertex_ai`)

Vertex AI has no API key. The gateway calls
`projects/<litellm.vertex.projectId>/locations/<litellm.vertex.location>`
as a Google Service Account reached through Workload Identity. `projectId`
defaults to `platformAgent.harness.projectId`; `location` defaults to `global`
rather than the harness location, since a model is only callable from a
location that serves it. Set a region for a data-residency requirement or a
Model Garden partner model: [Concepts → Inference gateway](https://gke-labs.github.io/kube-agents/concepts/inference-gateway/#vertex-ai-and-model-garden). That GSA, its
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
`model_provider = "vertex_ai"` — the second `kube-agents-iam` module
instantiation creates the identity and roles, and the chart values above carry
the annotated KSA.

### Turning telemetry off

A cluster with no collector needs nothing done: when discovery completes and
finds none — a plain `gke-cluster` module cluster has no `gke-managed-otel`
namespace — the operator gives the agent no endpoint and sets
`OTEL_SDK_DISABLED=true` itself. `status.telemetry.otlpEndpointSource` reads
`None`, and the operator re-probes every 15 minutes, so installing a collector
later turns export back on without a restart.

The manual switch is still there for the cases the operator will not decide:
discovery switched off with `OTEL_COLLECTOR_DISCOVERY=false`, an endpoint pinned
through `telemetry.otlpEndpoint`, or a collector that exists but that you do not
want this agent exporting to. `platformAgent.deployment.env` is applied after the
operator's own container environment, so it wins either way:

```yaml
platformAgent:
  deployment:
    env:
      - name: OTEL_SDK_DISABLED
        value: "true"
```

Setting that value to `"false"` re-enables the SDK on a cluster where discovery
found nothing, but on its own it does not produce a working exporter: the
operator emitted no endpoint, so the SDK falls back to `http://localhost:4318`,
and the NetworkPolicy it renders for a `None` agent carries no collector egress
rule. Pair it with `telemetry.otlpEndpoint` if you want the export to land
somewhere.

Use `telemetry.otlpEndpoint` instead when you do have a collector to point at.

### Integrations

- **Google Chat** — `platformAgent.integration.googleChat.enabled=true` plus the
  topic/subscription names (defaults match the `chat-pubsub` Terraform
  module). Requires the Chat Pub/Sub backend to exist
  (`terraform/modules/chat-pubsub`); `projectId`
  is taken from `platformAgent.harness.projectId`. Restrict access via
  `allowedUsers` (empty = everyone).
- **Slack** — `platformAgent.integration.slack.enabled=true`; the bot/app
  tokens are read from the credentials Secret's `SLACK_BOT_TOKEN` /
  `SLACK_APP_TOKEN` keys (the CRD requires both refs when Slack is enabled).
- **Microsoft Teams** — `platformAgent.integration.teams.enabled=true`; the bot
  credentials are read from the credentials Secret's `TEAMS_APP_ID` and
  `TEAMS_APP_PASSWORD` keys. Optional single-tenant lock-down is set via
  `tenantId`, and user authorization is configured via `allowedUsers` (or
  `allowAllUsers: true`). Supports Microsoft Adaptive Cards v1.5 with markdown
  fallback.
- **GitHub** — `platformAgent.integration.github.org` sets the GitHub
  Organization where the GitHub App is installed, and optional
  `platformAgent.integration.github.gitRepo` sets the initial GitOps repository.
  GitOps repositories can also be registered in the ConfigMap by cluster administrators.

Chat, Slack, and Teams each need a one-time manual registration that no install
automation can perform (the Chat app on the Chat API console page pointed at
the Pub/Sub topic; Socket Mode + bot scopes in the Slack app console; Azure Bot
registration & Teams App manifest) —
[INSTALL.md § Enable Chat Integrations](../../INSTALL.md) and
[Microsoft Teams ChatOps Guide](../../docs/chatops/microsoft-teams.md) are the
canonical walkthroughs.

### Agent runtime knobs

`platformAgent.harness.hermes`, `platformAgent.harness.memory`, and
`platformAgent.deployment.availability` expose the remaining PlatformAgent CR
fields, so a chart install can reach every field of the CR without editing it
by hand. Each one defaults
to `null`/`""`, which **omits** the field and lets the CRD's own default apply
— setting `false` is therefore distinct from leaving it unset, and `replicas: 0`
means zero rather than unset.

`platformAgent.deployment.image.pullPolicy` defaults to `Always`. Under
`IfNotPresent` a node that has already cached the tag never
picks up a rebuild, which is the normal case for the Terraform composition's
default `image_tag = "latest"`.

**Consider `IfNotPresent` when you pin the tag.** The chart's own default tag is
`.Chart.AppVersion`, which the release workflow overwrites with the git tag — an
immutable tag, where `Always` buys nothing and costs a registry round-trip on
every pod start. It also removes a fallback: if the agent pod is rescheduled
while ghcr.io is unreachable or rate-limiting, `Always` fails the pull and the
pod sits in `ImagePullBackOff` where `IfNotPresent` would have started from the
node's cache. The chart and the Terraform composition agree on `Always` for the
mutable-tag case they were both written for; an install at a pinned release
tag is the case that wants the override.

Four knobs need context beyond the chart:

- `deployment.availability.runtimeClassName` defaults to `gvisor`, because the
  agent executes model-authored commands and an unsandboxed pod shares the node
  kernel with everything else on the node. That needs a GKE Sandbox node pool on
  a Standard cluster — the `gke-cluster` module's `enable_gvisor_node_pool`
  creates one; Autopilot ships the RuntimeClass natively from GKE
  `1.27.4-gke.800`. Where neither holds, the operator refuses to write the agent
  Deployment and reports `RuntimeClassNotFound` on the PlatformAgent; set the
  value to `""` to run on the standard container runtime instead. Installs
  driven by the Terraform composition never see this default — it always renders
  `runtimeClassName` explicitly, from its own `agent_runtime_class` variable,
  which `install.sh` writes from `--gvisor`. That variable still defaults to
  `""`, so a bare `terraform apply` against the composition leaves the agent
  unsandboxed where a bare `helm install` sandboxes it.
- `harness.experimental.shellSandbox.runtimeClassName` is the same choice for the
  shell sandbox pod, and it is a separate key because the two pods are scheduled
  and sized separately — a node pool that can run one need not be the pool the
  other lands on. It has no default: unset leaves the sandbox on the node's
  standard runtime, and `gvisor` needs the same GKE Sandbox node pool the agent's
  key does.
- `security.workloadIdentityFederation` needs a Workload Identity pool and
  provider trusting the cluster's OIDC issuer, and one
  `roles/iam.workloadIdentityUser` grant on the agent's GSA. Nothing creates
  them: the three `gcloud` commands are in
  [`designs/agent-shell-sandboxing.md`](../../docs/designs/agent-shell-sandboxing.md#setting-up-the-pool).
  Set it with `harness.experimental.shellSandbox.enabled` — the chart fails the
  render if you set one without the other, since federation only takes effect
  when the credential proxy runs beside the sandbox.
- `harness.hermes.dashboardEnabled` defaults to `null`, which leaves the field
  out of the CR so the CRD default (`true`) applies. Set it explicitly when an
  install must pin the dashboard on or off rather than float with the CRD.

### Plugins & Runtime Tuning

`plugins.*` renders optional `AgentPlugin` resources into the main `kube-agents` release:

- `plugins.pubsubPlatform.enabled` (default `false`): Deploys the Cloud Pub/Sub platform adapter (`AgentPlugin/pubsubplatform`), providing Pub/Sub message ingress and kanban task dispatching.
- `plugins.stockoutInvestigator.enabled` (default `false`): Deploys the GKE Stockout Investigator (`AgentPlugin/gkestockoutinvestigator`) targeting the `platform` profile, which investigates autoscaler scale-up failures. Requires `plugins.pubsubPlatform.enabled=true`.

When `plugins.stockoutInvestigator.enabled=true`, the chart automatically seeds `platformAgent.harness.tuning` execution limits (`maxInProgress: 3`, `platform: {apiMaxRetries: 8, maxTurns: 200}`, `cluster: {apiMaxRetries: 8, maxTurns: 150}`). Stockout remediation is long-running and quota-intensive; these limits ensure the platform agent and delegated cluster workers have sufficient turns and retry budgets to diagnose and remediate capacity incidents across the fleet. Explicit settings in `platformAgent.harness.tuning.*` take precedence over these defaults.

### Scoped service accounts

`platformAgent.security.scopedServiceAccounts` maps each GKE cluster the agent
may read to the Google service account that reads it. Empty is the default and
should stay empty: the accounts hold no IAM grant as of 2026-08-12, so a
non-empty list arms the credential broker onto identities that can read
nothing, and every cluster read fails — a mapped cluster gets a powerless
token and a `Forbidden` from GKE, an unmapped one is refused by the broker
before any GKE call. The
`terraform/examples/full-install` composition fills it in from its
`scoped_service_accounts` output when `scoped_clusters` is set. See the site's
[security-and-iam reference](https://github.com/gke-labs/kube-agents/blob/main/docs/site/src/content/docs/reference/security-and-iam.md)
for what the pool does and does not bound.

### ServiceAccount ownership

Exactly one owner creates the agent's KSA, depending on
`platformAgent.security.serviceAccountAnnotations`:

- **Annotations set** (the Workload Identity case): the **operator** creates
  and manages the KSA with those annotations.
- **No annotations**: the operator treats the named KSA as user-managed and
  does not create it — the **chart** renders it instead, so a default install
  still starts.

### Agent-RBAC admission policies

`admissionPolicy.enabled` (default `true`) installs two cluster-scoped
`ValidatingAdmissionPolicy` objects and their bindings, generated from
`k8s-operator/config/admission/agent-rbac-policy.yaml`. They deny agent RBAC
that grants a write or privilege-escalation verb, grants Secrets, or gives a
namespace-tier agent ServiceAccount a cluster-scoped binding. They do **not**
check the rules of a role a binding _references_ — CEL cannot read another
object — and the content policy only selects manifests carrying the
`kube-agents/tier` label; see that file's header.

The template checks `.Capabilities.KubeVersion` as well as this value, so on a
cluster below Kubernetes 1.30 — where the policy API is not yet `v1` — it
renders nothing instead of failing the install. `Chart.yaml` accepts `>=1.29.0-0`,
so that case is inside the supported range and has to work.

Set `admissionPolicy.enabled=false` for a second kube-agents release in a cluster
that already has them: the objects are cluster singletons with fixed names, so
Helm refuses the second install on ownership rather than duplicating them.

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
stranded CR it prevents. It follows `global.thirdPartyImageRegistry` like the
other third-party images, so a mirrored install needs nothing extra; set
`platformAgent.cleanupHook.image.repository` and `.tag` to point somewhere else
entirely — any image with `kubectl` and `/bin/sh` works.

> **Breaking:** `platformAgent.cleanupHook.image` was a single string
> (`alpine/k8s:<tag>`) and is now a `{repository, tag}` map, because the
> registry rewrite needs the two halves separately. A values file that still
> sets the string form fails the render rather than installing something wrong:
>
> ```
> coalesce.go:286: warning: cannot overwrite table with non table for
>   kube-agents.platformAgent.cleanupHook.image
> Error: template: kube-agents/templates/platform-agent-cr-cleanup.yaml:94:85:
>   can't evaluate field repository in type interface {}
> ```
>
> Split it:
>
> ```yaml
> platformAgent:
>   cleanupHook:
>     image:
>       repository: docker.io/alpine/k8s # was: image: alpine/k8s:<tag>
>       tag: "<tag>" # or drop both lines to take the chart default
> ```
>
> The default reference also gained its registry — the implied `alpine/k8s` is
> now spelled `docker.io/alpine/k8s`. That resolves to the same image on a
> default install; it is written out because a prefix cannot be prepended to a
> reference whose registry is implied. The default tag itself is in
> `values.yaml`, mirrored from `images.json`.

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
- The CRD, RBAC and admission-policy manifests under this chart are generated
  copies of `k8s-operator/config/` — edit the source and run `make chart-sync`
  (CI enforces this via `make chart-check`).

See [docs/site/src/content/docs/deploy/release-versioning.md](../../docs/site/src/content/docs/deploy/release-versioning.md) for versioning rules.
