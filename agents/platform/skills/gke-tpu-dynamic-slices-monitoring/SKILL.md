---
name: gke-tpu-dynamic-slices-monitoring
description: >-
  Monitors, troubleshoots, and manages GKE TPU Dynamic Slices custom resources. Use when checking TPU slice lifecycle states, troubleshooting slice provisioning failures, validating single-slice or multi-slice (JobSet) workload manifests, or diagnosing stuck finalizers and the slice controller for the platform operator to clear. Don't use for generic GKE cluster node pool creation or standard non-TPU workload management (use gke-basics or gke-cluster-creation instead).
metadata:
  category: Containers
---

# GKE TPU Dynamic Slices Monitoring & Management

Monitors the status of TPU Slice custom resources, troubleshoots provisioning
failures, validates workload manifests on dynamic slices, and hands cleanups to the platform operator.

> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

## Prerequisites

-   Cloud Logging enabled for the project.
-   `kubectl` and `gcloud` CLIs configured to access the GKE cluster.

## Diagnostic Workflow

### Step 0: Context Acquisition & Time Window Definition

Gather project, cluster, and slice context using cluster tools or the following
parameters:

-   **Project ID**: `{project_id}` (e.g., `my-gcp-project`)
-   **Cluster Name**: `{cluster_name}` (e.g., `tpu-cluster`)
-   **Region/Zone**: `{location}` (e.g., `us-central1-a`)
-   **Slice Name**: `{slice_name}` (e.g., `test-slice`)
-   **Issue Time**: `{timestamp}` (Optional; default to the last 30 minutes
    window `[T - 30m]` to `[T + 30m]`)

--------------------------------------------------------------------------------

### Step 1: Describe the Slice Custom Resource [Low Risk]

When asked to inspect, troubleshoot, or check a slice status, immediately execute `kubectl describe slice {slice_name}` using available cluster tools to perform the inspection. Parse the resulting `Status.Conditions` output against the condition table below to diagnose the exact state and provide concrete recommendations.

-   **Command**:

    ```bash
    kubectl describe slice {slice_name}
    ```

#### State & Reason Analysis

Analyze the `Status.Conditions` (especially `Type: Ready` and its `Reason` and
`Status`):

| Lifecycle State / Reason  | Meaning                | Recommended Action      |
| :------------------------ | :--------------------- | :---------------------- |
| **`SliceNotCreated`**     | GKE Slice Controller   | Wait a few minutes and  |
:                           : is initializing the    : re-check slice status.  :
:                           : slice and performing   :                         :
:                           : resource checks.       :                         :
| **`SliceCreationFailed`** | Prerequisites          | Verify selected nodes   |
:                           : validation failed      : exist, are unallocated, :
:                           : (e.g., selected nodes  : and topology matches    :
:                           : don't exist, nodes are : partition count.        :
:                           : already used by        :                         :
:                           : another slice, or the  :                         :
:                           : topology doesn't match :                         :
:                           : the number of          :                         :
:                           : partitions).           :                         :
| **`ACTIVATING`**          | GKE is actively        | Monitor node            |
:                           : forming and            : provisioning.           :
:                           : provisioning the TPU   :                         :
:                           : slice.                 :                         :
| **`ACTIVE`**              | The TPU slice is       | Proceed to deploy or    |
:                           : successfully formed    : check workloads.        :
:                           : and ready to host      :                         :
:                           : workloads.             :                         :
| **`ACTIVE_DEGRADED`**     | The slice is usable,   | Monitor workload logs   |
:                           : but one or more        : for interconnect or     :
:                           : sub-blocks are         : device errors. Check    :
:                           : degraded.              : faulty node VMs.        :
| **`FAILED`**              | GKE failed to form the | Ensure all selected     |
:                           : TPU slice (e.g.,       : nodes belong to the     :
:                           : selected nodes are not : same reservation block. :
:                           : part of the same       :                         :
:                           : reservation block).    :                         :
| **`DEACTIVATING`**        | The slice is           | Wait for dismantling to |
:                           : dismantling (triggered : finish, or patch        :
:                           : by user deletion or a  : finalizers if stuck.    :
:                           : critical systemic      :                         :
:                           : failure).              :                         :
| **`INCOMPLETE`**          | The terminal phase     | No action required; the |
:                           : before the Slice CR is : resource will be        :
:                           : deleted from the       : removed shortly.        :
:                           : cluster.               :                         :

#### Provisioning Failure Troubleshooting Checklist

When investigating slice creation or provisioning failures (`SliceCreationFailed` or `FAILED`), perform the following verification steps:

1. **Node Existence & Allocation Check**: Verify that the selected TPU nodes exist in the cluster and are not already allocated to another slice (`kubectl get nodes -l cloud.google.com/gke-tpu-slice`, `kubectl get slice -A`).
2. **Topology Alignment**: Confirm that the partition count matches the requested topology dimensions (e.g. topology `2x2` requires 4 nodes).
3. **Reservation Block Alignment Check**: Confirm that all selected TPU nodes belong to the same reservation and reservation block.

--------------------------------------------------------------------------------

### Step 2: Verify Workload Specification [Low Risk]

Ensure workload manifests are configured correctly to target the dynamic slice.

#### 1. Single-Slice Workload Requirements

Check that the Pod template contains the following annotations and selectors:

-   **Annotations**:
    -   `cloud.google.com/gke-tpu-slice-topology: "{topology}"` (e.g.,
        `"4x4x4"`)
-   **NodeSelector**:
    -   `cloud.google.com/gke-tpu-topology: "{topology}"` (e.g., `"4x4x4"`)
    -   `cloud.google.com/gke-tpu-accelerator: "{accelerator_type}"` (e.g.,
        `"tpu7x"`)
    -   `cloud.google.com/gke-tpu-slice: "{slice_name}"` (e.g., `"test-slice"`)

#### 2. Multi-Slice (JobSet) Workload Requirements

If deploying a multi-slice JobSet, verify:

-   **JobSet Annotation**:
    -   `alpha.jobset.sigs.k8s.io/exclusive-topology:
        cloud.google.com/gke-tpu-slice`
-   **Pod Template Annotations**:
    -   `cloud.google.com/gke-tpu-slice-topology: "{topology}"`
-   **Pod Template NodeSelector**:
    -   `cloud.google.com/gke-tpu-topology: "{topology}"`
    -   `cloud.google.com/gke-tpu-accelerator: "{accelerator_type}"`
    -   *Note: Do NOT manually specify `cloud.google.com/gke-tpu-slice` in the
        nodeSelector; JobSet handles slice assignment automatically.*

--------------------------------------------------------------------------------

## Resolution & Management Workflow

### Resolution 1: Force Delete a Stuck Slice [High Risk]

If a slice is stuck in `DEACTIVATING` or deletion hangs indefinitely due to stuck finalizers:

1. **Identify Cause**: Explain that finalizers on the slice resource (`metadata.finalizers`) are preventing Kubernetes from completing resource deletion.
2. **Propose Resolution**: Propose removing finalizers from the metadata path (`/metadata/finalizers`) of slice `{slice_name}`. This is a write to a live object with no pull request form, and the broker refuses `kubectl` writes, so it is a step for the platform operator, not one to run from here.

3. **Provide Warning**: Explicitly warn the user that removing finalizers bypasses standard controller dismantling and may leave underlying VM, network, or accelerator resources uncleaned or orphaned.
4. **CRITICAL SAFETY MANDATE**: The response MUST explicitly ask the user for confirmation (e.g. *"Removing finalizers on `/metadata/finalizers` via JSON patch is a high-risk operation that may leave orphaned resources. Do you confirm you want to apply this patch to slice `{slice_name}`?"*) and pause for user confirmation before handing the step on.

--------------------------------------------------------------------------------

### Resolution 2: Disable and Clean Up Slice Controller [High Risk]

If dynamic slicing needs to be disabled:

1.  **Check for existing Slices**:

    ```bash
    kubectl get slice -A
    ```

    Ensure all slices are deleted before disabling the controller.

2.  **Disable Slice Controller**: this cluster setting has no declarative path in a Config Connector repository (the KCC v1.158.0 schema has no field for it), so the agent blocks rather than inventing a field.

3.  **Delete the Slice CRD** (`slices.accelerator.gke.io`) and **clean up node
    labels** (`cloud.google.com/gke-tpu-slice` and
    `cloud.google.com/gke-tpu-slice-topology` on every node): these have no pull
    request form and the broker refuses `kubectl` writes, so they are steps for the
    platform operator.

-   **Safety Rule**: Propose each step and confirm before handing on disabling or
    destructive cleanup steps.
