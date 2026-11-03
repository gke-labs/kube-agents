---
title: Fleet and host-cluster sizing
description: When the default pod sizes are outgrown, what a fleet-scale install asks of the host GKE cluster, what Autopilot does to limits and ephemeral storage, and the example values file for the largest tier.
sidebar:
  order: 10
---

A default install fits a handful of clusters. This page says when it stops fitting, what to
raise, and how large the GKE cluster that hosts it has to be. Every figure is labelled one of
three ways:

- **declared**: what the chart and the operator render today, read from `charts/kube-agents/values.yaml`
  and `charts/kube-agents/files/footprint.yaml`;
- **measured**: observed on one production fleet of 29 clusters and 44,224 nodes, where the defaults
  gave broker restarts, disk-pressure evictions and audit sweeps that failed their gate;
- **modeled**: derived from those two, or from public list prices. A modeled figure is a starting
  point to measure against on your own install.

## Tiers

The tier is the largest row any metric lands in. The thresholds are modeled: the `fleet`
column is where the measured fleet sat, and the `medium` band is where the defaults start to
need the raises below.

| Metric                                                              | `small` (default) | `medium`         | `fleet`                     |
| ------------------------------------------------------------------- | ----------------- | ---------------- | --------------------------- |
| Registered clusters                                                 | under 10          | 10 to 24         | 25 or more (to 2,500)       |
| Fleet nodes                                                         | under 1,000       | 1,000 to 14,999  | 15,000 or more (to 250,000) |
| Fleet controllers (Deployments, StatefulSets, DaemonSets, CronJobs) | under 500         | 500 to 2,999     | 3,000 or more               |
| Fleet pods                                                          | under 5,000       | 5,000 to 29,999  | 30,000 or more              |
| Pods in the largest single cluster                                  | under 2,500       | 2,500 to 7,499   | 7,500 or more               |
| Objects in the host cluster                                         | under 15,000      | 15,000 to 49,999 | 50,000 or more              |

The host-cluster row is the one that sizes the operator. The manager caches every Deployment,
StatefulSet, Service and PersistentVolumeClaim in the cluster it runs in, so a shared host
cluster with thousands of them needs the operator memory raise even at a small fleet. The other
rows size the credential proxy, the event watcher and the data volumes, which grow with what the
agent reads.

## What each pod needs

The table is the `today` component stack with the GitHub minter on, the web console off and
Hindsight off. The default column is declared. The fleet column is what
[`values-fleet.yaml`](#the-fleet-values-file) sets, plus the one field it cannot, and is modeled.
CPU is in vCPU, memory in GiB, and each cell reads requests then limits. A test derives the
declared column from the two files and fails when the page falls behind them.

| Workload                                                     | Pods | Runtime                                                                | Default (declared)                  | Fleet (modeled)                                | Ephemeral (declared)     | Persistent                                               |
| ------------------------------------------------------------ | ---- | ---------------------------------------------------------------------- | ----------------------------------- | ---------------------------------------------- | ------------------------ | -------------------------------------------------------- |
| Credential proxy (Envoy and the broker)                      | 1    | standard                                                               | 0.5 / 1 then 2 / 2                  | 4 / 8 then 8 / 8                               | 8 GiB, request and limit | none                                                     |
| Gateway pod: agent, dashboard, log sidecar, `agent-api-auth` | 1    | gVisor under the installer default `runtimeClassName`                  | 1.506 / 3 then 5.5 / 12.25          | 1.9 / 8.6 then 5.5 / 18.25                     | 3 GiB (the two sidecars) | data claim 50 GiB default, 100 GiB fleet; 1 GiB metadata |
| Shell sandbox `<name>-shell-0`                               | 1    | node default; its own `shellSandbox.runtimeClassName` opts into gVisor | 0.2 / 0.5 then 2 / 2                | unchanged; its own `resources` field raises it | none declared            | data claim follows the gateway's; 1 GiB for `sshd`       |
| Operator                                                     | 1    | standard                                                               | 0.05 / 0.125 then 0.5 / 0.5         | 0.25 / 1 then 0.5 / 1                          | none declared            | none                                                     |
| LiteLLM                                                      | 2    | standard                                                               | 0.1 / 2 then 0.5 / 3, each          | unchanged                                      | 1.25 GiB each            | none                                                     |
| GitHub token minter                                          | 2    | standard                                                               | 0.05 / 0.0625 then 0.5 / 0.25, each | unchanged                                      | none declared            | none                                                     |
| **Total**                                                    | 8    |                                                                        | **2.556 / 8.75 then 12 / 23.25**    | **6.6 / 22.25 then 18 / 35.75**                | **13.5 GiB**             | **102 GiB default, 202 GiB fleet**                       |

On GKE Autopilot the operator renders the credential proxy's requests at 2 vCPU and 2 GiB
instead of 0.5 and 1, because Autopilot without bursting clamps a container's limits to its
requests. The default total there is 1.5 vCPU and 1 GiB higher than the table's; the fleet
column sets the pair itself and is the same on both cluster types.

The gateway pod's fleet row raises two containers. `agent-api-auth` is the example's
`agentAPIAuth.resources`, 0.5 vCPU and 2 GiB requests with a 4 GiB memory limit. The agent
container is the CR's `spec.deployment.resources`, which the chart has no value for; the row
models it at 1 vCPU and 6 GiB requests and 3 vCPU and 12 GiB limits. That field replaces the
operator's defaults (1 vCPU and 2 GiB requests, 3 vCPU and 8 GiB limits) as a whole block, so
set every key you want to keep. Set it on the CR with `kubectl edit`; a later `helm upgrade`
leaves it alone, because Helm patches only the fields its render changes. The
[CRD page](/kube-agents/operator/platformagent-crd/#specdeployment) describes the field.

The shell sandbox's 2 vCPU and 2 GiB limits are the operator's defaults.
[`shellSandbox.resources`](/kube-agents/operator/platformagent-crd/#shellsandbox) raises them one
key at a time; the example leaves them, so a fleet audit that needs more memory in the sandbox is
bounded by `maxInProgress`, which the example lowers to 3. The sandbox runs on the node's default
runtime unless
[`spec.harness.experimental.shellSandbox.runtimeClassName`](/kube-agents/operator/platformagent-crd/#shellsandbox)
names `gvisor`; the gateway pod's class is the separate `availability.runtimeClassName`, which
the installer sets.

Two optional stacks add to the total:

- **Hindsight memory** (`platformAgent.harness.memory.provider: kube_agents_memory`): the API
  at 0.25 vCPU and 1 GiB requests, 4 vCPU and 4 GiB limits; Postgres at 0.25 vCPU and 256 MiB
  requests, 1 vCPU and 1 GiB limits, 1.1 GiB ephemeral and an 8 GiB claim. Declared.
- **`spec.mode: next`**: about 0.7 vCPU and 1.1 GiB of standing requests across seven pods, and
  a 40 GiB NATS claim. Each delegation adds a session pod at 0.25 vCPU and 512 MiB requests with
  2 vCPU and 2 GiB limits, up to `maxSessions` (default 10) at once. The
  [prerequisites page](/kube-agents/install/prerequisites/#cluster-requirements) itemises them.

## The host cluster

### Option A: GKE Autopilot

Autopilot is the installer's default `CLUSTER_MODE` and the recommended shape: it bills the
requests, sizes nodes to fit, and ships the gVisor RuntimeClass. Three rules change what the
table above means there:

- **Limits equal requests** on a cluster without bursting. The proxy runs at 4 vCPU, the
  `agent-api-auth` sidecar at 2 GiB (its watcher reads a 1 GiB soft limit), and every container
  whose limit is above its request runs at the request. A cluster with bursting admits the
  manifest as rendered, so the example sets the requests it means to run at.
- **1 to 6.5 GiB of memory per vCPU** on the requests pair, and, without bursting, per-pod
  minimums of 250m CPU and 512 MiB. A pair outside the band has its smaller request raised, and the chart's quota
  preflight, which sums the declared figures, under-counts by the difference. Every pair in the
  example is in the band.
- **Ephemeral storage up to 10 GiB per pod** in the general-purpose class. The credential
  proxy's 8 GiB is the largest declared; raising `limits.ephemeral-storage` past 10 GiB leaves
  the pod unschedulable.

Billed capacity for the fleet column is about 7 vCPU and 23 GiB, plus 202 GiB of `pd-balanced`
(modeled, without bursting). At `us-central1` list prices that is about $400 to $500 a month
including the cluster fee; modeled, so check the pricing calculator against your region.

### Option B: GKE Standard

The composition creates a default pool of `e2-standard-4` nodes, one per zone, and with gVisor
on a `gvisor-pool` of `e2-standard-4`, one per zone, carrying GKE's sandbox taint. Neither
machine type is an installer setting, and a machine type changed on a managed pool outside
Terraform shows as drift on the next plan, so add pools beside them for a fleet.

Add them before applying the example. An `e2-standard-4` node has under 4 vCPU allocatable once
the system DaemonSets take theirs, so the example's 4 vCPU proxy request fits on none of them.
The proxy Deployment recreates its pod rather than rolling it: applied on the default pools, the
example leaves the new proxy pod `Pending` with the old one gone. Every brokered `gcloud` and
`kubectl` command, and the Google Chat relay, is then down until a node fits.

| Pool             | Shape (modeled)                                                                            | Carries                                                               |
| ---------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| Default          | 3 × `n2-standard-8` (or `c3-standard-8`), 200 GB `pd-balanced` boot disk, autoscale 2 to 4 | proxy, operator, LiteLLM, minter, the `next` stack                    |
| gVisor (sandbox) | 1 × `n2-standard-4`, 150 GB boot disk, autoscale 1 to 2; `n2-standard-8` for `mode: next`  | gateway pod; the shell sandbox once its own `runtimeClassName` is set |

Create the gVisor pool with `--sandbox type=gvisor`, so it carries the taint only the sandboxed
pods tolerate. The boot disk sizes are modeled: the measured fleet's evictions were disk
pressure, and the pod table's ephemeral figures plus the node's images have to fit under the
eviction threshold with room left. About $900 to $1,100 a month on demand at `us-central1` list
prices, modeled.

## The fleet values file

[`charts/kube-agents/examples/values-fleet.yaml`](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/examples/values-fleet.yaml)
sets what the chart exposes for the `fleet` tier, each key with its reason and its Autopilot
caveat beside it:

- `operator.resources`: 1 GiB memory, request and limit alike, for a host cluster at or past
  50,000 objects. The chart's floor on the limit is 512 MiB.
- `platformAgent.storage.size: 100Gi`: both data claims. A claim cannot shrink, and each GiB
  counts twice against a namespace `requests.storage` quota.
- `platformAgent.harness.tuning.maxInProgress: 3`: kanban workers at once, board-wide.
- `platformAgent.deployment.credentialProxy.resources`: 4 vCPU and 8 GiB requests, 8 vCPU and
  8 GiB limits. The broker admits at most eight commands at once, a cap the operator fixes, and
  the 2 GiB default already covers the eight at 176 MiB a slot. The raise is headroom for a
  child that outgrows its reserve on a large cluster, close to 1 GiB a slot (modeled), and it
  admits nothing more in parallel.
- `platformAgent.deployment.agentAPIAuth.resources`: 0.5 vCPU and 2 GiB requests, a 4 GiB
  memory limit, `limits.cpu` left at its default because the watcher's `GOMAXPROCS` follows it.
- `platformAgent.deployment.env`: `INCIDENT_WORKLOAD_DEDUP_SECONDS=3600`, which folds an
  admitted workload's later events into one incident for an hour, and
  `KUBEAGENTS_AUDIT_AUTO_PROMOTE=false`, which keeps the fleet audits from opening remediation
  pull requests on their own.

The operator copies `platformAgent.deployment.env` onto the agent container only for names on
its allowlist, forwards `KUBEAGENTS_AUDIT_AUTO_PROMOTE` to the shell sandbox, where the audits
run, and passes every name it does not reserve to the credential proxy and `agent-api-auth`
containers. Both names in the example reach the container that reads them. Process tunables such
as `CLOUDSDK_CORE_DISABLE_FILE_LOGGING` and `MALLOC_ARENA_MAX` are not on the agent container's
allowlist: a values file that sets them renders a proxy pod that carries them, where `gcloud` and
`kubectl` run, and an agent container that does not. The example leaves them out. The
[chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#sizing-for-a-fleet-examplesvalues-fleetyaml)
has the chart-side detail.

### Applying it

A hand-run `helm upgrade` applies no CRDs, and `spec.storage`, `spec.deployment.credentialProxy`
and `spec.deployment.agentAPIAuth` need a CRD that declares them, so apply `crds/` first; the
[upgrade modes](/kube-agents/install/upgrade/#upgrade-modes) say which `upgrade.sh` runs do that
for you. On Standard, add a pool that fits the proxy's 4 vCPU request before this
([Option B](#option-b-gke-standard)).

```bash
kubectl apply --server-side -f charts/kube-agents/crds/
helm upgrade kube-agents ./charts/kube-agents -n kubeagents-system \
  --reset-then-reuse-values \
  -f charts/kube-agents/examples/values-fleet.yaml
```

An install made with `install.sh` re-renders the chart from `install.env` on a full
[upgrade](/kube-agents/install/upgrade/), so a value passed only by hand is rendered away on the
next run. Put the data volume size in `PLATFORM_AGENT_STORAGE_SIZE=100Gi` and the rest in the
file's `TF_VAR_extra_helm_values` line, which the
[quickstart](/kube-agents/install/quickstart-gke/#operator-settings) describes. The file holds one
such line and a second `platformAgent` key replaces the first, so merge these keys into a line
that already has one rather than pasting over it:

```bash
PLATFORM_AGENT_STORAGE_SIZE=100Gi
TF_VAR_extra_helm_values='{operator={resources={requests={cpu="250m",memory="1Gi"},limits={cpu="500m",memory="1Gi"}}},platformAgent={harness={tuning={maxInProgress=3}},deployment={env=[{name="INCIDENT_WORKLOAD_DEDUP_SECONDS",value="3600"},{name="KUBEAGENTS_AUDIT_AUTO_PROMOTE",value="false"}],credentialProxy={resources={requests={cpu="4",memory="8Gi"},limits={cpu="8",memory="8Gi"}}},agentAPIAuth={resources={requests={cpu="500m",memory="2Gi"},limits={memory="4Gi"}}}}}}'
```

Read the result on the CR and the pods:

```bash
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.spec.storage}{"\n"}{.spec.deployment.credentialProxy}{"\n"}{.status.conditions}{"\n"}'
kubectl get pods -n kubeagents-system -o custom-columns='NAME:.metadata.name,REQ:.spec.containers[*].resources.requests,LIM:.spec.containers[*].resources.limits'
```

A `Degraded` condition with reason `InvalidCredentialProxyResources` or
`InvalidAgentAPIAuthResources` means the operator refused an override and rendered that
container at its defaults; `DataVolumeNotExpanded` means the StorageClass does not expand
claims online. On Autopilot the pod's limits read equal to its requests where bursting is off,
which is the cluster rewriting the manifest rather than the chart dropping a key.
