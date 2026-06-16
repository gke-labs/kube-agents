---
name: kube-agents-observability
description: Audit, monitor, and debug the logging, tracing, metrics, and API/dashboard observability of Platform, Operator, and DevTeam agents.
---

# Task

Audit, verify, and troubleshoot the logging, metrics, and distributed tracing observability of the Platform, Operator, and DevTeam agents.

> [!TIP]
> The provided Python scripts in the `scripts/` subdirectory are parameterized reference implementations. When troubleshooting, you can run them directly, customize their parameters, or write custom just-in-time scripts/commands to query more specific metrics, endpoints, or time ranges as required by the task context.

# Workflow

## Logging

### 1. Audit Agent Main Logs

- Verify that the main agent container is writing logs to `/opt/data/logs/*.log`.
- View the internal agent log files directly:
  ```bash
  kubectl exec <pod-name> -c <agent-container-name> -n agent-system -- tail -n 100 /opt/data/logs/agent.log
  ```

### 2. Inspect Sidecar Log Aggregator (Fluent-bit)

- Verify the `fluent-bit` sidecar container tails the log directory and streams to standard output:
  ```bash
  kubectl logs <pod-name> -c fluent-bit -n agent-system --tail=100
  ```
- Retrieve and verify the configuration of the Fluent-bit sidecar:
  ```bash
  kubectl get configmap <agent-name>-fluent-bit-config -n agent-system -o yaml
  ```
- Ensure the shared `/opt/data` volume is mounted to both the agent and Fluent-bit containers:
  ```bash
  kubectl get pod <pod-name> -n agent-system -o jsonpath='{.spec.containers[*].volumeMounts}'
  ```

### 3. Identify Active Chat Users (Auditing Interactions)

To determine which users have interacted with the system via Google Chat in the last 24 hours (or a custom window):

- Run the packaged Python helper script to automatically query and parse the GKE container logs from Google Cloud Logging:

  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/get_chat_users.py --project-id <PROJECT_ID> [--hours <HOURS>]

  ```

- Alternatively, search Cloud Logging manually (via console or gcloud CLI) for the custom GChat event format emitted by the hermes session store:
  ```bash
  gcloud logging read 'resource.type="k8s_container" "Logging incoming GChat event"' --project=<PROJECT_ID> --limit=1000 --format="json"
  ```
  Look for log lines containing the format: `Logging incoming GChat event: User=<email>, Session=<session_id>`.

## Metrics

> [!NOTE]
> LLM token and operational metrics are conditional on the LLM proxy or inference server used.
>
> - **LiteLLM**: The scripts below query custom LiteLLM metrics. See the [LiteLLM Prometheus Documentation](https://docs.litellm.ai/docs/proxy/prometheus) for a complete list of metrics.

> - **vLLM**: Exposes different Prometheus metrics (e.g., `vllm:num_requests_waiting`). See the [vLLM Metrics Documentation](https://docs.vllm.ai/en/stable/usage/metrics/) for details.
> - **Other providers**: Query names will vary based on the specific provider's exporter.

### 1. Verify Cloud Monitoring & Prometheus State

- Check that Google Cloud Managed Service for Prometheus (GMP) is running in the cluster:
  ```bash
  kubectl get pods -n gmp-system
  ```
- Verify the agent deployment has correct annotations for Prometheus scraping:
  ```bash
  kubectl get deployment <agent-deployment-name> -n agent-system -o yaml
  ```

### 2. Inspect CPU and Memory Metrics

- Query Kubernetes metrics API to verify resource usage of the agent pods:
  ```bash
  kubectl top pod -l app=<agent-name> -n agent-system
  ```

### 3. Check Token Usage (Last 24h)

- Run the python script to fetch LiteLLM total token metrics from Cloud Monitoring:
  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/check_token_usage.py --project-id <project-id>
  ```

### 4. List LiteLLM Metric Descriptors

- Run the python script to list all available metric descriptors for LiteLLM:
  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/get_metric_descriptors.py --project-id <project-id>
  ```

## Traces

> [!NOTE]
> The system relies on GKE Managed OpenTelemetry for distributed tracing.
>
> - **Harness Agents**: Emit traces natively via the `hermes_otel` plugin.
> - **LiteLLM**: Emits trace spans via its OTLP callback system.
> - **Visualization**: Exported traces are stored in Google Cloud Trace and can be searched/analyzed in the **Trace Explorer** console.

### 1. Verify OpenTelemetry (OTel) Configuration

- Ensure the `hermes_otel` plugin is enabled in `/opt/data/config.yaml` or `/opt/defaults/config.yaml`.
- Verify the exporter backend is configured to use the GKE managed collector endpoint: `http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces`

### 2. Diagnose Trace Collector Connectivity

- Test network reachability from the agent container to the OpenTelemetry collector:
  ```bash
  kubectl exec <pod-name> -c <agent-container-name> -n agent-system -- curl -i -s -o /dev/null -w "%{http_code}" -X POST http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces
  ```
- Check the agent logs for OTLP connection warnings or trace export failures:
  ```bash
  kubectl logs <pod-name> -c <agent-container-name> -n agent-system --tail=500 | grep -iE "(otel|trace|exporter|export)"
  ```

### 3. Fetch and Analyze Traces (Locating Performance Bottlenecks)

To list recent traces or analyze span latency distributions to locate performance bottlenecks (such as slow tool executions or model calls):

- Run the trace latency analyzer script:

  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/analyze_trace_latency.py --project-id <project-id> [--hours <hours>] [--limit <limit>]
  ```

  **Example Output:**

  ```text
  Retrieving the last 3 traces...
  ======================================================================
  Trace ID: 0006344377aac15d1baede1a41e88a2c
  Total Duration: 0.647 seconds | Total Spans: 3
  Breakdown of spans:
    - POST /v1/chat/completions                          :  0.646s (99.9%)
    - chat model-default                                 :  0.627s (97.0%)
    - auth /v1/chat/completions                          :  0.001s ( 0.1%)
  ```

- Alternatively, run the raw trace list script:
  ```bash
  python3 /opt/hermes/skills/kube-agents-observability/scripts/fetch_traces.py --project-id <project-id> --hours 24
  ```

## Agent Status and Health

### 1. Diagnose Agent API and Dashboard Exposure

- Verify pod running status and details:
  ```bash
  kubectl get pods -n agent-system -l app=<agent-name> -o wide
  ```
- Inspect Service configurations for the API port (`8642`) and Dashboard port (`9119`):
  ```bash
  kubectl get service platform-agent -n agent-system -o yaml
  ```
- Forward agent ports locally to test web UI or API access:
  ```bash
  kubectl port-forward svc/<agent-service-name> -n agent-system 9119:9119
  ```

### 2. Inspect Persistent Internal State & Memory

- Inspect the agent's active memory files and settings:
  ```bash
  kubectl exec <pod-name> -c <agent-container-name> -n agent-system -- ls -la /opt/data/memory/
  kubectl exec <pod-name> -c <agent-container-name> -n agent-system -- cat /opt/data/memory/heartbeat-state.json
  ```
