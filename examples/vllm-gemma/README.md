# vLLM Gemma example

This example serves Gemma 4 26B-A4B through vLLM's OpenAI-compatible server (`vllm serve`, `/v1/chat/completions` with tool calls), on one GKE node with two NVIDIA L4 GPUs and a 128k-token context. kube-agents reaches it with the `openai_compatible` model provider. The installer does not deploy it: you apply it yourself.

The model is `RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic`, an FP8 checkpoint of Google's Gemma 4 26B-A4B, a mixture-of-experts model with 3.8B active parameters. The weights are under the Apache 2.0 license and are not gated, so no Hugging Face token is required.

## Prerequisites

- kube-agents installed in `kubeagents-system` on a GKE Standard cluster. A default install creates an Autopilot cluster; install with `--gke-cluster-mode=standard` to get one you can add a GPU node pool to.
- A GPU node pool for the server. The image is built on CUDA 13 and needs NVIDIA driver 580 or later. `gpu-driver-version=latest` installs it:

  ```bash
  gcloud container node-pools create gemma-l4 \
    --cluster=<CLUSTER> --location=<REGION or ZONE> --node-locations=<ZONE> \
    --machine-type=g2-standard-24 \
    --accelerator=type=nvidia-l4,count=2,gpu-driver-version=latest \
    --num-nodes=1 --disk-size=200
  ```

  `g2-standard-24` has two L4 GPUs, 24 vCPUs and 96 GB of memory. The 200 GB boot disk holds the image (about 10 GB) and the weights cache. If the pool fails with `GCE_STOCKOUT`, the zone has no GPUs free: try another zone.

## Deploy

```bash
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f networkpolicy.yaml
kubectl apply -f podmonitoring.yaml
```

On a new node, the pod is ready about 11 to 12 minutes after it is scheduled: about 4 minutes to pull the image, then 7 to 8 minutes to download the weights (about 2.5 minutes), compile the model (about 2 minutes) and load it:

```bash
kubectl -n kubeagents-system logs -f deploy/vllm-gemma-deployment
```

The Deployment uses the `Recreate` strategy: the pod takes both GPUs on its node, so a second pod cannot start beside it. Each rollout is an outage of about 8 minutes. For a rollout without an outage, give the pool a second node and change the strategy to `RollingUpdate` with `maxUnavailable: 0`.

The container drops all capabilities and cannot escalate privileges, but it runs as root: the image has no non-root user.

## Check it

```bash
kubectl -n kubeagents-system port-forward svc/llm-service 8080:80
```

In a second terminal:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "gemma-4-26b-a4b-it",
  "messages": [{"role": "user", "content": "How many pods are running in kube-system?"}],
  "tools": [{"type": "function", "function": {"name": "kubectl_get",
    "description": "Run kubectl get for a resource type in a namespace.",
    "parameters": {"type": "object", "properties": {"resource": {"type": "string"},
      "namespace": {"type": "string"}}, "required": ["resource", "namespace"]}}}]
}'
```

A working server answers with `"finish_reason": "tool_calls"` and a `kubectl_get` call.

## Connect kube-agents

Install or upgrade with the `openai_compatible` provider ([Inference gateway: OpenAI-compatible endpoints](https://gke-labs.github.io/kube-agents/concepts/inference-gateway/#openai-compatible-endpoints-self-hosted-or-private)):

| `install.env` key               | Value                                            |
| ------------------------------- | ------------------------------------------------ |
| `MODEL_PROVIDER`                | `openai_compatible`                              |
| `MODEL_DEFAULT_NAME`            | `gemma-4-26b-a4b-it` (the `--served-model-name`) |
| `OPENAI_COMPATIBLE_API_BASE`    | `http://llm-service.kubeagents-system.svc/v1`    |
| `OPENAI_COMPATIBLE_TARGET_PORT` | `8000` (the pod port, not the Service port)      |
| `MODEL_MAX_TOKENS`              | `4096` (the output budget of each request)       |

The example runs in the release namespace, so the gateway's egress rule admits every pod in `kubeagents-system` on port 8000. No other pod there listens on that port.

## Why a 128k context

The agent sends its whole context on every request. On one agent turn through the gateway against this server, the agent made 19 requests that averaged about 25,900 prompt tokens, and 15 of them were between 20,000 and 50,000. A scheduled audit's request can reach about 83,000 tokens. Each request also asks for `MODEL_MAX_TOKENS` of output, and the prompt and the output must fit the window together. A 32k window refuses most agent requests.

## Choose a size

Measured on 2026-10-16 and 2026-10-17 with vLLM 0.31.0 and `--gpu-memory-utilization=0.90`. "Tool call" is the short request from [Check it](#check-it). "27k prompt" and "83k prompt" are synthetic requests (a pod list in the system message) with one tool, the first time and again with the prefix cache.

| Model                                     | GPUs         | Context | Weights per GPU | KV cache (requests at full context) | 27k prompt, cold / cached | 83k prompt, cold / cached | Tool call (short prompt) |
| ----------------------------------------- | ------------ | ------- | --------------- | ----------------------------------- | ------------------------- | ------------------------- | ------------------------ |
| `RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic` | 2x L4 (TP 2) | 131,072 | 13.74 GiB       | 395,955 tokens (3.02)               | 7.7 s / 0.6 s             | 39.2 s / 0.7 s            | yes                      |
| `google/gemma-4-31B-it-qat-w4a16-ct`      | 2x L4 (TP 2) | 32,768  | 10.67 GiB       | 78,275 tokens (2.39)                | 31.0 s / 3.0 s            | does not fit              | yes                      |
| `google/gemma-4-12B-it` (BF16)            | 2x L4 (TP 2) | 32,768  | 11.81 GiB       | 173,189 tokens (5.29)               | 13.7 s / 1.5 s            | not measured              | yes                      |
| `google/gemma-4-31B-it-qat-w4a16-ct`      | 1x A100 40GB | 32,768  | 19.78 GiB       | 68,352 tokens (2.09)                | 17.1 s / 1.6 s            | does not fit              | yes                      |

- The default, 26B-A4B in FP8, is the size that holds a 128k window on two L4 GPUs. With the settings above, it answered an agent turn through the gateway correctly in 84 seconds.
- `google/gemma-4-31B-it-qat-w4a16-ct` scores higher for agent work on the model card (Tau2 for the instruction-tuned models: 76.9% for 31B, 69.0% for 12B, 68.2% for 26B-A4B). On 2x L4 its KV cache holds 78,275 tokens, so its window cannot reach a scheduled audit's request. Four L4 GPUs (`g2-standard-48`) would give it more room; that shape was not measured, because no zone had capacity for it at the time.
- To change the model, change `MODEL_ID`, `--served-model-name`, the `ai.gke.io/model` label and the `model` in the [Check it](#check-it) request, keep `--max-model-len` below the KV cache size the server logs at startup, and set `MODEL_DEFAULT_NAME` to the new served name.
- On one A100 40GB (`a2-highgpu-1g`), set `--tensor-parallel-size=1`, `nvidia.com/gpu: "1"`, a CPU request of at most 10 and the `nvidia-tesla-a100` accelerator label, and keep `--disk-size=200`.
- 31B in BF16 (about 62 GB of weights) does not fit 2x L4 or one A100 40GB. 31B in FP8 does not fit 2x L4 at a 32k context.
- E2B and E4B are too small for agent work (Tau2 24.5% and 42.2%).

## Notes

- The image is `vllm/vllm-openai:v0.31.0`, pinned by digest. The `-cu129` build of this release does not start: its `torchcodec` needs CUDA 13 libraries that the image does not have.
- `--limit-mm-per-prompt` turns off image and audio input. The agent sends text only, and the memory goes to the KV cache.
- Set `HF_TOKEN` in the pod if Hugging Face rate-limits the weight download.

## Metrics

- Directly: query the `/metrics` endpoint on port 8000 of the vLLM container. vLLM 0.31 names its metrics `vllm:<name>`, for example `vllm:num_requests_waiting`, `vllm:kv_cache_usage_perc` and `vllm:request_prompt_tokens`.
- Cloud Monitoring: `podmonitoring.yaml` sends them to Google Cloud Managed Service for Prometheus. Search Metrics Explorer for `num_requests_waiting` or `kv_cache_usage_perc`.
