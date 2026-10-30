---
name: gke-batch-hpc
description: >-
  Runs batch and HPC workloads on GKE, utilizing job queues and parallel
  processing. Use when running GKE batch jobs, configuring GKE HPC, or setting
  up GKE job queues. Don't use for standard web application deployments (use
  gke-app-onboarding instead).
metadata:
  category: Containers
---

# GKE Batch & HPC Workloads

This reference covers running batch processing and high-performance computing
(HPC) workloads on GKE.

> **MCP Tools:** `get_k8s_resource`, `describe_k8s_resource`, `get_k8s_logs`,
> `list_k8s_events`
>
> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

## When to Use

-   Running batch data processing pipelines
-   HPC simulations (CFD, molecular dynamics, financial modeling)
-   Large-scale parallel computation (MPI, MapReduce)
-   ML training jobs
-   CI/CD build farms

## Batch Processing on GKE

### Kubernetes Jobs

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: batch-job
spec:
  parallelism: 10
  completions: 100
  backoffLimit: 3
  template:
    spec:
      containers:
      - name: worker
        image: <IMAGE>
        resources:
          requests:
            cpu: "1"
            memory: "2Gi"
      restartPolicy: Never
```

### JobSet (for Complex Multi-Job Workflows)

The golden path enables JobSet monitoring (`JOBSET` in monitoringConfig).

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: training-job
spec:
  replicatedJobs:
  - name: workers
    replicas: 4
    template:
      spec:
        parallelism: 1
        completions: 1
        template:
          spec:
            containers:
            - name: worker
              image: <IMAGE>
              resources:
                requests:
                  cpu: "4"
                  memory: "8Gi"
```

### Kueue (Job Queuing)

Kueue manages job scheduling and resource allocation for batch workloads. Its release
manifests and the queues below are proposed as a pull request through `submit-suggestion`.
Pin the manifests to a release tag rather than `latest`: the `manifests.yaml` asset at
`https://github.com/kubernetes-sigs/kueue/releases/download/<TAG>/manifests.yaml`, with the
tag named in the pull request.
Kueue's CRDs are too large for client-side apply, so the repository's applier must use
server-side apply; say so in the pull request if it does not:

```yaml
# Define a ClusterQueue
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: batch-queue
spec:
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default
      resources:
      - name: "cpu"
        nominalQuota: 100
      - name: "memory"
        nominalQuota: "200Gi"
---
# Allow a namespace to use the queue
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: batch-local
  namespace: batch-jobs
spec:
  clusterQueue: batch-queue
```

## HPC on GKE

### Compact Placement (Low-Latency Networking)

For tightly-coupled HPC workloads that need low-latency inter-node
communication, Standard clusters use a node pool with compact placement, proposed as a
`ContainerNodePool` in Config Connector YAML through `gcp-config-connector`: for example
`spec.nodeConfig.machineType: c3-standard-44`, `spec.placementPolicy.type: COMPACT`,
`spec.initialNodeCount: 8`, and `spec.autoscaling` with `minNodeCount: 0` and
`maxNodeCount: 16`.

### MPI Workloads

Use the MPI Operator for MPI-based HPC applications. Its manifest and the `MPIJob` below
are proposed as a pull request through `submit-suggestion`. Pin the manifest to a release tag
rather than `master`:
`https://raw.githubusercontent.com/kubeflow/mpi-operator/<TAG>/deploy/v2beta1/mpi-operator.yaml`,
with the tag named in the pull request:

```yaml
apiVersion: kubeflow.org/v2beta1
kind: MPIJob
metadata:
  name: hpc-simulation
spec:
  slotsPerWorker: 4
  mpiReplicaSpecs:
    Launcher:
      replicas: 1
      template:
        spec:
          containers:
          - name: launcher
            image: <MPI_IMAGE>
            command: ["mpirun", "-np", "32", "./simulation"]
            resources:
              requests:
                cpu: "1"
                memory: "2Gi"
              limits:
                cpu: "2"
                memory: "4Gi"
    Worker:
      replicas: 8
      template:
        spec:
          containers:
          - name: worker
            image: <MPI_IMAGE>
            resources:
              requests:
                cpu: "4"
                memory: "8Gi"
              limits:
                cpu: "8"
                memory: "16Gi"
```

## Cost Optimization for Batch/HPC

### Spot VMs for Batch

Batch workloads are ideal Spot VM candidates (interruptible, can checkpoint).
Use a ComputeClass with Spot-first priority and `activeMigration` to return to
Spot when available. See the `gke-compute-classes` skill for the
Spot-with-fallback pattern.

### Scale-to-Zero

For batch clusters, allow node pools to scale to zero when no jobs are running:

-   Autopilot (golden path): Automatic, nodes scale to zero when no pods are
    scheduled
-   Standard: Set `spec.autoscaling.minNodeCount: 0` on batch node pools' `ContainerNodePool`

## Best Practices & Production Guidelines

-   **Resource Quotas**: Always specify resource requests and limits (CPU,
    memory, and optionally GPU/TPU) for all batch/HPC manifests. This is
    critical for Kueue admission, autoscaling, and preventing resource
    starvation in the cluster.
-   **TPU/Spot Cluster Maintenance**: For long-running AI training runs on Spot
    VMs/TPUs, advise using **GKE maintenance exclusions** to block automatic
    cluster upgrades/reboots during the active training window to minimize
    unnecessary preemption.
-   **MPI Workloads**: Use the **Kubeflow Training Operator** to orchestrate
    distributed MPI applications via the `MPIJob` custom resource.
-   **Kueue & JobSet**: Use **Kueue** for multi-tenant job queueing and fair
    sharing; use **JobSet** for multi-component tightly coupled workloads.
-   **Resilience**: Always set a `backoffLimit` on Jobs, and implement
    application-level checkpointing (e.g., using Orbax or PyTorch checkpointing)
    to survive Spot VM preemption.

<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Before scheduling a GPU/TPU batch job with a deadline

Before recommending a start time, zone, or capacity path for a GPU/TPU or large-shape batch job —
especially one that must finish inside a horizon — load the
[capacity-obtainability](../capacity-obtainability/SKILL.md) skill and run its **Future windows**
section: verify the regional quota for the exact accelerator metric, probe
`gcloud beta compute advice calendar-mode` once per candidate region for the job's shape, count,
duration, and horizon, and rank the returned windows. That skill owns the probe's flags, the
chips-per-node arithmetic, the ranking rule, and the paired ProvisioningRequest + LocalQueue
shapes; follow it rather than restating them here.
