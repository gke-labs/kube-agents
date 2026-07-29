---
name: gke-observability
description: Workflows for setting up and auditing observability (logging, monitoring, tracing) on GKE.
---

# GKE Observability Skill

This skill provides workflows for ensuring your GKE cluster and workloads have adequate observability for production use.

## Workflows

### 1. Audit Cluster Observability

Check if Cloud Logging and Cloud Monitoring are enabled on the cluster.

**Command:**

```bash
gcloud container clusters describe <cluster-name> --region <region> --project <project-id> --format="json(loggingConfig, monitoringConfig)"
```

Look for `loggingService` and `monitoringService` to be set to something other than `none` (usually `logging.googleapis.com/kubernetes` and `monitoring.googleapis.com/kubernetes`).

### 2. Enable Managed Service for Prometheus

Google Cloud Managed Service for Prometheus is the recommended way to collect metrics from your applications.

**Command to enable:**

```bash
gcloud container clusters update <cluster-name> \
    --enable-managed-prometheus \
    --region <region>
```

**Verify installation:**

```bash
kubectl get pods -n gmp-system
```

### 3. Workload Logging Verification

Ensure your workloads are logging to standard output, which Cloud Logging collects automatically.

**Check workload logs:**

```bash
kubectl logs <pod-name> -n <namespace>
```

Ensure logs are in a structured format (like JSON) if possible, for easier querying.

### 4. Dashboards and Alerts

Recommend creating dashboards in Cloud Monitoring for key metrics:

- CPU Utilization
- Memory Utilization
- Request Latency
- Error Rate

Set up alerting policies for critical thresholds.

### 5. Distributed Tracing

Enable distributed tracing to track requests across microservices.

- **Action**: Recommend using **OpenTelemetry** in the application to send traces to **Cloud Trace**.
- **Benefit**: Helps identify latency bottlenecks in distributed systems.

### 6. Continuous Profiling

Use continuous profiling to analyze application performance in production with low overhead.

- **Action**: Recommend integrating the **Cloud Profiler** agent in your application code.
- **Benefit**: Helps identify CPU and memory-consuming functions in production.

### 7. Querying Logs with LQL

Use Logging Query Language (LQL) in Cloud Logging to find specific logs.

**Example LQL Queries:**

- Find error logs for a specific container:
  ```text
  resource.type="k8s_container"
  resource.labels.container_name="my-app"
  severity>=ERROR
  ```
- Find logs with a specific message:
  ```text
  resource.type="k8s_container"
  textPayload:"connection refused"
  ```

### 8. Enable Control Plane Metrics

For Standard clusters, you can enable collection of metrics from the Kubernetes API server, scheduler, and controller manager.

**Command:**

```bash
gcloud container clusters update <cluster-name> \
    --monitoring=SYSTEM,API_SERVER,SCHEDULER,CONTROLLER_MANAGER \
    --region <region>
```

### 9. Enable Dataplane V2 Observability

If using GKE Dataplane V2, you can enable advanced L4 observability.

**Command:**

```bash
gcloud container clusters update <cluster-name> \
    --enable-dataplane-v2-observability \
    --region <region>
```

This allows you to observe traffic flows and network metrics.

## Best Practices

1. **Structured Logging**: Use JSON logging in your applications to make it easier to search and analyze logs in Cloud Logging.
2. **Custom Metrics**: Use Managed Service for Prometheus to expose and collect custom application metrics.
3. **Full Pillars of Observability**: Implement Tracing and Profiling in addition to Logs and Metrics for complete visibility.
4. **Control Plane Metrics**: Enable control plane metrics (if using Standard) to monitor the health of the API server and scheduler.
