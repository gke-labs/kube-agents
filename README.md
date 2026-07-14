# kube-agents: The Kubernetes Agentic Harness

The k8s agentic harness will fundamentally redefine the DevOps presentation layer by replacing traditional interfaces like kubectl, gcloud, and the Google Cloud console with intelligent, autonomous agents. By replacing the static, imperative nature of the traditional Kubernetes presentation layer with an autonomous agentic harness, we transition from reactive manual management to proactive, intent-driven operations.

## Documentation

- [Architecture & Sizing Guide](docs/architecture_and_sizing.md): Conceptual overview, software inventory, and cluster sizing guidelines.
- [Deployment Guide](docs/deployment.md): Step-by-step instructions for bootstrapping GCP/GKE and deploying the Platform Agent.
- [Multi-Agent Integration](docs/m1-demos.md): Details on routing policies, proof gates, and showcasing scenarios.

## Key Components

### 1. Platform Agent (`platform`)

The master custodian and agent architect configured with an architectural persona (`SOUL.md`). It manages multi-tenancy governance, RBAC boundaries, and GKE infrastructure lifecycle.

---

## Harness Integration & Setup

This workspace contains agent configurations, personas, and skills that can be imported into various pattern gateways or multi-agent platforms (such as CrewAI, Microsoft AutoGen, or LangGraph).

Multi-agent platforms and orchestrators can use the [Deployment Guide](docs/deployment.md) to set up the Platform Agent. To delegate this task to your platform, clone this repository to the workspace of the default agent and ask it:

> "Using `kube-agents/docs/deployment.md` provision k8s agentic harness and create platform agent"

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
