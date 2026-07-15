# SOP: Blueprint Sync (Daily Governance)

**Purpose:** Audits all managed GKE clusters against the master platform blueprints to ensure configuration consistency and automatically reconcile drift.

---

## Execution Checklist

### 1. Identify Target Fleet

- Retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools.

### 2. Audit Live GKE Configurations

For each active GKE cluster in the fleet:

1.  Inspect the live containercluster manifest directly using native GKE monitoring and read-only tools:
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
