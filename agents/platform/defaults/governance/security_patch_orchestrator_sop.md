# SOP: Security Patch Orchestrator (Daily Governance)

**Purpose:** Scans the GKE fleet for outdated Kubernetes control plane and node versions, audits active security CVEs, and coordinates the staggered, zero-downtime rollout of GKE upgrades.

---

## Execution Checklist

### 1. Audit GKE Control Plane & Node Versions

For each active GKE cluster retrieved by calling the native MCP tool `mcp_platform_control_list_operators`:

1.  Query the Operator Agent for the active master and node versions:
    ```bash
    ./scripts/agent_call.py operator-<cluster>-<location> "kubectl version -o json"
    ```
2.  Query the GCP GKE regional server configuration to find the latest available GKE security patches in the target region:
    ```bash
    gcloud container get-server-config --region="<location>" --project="agentic-harness-demo" --format="json"
    ```

### 2. Identify Security Vulnerabilities

- Compare the active GKE version against the **Latest Stable Security Patch** returned by the server configuration.
- Identify if the active GKE version contains any known high-severity GKE CVEs (Common Vulnerabilities and Exposures).

### 3. Coordinate Staggered Zero-Downtime Upgrades

If an emergency security patch upgrade is required:

1.  **Staggered Dev-First Rollout:**
    - Initiate the GKE master upgrade on the **development/staging cluster** (e.g., `mercury-03`) first by applying a GKE cluster version patch to the Custom Resource.
    - Monitor the Dev Operator Agent's health and metrics for 30 minutes.
2.  **Prod Promotion Gate:**
    - If the Dev cluster remains healthy, proceed to apply the version patch to the **production cluster** (e.g., `mercury-04`).
3.  **Log Release rollout progress:**
    - Document the rollout timeline and upgrade status in the cron output.
