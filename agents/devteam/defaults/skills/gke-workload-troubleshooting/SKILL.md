---
name: gke-workload-troubleshooting
description: Systematic Standard Operating Procedure (SOP) for diagnosing GKE workload failures, crash loops, resource OOMs, mounting errors, and connectivity timeouts.
---

# GKE Workload Troubleshooting Skill

Use this skill to systematically diagnose and resolve failures in application workloads deployed in GKE clusters. This skill enforces a read-only diagnostics boundary before proposing manifest or config corrections.

## 🔍 Diagnostic Workflow

### Step 0: Context Acquisition & Time Window Definition

To begin troubleshooting, acquire the following context from the user or active `SETTINGS.md` config:

- **Project ID** (e.g., `my-gcp-project`)
  - **Cluster Name** (e.g., `my-gke-cluster`)
  - **Workload Name** (e.g., `payment-api`)
  - **Workload Namespace** (e.g., `checkout`)
  - **Issue Time** (Optional, e.g., `2026-06-01T15:30:00Z`)

#### Time Handling & Fallbacks:

1. **Determine Issue Timestamp ($T$)**:
   - **Specific Time Provided**: If the user provides a specific timestamp, use it as $T$.
   - **Relative Time Provided (e.g., "5 minutes ago")**: Dynamically calculate the corresponding UTC timestamp based on the current system time, and use it as $T$.
   - **No Time Provided (Default)**:
     1. Retrieve the GKE pod status (`kubectl get pods -n <namespace> -o yaml`).
     2. If there are crashing or pending containers, check their state transition timestamps (e.g. `status.containerStatuses[*].lastState.terminated.finishedAt` or `status.startTime`) and use that transition time as $T$.
     3. If no active transitions are found, default to the **current system time** as $T$.

2. **Window Calculation**: Center a 1-hour query window around the issue timestamp $T$:
   - `Start_Time` = `T - 30m`
   - `End_Time` = `T + 30m`

---

### Step 1: Analyze Pod Status and Conditions

Inspect the workload's active pod states and controller status.

**Diagnostic Commands:**

```bash
kubectl get pods -l app=<workload_name> -n <workload_namespace>
kubectl get deploy/<workload_name> -n <workload_namespace> -o yaml
```

#### Diagnostic Decision Tree:

- **Phase: Pending**:
  - The Pod cannot schedule on any node. Proceed directly to **Step 2 (Query Namespace Events)**.
- **State: CrashLoopBackOff / Error**:
  - Container is booting but exiting repeatedly. Check the terminated status using:

    ```bash
    kubectl get pod <pod_name> -n <workload_namespace> -o jsonpath='{.status.containerStatuses[*].lastState.terminated}'
    ```

    - **ExitCode: 137 (OOMKilled)**: The container exceeded its memory limit. Propose scaling memory limits up (Go to **Step 5**).
    - **ExitCode: 1 or other non-zero codes**: The application code crashed. Proceed directly to **Step 3 (Inspect Logs)**.

- **State: ContainerCreating**:
  - The container is blocked during volume mount, networking setup, or image pulling. Proceed directly to **Step 2 (Query Namespace Events)**.

---

### Step 2: Query Namespace Events

Look for infrastructure, volume, image, or scheduling alerts in GKE.

**Diagnostic Command:**

```bash
kubectl get events -n <workload_namespace> --sort-by='.metadata.creationTimestamp'
# Or query Cloud Logging for historical GKE events within the time window:
gcloud logging read "resource.type=\"k8s_cluster\" AND logName=\"projects/<project_id>/logs/events\" AND jsonPayload.involvedObject.namespace=\"<workload_namespace>\"" --start-time="[Start_Time]" --end-time="[End_Time]" --project="<project_id>"
```

_Note: Retrieve the sorted events list and manually inspect the event timestamps (CreationTimestamp/LastSeen) to identify failures occurring within the `[Start_Time]` and `[End_Time]` window._

#### Signature Identifiers:

- **`FailedScheduling`**: Node resource exhaustion. Look for messages like `0/3 nodes are available: 3 Insufficient memory.` or missing node affinity tolerations (e.g. Spot VM taints).
- **`FailedMount`**:
  - Missing PersistentVolumeClaim (`PVC`).
  - Missing Secret (`Secret "<secret-name>" not found`).
  - Missing ConfigMap (`ConfigMap "<configmap-name>" not found`).
- **`Failed` / `BackOff` (Image Pull)**:
  - Wrong image tag, missing image registry authentication (e.g., ImagePullBackOff).

---

### Step 3: Inspect Application Logs

Extract exceptions and stack traces from the application runtime.

**Diagnostic Commands:**

```bash
# Check current active log stream (handles multi-container pods)
kubectl logs <pod_name> -n <workload_namespace> --all-containers --tail=100

# Check logs from previously terminated container instances (handles multi-container pods)
kubectl logs <pod_name> -n <workload_namespace> --all-containers -p --tail=100
```

#### Signature Identifiers:

- **Stack Trace / Unhandled Exception**: Look for language-specific stack traces (e.g., `panic:`, `NullPointerException`, `Traceback (most recent call)`). This indicates an application bug.
- **Egress Network Timeout**: Look for connection timeouts (e.g., `Connection timed out`, `dial tcp: i/o timeout`). Proceed to **Step 4 (Verify Connectivity)**.
- **Permission Errors (ReadOnlyRootFilesystem)**: Look for write errors (e.g., `Read-only file system`, `Permission denied` when writing to `/tmp` or `/var/log`). Propose adding an `emptyDir` volume mount to that directory in the manifest.

---

### Step 4: Verify Service Connectivity and Network Policies

Troubleshoot connection drops to other services.

**Diagnostic Commands:**

```bash
# Verify target endpoint is active
kubectl get endpoints <target_service_name> -n <target_namespace>

# Query network policies inside namespace
kubectl get networkpolicies -n <workload_namespace> -o yaml
```

#### Logic:

- If the endpoints list is empty, the target microservice itself is failing to schedule or boot (troubleshoot target service).
- If endpoints exist but logs show timeouts, analyze the `NetworkPolicy` egress blocks. Verify if egress to the target service's IP/port is explicitly whitelisted.

---

### Step 5: Propose GitOps Correction

Following the GitOps boundary, **do not apply patches directly to the cluster**.

1. Synthesize the root cause analysis for the human operator (e.g. _"payment-api is failing with exit code 137 because its memory limit is set to 256Mi while actual usage spiked to 270Mi"_).
2. Generate the corrected YAML manifest patch (e.g. increase memory limits, add missing Secret mounts, or add tolerations for Spot nodes).
3. Create a branch, commit the change, and open a Pull Request (PR) on GitHub. Wait for human merge.
