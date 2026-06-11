# SOP: Policy Propagation (Hourly Governance)

**Purpose:** Proactively propagates the latest security, networking, and resource policy changes from the platform defaults down to all active GKE clusters and subagent namespaces.

---

## Execution Checklist

### 1. Target Selection

- Call the native MCP tool `mcp_platform_control_list_operators` to retrieve the active GKE clusters list.

### 2. Distribute Policies

For each active GKE cluster in the fleet:

1.  **Sync Pod Security Policies:**
    - Read your local default templates folder: `/opt/defaults/templates/operator/` and `/opt/defaults/templates/devteam/`.
    - Extract the latest baseline `NetworkPolicy` and `ResourceQuota` YAML manifests.
2.  **Propagate over the Network:**
    - Invoke the native MCP tool `mcp_platform_control_call_agent` to delegate the updated manifests to the GKE Operator:
      - **`agent_id`**: `operator-<cluster>-<location>`
      - **`prompt`**: `"apply the following manifest using your SOP:\n<MANIFEST_CONTENT>"`
3.  **Verify Propagation:**
    - Query the Operator to confirm the policies are active inside GKE using its standard operating procedures.

### 3. Log Sync Completion

- Record the list of successfully synchronized GKE clusters and namespaces in the cron job run log.
