# Troubleshooting GKE Upgrade Issues

## Diagnostic flowchart

## Table of Contents
- [Diagnostic flowchart](#diagnostic-flowchart)
- [1. PDB blocking drain (most common)](#1-pdb-blocking-drain-most-common)
- [2. Resource constraints (no room for pods)](#2-resource-constraints-no-room-for-pods)
- [3. Bare pods blocking drain](#3-bare-pods-blocking-drain)
- [4. Admission webhooks rejecting pod creation](#4-admission-webhooks-rejecting-pod-creation)
- [5. PVC attachment issues](#5-pvc-attachment-issues)
- [6. Long termination grace periods](#6-long-termination-grace-periods)
- [7. Upgrade operation stuck at GKE level](#7-upgrade-operation-stuck-at-gke-level)
- [8. Stockout during critical upgrades (e.g. cert expiration)](#8-stockout-during-critical-upgrades-eg-cert-expiration)
- [9. GPU node upgrade regressions (CrashLoopBackOff, driver issues)](#9-gpu-node-upgrade-regressions-crashloopbackoff-driver-issues)
- [10. Upgrade paused or partially completed (check auto-upgrade status)](#10-upgrade-paused-or-partially-completed-check-auto-upgrade-status)
- [11. Upgrade too slow or too disruptive (tune blue-green soak)](#11-upgrade-too-slow-or-too-disruptive-tune-blue-green-soak)
- [12. New nodes fail to become Ready / control plane unhealthy](#12-new-nodes-fail-to-become-ready--control-plane-unhealthy)
- [Validation after applying a fix](#validation-after-applying-a-fix)

When an upgrade is stuck or failing, work through these checks in order. Each section has the diagnosis command, what to look for, and the fix.

> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

## 1. PDB blocking drain (most common)

**Diagnose:**
```bash
kubectl get pdb -A -o wide
# Look for ALLOWED DISRUPTIONS = 0
kubectl describe pdb PDB_NAME -n NAMESPACE
```

**Fix — temporarily relax the PDB, and restore it in the same procedure:**

1. Record the original values before touching anything:

   ```bash
   kubectl get pdb PDB_NAME -n NAMESPACE -o yaml > pdb-backup.yaml
   ```

2. Allow all disruptions temporarily: a pull request that removes `minAvailable` from the PDB's manifest and sets `maxUnavailable: "100%"`.
3. Once the upgrade completes, RESTORE (mandatory): a pull request that puts back the value `pdb-backup.yaml` records and clears the other field.
4. Verify the restore: the spec matches `pdb-backup.yaml` again.

```bash
kubectl get pdb PDB_NAME -n NAMESPACE -o yaml | grep -E 'minAvailable|maxUnavailable'
```

## 2. Resource constraints (no room for pods)

**Diagnose:**
```bash
kubectl get pods -A | grep Pending
kubectl get events -A --field-selector reason=FailedScheduling
kubectl top nodes
kubectl describe nodes | grep -A 5 "Allocated resources"
```

**Fix — increase surge capacity (Standard clusters only; Autopilot manages node upgrades automatically):** propose `spec.upgradeSettings.maxSurge: 2` and `spec.upgradeSettings.maxUnavailable: 0` on the pool's `ContainerNodePool` as a pull request.

Or lower non-critical workloads' replicas temporarily through a pull request.

## 3. Bare pods blocking drain

**Diagnose:**
```bash
kubectl get pods -A -o json | \
  jq -r '.items[] | select(.metadata.ownerReferences | length == 0) | "\(.metadata.namespace)/\(.metadata.name)"'
```

**Fix:** Remove bare pods (they won't reschedule anyway) or wrap them in Deployments, through a pull request.

## 4. Admission webhooks rejecting pod creation

**Diagnose:**
```bash
kubectl get validatingwebhookconfigurations
kubectl get mutatingwebhookconfigurations
# Check for webhooks matching broad API groups
kubectl describe validatingwebhookconfigurations WEBHOOK_NAME
```

**Fix — temporarily disable problematic webhook:** back up the webhook configuration first, then propose removing it as a pull request, and restore it after the upgrade with a second pull request.

```bash
kubectl get validatingwebhookconfigurations WEBHOOK_NAME -o yaml > webhook-backup.yaml
```

## 5. PVC attachment issues

**Diagnose:**
```bash
kubectl get pvc -A | grep -v Bound
kubectl get events -A --field-selector reason=FailedAttachVolume
```

**Fix:** Check if volumes are zone-locked. For regional clusters, PVs may need to be in the same zone as the new node. Consider migrating workloads to already-upgraded nodes.

## 6. Long termination grace periods

**Diagnose:**
```bash
kubectl get pods -A -o json | \
  jq '.items[] | select(.spec.terminationGracePeriodSeconds > 120) | {ns:.metadata.namespace, name:.metadata.name, grace:.spec.terminationGracePeriodSeconds}'
```

**Fix:** Reduce `terminationGracePeriodSeconds` in the workload spec if possible. GKE waits up to 1 hour for pod eviction during surge upgrades.

## 7. Upgrade operation stuck at GKE level

**Diagnose:**
```bash
gcloud container operations list --cluster CLUSTER_NAME --zone ZONE --filter="operationType=UPGRADE_NODES"
```

**Fix:** If the operation shows no progress for >2 hours after resolving pod-level issues, contact GKE support with cluster name, zone, and operation ID.

## 8. Stockout during critical upgrades (e.g. cert expiration)

**Diagnose:**
Upgrade is failing with `ZONE_RESOURCE_POOL_EXHAUSTED` or `QUOTA_EXCEEDED` errors, and the cluster has a critical pending deadline (e.g., control plane certificate expiring soon).

**Fix:**
1. **Change Upgrade Strategy (Standard clusters only)**: Modify the node pool to use a rolling in-place upgrade (no surge) to bypass quota limits: `spec.upgradeSettings.maxSurge: 0` and `spec.upgradeSettings.maxUnavailable: 1` on its `ContainerNodePool`, proposed as a pull request.
2. **Open Support Case**: Immediately open a P1/P2 Google Cloud Support case, citing urgent certificate expiration and stockout.
3. **Retry in Different Zone/Region**: If the cluster is regional or multi-zonal, check if you can retry the upgrade in a different zone that might have capacity, or add a temporary node pool in a different zone/region to migrate workloads.
4. **Credential Rotation**: A control plane credential rotation renews certificates without upgrading the GKE version. It has no declarative path in a Config Connector repository (the KCC v1.158.0 schema has no field for it), so the agent blocks rather than inventing a field.
5. **Enable DNS Endpoint**: If client connectivity is failing or at risk due to expired client certificates, enable the DNS-based control plane endpoint to allow IAM-based authentication: `spec.controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic` on the cluster's `ContainerCluster`, proposed as a pull request. Then get credentials using the DNS endpoint:
   ```bash
   gcloud container clusters get-credentials CLUSTER_NAME --dns-endpoint --zone ZONE
   ```

## 9. GPU node upgrade regressions (CrashLoopBackOff, driver issues)

**Diagnose:**
GPU nodes upgrade successfully, but ML pods are stuck in `CrashLoopBackOff` with `SIGSEGV` or driver initialization errors.

1. **Compare Node Metadata**: Check if the OS image, kernel version (`uname -r`), or NVIDIA driver version changed and differs between working (old) and non-working (new) nodes.
2. **Verify Driver Installer Logs**: Check logs of the `nvidia-driver-installer` container in the `nvidia-gpu-device-plugin` pod on the new node.
3. **Test GPU Access**: Deploy a simple test pod to verify if the GPU is accessible with the current driver:
   ```yaml
   apiVersion: v1
   kind: Pod
   metadata:
     name: gpu-test-vectoradd
   spec:
     containers:
     - name: vectoradd
       # Pin a tag whose CUDA version matches your node's driver
       image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1-ubuntu20.04
       resources:
         limits:
           nvidia.com/gpu: 1
     restartPolicy: Never
   ```

**Fix:**
1. **Pin Driver Version (Standard clusters only)**: If the default driver version changed, pin the node pool to the previous working driver version (e.g., `R535`) in `spec.nodeConfig.guestAccelerator[].gpuDriverInstallationConfig.gpuDriverVersion`. `guestAccelerator` is immutable on a `ContainerNodePool` (create-only in the `gcp-config-connector` skill's container catalogue), so propose a new pool with it as a pull request and move the workloads.
2. **Update Workload Dependencies**: Rebuild container images with a CUDA version compatible with the new driver.
3. **Rollback Node Pool (Standard clusters only)**: If production is blocked, roll back the node pool to the previous GKE version. This is a step for the platform operator, not a pull request: a `spec.version` in the repository would hold a release-channel pool at the old version against auto-upgrade. The command for the operator: `gcloud container clusters upgrade CLUSTER_NAME --node-pool NODE_POOL_NAME --cluster-version PREVIOUS_VERSION --location LOCATION`.

## 10. Upgrade paused or partially completed (check auto-upgrade status)

Before assuming a bug, check whether GKE has intentionally **paused** the upgrade.

**Diagnose:**
```bash
# Cluster-level upgrade status and paused reason
gcloud container clusters get-upgrade-info CLUSTER_NAME --location LOCATION

# Per node pool (Standard clusters)
gcloud container node-pools get-upgrade-info POOL_NAME --cluster CLUSTER_NAME --location LOCATION
```

**Auto-upgrade status values:**
- `ACTIVE` — upgrades proceeding normally.
- `MINOR_UPGRADE_PAUSED` — minor-version upgrades are paused.
- `UPGRADE_PAUSED` — all automatic upgrades are paused.

**Common paused reasons:**
- `MAINTENANCE_WINDOW` — a maintenance window is preventing upgrades.
- `MAINTENANCE_EXCLUSION_*` — a maintenance exclusion is blocking upgrades (suffix = scope, e.g. `MAINTENANCE_EXCLUSION_NO_UPGRADES`).
- `CLUSTER_DISRUPTION_BUDGET` / `CLUSTER_DISRUPTION_BUDGET_MINOR_UPGRADE` — a post-operation cooldown protecting cluster stability.
- `SYSTEM_CONFIG` — temporarily paused by GKE for technical or business reasons. **Do not force a manual upgrade unless it is required.**

**Fix — resume a canceled/partially-completed node pool upgrade (Standard clusters only)** by re-issuing the same upgrade. This is a step for the platform operator, not a pull request: a `spec.version` in the repository would pin a release-channel pool against auto-upgrade. The command for the operator:
```bash
gcloud container clusters upgrade CLUSTER_NAME \
  --node-pool NODE_POOL_NAME \
  --cluster-version VERSION \
  --location LOCATION
```
A partially-upgraded node pool runs mixed versions until you resume it or roll it back.

## 11. Upgrade too slow or too disruptive (tune blue-green soak)

Blue-green upgrades add batch/soak controls that surge upgrades don't have. Use them when an upgrade churns nodes too aggressively, or when the soak wait is longer than needed.

**Parameters:**
- `BATCH_NODE_COUNT` / `BATCH_PERCENT` — how many blue nodes drain per batch (default `BATCH_NODE_COUNT=1`; set either to `0` to skip the batched drain phase).
- `BATCH_SOAK_DURATION` — wait after each batch drain (default `0s`).
- `NODE_POOL_SOAK_DURATION` — wait after all batches drain, before the blue pool is deleted (default `3600s`).

**Update an existing node pool (Standard clusters only):** on its `ContainerNodePool`, propose the blue-green strategy in `spec.upgradeSettings.strategy` (`BLUE_GREEN`, as the GKE API spells it) with `spec.upgradeSettings.blueGreenSettings.standardRolloutPolicy` (e.g. `batchNodeCount: 2`, `batchSoakDuration: 10s`) and `spec.upgradeSettings.blueGreenSettings.nodePoolSoakDuration: 600s` as a pull request.

**Maintenance-window interaction:** surge upgrades **pause** when they run past the maintenance window and resume in the next one; blue-green upgrades **continue to completion** even past the window, and the extra pool keeps workloads available — prefer blue-green when a window is too short to finish.

## 12. New nodes fail to become Ready / control plane unhealthy

An upgrade can stall because replacement nodes never reach `Ready`, or because the control plane itself is unhealthy.

**Diagnose:**
```bash
kubectl get nodes -o wide          # look for NotReady / SchedulingDisabled
kubectl describe node NODE_NAME    # Conditions + Events
```

**Common node-level causes of an incomplete upgrade:** new nodes failing to register, IP address exhaustion in the pod/node ranges, or insufficient resource quota. For a node stuck `NotReady`, follow the dedicated node-NotReady flow (kubelet / container-runtime / networking).

**Control plane:** during a control-plane upgrade GKE re-creates the API server; a firewall or webhook that blocks the new control plane can fail the upgrade, and a control plane that stays unhealthy causes the operation to fail (often transient — GKE retries). Confirm control-plane/node **version skew** stays within two minor versions:
```bash
gcloud container clusters describe CLUSTER_NAME --location LOCATION \
  --format="value(currentMasterVersion,currentNodeVersion)"
```

## Validation after applying a fix

```bash
# Monitor node upgrade progress
watch 'kubectl get nodes -o wide | grep -E "NAME|CURRENT_VERSION|TARGET_VERSION"'

# Check no pods stuck
kubectl get pods -A | grep -E "Terminating|Pending"

# Confirm upgrade resuming
gcloud container operations list --cluster CLUSTER_NAME --zone ZONE --limit=1
```
