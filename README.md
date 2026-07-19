# kube-agents: The Kubernetes Agentic Harness

The k8s agentic harness will fundamentally redefine the DevOps presentation layer by replacing traditional interfaces like kubectl, gcloud, and the Google Cloud console with intelligent, autonomous agents. By replacing the static, imperative nature of the traditional Kubernetes presentation layer with an autonomous agentic harness, we transition from reactive manual management to proactive, intent-driven operations.

## Key Components

### 1. Platform Agent (`platform`)

The master custodian and agent architect configured with an architectural persona (`SOUL.md`). It manages multi-tenancy governance, RBAC boundaries, and GKE infrastructure lifecycle.

### Automated Audit Schedule

The Platform Agent autonomously runs background audit jobs on a recurring schedule to ensure continuous compliance and cluster health:

| Job ID                         | Frequency              | Target Skill / SOP                                                                             | Description & Action                                                             |
| :----------------------------- | :--------------------- | :--------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------- |
| `github-issue-resolver`        | `* * * * *` _(1m)_     | [`github-issue-resolver`](agents/platform/skills/github-issue-resolver/SKILL.md)               | Polls and triages open GitHub repository issues.                                 |
| `node-problem-detector`        | `*/15 * * * *` _(15m)_ | [`gke-node-problem-detector`](agents/platform/skills/gke-node-problem-detector/SKILL.md)       | Audits node kernel deadlocks, read-only filesystems, and OOM kills.              |
| `security-posture-audit`       | `0 8 * * *` _(Daily)_  | [`gke-security-posture-audit`](agents/platform/skills/gke-security-posture-audit/SKILL.md)     | Scans for root execution, privileged pods, and Pod Security Admission standards. |
| `policy-propagation`           | `0 * * * *` _(Hourly)_ | `policy_propagation_sop.md`                                                                    | Inspects security policies and default-deny rules across all clusters.           |
| `global-capacity-orchestrator` | `0 * * * *` _(Hourly)_ | `global_capacity_orchestrator_sop.md`                                                          | Analyzes fleet-wide regional demand and cluster capacity pools.                  |
| `blueprint-sync`               | `0 9 * * *` _(Daily)_  | `blueprint_sync_sop.md`                                                                        | Verifies GKE cluster compliance against master blueprints.                       |
| `fleet-wide-cost-analysis`     | `0 10 * * *` _(Daily)_ | [`gke-cost-analysis`](agents/platform/skills/gke-cost-analysis/SKILL.md)                       | Aggregates cost usage stats, unattached disks, and Spot VM deltas.               |
| `security-patch-orchestrator`  | `0 11 * * *` _(Daily)_ | `security_patch_orchestrator_sop.md`                                                           | Scans for GKE node vulnerabilities and plans rolling updates.                    |
| `quota-limits-checker`         | `0 12 * * *` _(Daily)_ | [`gke-quota-and-limits-checker`](agents/platform/skills/gke-quota-and-limits-checker/SKILL.md) | Audits unconstrained containers and ResourceQuota usage.                         |
| `obtainability-audit`          | `0 12 * * *` _(Daily)_ | `obtainability_audit_sop.md`                                                                   | Identifies rigid cluster allocations and generates dynamic capacity patches.     |
| `gateway-api-diagnostics`      | `0 14 * * *` _(Daily)_ | [`gke-gateway-api-diagnostics`](agents/platform/skills/gke-gateway-api-diagnostics/SKILL.md)   | Audits Gateways, HTTPRoutes, ManagedCertificates, and ingress backend health.    |
| `compliance-audit`             | `0 9 * * 0` _(Weekly)_ | `compliance_audit_sop.md`                                                                      | Deep scans for deviations from corporate security policies.                      |

---

## Harness Integration & Setup

This workspace contains agent configurations, personas, and skills that can be imported into various pattern gateways or multi-agent platforms (such as CrewAI, Microsoft AutoGen, or LangGraph).

Multi-agent platforms and orchestrators can use the [INSTALL.md](INSTALL.md) guide to set up the Platform Agent. To delegate this task to your platform, clone this repository to the workspace of the default agent of multi-agent platform and ask it:

> "Using `kube-agents/INSTALL.md` provision k8s agentic harness and create platform agent"

### 1. Declarative Registration (YAML/JSON)

For platforms or gateways that load agents declaratively, add the Platform Agent workspace path to your profile or orchestrator configuration:

```yaml
agents:
  - id: platform
    workspace: ./agents/platform
```

### 2. Imperative CLI Registration

For hosts supporting CLI-driven imports, register the Platform Agent directory from the repository root. For example (using a generic gateway CLI or reference host):

```bash
# Register platform agent
gateway-cli agents add platform --workspace ./agents/platform --non-interactive
```

For more details on routing policies, proof gates, and showcasing scenarios, see the [Kubernetes Multi-Agent Integration Guide](docs/m1-demos.md).

## Disclaimer

This is not an officially supported Google product.

This project is not eligible for the Google Open Source Software Vulnerability Rewards Program.
