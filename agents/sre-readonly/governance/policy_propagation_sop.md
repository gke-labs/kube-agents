# SOP: Policy Propagation (Hourly Governance)

**Purpose:** Proactively propagates the latest security, networking, and resource policy changes from the platform defaults down to all active GKE clusters and managed namespaces.

---

## Execution Checklist

### 1. Target Selection

- Retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools.

### 2. Distribute Policies

For each active GKE cluster in the fleet:

1.  **Sync Pod Security Policies:**
    - Read your local default templates folder: `/opt/defaults/templates/`.
    - Extract the latest baseline `NetworkPolicy` and `ResourceQuota` YAML manifests.
2.  **Propagate and Verify:**
    - Inspect and verify that the policies are active inside GKE directly using native GKE monitoring and read-only tools.

### 3. Log Sync Completion

- Record the list of successfully synchronized GKE clusters and namespaces in the cron job run log.
