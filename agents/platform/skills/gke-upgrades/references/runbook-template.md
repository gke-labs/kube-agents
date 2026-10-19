# Runbook Command Templates

Standard command sequences for GKE upgrades. Replace placeholders: `CLUSTER_NAME`, `ZONE`, `LOCATION` (the zone or region), `TARGET_VERSION`, `NODE_POOL_NAME`. The operator steps take `--location`, which accepts a zone or a region.

> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

Each change step below is a pull request editing the cluster's `ContainerCluster` or the pool's `ContainerNodePool`, except a node pool's version, which is a step for the platform operator (a `spec.version` in the repository pins a release-channel pool against auto-upgrade); each check step is a read command.

## Table of Contents
- [Pre-flight](#pre-flight) (Line 16-35)
- [Control plane upgrade](#control-plane-upgrade) (Line 36-47)
- [Node pool upgrade (Standard only)](#node-pool-upgrade-standard-only) (Line 48-71)
- [Maintenance window configuration](#maintenance-window-configuration) (Line 72-80)
- [Rollback/Downgrade guidance](#rollbackdowngrade-guidance) (Line 81-137)

## Pre-flight

```bash
# Current versions
gcloud container clusters describe CLUSTER_NAME \
  --zone ZONE \
  --format="table(name, currentMasterVersion, nodePools[].version)"

# Available versions for channel
gcloud container get-server-config --zone ZONE \
  --format="yaml(channels)"

# Deprecated API usage
kubectl get --raw /metrics | grep apiserver_request_total | grep deprecated

# Cluster health
kubectl get nodes
kubectl get pods -A | grep -v Running | grep -v Completed
```

## Control plane upgrade

Propose `spec.minMasterVersion: TARGET_VERSION` on the cluster's `ContainerCluster` as a pull request. After it merges and applies, verify (wait ~10-15 min):

```bash
gcloud container clusters describe CLUSTER_NAME \
  --zone ZONE \
  --format="value(currentMasterVersion)"

kubectl get pods -n kube-system
```

## Node pool upgrade (Standard only)

Propose the pool's surge settings (`spec.upgradeSettings.maxSurge: MAX_SURGE`, `spec.upgradeSettings.maxUnavailable: MAX_UNAVAILABLE`) on its `ContainerNodePool` as a pull request. The version change is not part of it: a `spec.version` in the repository pins a release-channel pool against auto-upgrade, so once the surge settings apply, the upgrade is a step for the platform operator to run, not one to run from here:

```bash
# Platform operator step (node pools upgrade through `clusters upgrade
# --node-pool`; there is no `gcloud container node-pools upgrade` command)
gcloud container clusters upgrade CLUSTER_NAME \
  --node-pool NODE_POOL_NAME \
  --cluster-version TARGET_VERSION \
  --location LOCATION
```

Then check:

```bash
# Monitor progress
watch 'kubectl get nodes -o wide -L cloud.google.com/gke-nodepool'

# Verify
gcloud container node-pools list --cluster CLUSTER_NAME --zone ZONE
kubectl get pods -A | grep -v Running | grep -v Completed
```

## Maintenance window configuration

Each of these is proposed as a pull request on the cluster's `ContainerCluster` through `gcp-config-connector`:

- **Recurring maintenance window**: `spec.maintenancePolicy.recurringWindow` with `startTime` and `endTime` (`YYYY-MM-DDTHH:MM:SSZ`) and `recurrence` (e.g. `FREQ=WEEKLY;BYDAY=SA`).
- **Maintenance exclusion (up to 90 days)**: an entry in `spec.maintenancePolicy.maintenanceExclusion` with `exclusionName`, `startTime`, `endTime` and `exclusionOptions.scope` (as the GKE API spells it: `NO_UPGRADES`, `NO_MINOR_UPGRADES` or `NO_MINOR_OR_NODE_UPGRADES`).
- **Exclusion until End of Support**: the same entry with `endTime` set to the minor version's End of Support date and `exclusionOptions.scope` `NO_MINOR_UPGRADES` or `NO_MINOR_OR_NODE_UPGRADES`. The schema has no field that follows the End of Support date if it moves, so a later date is a new pull request moving `endTime`.
- **Node pool level exclusions**: `ContainerNodePool` has no exclusion field (confirm with `kubectl explain`), so the agent blocks rather than inventing one.

## Rollback/Downgrade guidance

- **Control Plane Patches**: Can be downgraded to the target older patch version.
- **Control Plane Minors**: Rollback is only available during the first step of the 2-step upgrade process.
- **Node Pools (Minor & Patch)**: Can be downgraded directly by targeting the older version, OR by creating a new pool at the old version and migrating workloads (safer).

### Downgrade Control Plane (Patch or Step-1 Minor)

A downgrade to `TARGET_PREVIOUS_VERSION` has no declarative path in a Config Connector repository (`spec.minMasterVersion` is a floor GKE upgrades past, not a downgrade), so the agent blocks rather than inventing a field.

### Downgrade Node Pool (Direct)

A step for the platform operator, not a pull request: a `spec.version: TARGET_PREVIOUS_VERSION` in the repository would hold the pool at the old version indefinitely, against the release channel's patches.

```bash
# Platform operator step
gcloud container clusters upgrade CLUSTER_NAME \
  --node-pool NODE_POOL_NAME \
  --cluster-version TARGET_PREVIOUS_VERSION \
  --location LOCATION
```

### Downgrade Node Pool (Safe migration - recommended)

Propose a new replacement `ContainerNodePool` named `NODE_POOL_NAME-rollback` (`spec.initialNodeCount: NUM_NODES`, `spec.nodeConfig.machineType: MACHINE_TYPE`, no `spec.version`) as a pull request. Once it is up, the platform operator moves it to the previous version with `gcloud container clusters upgrade CLUSTER_NAME --node-pool NODE_POOL_NAME-rollback --cluster-version PREVIOUS_VERSION --location LOCATION`, for the same reason as the direct downgrade. That is two node rolls, one to create the pool at the control plane's version and one to move it down, so the operator downgrades the replacement before any workload shifts onto it, and workloads move only once. Keep the old pool as the fallback while workloads move: the platform operator cordons and drains it (a node operation, not a declarative change), and only after the workloads run on the replacement does a later pull request remove its `ContainerNodePool`. Before the drain:

```bash
# Record current PDB state BEFORE touching anything — this file is how you put
# it back. Do not skip; a relaxed PDB left in place is an outage waiting for
# the next voluntary disruption.
kubectl get pdb -A -o yaml > /tmp/pdb-backup-$(date +%s).yaml
kubectl get pdb -A   # ALLOWED DISRUPTIONS = 0 will block the drain
```

If a PDB blocks the drain, relax it **temporarily** and restore it as a required
step of the same runbook — never as a follow-up someone may forget:

1. Relax the blocking PDB, only after confirming the replacement pool is Ready and
   can accept the workload: a pull request that removes `minAvailable` from its
   manifest and sets `maxUnavailable: "100%"`.
2. After the drain, verify workloads are Running on the replacement pool:

   ```bash
   kubectl get pods -o wide --field-selector spec.nodeName!='' -A | grep NODE_POOL_NAME-rollback
   ```

3. RESTORE the original PDB — mandatory, not optional: a pull request that puts back
   the `minAvailable` or `maxUnavailable` value the backup records for `PDB_NAME` and
   clears the field step 1 set (an integer as-is, a percentage quoted, e.g. `"50%"`).
4. Confirm the restore took effect:

   ```bash
   kubectl get pdb -A   # ALLOWED DISRUPTIONS should match the pre-drain values
   ```

> A rollback runbook that relaxes PDBs without restoring them has silently
> removed the cluster's disruption protection. Always include steps 3 and 4.
