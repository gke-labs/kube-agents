# SOP: Compliance Audit (Weekly Governance)

**Purpose:** Performs a fleet-wide security and architectural policy audit across all GKE namespaces and clusters.

---

## Execution Checklist

### 1. Auditing Target Fleet

- Call the native MCP tool `mcp_platform_control_list_operators` to retrieve the active GKE clusters list.

### 2. GKE Security Auditing Rules

For each active cluster, query the cluster's Operator Agent using the **`call_agent`** tool (from the **`agent_common`** toolset) to execute these auditing checks:

1.  **Workload Hardening Audits:**
    - Query: `"kubectl get pods -A -o jsonpath='{.items[*].spec.containers[*].securityContext.privileged}'"`
    - 🚨 **Policy Violation:** Any container running with `privileged: true` must be logged immediately as a Critical Violation.
2.  **Namespace Isolation Audits:**
    - Query: `"kubectl get networkpolicies -A"`
    - 🚨 **Policy Violation:** Every namespace (except `kube-system` and `cnrm-system`) **must** possess an active `NetworkPolicy` that restricts ingress/egress. Any namespace lacking an active `NetworkPolicy` is a Major Violation.
3.  **RBAC Over-Privilege Audits:**
    - Query: `"kubectl get clusterrolebindings -o json"`
    - 🚨 **Policy Violation:** Verify that no non-system service accounts have been granted the `cluster-admin` role. Wildcard `*` bindings on resources are strictly forbidden for non-operator workloads.

### 3. Report & Warn

- Generate a formatted compliance markdown report.
- If violations are found, present them clearly to the platform administrators with exact namespaces, pod names, and remediation instructions (e.g., recommended NetworkPolicy YAMLs).
