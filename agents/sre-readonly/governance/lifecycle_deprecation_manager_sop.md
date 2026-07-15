# SOP: Lifecycle / Deprecation Manager (Monthly Governance)

**Purpose:** Proactively scans application manifests fleet-wide for deprecated Kubernetes API versions and alerts development teams before impending GKE cluster upgrades.

---

## Execution Checklist

### 1. Identify Target GKE Version Upgrades

- Scan GKE server configurations to identify the next target GKE upgrade version (e.g. upgrading from `1.28` to `1.29`).
- Identify **Impending API Deprecations** in the target version (e.g., `flowcontrol.apiserver.k8s.io/v1beta2` is deprecated in `1.29`).

### 2. Scan Application Workload Manifests

For each active namespace in the fleet:

1.  Inspect workload manifests directly using native GKE monitoring and read-only tools:
2.  Inspect all resource API versions (`apiVersion` keys).
3.  Identify any resources using the deprecated API versions.

### 3. Send Proactive Deprecation Warnings

If any deprecated APIs are found in a namespace:

1.  Formulate a concise warning and log the deprecation report directly.
2.  Log the list of notified teams in your monthly report.
