# vLLM Gemma Example

This directory contains an example of deploying vLLM configured to serve Google's Gemma models on Kubernetes, based on the [official GKE tutorial](https://docs.cloud.google.com/kubernetes-engine/docs/tutorials/serve-gemma-gpu-vllm). It is the model-server half of a pair: [`examples/litellm-hosted-vllm/`](../litellm-hosted-vllm/) is the LiteLLM gateway that routes the agent to it, and `MODEL_PROVIDER=hosted_vllm` is the install-path equivalent.

## Prerequisites

- A Kubernetes cluster with an NVIDIA L4 node (GKE Standard: a node pool with `nvidia-l4` and the latest driver; GKE Autopilot: nothing, the `nodeSelector` is enough).
- For an online pull, a Hugging Face token with access to the gated Gemma models.

## Sizing

The Deployment serves `google/gemma-4-E4B-it` on one L4 with a 131072-token context. The context is not a tuning choice: the agent requests 65536 output tokens on every call (the Hermes `custom` provider default, and the agent's config sets no override), and vLLM rejects any request whose output budget exceeds `--max-model-len`. With the agent's roughly 20k-token prompt, anything under about 90k fails every turn with an HTTP 500. That rules out the larger Gemma 4 checkpoints on L4-class hardware: a 31B model with FP8 weights holds 32k on two L4s and no more. Any model and GPU that can hold the context works; edit `MODEL_ID` and the resources together.

## Setup

### Option A: pre-staged weights (air-gapped)

1. Copy the checkpoint onto a `PersistentVolumeClaim` (for example from `gs://vertex-model-garden-public-us/gemma4/gemma-4-E4B-it/`, which needs no token).
2. Uncomment the `model-weights` volume and its mount in `deployment.yaml`, and set `MODEL_ID` to the mount path.
3. Apply:

   ```bash
   kubectl apply -f deployment.yaml
   kubectl apply -f service.yaml
   kubectl apply -f networkpolicy.yaml
   kubectl apply -f podmonitoring.yaml
   ```

### Option B: online pull from Hugging Face

1. Create the token Secret the Deployment reads (it is optional, so option A needs none):

   ```bash
   kubectl create secret generic hf-secret \
     --namespace kubeagents-system \
     --from-literal=token="<YOUR_HF_TOKEN>"
   ```

2. Apply the same four manifests as option A.

## Verification

The Service is `llm-service` on port 80, forwarding to the server's port 8000. Readiness follows vLLM's own `/health`, which answers only once the model is resident on the GPU; the first start takes several minutes.

```bash
kubectl -n kubeagents-system port-forward svc/llm-service 8000:80 &
curl -s http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "google/gemma-4-E4B-it", "messages": [{"role": "user", "content": "Say hello."}]}'
```

Metrics: `/metrics` on port 8000 of the container, or in Cloud Monitoring under `prometheus.googleapis.com/vllm_` (for example `vllm_num_requests_waiting/gauge`).
