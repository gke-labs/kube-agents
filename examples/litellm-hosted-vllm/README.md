# LiteLLM Hosted vLLM Example

This directory contains an example of deploying a LiteLLM proxy configured to route to a vLLM server running in the same cluster, through LiteLLM's `hosted_vllm` provider. No API key is involved.

## Prerequisites

- A Kubernetes cluster. The gateway itself needs no GPU; the server it points at does.
- A vLLM server reachable as `llm-service` in `kubeagents-system`. [`examples/vllm-gemma/`](../vllm-gemma/) deploys one serving `google/gemma-4-E4B-it`; to use another server or model, change `HOSTED_VLLM_API_BASE` in `deployment.yaml` (keep the `/v1` path), the model line in `configmap.yaml`, and the egress rule in `networkpolicy.yaml`: its port is the server pod's port and its namespace selector is the server's namespace.

## Setup

1.  Apply the manifests:

    ```bash
    kubectl apply -f configmap.yaml
    kubectl apply -f deployment.yaml
    kubectl apply -f service.yaml
    ```

2.  Apply NetworkPolicy and configure Prometheus monitoring:
    ```bash
    kubectl apply -f networkpolicy.yaml
    kubectl apply -f podmonitoring.yaml
    ```

## Verification

Send one request through the gateway and confirm it reaches the server:

```bash
kubectl -n kubeagents-system port-forward svc/litellm 8080:80 &
curl -s http://localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "model-default", "messages": [{"role": "user", "content": "Say hello."}]}'
```

Metrics are exported the same way as the other LiteLLM examples: `/metrics` on port 8080 of the container, or `prometheus.googleapis.com/litellm_requests_metric_total/counter` in Cloud Monitoring.

## When to use

Air-gapped or policy-restricted clusters whose prompts must not leave the cluster, and any hand-applied setup where the install did not select `MODEL_PROVIDER=hosted_vllm`.
