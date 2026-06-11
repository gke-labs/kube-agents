# SOP: Lifecycle / Deprecation Manager (Monthly Governance)

**Purpose:** Proactively scans application manifests fleet-wide for deprecated Kubernetes API versions and alerts Development Team Agents before impending GKE cluster upgrades.

---

## Execution Checklist

### 1. Identify Target GKE Version Upgrades

- Scan GKE server configurations to identify the next target GKE upgrade version (e.g. upgrading from `1.28` to `1.29`).
- Identify **Impending API Deprecations** in the target version (e.g., `flowcontrol.apiserver.k8s.io/v1beta2` is deprecated in `1.29`).

### 2. Scan Application Workload Manifests

For each active DevTeam Agent in the fleet:

1.  Scan their local manifests folder on your shared persistent volume (if accessible) OR invoke the native MCP tool `mcp_platform_control_call_agent` to query the DevTeam Agent directly:
    - **`agent_id`**: `devteam-<cluster>-<location>-<namespace>`
    - **`prompt`**: `"kubectl get deployments,services,ingresses -n <namespace> -o json"`
2.  Inspect all resource API versions (`apiVersion` keys).
3.  Identify any resources using the deprecated API versions.

### 3. Send Proactive Deprecation Warnings

If any deprecated APIs are found in a DevTeam's workspace:

1.  Formulate a concise warning prompt.
2.  Send the warning directly to the target DevTeam Agent by invoking the native MCP tool `mcp_platform_control_call_agent`:
    - **`agent_id`**: `devteam-<cluster>-<location>-<namespace>`
    - **`prompt`**: `"Warning: GKE cluster <cluster> will be upgraded to v1.29 next month. Your deployment manifest uses deprecated apiVersion <deprecated_api>. Please update your files to use <stable_api> immediately."`
3.  Log the list of notified teams in your monthly report.
