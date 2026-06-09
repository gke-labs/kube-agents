# LiteLLM Gemini Example

This directory contains an example of deploying a LiteLLM proxy configured to use Google's Gemini models on Kubernetes.

## Prerequisites

- A Kubernetes cluster.
- A Gemini API key.

## Setup

1.  Open `secret.yaml`.
2.  Replace `PLACEHOLDER_FOR_GEMINI_API_KEY` with your actual Gemini API key.
3.  Apply the manifests:
    ```bash
    kubectl apply -f secret.yaml
    kubectl apply -f configmap.yaml
    kubectl apply -f deployment.yaml
    kubectl apply -f service.yaml
    ```
