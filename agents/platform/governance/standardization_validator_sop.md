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
3.  **Immutable Image Tag Compliance:**
    - Query: `"kubectl get deployments,statefulsets,daemonsets -A -o json"`
    - 🚨 **Standard Violation:** In any non-development namespace (`staging-*`, `prod-*`) — and in `kubeagents-system` on production clusters — container images **must not** use `:latest`, empty tags, or bare commit-SHA tags (immutable, but not comparable or auditable as releases). Any such workload running without a valid SemVer tag (`vMAJOR.MINOR.PATCH`) is flagged as a **High-Risk Architectural Violation**.
    - Cluster classification for the `kubeagents-system` check: a cluster is **production** when it contains at least one `prod-*` namespace; every other cluster is dev/test, where `kubeagents-system` is exempt — this includes the RC pipeline's dedicated validation project, whose commit-SHA deploys land in that namespace. Note that the shipped dev-install defaults (`kustomization.yaml` `newTag: latest`; the interactive scripts default `IMAGE_TAG` to `latest`) intentionally produce `latest` in development installs; production installs must override them.

### 3. Generate Standardization Audit Log

- List all non-standard resources and violations in a structured weekly diff report.
