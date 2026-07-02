# SOP: Standardization Validator (Weekly Governance)

**Purpose:** Performs a deep-diff structural audit between the live GKE configurations and standard corporate architectural patterns to prevent configuration drift and metadata chaos.

---

## Execution Checklist

### 1. Auditing Target Fleet

- Retrieve the active GKE clusters list directly using native GKE monitoring and read-only tools.

### 2. Standardization Verification Rules

For each active GKE cluster, run these standardization audits directly using native GKE monitoring and read-only tools:

1.  **Resource Labeling Compliance:**
    - Query: `"kubectl get deployments,services -A -o json"`
    - 🚨 **Standard Violation:** Every active deployment and service **must** possess the following standard metadata labels:
      - `app.kubernetes.io/name` (identifying the application)
      - `owner` (identifying the engineering team)
      - `environment` (identifying `dev`, `staging`, or `prod`)
    - Any resource lacking these three labels is a Non-Standard Violation.
2.  **Private Service Exposition compliance:**
    - Query: `"kubectl get services -A -o jsonpath='{.items[?(@.spec.type=="LoadBalancer")].status.loadBalancer.ingress[*].ip}'"`
    - 🚨 **Standard Violation:** No GKE Service inside a development namespace is allowed to expose a **public External LoadBalancer IP** unless it has the explicit annotation `platform.harness.io/public-exposition-approved: "true"`. Public endpoints exposed without this approval represent a High-Risk Architectural Violation.

### 3. Generate Standardization Audit Log

- List all non-standard resources and violations in a structured weekly diff report.
