# LiteLLM ChatGPT Subscription Example

This directory contains an example of deploying a LiteLLM proxy configured to use a consumer ChatGPT subscription (ChatGPT Plus/Pro) via the OAuth device flow. This is particularly useful if you do not have a separate OpenAI Developer API key but want to leverage your $20/month consumer subscription for your agents.

## Prerequisites

- A Kubernetes cluster.
- A consumer ChatGPT Plus or Pro subscription.

## Setup

### 1. Apply the Manifests
Apply the configuration, deployment, and service to your cluster:

```bash
kubectl apply -f configmap.yaml
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
```

### 2. Retrieve the Authentication Link
LiteLLM uses the OAuth Device Code flow. You must retrieve the unique authorization link and code from the pod's logs:

```bash
kubectl logs -n agent-system -l app=litellm -f
```

### 3. Confirm Configuration
Verify that the ConfigMap is correctly applied and pointing to the `chatgpt/` model:

```bash
kubectl get configmap litellm-config -n agent-system -o yaml
```

---
**Note:** Because Kubernetes pod filesystems are ephemeral, you may need to repeat the login process if the LiteLLM pod restarts, as the token cache will be lost unless you configure a Persistent Volume for `/root/.config/litellm/`.
