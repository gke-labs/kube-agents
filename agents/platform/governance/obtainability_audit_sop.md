# SOP: Obtainability Audit (Daily Governance)

**Purpose:** Audits GKE cluster configurations fleet-wide to identify rigid, high-risk node resource allocations (e.g., hardcoded hostname bindings, static zone selectors) and automatically generates remediation YAML patches to align them with flexible capacity pools.

---

## Execution Checklist

### 1. Auditing Target Fleet

- Retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools.

### 2. Obtainability & Rigidity Auditing Rules

For each GKE cluster, inspect workload configuration rigidity directly:

1.  **Static Node Bindings Audits:**
    - Query: `"kubectl get deployments,statefulsets -A -o json"`
    - 🚨 **Rigid Allocation:** Any workload utilizing `nodeSelector` targeting a specific hostname (e.g., `kubernetes.io/hostname`) or a specific zone (e.g., `topology.kubernetes.io/zone: us-central1-a`) is flagged.
    - _Why:_ This prevents the cluster autoscaler from dynamically scheduling pods across flexible node pools, leading to capacity bottlenecks.
2.  **Autoscaling Compliance Audits:**
    - Query: `"kubectl get deployments -A -o json"`
    - 🚨 **Rigid Allocation:** Any deployment running with `replicas: > 3` that **lacks** an associated `HorizontalPodAutoscaler` (HPA) resource is flagged as a rigid capacity allocation.
3.  **Legacy GKE Standard Priority Expander Audit (Pre-1.33.3):**
    - Query: `"kubectl get configmap cluster-autoscaler-priority-expander -n kube-system"`
    - 🚨 **Legacy Fallback Warning:** On pre-1.33.3 GKE Standard clusters, verify that the `priority-expander` ConfigMap or Node Auto-Provisioning (NAP) is configured so Spot node pool stockouts automatically fall back to On-Demand node pools without deadlocking pods in `Pending`.
4.  **Flex / DWS FlexStart Private Preview Whitelist Notice:**
    - ⚠️ **Private Preview Warning:** Flex / DWS FlexStart and FlexCUD obtainability capabilities require GCP Project Whitelisting during Private Preview. When recommending FlexStart or `flexCud: true` fallback priorities, explicitly flag to the user to confirm project whitelisting before deploying Flex ComputeClasses.

### 3. Generate Remediation Recommendations

If rigid allocations are identified:

1.  **Synthesize YAML patches:** Dynamically generate the recommended K8s YAML patches:
    - Remove static node selectors and replace them with standard `ComputeClass` node tolerations.
    - Generate an `HorizontalPodAutoscaler` (HPA) spec for the rigid deployment.
2.  **Log in daily report:** Document the list of audited workloads and generated patches in the daily Obtainability Report.
