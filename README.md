# kube-agents: The Kubernetes Agentic Harness

The k8s agentic harness will fundamentally redefine the DevOps presentation layer by replacing traditional interfaces like kubectl, gcloud, and the Google Cloud console with intelligent, autonomous agents. By replacing the static, imperative nature of the traditional Kubernetes presentation layer with an autonomous agentic harness, we transition from reactive manual management to proactive, intent-driven operations.

## Key Components

### 1. Platform Agent (`platform`)
The master custodian and agent architect configured with an architectural persona (`SOUL.md`). It manages multi-tenancy governance, RBAC boundaries, and dynamically provisions specialized subagents (`devteam` and `operator`) at runtime to manage specific scopes.

### 2. Kubernetes Operator Agent (`operator`)
An autonomous custodian of the infrastructure configured with a calm, analytical persona (`SOUL.md`). It manages global concerns like multi-cluster balancing, capacity, upgrades, and platform security policies, while executing scheduled cron jobs (health patrols, CVE scans, log rotations, certificate scans).

### 3. Development Team Agent (`devteam`)
A production-safety coach and application workload custodian configured with a performance-driven persona (`SOUL.md`). It represents developer interests, enforcing schema validation, resource requests/limits templates, and automated NetworkPolicies, while running development-specific cron tasks (rollout watches, error rate monitors, and SLO checks).

---

## Harness Integration & Setup

This workspace contains agent configurations, personas, and skills that can be imported into various pattern gateways or multi-agent platforms (such as CrewAI, Microsoft AutoGen, or LangGraph).

Multi-agent platforms and automated orchestrators can use the [INSTALL.md](INSTALL.md) guide for automatic installation and configuration of the Platform Agent.

### 1. Declarative Registration (YAML/JSON)
For platforms or gateways that load agents declaratively, add the Platform Agent workspace path to your profile or orchestrator configuration:

```yaml
agents:
  - id: platform
    workspace: ./workspace/agents/platform
```
*Note: Specialized child agents (`operator`, `devteam`) are provisioned dynamically by the Platform Agent at runtime from internal templates (`workspace/agents/platform/templates`).*

### 2. Imperative CLI Registration
For hosts supporting CLI-driven imports, register the Platform Agent directory from the repository root. For example (using a generic gateway CLI or reference host):

```bash
# Register platform agent
gateway-cli agents add platform --workspace ./workspace/agents/platform --non-interactive
```

For more details on routing policies, proof gates, and showcasing scenarios, see the [Kubernetes Multi-Agent Integration Guide](workspace/README.md).

## Disclaimer

This is not an officially supported Google product.

This project is not eligible for the Google Open Source Software Vulnerability Rewards Program.
