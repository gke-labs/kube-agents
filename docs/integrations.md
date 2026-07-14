# Kube-Agents Integrations Guide

This document details the configuration and deployment of external integrations for Kube-Agents, specifically the LiteLLM Gateway (for model routing) and the GitHub Token Broker (Minty).

---

## LiteLLM Gateway & Model Configuration

LiteLLM is automatically deployed during the `make gcp-provision` flow. The following details how to configure models and monitor usage.

### Model Configuration & Routing

The Platform Agent is configured to request a generic model named `model-default` from LiteLLM. LiteLLM acts as a proxy and routes this request to the actual LLM provider based on its configuration.

To change the model used by the agents:

1.  **Determine the Model**: Choose the model provider and name (e.g., `gemini` and `gemini-3.5-flash` or `gemini-3.5-pro`). _Note: Only Gemini 3.1 and newer models are supported; older models are deprecated._
2.  **Redeploy LiteLLM**: Set the environment variables and run the deployment command:
    ```bash
    export MODEL_PROVIDER=gemini
    export MODEL_DEFAULT_NAME=gemini-3.5-flash # E.g., switch to Gemini 3.5 Flash
    make deploy-litellm
    ```
    This will update the LiteLLM ConfigMap and trigger a rolling restart of the LiteLLM gateway, routing all future `model-default` requests to the new model.

#### Supported Gemini Models (Examples)

- `gemini-3.1-flash` (Default - fast, lightweight)
- `gemini-3.5-flash` (Balanced performance)
- `gemini-3.5-pro` (Advanced reasoning, larger context)

### Token Usage & Monitoring

LiteLLM tracks token usage for all requests routed through it.

- **Logging**: LiteLLM prints token usage metrics (input/output tokens) to its container logs.
- **Fluent Bit Sidecar**: The Fluent Bit sidecar in the agent pods captures these logs and forwards them to Google Cloud Logging.
- **Metrics (Optional)**: If Prometheus and OpenTelemetry are enabled in your cluster, LiteLLM exports metrics that can be scraped to monitor total token consumption and costs over time.

---

## GitHub Integration (Minty)

The GitHub Token Broker (Minty) can be deployed to the Kubernetes cluster using the `kustomize` targets in the Makefile.

### Prerequisites

Before deploying the GitHub integration, ensure you have:

1.  Created the `github-app-credentials` Secret containing your GitHub App ID in the destination namespace.
2.  Completed the Workload Identity and GCP Cloud KMS setup (see `integrations/github/README.md` for details).

### Step-by-Step Deployment

```bash
# 1. Define the GCP and GitHub parameter variables:
export PROJECT_ID=your-gcp-project-id
export REGION=your-gcp-region
export CLUSTER_NAME=your-gke-cluster-name
export KMS_KEYRING=your-kms-keyring
export KMS_KEY=your-kms-key
export KMS_KEY_VERSION=your-kms-key-version
export GITHUB_ORG=your-github-org
export GITHUB_REPO=your-github-repo
export GITHUB_MINTER_KSA_NAME=kubeagents-github-minter
export GITHUB_MINTER_GSA_NAME=kubeagents-github-minter-gsa
export PLATFORM_AGENT_GSA_NAME=kubeagents-platform-agent-gsa

# 2. Deploy GitHub integration:
make deploy-github
```
