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

## Configuration

The `deployment.yaml` is configured to use the `gemma-4-e2b-it` model and the specialized Vertex AI vLLM image as described in the GKE tutorial.
