# Kube-Agents Deployment & Operations

Welcome to the deployment and operations documentation for Kube-Agents. This guide is split into focused topics to help you design, install, and configure your agentic harness on Google Kubernetes Engine (GKE).

---

## Documentation Index

### 1. [Architecture & Sizing Guide](architecture_and_sizing.md)

Before deploying, understand the system components and choose the right cluster size:

- **Conceptual Overview**: How the harness connects to your cluster.
- **Software Inventory**: What components (Operator, LiteLLM, Minty, etc.) are installed.
- **Topology Options**: Recommendations for shared vs. dedicated clusters.
- **Sizing Table**: Resource recommendations (Small, Medium, Large) based on fleet size.

### 2. [Installation Guide](installation.md)

Step-by-step instructions to get your environment running:

- **API Key Setup**: How to get and configure a Gemini API key.
- **Bootstrapping**: Running the automated provisioning pipeline (`provision.sh` or `make gcp-provision`).
- **Teardown**: Safely deleting cloud resources.

### 3. [Integrations Guide](integrations.md)

Configure advanced capabilities and model settings:

- **LiteLLM Configuration**: How to switch models (e.g., to Gemini 3.5) and configure routing.
- **Token Usage**: Monitoring LLM usage and logs.
- **GitHub Token Broker**: Deploying and configuring the GitHub integration (Minty) for GitOps workflows.

### 4. [Security & IAM Guide](security_and_iam.md)

Understand the security model and configure restricted access:

- **Identity Model**: How Workload Identity connects GKE and GCP.
- **Default Permissions**: Reviewing the default administrative privileges.
- **Read-Only Mode**: Step-by-step instructions to configure a strict auditing-only setup.
- **GitOps Path**: Using GitOps to perform write actions securely.

### 5. [Frequently Asked Questions (FAQ)](faq.md)

Common questions regarding safety, security, and operations:

- **What is installed**: Inventory of cluster changes.
- **Approval & Controls**: How to prevent unauthorized changes.
- **Safety Guardrails**: Runaway loops, token usage limits, and resource quotas.

---

## Recommended Path

1.  Start by reading the [Architecture & Sizing Guide](architecture_and_sizing.md) to plan your deployment.
2.  Review the [FAQ](faq.md) to understand safety guardrails and changes.
3.  Acquire your API keys as detailed in [Installation Guide: API Key Configuration](installation.md#prerequisites--api-key-configuration).
4.  Choose your security posture (Default Admin vs. [Read-Only Mode](security_and_iam.md#configuring-read-only-auditing-mode)).
5.  Run the provisioner following the steps in [Installation Guide: Bootstrapping](installation.md#bootstrapping-gcp--gke-infrastructure).
6.  Configure your models and integrations using the [Integrations Guide](integrations.md).
