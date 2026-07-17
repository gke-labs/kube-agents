# LiteLLM ChatGPT Subscription Example

This directory contains an example of deploying a LiteLLM proxy configured to use a consumer ChatGPT subscription via the OAuth device flow.

## Prerequisites

- A Kubernetes cluster.
- A consumer ChatGPT Plus or Pro subscription.

## Setup

### 1. Apply the Manifests

Apply the configuration, deployment, service, and **Persistent Volume Claim** to your cluster:

```bash
kubectl apply -f configmap.yaml
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
```

### 2. Configure Monitoring and Security

Apply the NetworkPolicy and PodMonitoring configurations to the namespace:

```bash
kubectl apply -f networkpolicy.yaml
kubectl apply -f podmonitoring.yaml
```

### 3. Retrieve the Authentication Link

LiteLLM uses the OAuth Device Code flow. You must retrieve the unique authorization link and code from the pod's logs:

```bash
kubectl logs -n kubeagents-system -l app=litellm -f
```

### 4. Complete the Browser Login

Once the logs start streaming, look for a message that looks like this:

```text
Sign in with ChatGPT using device code:
1) Visit https://auth.openai.com/codex/device
2) Enter code: XXXX-XXXX
```

1.  **Open the link:** Go to [https://auth.openai.com/codex/device](https://auth.openai.com/codex/device) in your browser.
2.  **Authenticate:** Sign in with the OpenAI account that has your active ChatGPT Plus/Pro subscription.
3.  **Submit Code:** Enter the unique 8-character code displayed in your terminal logs.
4.  **Confirm:** Check `kubectl logs` in previous log to confirm login was successful.

### 5. Confirm Configuration

Verify that the ConfigMap is correctly applied and pointing to the `chatgpt/` model:

```bash
kubectl get configmap litellm-config -n kubeagents-system -o yaml
```

---

**Note:** This example includes a `PersistentVolumeClaim` (PVC) mounted to `/data/litellm/chatgpt`. This ensures that your OAuth login tokens are preserved even if the pod is evicted or restarted, so you don't have to re-authenticate every time.

## Verification

You can verify that metrics are being successfully exported by querying the endpoint directly or via Cloud Monitoring:

- Directly: Query `/metrics` on port 4000 of the LiteLLM container.
- Cloud Monitoring: Look for the metric `prometheus.googleapis.com/litellm_requests_metric_total/counter` under the `prometheus_target` resource.
