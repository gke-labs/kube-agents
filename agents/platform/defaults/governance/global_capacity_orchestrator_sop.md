# SOP: Global Capacity Orchestrator (Hourly Governance)

**Purpose:** Audits aggregate resource utilization across all GKE clusters and regions, automatically identifying hot spots and directing Cluster Operators to scale or balance workloads.

---

## Execution Checklist

### 1. Gather Resource Metrics

For each active GKE cluster in the fleet (retrieved by calling the native MCP tool `mcp_platform_control_list_operators`):

1.  Invoke the native MCP tool `mcp_platform_control_call_agent` to query the Cluster Operator for GKE resource metrics:
    - **`agent_id`**: `operator-<cluster>-<location>`
    - **`prompt`**: `"kubectl top nodes"`
2.  Calculate the total capacity vs. active utilization:
    - **Aggregate CPU Utilization (%)**
    - **Aggregate Memory Utilization (%)**

### 2. Audit Capacity Limits

Evaluate the metrics against the following **SRE Capacity Thresholds**:

- 🔴 **Critical ( > 85% Utilization):** Risk of node resource exhaustion.
- 🟢 **Under-Utilized ( < 30% Utilization):** Waste of project billing resources.

### 3. Orchestrate Rebalancing Actions

1.  **Scale Up/Down:** If a cluster exceeds `85%` utilization, check if Autopilot is scaling nodes successfully. If not, direct the Operator Agent to check for unschedulable Pods and recommend ComputeClass adjustments.
2.  **Cross-Region Alerting:** If a region (e.g., `us-east1`) is consistently overloaded while another (e.g., `us-central1`) has surplus capacity, generate a recommendation to shift multi-region devteam workloads to the underutilized region.
3.  **Report Output:** Deliver a formatted Fleet Resource Map in your cron run report.
