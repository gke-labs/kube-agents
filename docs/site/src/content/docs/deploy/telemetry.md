---
title: Telemetry
description: Where OpenTelemetry, Prometheus, and Cloud Logging fit into the shipping deploy.
sidebar:
  order: 4
---

The shipping deploy wires the Platform Agent, LiteLLM, and vLLM into **GKE Managed telemetry** so you don't run your own OTel collector or Prometheus. Container logs go to Cloud Logging automatically.

For what's exported and how the agent surfaces it in Chat replies, see [Concepts → Observability](/kube-agents/concepts/observability/). This page covers deploy-side details.

## What runs where

| Signal          | Producer                        | Collector                               | Destination      |
| --------------- | ------------------------------- | --------------------------------------- | ---------------- |
| Metrics         | LiteLLM, vLLM                   | GKE Managed Prometheus                  | Cloud Monitoring |
| Traces          | LiteLLM, vLLM, Hermes           | GKE OTel collector (`gke-managed-otel`) | Cloud Trace      |
| Container logs  | All containers                  | GKE built-in log agent                  | Cloud Logging    |
| Tool-call audit | Hermes `tool_call_audit` plugin | GKE built-in log agent (via `stdout`)   | Cloud Logging    |

## GKE Managed Prometheus

Enabled at the cluster level (default on new GKE Standard clusters, opt-in on older). LiteLLM and vLLM expose Prometheus `/metrics` endpoints (LiteLLM on port 4000, vLLM on port 8000); managed Prometheus scrapes them via `PodMonitoring` resources shipped with each integration (the LiteLLM operator base at `k8s-operator/config/integrations/litellm/base/podmonitoring.yaml` and the vLLM example manifests under `examples/`).

## OpenTelemetry

The Hermes runtime enables the `hermes_otel` plugin (`agents/platform/config.yaml`). Its trace backend is baked into the image, pointing spans at `http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces` (`deploy/docker/Dockerfile`), which forwards to Cloud Trace.

LiteLLM (via the `otel` callback and `OTEL_EXPORTER_OTLP_ENDPOINT`) and vLLM (via `--otlp-traces-endpoint`) are configured in their deployment manifests to export directly to the same collector — no per-component collector deploy.

## Cloud Logging

Container `stdout`/`stderr` is ingested automatically by the GKE log agent. Pod, namespace, and cluster labels are attached; you can query per-pod in [Logs Explorer](https://console.cloud.google.com/logs/query).

## Session metadata

Chat session context (space ID, user, thread) flows through Hermes as OTel span attributes. Trace lookup by session ID works out of the box. Full data flow: [`docs/gchat-session-metadata-data-flow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/gchat-session-metadata-data-flow.md).

## Console links

The persona ([`SOUL.md §6`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md)) surfaces direct Cloud Console URLs in Chat replies. Templates are documented on [Concepts → Observability](/kube-agents/concepts/observability/#inline-console-links).

## Non-GKE clusters

The current wiring assumes GKE Managed OTel and Prometheus. On other Kubernetes distributions:

- Deploy an OTel collector and reconfigure `hermes_otel` plugin destination.
- Deploy Prometheus (kube-prometheus-stack works) and add scrape jobs for LiteLLM and vLLM.
- Configure a log-forwarding agent (Fluent Bit, Vector) to your log backend.

The Hermes runtime and integrations are collector-agnostic; the shipping _config_ is GKE-specific.
