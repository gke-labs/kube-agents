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

4.  Apply NetworkPolicy and configure Prometheus monitoring:
    ```bash
    kubectl apply -f networkpolicy.yaml
    kubectl apply -f podmonitoring.yaml
    ```

## Verification

You can verify that metrics are being successfully exported by querying the endpoint directly or via Cloud Monitoring:

- Directly: Query `/metrics` on port 4000 of the LiteLLM container.
- Cloud Monitoring: Look for the metric `prometheus.googleapis.com/litellm_requests_metric_total/counter` under the `prometheus_target` resource.
