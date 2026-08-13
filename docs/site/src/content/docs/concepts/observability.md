---
title: Observability
description: OpenTelemetry traces, Prometheus metrics, and Cloud Logging routing for the Platform Agent and its inference gateway.
sidebar:
  order: 9
---

The Platform Agent (Hermes) Deployment exports OpenTelemetry traces, and LiteLLM and vLLM export both OpenTelemetry traces and Prometheus metrics to GKE Managed telemetry. Container logs go to Cloud Logging. The Platform Agent's persona also generates Cloud Console links inline in Chat replies whenever it's discussing telemetry.

## What gets exported

### Prometheus metrics

- **LiteLLM** — request latency, per-model token counts, error rates on its `/metrics` endpoint (port 8080). Scraped by GKE Managed Prometheus via the `litellm-monitoring` `PodMonitoring` shipped in the LiteLLM integration base (`k8s-operator/config/integrations/litellm/base/podmonitoring.yaml`).
- **vLLM** — per-request latency histograms, queue depth, and GPU/KV-cache stats when running local models on GPU node pools. Exposed on its own `/metrics` endpoint and scraped by GKE Managed Prometheus.
- **Hindsight** — the Chat Agent's memory store. Retrieval and reranking latency (`hindsight_operation_duration_seconds`), HTTP request counts and durations, database pool wait times, and the token spend of its own extraction and consolidation calls (`hindsight_llm_*`, which bill through LiteLLM). Served on the API's ordinary HTTP port (8888), not a separate metrics listener, and scraped via the `hindsight-monitoring` `PodMonitoring` in `k8s-operator/config/integrations/hindsight/podmonitoring.yaml`. Recall latency is dominated by the reranker, so `hindsight_operation_duration_seconds` is the signal to watch — see [`docs/designs/memory.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/memory.md) for why. The Postgres StatefulSet exports nothing; the `ankane/pgvector` image ships no exporter.

The Platform Agent (Hermes) Deployment does **not** expose a Prometheus `/metrics` endpoint — it serves only the API (`8642`) and Dashboard (`9119`) ports. Its runtime signals surface as OpenTelemetry traces (below) and `tool_call_audit` log records; pod-level CPU/memory is available through the Kubernetes metrics API (`kubectl top`). The event watcher, which runs inside the `envoy-credential-proxy` sidecar, can expose watcher metrics (`k8s_event_watcher_*`) via a `--metrics-addr` flag, but this is disabled by default in the shipping deploy.

### OpenTelemetry traces

- **LiteLLM** and **vLLM** export spans directly to the GKE OTel collector (`gke-managed-otel` namespace). That collector is the default, not a requirement — see [Deploy → Telemetry](/kube-agents/deploy/telemetry/#pointing-at-your-own-collector) for pointing the deploy at your own.
- **Hermes** exports session, tool-call, and MCP spans via the `hermes_otel` plugin, enabled in every profile config (`agents/chat/config.yaml` for the Chat Agent, `agents/platform/config.yaml` for the Platform Agent, and the `agents/cluster/config.yaml` template for the per-cluster Cluster Agents).
- Traces route to Google Cloud Trace.

### Cloud Logging

All container `stdout`/`stderr` is ingested by Cloud Logging by the GKE log agent. Cluster and pod labels flow through automatically. The Platform Agent writes its own logs to files under `/opt/data/logs/*.log`; a `fluent-bit` sidecar tails that shared volume and streams the lines to stdout so they reach Cloud Logging alongside every other container.

**Nothing in the `envoy-credential-proxy` container may log credential material.** That container is the one place holding cluster credentials, GCP tokens, and chat secrets, and everything it writes to stdout leaves the cluster through the path above. The event watcher runs there too, so the rule covers it: it logs identifiers — cluster, namespace, pod, event reason, profile directory — and never a token, a kubeconfig body, or a request header.

The exposure to watch when changing this code is **wrapped errors**, not deliberate logging. A failure from parsing a profile's `kubeconfig.yaml`, minting a token, or an API server rejecting a request can carry its input into the error string, and those inputs are credentials. When adding a log line, prefer the identifier over the value: the profile name rather than the file's contents, the cluster rather than the token, the status code rather than the response body.

## Session metadata plumbing

Every Chat message carries session context (space ID, user, thread) that flows through Hermes as OpenTelemetry span attributes and out to Cloud Trace. The `session_store` and `session_otel_bridge` plugins that do this run on the Chat Agent profile, which owns chat ingress. The trace is documented in [`docs/designs/gchat-session-metadata-data-flow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/gchat-session-metadata-data-flow.md).

## Inline Console links

`SOUL.md §5` requires the agent, whenever it's discussing telemetry, tracing, logs, or debugging, to generate clickable Cloud Console links using the active project ID. The URL templates live in [`agents/platform/docs/gcp-console-links.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/docs/gcp-console-links.md) — a shared runtime reference baked into the agent image at `/opt/defaults/docs/` — covering Logs Explorer, Trace Explorer, Metrics Explorer, and the GKE Workloads console. The agent substitutes the runtime project ID and formats the links as Markdown so they render clickable in Chat.

## Auditing the agent itself

The [`kube-agents-observability` skill](https://github.com/gke-labs/kube-agents/tree/main/agents/platform/skills/kube-agents-observability) audits the harness's own telemetry — logs, traces, metrics, API/dashboard observability of the Platform Agent. Use it when triaging "why did the agent do X?" or "why isn't the agent responding?".

## Tool-call audit

The `tool_call_audit` plugin (enabled on the Chat Agent and Platform Agent profiles; Cluster Agent profiles enable only `hermes_otel`) writes per-tool-call records for every skill invocation and MCP tool call. These flow through the standard log pipeline and are queryable in Logs Explorer.

## Where to go next

- [Deploy → Telemetry](/kube-agents/deploy/telemetry/) — install-side details on the GKE Managed OTel and Prometheus config.
- [Reference → Attribution](/kube-agents/reference/attribution/) — how a tool call ties back to the authenticated human.
