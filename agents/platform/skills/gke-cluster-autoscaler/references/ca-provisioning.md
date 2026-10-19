# Cluster Autoscaler: Provisioning & Strategies

> A change goes out as a pull request to the GitOps repository, never as a write from here: Kubernetes objects as YAML through `gke-manifest-generation`, cluster settings and other Google Cloud resources as Config Connector YAML through `gcp-config-connector`, opened with `submit-suggestion`. Each profile's config offers only the `gke` MCP server's read tools, and the broker refuses `kubectl` and `gcloud` writes.

## Enabling Scaling (Standard)

### cluster autoscaler - Per Pool

- **Enable (New or Existing Pool):** `spec.autoscaling.minNodeCount: 1` and `spec.autoscaling.maxNodeCount: 10` on the pool's `ContainerNodePool`, proposed through `gcp-config-connector`.

### Node Auto Provisioning - Cluster-wide

- **Enable:** `spec.clusterAutoscaling.enabled: true` on the cluster's `ContainerCluster`, with `spec.clusterAutoscaling.resourceLimits` entries for `cpu` (minimum 4, maximum 200) and `memory` (minimum 16, maximum 800), proposed through `gcp-config-connector`.

### Node pool auto-creation - Per ComputeClass

- **Enable:** Set `nodePoolAutoCreation.enabled: true` in the ComputeClass.
- **GKE 1.33.3+:** Works without cluster-wide Node Auto Provisioning enabled.

## Provisioning Strategies

| Strategy | Strengths | Use Case |
|----------|-----------|----------|
| **Manual Pools** | Fast scheduling; Stable names. | Latency-sensitive; manual management. |
| **node pool auto-creation (ComputeClass)** | Best obtainability; Scale-to-zero. | Bursty; batch; cost-sensitive. |
| **Hybrid** | Manual pool at top; node pool auto-creation fallback. | **Recommended for Production.** |

## Cutover: Node Auto Provisioning to node pool auto-creation
1. **Propose ComputeClasses:** Classes with `nodePoolAutoCreation.enabled: true`, as a pull request.
2. **Opt Workloads In:** Add `nodeSelector: cloud.google.com/compute-class: <name>` to their manifests, as a pull request.
3. **Drain Old Pools:** Draining nodes in old Node Auto Provisioning-managed pools has no pull request form; it is the platform operator's step, not one to run from here.

## Scale-to-Zero Behavior
- **Manual Pools:** Standard cluster autoscaler keeps ≥1 node unless empty pool deletion is supported/enabled.
- **node pool auto-creation-managed:** Autoscaler can delete the entire pool when empty.
