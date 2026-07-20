---
title: Observability
description: OpenTelemetry traces, Prometheus metrics, and Cloud Logging routing for the Platform Agent and its inference gateway.
sidebar:
  order: 8
---

The Platform Agent Deployment, LiteLLM, and vLLM all export OpenTelemetry traces and Prometheus metrics to GKE Managed telemetry. Container logs go to Cloud Logging. The Platform Agent's persona also generates Cloud Console links inline in Chat replies whenever it's discussing telemetry.

## What gets exported

### Prometheus metrics

- **LiteLLM** — request latency, per-model token counts, error rates. Scraped by GKE Managed Prometheus.
- **vLLM** — GPU utilization, KV cache hit rate, per-request latency histograms. Scraped by GKE Managed Prometheus.
- **Hermes runtime** — session count, tool-call rate, LLM turn latency. Exposed via the `hermes_otel` plugin.

### OpenTelemetry traces

- **LiteLLM** and **vLLM** export spans directly to the GKE OTel collector (`gke-managed-otel` namespace).
- **Hermes** exports session, tool-call, and MCP spans via the `hermes_otel` plugin (`agents/platform/config.yaml`).
- Traces route to Google Cloud Trace.

### Cloud Logging

All container `stdout`/`stderr` is ingested by Cloud Logging. Cluster and pod labels flow through automatically.

## Session metadata plumbing

Every Chat message carries session context (space ID, user, thread) that flows through Hermes as OpenTelemetry span attributes and out to Cloud Trace. The trace is documented in [`docs/gchat-session-metadata-data-flow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/gchat-session-metadata-data-flow.md).

## Inline Console links

`SOUL.md §6` requires the agent, whenever it's discussing telemetry, tracing, logs, or debugging, to generate clickable Cloud Console links using the active project ID. Templates:

| Console page                        | URL template                                                                                                                                                   |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cloud Logging (Logs Explorer)       | `https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22%0Aresource.labels.project_id%3D%22{project_id}%22?project={project_id}` |
| Cloud Trace (Trace Explorer)        | `https://console.cloud.google.com/traces/list?project={project_id}`                                                                                            |
| Cloud Monitoring (Metrics Explorer) | `https://console.cloud.google.com/monitoring/metrics-explorer?project={project_id}`                                                                            |
| GKE Workloads                       | `https://console.cloud.google.com/kubernetes/workload/overview?project={project_id}`                                                                           |

The agent substitutes the runtime project ID and formats the links as Markdown so they render clickable in Chat.

## Auditing the agent itself

The [`kube-agents-observability` skill](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/kube-agents-observability) audits the harness's own telemetry — logs, traces, metrics, API/dashboard observability of the Platform Agent. Use it when triaging "why did the agent do X?" or "why isn't the agent responding?".

## Tool-call audit

The `tool_call_audit` plugin (enabled in `config.yaml`) writes per-tool-call records for every skill invocation and MCP tool call. These flow through the standard log pipeline and are queryable in Logs Explorer.

## Where to go next

- [Deploy → Telemetry](/kube-agents/deploy/telemetry/) — install-side details on the GKE Managed OTel and Prometheus config.
- [Reference → Attribution](/kube-agents/reference/attribution/) — how a tool call ties back to the authenticated human.
