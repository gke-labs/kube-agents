# SOP: Blueprint Sync (Daily Governance)

**Purpose:** Audits all managed GKE clusters against the master platform blueprints to ensure configuration consistency and automatically reconcile drift.

---

## Execution Checklist

### 1. Identify Target Fleet

- Call the native MCP tool `mcp_platform_control_list_operators` to retrieve the active GKE operator agents list.
- Extract the list of all active GKE `agent_id` and `cluster_name` records from the tool output.

### 2. Audit Live GKE Configurations

For each active GKE cluster in the fleet:

1.  Invoke the native MCP tool `mcp_platform_control_call_agent` to query the cluster's GKE Operator Agent:
    - **`agent_id`**: `operator-<cluster>-<location>`
    - **`prompt`**: `"kubectl get containercluster <cluster> -n agent-system -o json"`
2.  Compare the returned manifest against the **Platform Master Blueprint**:
    - ✅ `enableAutopilot` must be `true`.
    - ✅ `privateClusterConfig.enablePrivateNodes` must be `true`.
    - ✅ `privateClusterConfig.enablePrivateEndpoint` must be `false`.
    - ✅ `metadata.annotations["cnrm.cloud.google.com/remove-default-node-pool"]` must be `"true"`.

### 3. Reconcile Configuration Drift

If any discrepancies or configuration drifts are identified:

1.  Generate the corrected GKE cluster Custom Resource YAML file.
2.  **Do NOT apply the changes directly to the cluster control plane.**
3.  Exclusively utilize your **`submit-suggestion` skill** to commit the corrected manifest to a GitOps branch and **submit a GitHub Pull Request (PR)** for human review and approval.
4.  Log a detailed summary of the drift and the submitted PR link in your session output.
