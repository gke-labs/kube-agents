# SOP: Fleet-wide Cost Analysis (Daily Governance)

**Purpose:** Aggregates node instance type layouts and cluster resource requests across the GKE fleet to identify daily cost deltas and compute right-sizing optimization opportunities.

---

## Execution Checklist

### 1. Gather Node Topology & Billing Layouts

For each GKE cluster retrieved by calling the native MCP tool `mcp_platform_control_list_operators`:

1.  Invoke the native MCP tool `mcp_platform_control_call_agent` to query the cluster's GKE Operator Agent to retrieve active node configurations:
    - **`agent_id`**: `operator-<cluster>-<location>`
    - **`prompt`**: `"kubectl get nodes -o json"`
2.  Extract:
    - Instance Types (e.g., `e2-standard-4`, `n2-highmem-8`).
    - Pricing Model (Spot VMs vs. Standard On-Demand).
    - Unused/idle CPU and Memory allocations.

### 2. Compute Optimization Opportunities

1.  **Spot VM Candidate Search:** Identify namespaces running non-critical, stateless development workloads (e.g. `devteam-*` namespaces) on expensive standard On-Demand VMs.
2.  **Idle Capacity Reclamation:** Identify nodes where aggregate Pod CPU/Memory _requests_ are less than `40%` of the node's capacity.
3.  **Right-Sizing Recommendations:** Formulate recommendations to:
    - Shift stateless development pods to **Spot VMs**.
    - Recommend resource limits downsizing in the corresponding DevTeam workspaces.

### 3. Publish Daily Cost Delta Report

- Deliver a detailed, comparative billing efficiency chart in the cron output report, identifying exact monthly savings (USD) if the optimizations are applied.
