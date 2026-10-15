---
name: gke-workload-scaling
description: Specific workflows for scaling GKE workloads using HPA and VPA, as well as best practices for autoscaling configuration.
---

# GKE Workload Scaling

This skill provides workflows and best practices for scaling applications on Google Kubernetes Engine (GKE). It covers manual scaling, Horizontal Pod Autoscaling (HPA), and Vertical Pod Autoscaling (VPA).

> You are read-only: each change below is a proposal you report with your findings, never a command you run.

## Workflows

### 1. Manual Scaling

Set a deployment to a fixed number of replicas. Useful for immediate manual intervention or testing.

Propose the new `spec.replicas` value as a patch to the Deployment.

### 2. Horizontal Pod Autoscaling (HPA)

Automatically scale the number of pods based on observed CPU utilization, memory utilization, or custom metrics.

**Prerequisites:**

- Metrics Server must be running (enabled by default on GKE).
- Containers clearly define resource requests/limits.

**Manifest:**
Propose an `autoscaling/v2` `HorizontalPodAutoscaler` whose `scaleTargetRef` names the Deployment, with `minReplicas`, `maxReplicas` and a `Resource` metric target (for example, CPU at 50% average utilization). A complete example is in [assets/hpa-example.yaml](assets/hpa-example.yaml).

**Custom Metrics & External Metrics:**
For GKE, the modern and recommended approach for scaling based on Cloud Monitoring metrics (e.g., Pub/Sub queue length) is to use the **External** metric type, which is natively supported by the GKE control plane without requiring the Custom Metrics Adapter. For application-specific metrics exposed via Prometheus, you can use **Google Cloud Managed Service for Prometheus** or the Prometheus Adapter.

### 3. Vertical Pod Autoscaling (VPA)

Automatically adjust the CPU and memory reservations for your pods to match actual usage. This is critical for right-sizing workloads.

**Prerequisites:**

- VPA must be enabled on the cluster.
  - **Autopilot:** Enabled by default.
  - **Standard:** Must be enabled manually.

**VPA on a Standard Cluster:**
Propose turning on vertical Pod autoscaling for the cluster (Config Connector `ContainerCluster` field `spec.verticalPodAutoscaling.enabled: true`).

**Update Modes:**

- `Off`: Calculates recommendations but does not apply them. Good for "dry run" analysis.
- `Initial`: Assigns resources only at pod creation time.
- `Auto`: Updates running pods by restarting them if recommendations differ significantly from requests.
- `InPlaceOrRecreate`: Attempts to update Pod resources without recreating the Pod. If in-place update is not possible, it reverts to `Auto` mode (requires GKE 1.34+).

**Example:**
Propose an `autoscaling.k8s.io/v1` `VerticalPodAutoscaler` whose `targetRef` names the Deployment, with an `updatePolicy.updateMode` from the list above and, if needed, `minAllowed`/`maxAllowed` bounds per container. A complete example is in [assets/vpa-example.yaml](assets/vpa-example.yaml).

### 4. Cluster Autoscaler

While not a workload-level scaler, the Cluster Autoscaler is essential for ensuring your cluster has enough nodes to run the scaled pods.

**On a Node Pool:**
Propose node-pool autoscaling with a minimum and maximum node count (Config Connector `ContainerNodePool` fields `spec.autoscaling.minNodeCount` and `spec.autoscaling.maxNodeCount`).

## Best Practices

1. **Define Resource Requests:** HPA and VPA rely on accurate resource requests. Always define them in your container specs.
2. **Avoid Metric Conflicts:** Do not configure HPA and VPA to use the same metric (e.g., both CPU). This causes thrashing.
   - _Typical Pattern:_ HPA on CPU, VPA on Memory.
3. **Pod Disruption Budgets (PDBs):** Define PDBs to ensure application availability during scaling events or node upgrades.
4. **HPA Lag:** HPA has a stabilization window (default 5 mins) to prevent rapid fluctuation.
5. **VPA "Auto" Mode Risks:** In "Auto" mode, VPA restarts pods to change resources. Ensure your application handles restarts gracefully (e.g., handles SIGTERM).
   - _Note:_ By default, VPA requires at least 2 replicas to perform evictions. In GKE 1.22+, you can override this by setting `minReplicas` in `PodUpdatePolicy`.
