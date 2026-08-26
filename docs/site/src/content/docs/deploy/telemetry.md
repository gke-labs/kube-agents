---
title: Telemetry
description: Where OpenTelemetry, Prometheus, and Cloud Logging fit into the shipping deploy.
sidebar:
  order: 5
---

The shipping deploy wires the Platform Agent, LiteLLM, and vLLM into **GKE Managed telemetry** so you don't run your own OTel collector or Prometheus. Container logs go to Cloud Logging automatically. If you already run a collector, [point the deploy at it](#pointing-at-your-own-collector) instead.

For what's exported and how the agent surfaces it in Chat replies, see [Concepts → Observability](/kube-agents/concepts/observability/). This page covers deploy-side details.

## What runs where

| Signal          | Producer                        | Collector                               | Destination      |
| --------------- | ------------------------------- | --------------------------------------- | ---------------- |
| Metrics         | LiteLLM, vLLM                   | GKE Managed Prometheus                  | Cloud Monitoring |
| Traces          | LiteLLM, vLLM, Hermes           | GKE OTel collector (`gke-managed-otel`) | Cloud Trace      |
| Container logs  | All containers                  | GKE built-in log agent                  | Cloud Logging    |
| Tool-call audit | Hermes `tool_call_audit` plugin | GKE built-in log agent (via `stdout`)   | Cloud Logging    |

## GKE Managed Prometheus

Enabled at the cluster level (default on new GKE Standard clusters, opt-in on older). LiteLLM, vLLM, and the Hindsight memory API expose Prometheus `/metrics` endpoints (LiteLLM on port 8080, vLLM on port 8000, Hindsight on port 8888); managed Prometheus scrapes them via `PodMonitoring` resources shipped with each integration (the LiteLLM operator base at `k8s-operator/config/integrations/litellm/base/podmonitoring.yaml`, the Hindsight one at `k8s-operator/config/integrations/hindsight/podmonitoring.yaml`, and the vLLM example manifests under `examples/`).

## Where token spend lives

LiteLLM is the only component that knows what a request cost, and its `prometheus` callback publishes that as `litellm_spend_metric_total` — a counter labelled with the real model and provider it routed to alongside the `requested_model` alias the agent asked for:

```text
litellm_spend_metric_total{api_provider="gemini",model="gemini-3.5-flash",requested_model="model-default",…} 2.907
```

That metric is the source of truth for spend, and it is already scraped by the `PodMonitoring` above.

**Hermes' own per-session cost fields are not.** A session records `estimated_cost_usd: 0.0`, `cost_status: unknown`, and `cost_source: none` no matter how many tokens it burned. This is a consequence of routing every agent through the gateway rather than a misconfiguration: Hermes prices a turn either from a built-in table keyed by a first-party provider (`gemini`, `anthropic`, `openai`) or from pricing published by the endpoint's own `/v1/models`. Agents here are configured `provider: custom` against LiteLLM, which misses the table, and LiteLLM's `/v1/models` carries no pricing by design — it is the OpenAI-compatibility shim, and points at its own `/model/info` for pricing, which nothing probes. Naming a real model instead of the `model-default` alias does not help; the `custom` route is what misses, not the alias.

Read the Prometheus counter, not the session fields.

## OpenTelemetry

The Hermes runtime enables the `hermes_otel` plugin (enabled in every profile config — Planning Agent, Platform Agent, and the Cluster Agent template). Its trace backend is baked into the image pointing at `http://opentelemetry-collector.gke-managed-otel.svc.cluster.local:4318/v1/traces` (`deploy/docker/Dockerfile`), which forwards to Cloud Trace. That bake is a **fallback**: at container start the entrypoint rewrites the backend of every plugin config it can see — the runtime root and each existing profile — to whatever `OTEL_EXPORTER_OTLP_ENDPOINT` says, and profiles scaffolded later (the per-cluster Cluster Agents) get the same treatment when they are created. Leave the variable unset and the baked endpoint stands.

LiteLLM (via the `otel` callback and `OTEL_EXPORTER_OTLP_ENDPOINT`) and vLLM (via `--otlp-traces-endpoint`) are configured in their deployment manifests to export directly to the same collector — no per-component collector deploy.

## Pointing at your own collector

If you run your own collector — say a Service `otel-collector` in namespace `otel-collector` — you do not have to edit any manifest. The endpoint resolves per PlatformAgent at reconcile time, in this order:

| #   | Source                                             | `status.telemetry.otlpEndpointSource` |
| --- | -------------------------------------------------- | ------------------------------------- |
| 1   | `spec.deployment.env[OTEL_EXPORTER_OTLP_ENDPOINT]` | `DeploymentEnv`                       |
| 2   | `spec.telemetry.otlpEndpoint`                      | `Spec`                                |
| 3   | `OTEL_COLLECTOR_ENDPOINT` on the operator          | `OperatorEnv`                         |
| 4   | In-cluster discovery found one                     | `Discovered`                          |
| 5   | Discovery ran and this cluster has none            | `None`                                |
| 6   | The GKE managed collector                          | `Default`                             |

Rungs 1–3 suppress discovery entirely — no probe, no API calls. The result is on the resource, which is the only way to tell "discovered the managed collector" from "fell back to it":

```bash
kubectl get platformagent -A -o jsonpath='{.items[*].status.telemetry}'
```

### Discovery

The operator probes a fixed list of well-known Services by name — `gke-managed-otel/opentelemetry-collector` first, then `otel-collector/otel-collector`, `opentelemetry/opentelemetry-collector`, `opentelemetry-operator-system/otel-collector`, `observability/otel-collector`, `monitoring/otel-collector` — and if none exist, lists Services matching `app.kubernetes.io/name=opentelemetry-collector`, then `app.kubernetes.io/component=opentelemetry-collector`, then `app=opentelemetry-collector`. Multiple matches are sorted by `(namespace, name)` and the first wins; the losers are logged.

A Service qualifies on a TCP port named `otlp-http`, else `http-otlp`, else numbered `4318`. **A collector exposing only 4317 is skipped, not selected** — the agents and LiteLLM speak `http/protobuf` and `hermes_otel` POSTs to `/v1/traces`, so a gRPC-only pick would fail on every span while looking configured. Point at it explicitly if that is what you want. Discovery is structural: a Service can expose 4318 and still refuse OTLP (wrong receiver, mTLS). No TLS is inferred, so an `https://` endpoint has to be set explicitly.

Results are cached cluster-wide for 5 minutes, including the "found nothing" answer. When the answer is `None` or `Default` the operator re-probes every 15 minutes, so a collector installed after the agent still gets picked up without a restart. API errors are never cached: the last known good answer is reused — including a cached `None` — rather than flapping and rolling the pod. An install that narrows the operator's cluster-wide RBAC on `services` makes discovery return `Default` silently, and `otlpEndpointSource` is what makes that visible.

`None` and `Default` are different answers and the distinction is the point. `None` means the probe completed and there is no collector here, so the agent is given no `OTEL_EXPORTER_OTLP_ENDPOINT` at all and runs with `OTEL_SDK_DISABLED=true`; the collector egress rule is left out of its NetworkPolicy for the same reason. `Default` means nobody established that — discovery is switched off via `OTEL_COLLECTOR_DISCOVERY=false`, or no probe has completed — and the GKE managed collector is assumed, which is what installs did before discovery existed.

Wiring the managed endpoint on a cluster that does not have it is what the `None` rung exists to stop. What it stops specifically is the OpenTelemetry SDK's **metric** exporter, which reads `OTEL_EXPORTER_OTLP_ENDPOINT` directly: on a collector-less cluster it POSTs `/v1/metrics` to a name that never resolves, once per export interval, for the life of the pod, and the resulting traceback buries every real line in the gateway log. Omitting the variable on its own would not stop it — with nothing set the SDK falls back to its own default of `http://localhost:4318` and trades the DNS failure for a refused connection at the same rate — so `None` sets `OTEL_SDK_DISABLED=true` rather than merely leaving the endpoint out.

`None` does **not** repoint the `hermes_otel` plugin, which is a separate path and the one that carries agent spans. As the section above says, that plugin does not read `OTEL_EXPORTER_OTLP_ENDPOINT`; its backend is baked into the image and rewritten at start-up only when an endpoint is set, so on a `None` cluster it keeps the baked `gke-managed-otel` URL and the gateway still logs `[hermes-otel] ✓ gke-managed-otel at …` at start-up. That is cosmetic rather than noisy — the plugin exports only when there are spans and produces no retry storm — but it does mean the `✓` is not evidence that anything was verified. Tracked in [#933](https://github.com/gke-labs/kube-agents/issues/933).

Nothing here overrides configuration: an operator who sets rung 1 or 2 gets that endpoint on a collector-less cluster. Setting `OTEL_SDK_DISABLED=false` in `spec.deployment.env` re-enables the SDK, but on its own it does not give the exporter anywhere to send — pair it with rung 1 or 2 if you want a working exporter rather than a localhost one.

### Helm

Discovery only covers the agents. LiteLLM's exporter and the LiteLLM NetworkPolicy are rendered by Helm, before any reconcile has happened, so one chart value drives all three:

```yaml
telemetry:
  otlpEndpoint: "http://otel-collector.otel-collector.svc.cluster.local:4318"
  collectorNamespace: "" # only needed if the namespace can't be read off the host
```

| `telemetry.otlpEndpoint` | PlatformAgent CR              | LiteLLM env (only when `litellm.otel=true`) | NetworkPolicy egress namespace                 |
| ------------------------ | ----------------------------- | ------------------------------------------- | ---------------------------------------------- |
| `""` (default)           | field omitted                 | managed collector                           | `gke-managed-otel`                             |
| set                      | `spec.telemetry.otlpEndpoint` | the value                                   | derived from the host, or `collectorNamespace` |

Empty means "do not decide here". The operator can act on that; Helm cannot, so it keeps the shipping default. Setting the value therefore also pins the agents — a release can never have the agent discover collector A while LiteLLM exports to B and the policy opens egress only to B's namespace.

`litellm.otel` stays a separate switch, and it defaults to **off**. Naming a collector does not turn the LiteLLM otel callback on, because that callback aborts every LLM request on DNS failure — too severe to flip as a side effect. So on a default install `telemetry.otlpEndpoint` moves the agents and the egress rule; the LiteLLM `OTEL_EXPORTER_OTLP_ENDPOINT` variable does not exist until you also set `litellm.otel=true`.

The namespace is read off the endpoint host when it names an in-cluster Service (`<svc>.<ns>` or `<svc>.<ns>.svc…`). A vendor endpoint or a bare hostname has no namespace to read, and what happens then depends on that same switch: with `litellm.otel=true` the render **fails**, rather than emitting a policy that blocks the export you just configured — set `telemetry.collectorNamespace`, or `litellm.networkPolicy=false` if the policy is managed elsewhere. With the callback off there is no LiteLLM export for the rule to block, so the rule keeps `gke-managed-otel` and the install proceeds.

### Kustomize and examples

The kustomize LiteLLM base stays on the managed collector; to point it elsewhere, install through the chart and set `telemetry.otlpEndpoint`, which moves the exporter env and the egress `namespaceSelector` together. The vLLM manifests under `examples/` still carry the managed endpoint literally — edit `--otlp-traces-endpoint` there if you redirect the rest.

## Cloud Logging

Container `stdout`/`stderr` is ingested automatically by the GKE log agent. Pod, namespace, and cluster labels are attached; you can query per-pod in [Logs Explorer](https://console.cloud.google.com/logs/query).

## Session metadata

Chat session context (space ID, user, thread) flows through Hermes as OTel span attributes. Trace lookup by session ID works out of the box. Full data flow: [`docs/designs/gchat-session-metadata-data-flow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/gchat-session-metadata-data-flow.md).

## Console links

The persona ([`SOUL.md §5`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md)) surfaces direct Cloud Console URLs in Chat replies. Templates are documented on [Concepts → Observability](/kube-agents/concepts/observability/#inline-console-links).

## Non-GKE clusters

The current wiring assumes GKE Managed OTel and Prometheus. On other Kubernetes distributions:

- Deploy an OTel collector. You do not have to reconfigure the `hermes_otel` plugin by hand — see [Pointing at your own collector](#pointing-at-your-own-collector); a collector at one of the well-known names is picked up automatically, and anything else is one chart value.
- Deploy Prometheus (kube-prometheus-stack works) and add scrape jobs for LiteLLM and vLLM.
- Configure a log-forwarding agent (Fluent Bit, Vector) to your log backend.

The Hermes runtime and integrations are collector-agnostic; the shipping _config_ is GKE-specific.
