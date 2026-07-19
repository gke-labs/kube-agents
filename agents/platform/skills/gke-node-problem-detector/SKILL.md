---
name: gke-node-problem-detector
description: Workflows for diagnosing node-level issues, kernel deadlocks, OOM kills, and hardware/system degradation on GKE clusters.
---

# GKE Node Problem Detector & Health Triage

This skill provides diagnostic workflows for detecting, triaging, and remediating node-level problems on GKE clusters before they cause widespread workload outages.

## Workflows

### 1. Check Node Problem Conditions

Inspect all nodes for active Node Problem Detector (NPD) conditions such as `KernelDeadlock`, `ReadonlyFilesystem`, `FrequentKubeletRestart`, `FrequentDockerRestart`, and `OOMKilling`.

**Command:**

```bash
kubectl get nodes -o custom-columns=\
NAME:.metadata.name,\
STATUS:.status.conditions[-1].type,\
KERNEL_DEADLOCK:.status.conditions[?(@.type=="KernelDeadlock")].status,\
READONLY_FS:.status.conditions[?(@.type=="ReadonlyFilesystem")].status,\
OOM_KILLS:.status.conditions[?(@.type=="OOMKilling")].status
```

### 2. Audit Node Memory & OOM Events

Search recent cluster events and system logs for Out-Of-Memory (OOM) kills affecting node daemons or application pods.

**Command:**

```bash
kubectl get events -A --field-selector reason=OOMKilling --sort-by='.metadata.creationTimestamp'
```

### 3. Diagnose Node Resource Pressure

Check for DiskPressure, MemoryPressure, or PIDPressure across node pools.

**Command:**

```bash
kubectl get nodes -o custom-columns=\
NAME:.metadata.name,\
READY:.status.conditions[?(@.type=="Ready")].status,\
MEMORY_PRESSURE:.status.conditions[?(@.type=="MemoryPressure")].status,\
DISK_PRESSURE:.status.conditions[?(@.type=="DiskPressure")].status,\
PID_PRESSURE:.status.conditions[?(@.type=="PIDPressure")].status
```

### 4. Node Remediation Guidance

When a node reports a critical condition (`ReadonlyFilesystem` or `KernelDeadlock`):

1. **Cordon the Node**: Prevent new pods from scheduling onto the unhealthy node:
   ```bash
   kubectl cordon <node-name>
   ```
2. **Safely Drain Workloads**: Evict pods to healthy nodes:
   ```bash
   kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data --grace-period=30
   ```
3. **Trigger GKE Node Repair**: If GKE Auto-repair is enabled, deleting the underlying GKE instance recreates the node cleanly:
   ```bash
   gcloud compute instances delete <node-name> --zone <zone> --quiet
   ```
