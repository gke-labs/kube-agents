# SOP: Security Patch Orchestrator (Daily Governance)

**Purpose:** Scans the GKE fleet for outdated Kubernetes control plane and node versions, audits active security CVEs, and coordinates the staggered, zero-downtime rollout of GKE upgrades.

---

## Execution Checklist

### 1. Audit GKE Control Plane & Node Versions

For each active GKE cluster retrieved by calling the native MCP tool `mcp_platform_control_list_operators`:

1.  Invoke the native MCP tool `mcp_platform_control_call_agent` to query the Operator Agent for the active GKE master and node versions:
    - **`agent_id`**: `operator-<cluster>-<location>`
    - **`prompt`**: `"kubectl version -o json"`
2.  Query the GCP GKE regional server configuration to find the latest available GKE security patches in the target region:
    ```bash
    gcloud container get-server-config --region="<location>" --project="agentic-harness-demo" --format="json"
    ```

### 2. Identify Security Vulnerabilities

- Compare the active GKE version against the **Latest Stable Security Patch** returned by the server configuration.
- Identify if the active GKE version contains any known high-severity GKE CVEs (Common Vulnerabilities and Exposures).

### 3. Coordinate Staggered Zero-Downtime Upgrades

If an emergency security patch upgrade is required:

1.  **Propose Dev-First Upgrade (GitOps PR):**
    - Do **NOT** apply the version patch directly to the cluster.
    - Utilize your **`submit-suggestion` skill** to update the GKE version inside the cluster manifest in git, and **submit a GitHub Pull Request (PR)** for the development/staging cluster (e.g., `mercury-03`).
    - Inform the SRE team that the Dev upgrade PR is ready for manual review and merge.
2.  **Propose Prod Promotion (GitOps PR):**
    - Once the Dev upgrade is merged, provisioned, and monitored healthy for 30 minutes, repeat the process.
    - Utilize the **`submit-suggestion` skill** to submit a Pull Request (PR) proposing the version upgrade for the production cluster (e.g., `mercury-04`).
3.  **Log Release Rollout Progress:**
    - Document the PR links and the staggered rollout timeline in the cron output.
