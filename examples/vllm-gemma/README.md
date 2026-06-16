# vLLM Gemma Example

This directory contains an example of deploying vLLM configured to serve Google's Gemma models on Kubernetes, based on the [official GKE tutorial](https://docs.cloud.google.com/kubernetes-engine/docs/tutorials/serve-gemma-gpu-vllm).

## Prerequisites

- A Kubernetes cluster with GPU nodes (e.g., NVIDIA L4 or RTX Pro 6000).

## Setup

1.  Apply the manifests:

    ```bash
    kubectl apply -f deployment.yaml
    kubectl apply -f service.yaml
    ```

2.  Apply NetworkPolicy and configure Prometheus monitoring:
    ```bash
    kubectl apply -f networkpolicy.yaml
    kubectl apply -f podmonitoring.yaml
    ```

## Configuration

The `deployment.yaml` is configured to use the `gemma-4-e2b-it` model and the specialized Vertex AI vLLM image as described in the GKE tutorial.

## Verification

You can verify that metrics are being successfully exported and collected:

- Directly: Query the `/metrics` endpoint on port 8000 of the vLLM container.
- Cloud Monitoring: Search for vLLM metrics prefixed with `prometheus.googleapis.com/vllm_` (e.g. `prometheus.googleapis.com/vllm_num_requests_waiting/gauge` or `prometheus.googleapis.com/vllm_gpu_cache_usage_factor/gauge`).
