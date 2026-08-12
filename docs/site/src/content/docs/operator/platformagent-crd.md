---
title: PlatformAgent CRD
description: The single custom resource the operator reconciles.
sidebar:
  order: 1
---

The `PlatformAgent` resource declares everything the operator needs to run one Platform Agent instance: which Hermes image, which service account, which chat integrations, and which framework-level toggles.

- **API group / version**: `kubeagents.x-k8s.io/v1alpha1`
- **Kind**: `PlatformAgent`
- **Source**: [`k8s-operator/api/v1alpha1/platformagent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/platformagent_types.go)
- **Sample**: [`k8s-operator/examples/platformagent.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/platformagent.yaml)

## Top-level shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platformagent
  namespace: kubeagents-system
spec:
  harness: { ... } # execution environment + framework
  deployment: { ... } # container image, pull policy, containers, volumes
  security: { ... } # service account + Workload Identity
  telemetry: { ... } # OTLP collector endpoint (optional)
  integration: { ... } # Google Chat, Slack, GitHub
```

`spec.deployment`, `spec.security`, and `spec.telemetry` are inlined from the shared `AgentSpec`, so they are common to every agent type. `spec.harness` is required; `spec.integration` and `spec.telemetry` are optional.

## `spec.harness`

Framework-level settings passed to Hermes. `clusterName`, `location`, and `projectId` are all
required — the API server rejects a `PlatformAgent` that omits any of them. The credential proxy
only renders its kubeconfig bootstrap (the `gcloud container clusters get-credentials` that gives
the agent a usable kubectl context) when it has the complete triple; with one missing, every
`kubectl` the agent runs resolves to `localhost:8080` instead of a cluster.

| Field                                          | Type   | Purpose                                                                                                                                                      |
| ---------------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `clusterName`                                  | string | Logical cluster name (e.g. `cluster-a`). Surfaces in observability and chat replies.                                                                         |
| `location`                                     | string | Cloud region (e.g. `us-central1-a`).                                                                                                                         |
| `projectId`                                    | string | GCP Project ID of the cluster. Required.                                                                                                                     |
| `hermes.dashboardEnabled`                      | bool   | Toggle the Hermes dashboard endpoint. Default `true`.                                                                                                        |
| `hermes.pluginsDebug`                          | bool   | Enable plugin-level debug logging. Default `false`.                                                                                                          |
| `hermes.agentHome`                             | string | Path to the `AGENT_HOME` directory. Default `/opt/data`.                                                                                                     |
| `hermes.apiServerSecretRef.name` + `key`       | string | `Secret` holding the Hermes API server key (`API_SERVER_KEY`).                                                                                               |
| `hermes.sessionKVApiKeySecretRef.name` + `key` | string | `Secret` holding the bearer token for the pod-local Session KV server (`SESSION_KV_API_KEY`). Optional; absent, the server rejects every request with `503`. |
| `hermes.sessionKVSaltSecretRef.name` + `key`   | string | `Secret` holding the HMAC salt used to pseudonymise chat identities (`SESSION_KV_SALT`). Optional; absent, the agent generates a per-pod salt and warns.     |
| `memory.memoryEnabled`                         | bool   | Toggle framework memory persistence. Default `false`.                                                                                                        |
| `memory.provider`                              | string | Memory provider implementation. Default `multiuser_memory`.                                                                                                  |
| `memory.userProfileEnabled`                    | bool   | Toggle per-user memory profiling. Default `false`.                                                                                                           |
| `tuning.<persona>.apiMaxRetries`               | int    | Model-call retries before a run gives up. Unset = Hermes default `3`.                                                                                        |
| `tuning.<persona>.maxTurns`                    | int    | Iterations allowed in a single turn. Unset = Hermes default `90`, except `platform` (see below).                                                             |
| `tuning.maxInProgress`                         | int    | Board-wide cap on concurrent kanban workers. Unset = uncapped (upstream).                                                                                    |

`sessionKVApiKeySecretRef` is optional in the API but not in practice, and the `503` above is the
milder half of what its absence costs. The `k8s-event-watcher` in the credential sidecar
authenticates to that same server, treats an empty `SESSION_KV_API_KEY` as fatal, and exits on every
start — so no cluster events are watched at all, while the container stays Ready and the CR
`.status` says nothing. An installation upgraded from before the key existed is the case that lands
here; add the key to the agent Secret and restart the pod.

### `spec.harness.tuning`

Execution limits per agent persona, where `<persona>` is one of `default` (the Chat Agent front
door), `platform` (the Platform Agent), or `cluster` (**every** Cluster Agent), plus the board-wide
`maxInProgress`.

**Everything here is opt-in.** The operator pins nothing of its own: what a fleet needs depends on
its model quota and on what its agents actually do, so a deployment doing short interactive work
should not inherit limits raised for long-running batch work. Unset therefore means whatever the
profile's own `config.yaml` carries, and the `default` and `cluster` configs set no execution limit
of their own — Hermes' defaults apply there, 3 retries, 90 iterations, uncapped dispatch. The
`platform` profile is the exception: the image ships `agent.max_turns: 250` in
`agents/platform/config.yaml` because the fleet audits outgrow 90, and
[Config reference](/kube-agents/reference/config/#agent) is canonical for why. Setting
`tuning.platform.maxTurns` here still wins — the overlay is merged after the image force-sync — and
removing it restores the image's value rather than Hermes'.

```yaml
spec:
  harness:
    tuning:
      maxInProgress: 1 # board-wide: serialise all kanban workers
      platform:
        apiMaxRetries: 8
        maxTurns: 200
      cluster:
        apiMaxRetries: 8
        maxTurns: 150
```

Raised limits belong with the workload that needs them, not with the platform. A long-running,
quota-hungry agent plugin should ship its own tuning — as a patch its installer applies — so that a
deployment without it stays on Hermes defaults, and installing the plugin brings the limits it
requires along with it.

The GKE Stockout Investigator is the worked example:
[`agentplugins/gke-stockout-investigator/tuning.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agentplugins/gke-stockout-investigator/tuning.yaml)
records the reasoning behind each number, and its `install.sh` applies it.

The keys are personas rather than profile names because the profiles are not all known when the CR
is written: Cluster Agent profiles are scaffolded at runtime, one per managed cluster, with
generated names like `cluster-<project>-<cluster>-<region>`. `cluster` therefore applies to all of
them at once — including ones onboarded after the pod last started, which pick the limits up as they
are scaffolded.

Both limits matter because they fail the same way, and it is not an obvious way. A run that
exhausts either stops mid-task without ever calling a terminal kanban tool. The card is charged a
`timed_out` failure whose error text names how the turn ended — `Iteration budget exhausted (N/M)`
for `maxTurns`, `turn_exit_reason=all_retries_exhausted_no_response` for `apiMaxRetries` — and
retrying re-runs into the same wall, so read that text and the upstream error rate before suspecting
the worker. An exit like this that reaches the dispatcher unexplained surfaces instead as a
**protocol violation**, which describes the symptom and hides the cause; the image narrows that
window in [`deploy/docker/patches/kanban_guardrail_exit.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/kanban_guardrail_exit.py).

Sizing notes: `maxTurns` is consumed mostly by repository exploration, so scale it against how much
the agent has to read rather than how complex the request is. `apiMaxRetries` exists because
Hermes' default of `3` assumes an interactive session where a human retries; a background worker
has nobody to retry it, so a transient burst of upstream 429s or 503s simply ends the run. Raising
`maxTurns` interacts with `maxInProgress`: under a serial dispatcher, one long-running worker
holds the only slot and blocks every other profile, so raising one is a reason to reconsider the
other.

## `spec.deployment`

Abstracts the pod/deployment configuration. The controller synthesises a `Deployment` from these plus the workspace ConfigMaps. Available fields:

- `image` — container image repository.
- `tag` — image tag. Applies only when `image` is set without a tag or digest, falling back to `latest` there; when `image` is omitted, the operator's build-injected default version applies instead.
- `imagePullPolicy` — one of `Always`, `Never`, `IfNotPresent`. Default `IfNotPresent`.
- `browserArgs` — extra command-line args for the agent's browser (e.g. `--no-sandbox`).
- `runtimeClassName` — pod runtime class (e.g. `gvisor`).
- `env` — additional container environment variables.
- `initContainers` / `sidecars` — standard init and sidecar containers.
- `extraVolumes` / `extraVolumeMounts` — custom volumes and mounts for the main container.
- `sidecarVolumes` — custom volumes for the sidecar containers.
- `podAnnotations` — annotations applied to the generated pod template.
- `scaleToZero` — when `true`, scales the deployment to 0 replicas (idle cost saving).

Default image: `ghcr.io/gke-labs/kube-agents/platform-agent:<operator release version>` (release builds inject the version; development builds fall back to `latest`), overridable operator-wide via the `PLATFORM_AGENT_IMAGE` env var on the controller manager (see [Docker images § Private / custom registry](/kube-agents/deploy/docker-images/#private--custom-registry)). Rebuild with `make dev-rebuild-agent ARGS="platform"` for local iteration.

## `spec.security`

- `serviceAccountName` — the KSA the pod runs as. `kubeagents-platform-agent` by convention.
- `serviceAccountAnnotations` — passed through to the KSA. Typically holds `iam.gke.io/gcp-service-account` for Workload Identity binding.

The Workload Identity target GSA (`kubeagents-platform-gsa@<project>.iam.gserviceaccount.com`) is created and bound by `provision_04_gcp_iam.sh` with one of these permission sets:

- `read-only` (default)
- `gke-admin`
- `custom` (roles supplied via `PLATFORM_AGENT_CUSTOM_ROLES`)

## `spec.telemetry`

- `otlpEndpoint` — the OTLP/HTTP collector **base** URL (no `/v1/traces` suffix; the exporters append their own per-signal path). Up to 2048 characters, `http://` or `https://`.

Optional, and omitting it is the point: with the field absent the operator discovers an in-cluster collector and falls back to GKE Managed OpenTelemetry. Setting it pins the endpoint and suppresses discovery. The full precedence ladder, the discovery order, and the Helm value that drives LiteLLM and the NetworkPolicy alongside this field are on [Deploy → Telemetry](/kube-agents/deploy/telemetry/#pointing-at-your-own-collector).

## `spec.integration`

Enables external integrations. Only the enabled ones need to be present.

- **`googleChat`** — `enabled` (default `false`), `projectId`, `topicName`, `subscriptionName`, `allowedUsers`, `homeChannel`, and `mode` (`default` or `debug`, default `default`). When `enabled`, `projectId`, `topicName`, and `subscriptionName` are required (enforced by a CEL validation rule). Populated by `provision_05_gcp_gchat.sh`.
- **`slack`** — `enabled` (default `false`), `botTokenSecretRef` and `appTokenSecretRef` (Secret refs, required when enabled), `allowedUsers`, `homeChannel`, and `homeChannelName`. Populated by `provision_06_slack.sh` when `SLACK_ENABLED=true`.
- **`github`** — `gitRepo`, the target GitOps repository URL for the agent environment (up to 2048 characters). Supports HTTPS/HTTP (`https://`, `http://`), SCP-style SSH (`git@...`), SSH/Git protocols (`ssh://`, `git://`), and bare `owner/repo` shorthand (e.g. `gke-labs/kube-agents`). Rejects URLs containing whitespace, control characters, or invalid syntax at admission (`failurePolicy: Fail`). If an invalid URL is encountered during reconciliation, `SETTINGS.md` defaults to `None` and a `Degraded` condition (`Reason: InvalidGitRepoURL`) is surfaced on the resource status. Populated by `provision_10_deploy_github_minter.sh`.

See [`k8s-operator/api/v1alpha1/platformagent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/platformagent_types.go) for the exact struct definitions.

## `status`

The operator writes observed state to the `status` subresource:

| Field                            | Type   | Purpose                                                                                                |
| -------------------------------- | ------ | ------------------------------------------------------------------------------------------------------ |
| `phase`                          | string | Overall state (`Pending`, `Provisioning`, `Ready`, `Failed`).                                          |
| `address`                        | string | Fully qualified domain name (FQDN) of the agent service.                                               |
| `lastReconcileTime`              | time   | Timestamp of the last status update.                                                                   |
| `conditions`                     | list   | Standard `metav1.Condition` observations, keyed by `type`.                                             |
| `deploymentStatus.name`          | string | Name of the underlying Deployment.                                                                     |
| `deploymentStatus.readyReplicas` | int32  | Number of fully ready replicas.                                                                        |
| `serviceStatus.endpoint`         | string | Primary URL/IP (with protocol and port) to reach the agent.                                            |
| `storageStatus.bound`            | bool   | Whether the primary PVC has been provisioned.                                                          |
| `telemetry.otlpEndpoint`         | string | The OTLP collector the agent was wired to.                                                             |
| `telemetry.otlpEndpointSource`   | string | Which rung of the ladder answered: `DeploymentEnv`, `Spec`, `OperatorEnv`, `Discovered`, or `Default`. |

## How config reaches each profile

A deployment runs several Hermes **profiles** from one pod: `default` (the Chat Agent front door),
`platform`, and one `cluster-*` profile per managed cluster. Every one of them is configured by an
overlay merged into an image-built base at startup, but what the operator puts in the `default`
profile's overlay, and what happens to the runtime's own writes, are both different.

| Profile     | Delivery                                                                                                 | Who owns the file                         |
| ----------- | -------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `default`   | Image-built base + `profile-default.overlay.yaml`, which carries the whole rendered config               | Shared three ways — see below             |
| `platform`  | Image-built base + `profile-platform.overlay.yaml` merged at startup                                     | Image owns the base, operator the overlay |
| `cluster-*` | Image-built base + `profileclass-cluster.overlay.yaml`, plus `profile-<name>.overlay.yaml` if one exists | Image owns the base, operator the overlay |

A cluster profile is the only one that can take two overlays: the class overlay carries
`tuning.cluster`, which applies to all of them, and a plugin targeting one specific cluster produces
a `profile-<name>` overlay for it as well. The class overlay merges first, so the per-profile file
wins any conflict.

**Why `default` is rendered whole but still merged.** It is the only profile whose config the
operator can fully own, so `renderConfigYAML` emits all of it rather than a few keys, and it is the
one change-control boundary: the deployed front door matches CR-derived intent and cannot drift from
a stale copy on the PVC or an image/operator version skew. (It is _not_ a security sandbox — see the
[AgentPlugin trust boundary](/kube-agents/reference/security-and-iam/#change-control--safety).)

It is also the only profile whose config the _running agent_ writes to: `/sethome` records the home
channel there, and the monitoring policy mints `monitoring.install_id`. So the render is merged in
rather than mounted over the file. A mount would make the path read-only and fail every one of those
writes — `/sethome` with a permission error, the rest silently.

Merging it means three parties write one file, so the entrypoint reconciles them with a three-way
merge rather than a plain overlay: the image base and the operator's overlay give the intended
config, and the runtime's own edits since the last start are carried onto it. **The operator wins any
key both it and the runtime changed** — that is what makes editing the CR mean anything — and the
runtime keeps the rest. `deploy/shared/default_profile_config.py` documents the per-key rules.

One consequence is worth knowing before you edit `renderConfigYAML`: because the image base and the
overlay both declare the same file, a list named in both is unioned, not replaced. Dropping an entry
from the render alone does nothing while [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml)
still lists it. The operator's `TestRenderConfigYAMLListsMatchChatConfig` fails when the two diverge.

**Why the others get overlays.** Their `config.yaml` is assembled at image build time by merging the
shared defaults with that profile's own overlay, content the operator does not have; a `cluster-*`
config additionally carries a runtime `cluster_identity` stamp that the reconciler matches profiles
to clusters by. Rendering either file in full would fork the source of truth and, for cluster
profiles, strip that identity record.

Every overlay is a key in the one `<agent>-config` ConfigMap, so a change to any of them moves the
config hash and rolls the pod. That restart is required, not incidental: the merge happens once at
startup, so a live ConfigMap update without a restart would be a no-op.

Startup is not the only moment a merge happens. Onboarding a cluster scaffolds a new profile without
changing the ConfigMap, so nothing rolls the pod; that profile applies the overlays itself as it is
created. Without it a Cluster Agent created between two pod starts would run on Hermes' own defaults
however the CR is tuned.

**Ordering.** The entrypoint force-syncs each profile's image-owned files first, then merges the
overlays. The reverse order would silently erase every overlay on each restart. The `default`
profile's `config.yaml` is the exception to the force-sync — it is rebuilt by the three-way merge
above, because a force-sync is exactly what would throw the runtime's edits away.

**Merge semantics.** Maps merge recursively, lists union (so `plugins.enabled` accumulates), and
scalars are replaced by the overlay. Precedence, lowest to highest: Hermes built-in default → the
value committed in `agents/<persona>/config.yaml` → the operator overlay from the CR.

**Two writers, two authorities.** Both `spec.harness.tuning` (operator policy) and an
`AgentPlugin`'s `spec.config` (plugin-supplied) land in the same overlay file, but not with equal
rights. A plugin's config is restricted to `approvals`, `platforms`, and `platform_toolsets`; the
`agent` subtree holding the execution limits is dropped from plugin config and writable only by the
operator. That is a coordination boundary rather than a security one — plugin code executes
in-process and could change these at runtime — but it keeps limits with board-wide consequences in
one reviewable place.

## Reconcile behavior

- On create/update, the controller ensures the Deployment, Service, ServiceAccount, and ConfigMaps match the spec.
- On delete, it garbage-collects owned resources.
- The admission webhook (behind cert-manager) validates the spec before it's persisted; it enforces at most one `PlatformAgent` per project, forbids sensitive environment variable overrides (`API_SERVER_KEY`, `HERMES_HOME`) and privileged containers/volumes (`hostPath`), and acts as a name-based tripwire against obvious privileged service account names (`cluster-admin`, `system:admin`). Note that full RBAC least-privilege enforcement is handled by controller- and pipeline-level policies rather than the admission webhook.
- The `kubeagents.x-k8s.io/prevent-deletion: "true"` annotation on a `PlatformAgent` blocks deletion of the resource via the validating webhook (`ValidateDelete`). This serves as an accidental-deletion guardrail rather than an authorization control — `ValidateUpdate` does not block removing the annotation, so any principal with update permissions can patch the annotation off before deleting.
- `provision_08_deploy_platform_agent.sh` renders and applies the CR; you can also edit it directly with `kubectl edit`.

## Where to go next

- [Development](/kube-agents/operator/development/) — build and test the controller locally.
- [Provisioning scripts](/kube-agents/operator/provisioning-scripts/) — how the CR gets applied in a fresh install.
