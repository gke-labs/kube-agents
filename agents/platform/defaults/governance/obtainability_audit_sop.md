# SOP: Obtainability Audit (Daily Governance)

**Purpose:** Audits GKE cluster configurations fleet-wide to identify rigid, high-risk node resource allocations (e.g., hardcoded hostname bindings, static zone selectors) and automatically generates remediation YAML patches to align them with flexible capacity pools.

---

## Execution Checklist

### 1. Auditing Target Fleet

- Call the native MCP tool `mcp_platform_control_list_operators` to retrieve the active GKE clusters list.

### 2. Obtainability & Rigidity Auditing Rules

For each GKE cluster, query the Operator Agent to check workload configuration rigidity:

1.  **Static Node Bindings Audits:**
    - Query: `"kubectl get deployments,statefulsets -A -o json"`
    - 🚨 **Rigid Allocation:** Any workload utilizing `nodeSelector` targeting a specific hostname (e.g., `kubernetes.io/hostname`) or a specific zone (e.g., `topology.kubernetes.io/zone: us-central1-a`) is flagged.
    - _Why:_ This prevents the cluster autoscaler from dynamically scheduling pods across flexible node pools, leading to capacity bottlenecks.
2.  **Autoscaling Compliance Audits:**
    - Query: `"kubectl get deployments -A -o json"`
    - 🚨 **Rigid Allocation:** Any deployment running with `replicas: > 3` that **lacks** an associated `HorizontalPodAutoscaler` (HPA) resource is flagged as a rigid capacity allocation.

### 3. Generate Remediation Recommendations

If rigid allocations are identified:

1.  **Synthesize YAML patches:** Dynamically generate the recommended K8s YAML patches:
    - Remove static node selectors and replace them with standard `ComputeClass` node tolerations.
    - Generate an `HorizontalPodAutoscaler` (HPA) spec for the rigid deployment.
2.  **Propagate to DevTeam:** Send the generated YAML patches directly to the corresponding DevTeam Agent's inbox, asking them to apply the changes to their workspace files.
3.  **Log in daily report:** Document the list of audited workloads and generated patches in the daily Obtainability Report.
