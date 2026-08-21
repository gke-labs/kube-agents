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
  networkPolicy: { ... } # generated egress NetworkPolicy (optional)
  integration: { ... } # Google Chat, Slack, GitHub
```

`spec.deployment`, `spec.security`, `spec.telemetry`, and `spec.networkPolicy` are inlined from the shared `AgentSpec`, so they are common to every agent type. `spec.harness` is required; `spec.integration`, `spec.telemetry`, and `spec.networkPolicy` are optional.

## `spec.harness`

Framework-level settings passed to Hermes. `clusterName`, `location`, and `projectId` are all
required — the API server rejects a `PlatformAgent` that omits any of them. The credential proxy
only renders its kubeconfig bootstrap (the `gcloud container clusters get-credentials` that gives
the agent a usable kubectl context) when it has the complete triple; with one missing, every
`kubectl` the agent runs resolves to `localhost:8080` instead of a cluster.

| Field                                  | Type     | Purpose                                                                                                   |
| -------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------- |
| `phase`                                | string   | Overall state (`Pending`, `Provisioning`, `Ready`, `Failed`).                                             |
| `address`                              | string   | Fully qualified domain name (FQDN) of the agent service.                                                  |
| `lastReconcileTime`                    | time     | Timestamp of the last status update.                                                                      |
| `conditions`                           | list     | Standard `metav1.Condition` observations, keyed by `type`.                                                |
| `deploymentStatus.name`                | string   | Name of the underlying Deployment.                                                                        |
| `deploymentStatus.readyReplicas`       | int32    | Number of fully ready replicas.                                                                           |
| `serviceStatus.endpoint`               | string   | Primary URL/IP (with protocol and port) to reach the agent.                                               |
| `storageStatus.bound`                  | bool     | Whether the primary PVC has been provisioned.                                                             |
| `telemetry.otlpEndpoint`               | string   | The OTLP collector the agent was wired to.                                                                |
| `telemetry.otlpEndpointSource`         | string   | Which rung of the ladder answered: `DeploymentEnv`, `Spec`, `OperatorEnv`, `Discovered`, or `Default`.    |
| `networkPolicy.generated`              | bool     | Whether the operator-managed NetworkPolicy is active (`false` when `spec.networkPolicy.enabled: false`).  |
| `networkPolicy.dnsClusterIPs`          | []string | The DNS ClusterIPs written into rule 1.                                                                   |
| `networkPolicy.dnsClusterIPsSource`    | string   | Which rung of the ladder answered: `Annotation`, `Spec`, `OperatorEnv`, `Discovered`, or `Default`.       |
| `networkPolicy.metadataDaemonIP`       | string   | The post-NAT daemon IP in rule 3, empty when suppressed.                                                  |
| `networkPolicy.metadataDaemonIPSource` | string   | Which rung of the ladder answered: `Annotation`, `Spec`, `OperatorEnv`, `Default`, or `Suppressed`.       |

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

A deployment runs several Hermes **profiles** from one pod: `default` (the Planning Agent front door),
`platform`, and one `cluster-*` profile per managed cluster. The named profiles are each configured
by an overlay merged into an image-built base at startup. The `default` profile is the exception: it
takes the operator's settings by _two_ routes at once — an overlay merged into its config, and a
read-only **managed scope** pinned over it.

| Profile                                                       | Delivery                                                                                                                                                   | Who owns the file                                      |
| ------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| `default`                                                     | Image-built base, writable on the PVC + `profile-default.overlay.yaml` merged at startup + a narrow set of keys pinned read-only at `/etc/hermes`          | Agent owns the file, operator the pins                 |
| `platform`                                                    | Image-built base + `profile-platform.overlay.yaml` merged at startup                                                                                       | Image owns the base, operator the overlay              |
| `platform`, with [`platformFrontDoor`](#platformfrontdoor) on | The same two inputs, but the base is back-filled rather than force-synced — and the `/etc/hermes` pins land here too, because that mount is machine-global | Agent owns the file, operator the overlay and the pins |
| `cluster-*`                                                   | Image-built base + `profileclass-cluster.overlay.yaml`, plus `profile-<name>.overlay.yaml` if one exists                                                   | Image owns the base, operator the overlay              |

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

The platform profile's `config.yaml` becomes a second exception under
[`platformFrontDoor`](#platformfrontdoor), and for the same reason: the gateway is homed there, so
that file is now the one `/sethome` and the monitoring policy write to. It leaves the force-sync
list and is back-filled from the image template instead, on exactly the terms `default` gets — keys
the template declares and the live file has lost are restored, keys it already holds are left
alone. Its overlay merges after that back-fill as it always did. Everything else the image owns in
that profile — the persona files, `cron/`, `skills/`, `governance/`, `hindsight/` — still
force-syncs either way.

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
- The admission webhook (behind cert-manager) validates the spec before it's persisted; it enforces at most one `PlatformAgent` per project, forbids sensitive environment variable overrides (`API_SERVER_KEY`, `HERMES_HOME`) and privileged containers/volumes (`hostPath`), requires each `imagePullSecrets` entry to name a Secret, and acts as a name-based tripwire against obvious privileged service account names (`cluster-admin`, `system:admin`). Note that full RBAC least-privilege enforcement is handled by controller- and pipeline-level policies rather than the admission webhook.
- The `kubeagents.x-k8s.io/prevent-deletion: "true"` annotation on a `PlatformAgent` blocks deletion of the resource via the validating webhook (`ValidateDelete`). This serves as an accidental-deletion guardrail rather than an authorization control — `ValidateUpdate` does not block removing the annotation, so any principal with update permissions can patch the annotation off before deleting.
- The Helm chart renders and applies the CR (the install engine drives it through `terraform apply`); you can also edit it directly with `kubectl edit`.

## Where to go next

- [Development](/kube-agents/operator/development/) — build and test the controller locally.
- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — how the CR gets applied in a fresh install.
