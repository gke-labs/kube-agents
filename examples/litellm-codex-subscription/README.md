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

### 3. Complete the Browser Login
Watch the log output for a message similar to this:

```text
Sign in with ChatGPT using device code:
1) Visit https://auth.openai.com/codex/device
2) Enter code: XXXX-XXXX
```

1.  **Copy the URL:** Open `https://auth.openai.com/codex/device` in your web browser.
2.  **Login:** Sign in with your OpenAI account that has the active ChatGPT subscription.
3.  **Enter Code:** Input the 8-character code shown in your terminal logs.
4.  **Authorize:** Approve the request to grant LiteLLM access to your subscription.

### 4. Verification
Once authorized, LiteLLM will automatically fetch and cache the necessary tokens. You can verify the setup by sending a test chat completion request to the proxy:

```bash
kubectl run test-curl --rm -i --restart=Never --image=curlimages/curl -- \
  http://litellm.agent-system.svc.cluster.local/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "model-name", "messages": [{"role": "user", "content": "Respond with the word SUCCESS"}]}'
```

---
**Note:** Because Kubernetes pod filesystems are ephemeral, you may need to repeat the login process if the LiteLLM pod restarts, as the token cache will be lost unless you configure a Persistent Volume for `/root/.config/litellm/`.
