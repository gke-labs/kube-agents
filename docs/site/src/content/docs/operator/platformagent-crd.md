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
| `memory.provider`                              | string | Memory provider implementation. Default `multiuser_memory`; `none` for none. See below.                                                                      |
| `memory.userProfileEnabled`                    | bool   | Toggle per-user memory profiling. Default `false`.                                                                                                           |
| `eventWatcher.enabled`                         | bool   | Start the `k8s-event-watcher`. Default `true`; `false` is the emergency stop for an event storm (see below).                                                 |
| `tuning.<persona>.apiMaxRetries`               | int    | Model-call retries before a run gives up. Unset = Hermes default `3`.                                                                                        |
| `tuning.<persona>.maxTurns`                    | int    | Iterations allowed in a single turn. Unset = Hermes default `90`, except `platform` (see below).                                                             |
| `tuning.maxInProgress`                         | int    | Board-wide cap on concurrent kanban workers. Unset = operator default `2`.                                                                                   |

`sessionKVApiKeySecretRef` is optional in the API but not in practice, and the `503` above is the
milder half of what its absence costs. The `k8s-event-watcher` in the credential sidecar
authenticates to that same server, treats an empty `SESSION_KV_API_KEY` as fatal, and exits on every
start — so no cluster events are watched at all, while the container stays Ready and the CR
`.status` says nothing. An installation upgraded from before the key existed is the case that lands
here; add the key to the agent Secret and restart the pod.

### `spec.harness.memory`

`provider` picks which long-term memory implementation the agents load. Two ship in this repository,
and the difference between them is the whole choice:

| Value                          | Fits                       | What it costs to run                                      | What it gives                                            |
| ------------------------------ | -------------------------- | --------------------------------------------------------- | -------------------------------------------------------- |
| `multiuser_memory` _(default)_ | small or personal installs | nothing — a per-user Markdown file inside the pod         | verbatim recall of everything, no ranking or search      |
| `kube_agents_memory`           | enterprise deployments     | a Hindsight API server and a Postgres database in-cluster | ranked recall, per-user and shared scopes, consolidation |
| `none`                         | —                          | nothing                                                   | no provider; Hermes' built-in store only                 |

The split is about how the store is read, not about how good it is. The file provider concatenates
everything into the system prompt on every turn, so it is bounded by the context window; Hindsight
retrieves only what a question needs, so its cost per turn barely moves as the store grows. A fleet
of a few clusters and a handful of people will not reach the bound, and paying for a database there
buys nothing.

Anything else is passed through to Hermes untouched, so its own external providers (`hindsight`,
`mem0`, `openviking`, …) work if you bring their configuration. `none` is this API's spelling of
Hermes' empty string, which cannot be expressed here: an absent field takes the CRD default.

Only a Hindsight-backed provider reaches the specialist profiles, because only that one can be made
read-only and scoped by tag. Under any other value the specialists get no provider at all and the
Chat Agent keeps the store to itself.

`memoryEnabled` and `userProfileEnabled` are a **different** mechanism — Hermes' built-in
`MEMORY.md` / `USER.md` files, which have no per-user scoping. Both providers above replace that
store rather than supplement it, so both run with `memoryEnabled: false`.

The installer's `MEMORY_ENABLED` variable is that same built-in store and nothing more; provisioning
step 8 copies it into this field unchanged. It is not a master switch — whether the agent remembers
anything is `provider`'s question, and `none` is how that answers no.

The provisioning scripts read only the provider: step 13 deploys Hindsight when `MEMORY_PROVIDER` is
Hindsight-backed, which is what a stock install gets, and nothing when it is `multiuser_memory` or
`none`. See
[`docs/designs/memory.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/memory.md).

### `spec.harness.eventWatcher`

The `k8s-event-watcher` runs in the credential sidecar, streams warning events from every managed
cluster, and posts each surviving incident to the pod-local Session KV server, which opens an
autonomous triage session for it. `enabled: false` stops it from starting at all.

```bash
kubectl patch platformagent platform-agent -n kubeagents-system --type merge \
  -p '{"spec":{"harness":{"eventWatcher":{"enabled":false}}}}'
```

The field has to exist in the installed CRD for that patch to mean anything, and
[Helm installs CRDs on first install but never upgrades them](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md).
A chart-upgraded install therefore needs the CRDs applied first — worth doing ahead of the incident
rather than during it, since a client that does not send strict field validation gets the unknown
field pruned and an emergency stop that reports success and does nothing.

```bash
kubectl apply --server-side -f k8s-operator/config/crd/bases/
```

`--server-side` is not optional here. Client-side apply stores the whole object in the
`kubectl.kubernetes.io/last-applied-configuration` annotation, and this CRD is far past the 262144-byte
annotation cap, so a plain `kubectl apply` fails with `metadata.annotations: Too long` and leaves the
CRD unchanged. `make install` in `k8s-operator/` applies them the same way.

**This is an emergency stop, not a tuning knob.** It exists for the case where events arrive faster
than the agent can triage them — a fleet-wide rollout gone wrong, a node pool flapping — and the
cheapest way to get the agent back is to cut the inflow rather than chase the cards it has already
been handed. It is all-or-nothing across every watched cluster: the watcher's reason and namespace
filters live in the sidecar's entrypoint and are not exposed on the CRD, so there is no way to
silence one noisy namespace through this field. If the board is merely busy rather than swamped,
[`tuning.maxInProgress`](#specharnesstuning) is the knob for that — it throttles how many cards run at
once without losing the events.

Three consequences before you press it:

- **It rolls the pod.** The value reaches the sidecar as an environment variable, so changing it
  rewrites the pod template. During a storm that restart is usually wanted anyway — it is also what
  ends the sessions already running.
- **It stops the inflow only.** Kanban cards and sessions created from events already delivered keep
  running and still have to be dealt with on the board. It reclaims nothing either: the watcher's
  kubeconfig, token projection, and mounts stay in place, and the sidecar keeps the memory request
  sized for the informer and dedup caches it is no longer running.
- **Nothing turns it back on.** An install left with the watcher off has no incident detection at
  all, and the container stays Ready throughout — the readiness probe covers the credential proxy,
  not the watcher. Two things say otherwise: a line in the sidecar log naming the consequence, and
  the `EventWatcher` condition on the CR ([`status`](#status) below). Set `enabled: true`, or remove
  the field, to start watching again.

Unset means enabled. The watcher is how a fleet notices its own incidents, so an install that never
mentions the field — which is every install today — keeps watching, and only an explicit `false`
turns it off.

### `spec.harness.tuning`

Execution limits per agent persona, where `<persona>` is one of `default` (the Chat Agent front
door), `platform` (the Platform Agent), or `cluster` (**every** Cluster Agent), plus the board-wide
`maxInProgress`.

**The per-run limits are opt-in.** The operator pins nothing of its own there: what a fleet needs
depends on its model quota and on what its agents actually do, so a deployment doing short
interactive work should not inherit limits raised for long-running batch work. Unset therefore means
whatever the profile's own `config.yaml` carries, and the `default` and `cluster` configs set no
execution limit of their own — Hermes' defaults apply there, 3 retries and 90 iterations. The
`platform` profile is the exception: the image ships `agent.max_turns: 250` in
`agents/platform/config.yaml` because the fleet audits outgrow 90, and
[Config reference](/kube-agents/reference/config/#agent) is canonical for why. Setting
`tuning.platform.maxTurns` here still wins — the overlay is merged after the image force-sync — and
removing it restores the image's value rather than Hermes'.

**`maxInProgress` is not.** Unset renders `2`, because the untuned case is the one that cannot
absorb the alternative — see [Why dispatch is capped by default](#why-dispatch-is-capped-by-default)
below. Set it on the CR to raise or lower that.

```yaml
spec:
  harness:
    tuning:
      maxInProgress: 4 # board-wide; raises the operator's default of 2
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
`maxTurns` interacts with `maxInProgress`: a long-running worker holds its slot for the whole task
and there are only `maxInProgress` of them, so raising one is a reason to reconsider the other.

#### Why dispatch is capped by default

A kanban worker is not a coroutine. It is a full `hermes … kanban task` process — a few hundred MiB
resident once its MCP proxies are up, and alive for as long as the task takes, which for an incident
triage is minutes rather than seconds. Uncapped, the dispatcher starts one per queued card, and a
burst of cluster events queues them faster than they retire.

What follows is invisible in the places you would look. The cgroup OOM killer takes a worker, not
PID 1, so there is no container restart and no Kubernetes event; the pod stays `Running` and the
only trace is `pid not alive` in the kanban ledger. The dispatcher's retry budget is 1, so the card
is stranded rather than re-dispatched, and the work it stood for is never done — a triage report
that simply never arrives, with nothing anywhere reporting a failure.

`2` is a floor for a deployment that has not measured itself, not a recommendation. It is chosen to
hold on the smallest pod anyone runs, and because the cost of being wrong is asymmetric: too low
delays a delegated task, too high loses it silently. Raise it once you know your worker footprint
and your model quota — that quota is the other shared resource, and for most deployments it binds
before memory does.

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

Three condition types appear in `conditions`, and only the first is always present:

| Type           | Written                                      | Meaning                                                                                                                      |
| -------------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| `Ready`        | Always                                       | Tracks `phase`; its `reason` and `message` carry whatever the reconcile is waiting on.                                       |
| `Degraded`     | Only while degraded                          | Something in the spec cannot be honoured — today, `Reason: InvalidGitRepoURL`.                                               |
| `EventWatcher` | Only while `eventWatcher.enabled` is `false` | `status: False`, `Reason: DisabledBySpec`. The emergency stop is still pressed and no cluster events are reaching the agent. |

`EventWatcher` is absent on a healthy install rather than `True`, deliberately: the operator can say
it asked for a watcher, but nothing here checks that one is alive, and a permanently-`True`
condition would read as a health signal it is not. Disabling the watcher is also not a `Degraded`
state — it is a decision somebody made, and `phase` stays `Ready`.

```console
$ kubectl describe platformagent platform-agent -n kubeagents-system
...
  Conditions:
    Type:     EventWatcher
    Status:   False
    Reason:   DisabledBySpec
    Message:  Cluster event ingestion is disabled by spec.harness.eventWatcher.enabled=false. …
```

## How config reaches each profile

A deployment runs several Hermes **profiles** from one pod: `default` (the Chat Agent front door),
`platform`, and one `cluster-*` profile per managed cluster. The named profiles are each configured
by an overlay merged into an image-built base at startup. The `default` profile is the exception: it
takes the operator's settings by _two_ routes at once — an overlay merged into its config, and a
read-only **managed scope** pinned over it.

| Profile     | Delivery                                                                                                                                          | Who owns the file                         |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| `default`   | Image-built base, writable on the PVC + `profile-default.overlay.yaml` merged at startup + a narrow set of keys pinned read-only at `/etc/hermes` | Agent owns the file, operator the pins    |
| `platform`  | Image-built base + `profile-platform.overlay.yaml` merged at startup                                                                              | Image owns the base, operator the overlay |
| `cluster-*` | Image-built base + `profileclass-cluster.overlay.yaml`, plus `profile-<name>.overlay.yaml` if one exists                                          | Image owns the base, operator the overlay |

A cluster profile is the only one that can take two overlays: the class overlay carries
`tuning.cluster`, which applies to all of them, and a plugin targeting one specific cluster produces
a `profile-<name>` overlay for it as well. The class overlay merges first, so the per-profile file
wins any conflict.

**Why `default` is also pinned.** The pins are the one change-control boundary the front door has:
the agent's own config file is writable, so without them a bad runtime edit survives a restart. (It
is _not_ a security sandbox — see the
[AgentPlugin trust boundary](/kube-agents/reference/security-and-iam/#change-control--safety).)

**What is pinned is narrow, on purpose.** `/etc/hermes` is machine-global — one file for every
profile in the pod, not just `default` — so it carries only what is identical for every profile
_and_ beyond the agent's own repair: `model.*`, `platforms.*`, `approvals.cron_mode` and
`display.platforms`. The reasoning is that as long as a human can reach the agent (`platforms`) and
the agent can reason (`model`), anything else it breaks it can be talked into fixing.

Everything else the operator owns for the front door goes in `profile-default.overlay.yaml`
instead: `plugins.enabled` for AgentPlugins with no `targetProfile`, those plugins' non-gateway
config subtrees, and `spec.harness.tuning`'s `default` limits and `maxInProgress`. Those are
profile-shaped — pinning them machine-globally would hand the front door's settings to every
specialist — and they are all recoverable by an agent that can still talk and still reason.
Nothing the operator renders appears on both routes. What appears on neither, and so stays the
image's alone, is each profile's toolsets, `mcp_servers` and `memory`.

It is also the only profile whose config the _running agent_ writes to: `/sethome` records the home
channel there, the monitoring policy mints `monitoring.install_id`, and slash commands save
preferences. Those two facts pulled in opposite directions, and the managed scope is what resolves
them.

The rendering is published as the `managed-config.yaml` key of the `<agent>-config` ConfigMap and
mounted read-only at `/etc/hermes/config.yaml`. Hermes treats that directory as an administrator
layer and overlays it, **per leaf key**, on top of `$HERMES_HOME/config.yaml` at every load. Three
things enforce it (`hermes_cli/managed_scope.py`):

- `load_config` deep-merges the managed dict on top of the agent's own;
- `save_config` strips every managed leaf before writing, so a save cannot persist one;
- `hermes config set` rejects a managed key by name.

So `$HERMES_HOME/config.yaml` stays an ordinary writable file — `/sethome` and the install id work —
while every leaf the operator renders is authoritative and immutable at runtime. Whatever ends up in
the PVC file, the operator's value is what loads, so a restart always heals. Earlier shapes did not
manage both: mounting the render over `$HERMES_HOME/config.yaml` made the path read-only and failed
every runtime write (`/sethome` with a permission error, the rest silently — issue #658), and
merging it into the file at startup left every merged key mutable, so an agent that repointed
`model.base_url` at nothing kept that across restarts.

`platforms.<platform>.home_channel` is deliberately **not** pinned, so `/sethome` can still set it
from chat. The platform credentials and endpoints that have no `config.yaml` equivalent are pinned
through a companion `/etc/hermes/.env`, which Hermes applies last with `override=True` and refuses to
let the agent overwrite — without that, a container env var would beat the pinned `platforms.*` leaf.

One consequence is worth knowing before you edit `renderConfigYAML`: the managed overlay is a
leaf-level merge, and a list is a leaf, so a list rendered here **replaces** the image's rather than
unioning with it — for every profile at once. That is why the render emits no lists at all today,
and why adding one is the change to think hardest about.

**Why the others get overlays.** Their `config.yaml` is assembled at image build time by merging the
shared defaults with that profile's own overlay, content the operator does not have; a `cluster-*`
config additionally carries a runtime `cluster_identity` stamp that the reconciler matches profiles
to clusters by. Rendering either file in full would fork the source of truth and, for cluster
profiles, strip that identity record.

Every overlay is a key in the one `<agent>-config` ConfigMap, so a change to any of them moves the
config hash and rolls the pod. That restart is required, not incidental: the merge happens once at
startup, so a live ConfigMap update without a restart would be a no-op. The managed key shares the
ConfigMap and so rolls the pod too, though for it the restart is belt-and-braces rather than
required — it is mounted as a directory, not a `subPath`, so the kubelet propagates updates and
Hermes re-reads the file when its mtime or size changes.

Startup is not the only moment a merge happens. Onboarding a cluster scaffolds a new profile without
changing the ConfigMap, so nothing rolls the pod; that profile applies the overlays itself as it is
created. Without it a Cluster Agent created between two pod starts would run on Hermes' own defaults
however the CR is tuned.

**Ordering.** The entrypoint force-syncs each profile's image-owned files first, then merges the
overlays. The reverse order would silently erase every overlay on each restart. The `default`
profile's `config.yaml` is the exception to the force-sync: it is the agent's own file, and a
force-sync is exactly what would throw the runtime's edits away. It is instead seeded from the image
on a fresh volume, and thereafter only back-filled — keys the image declares and the live file has
lost are restored, keys it already holds are left alone. Its overlay is merged after that
back-fill, so the operator's settings are not undone by it.

**Merge semantics.** These differ between the two mechanisms, which is the easiest thing to get
wrong here. In a startup **overlay** — every profile including `default` — maps merge recursively,
lists union, and scalars are replaced by the overlay; precedence, lowest to highest, is Hermes
built-in default → the value committed in `agents/<persona>/config.yaml` → the operator overlay from
the CR. In the **managed scope** the merge is per leaf key, so a list replaces rather than unions,
and it wins over everything else because it is applied at every load rather than once at startup.

**Two writers, two authorities.** Both `spec.harness.tuning` (operator policy) and an
`AgentPlugin`'s `spec.config` (plugin-supplied) land in the same overlay file, but not with equal
rights. A plugin's config is restricted to `approvals`, `platforms`, and `platform_toolsets`, and
for an untargeted plugin only `platforms` reaches the machine-global managed scope — the rest goes
to the front door's overlay. The `agent` subtree holding the execution limits is dropped from plugin
config and writable only by the operator. That is a coordination boundary rather than a security one — plugin code executes
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
