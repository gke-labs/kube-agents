# User Attribution: contract & queries

How an agent action is linked back to the human who requested it. Design rationale is in
[designs/audit-logging-user-attribution.md](designs/audit-logging-user-attribution.md); this
page is the operational contract and the query runbook.

The gateway authenticates the requesting user (Google Chat, via the allow-list) and stamps
that identity onto the records we already collect. There are three planes, joined by the
requester's email and the OpenTelemetry trace ID.

## The contract

| Plane | Carrier | Set by | Status |
|------|---------|--------|--------|
| **LLM calls** | OpenAI `user` field + `metadata.requested_by` on each request to LiteLLM | gateway / runtime | runtime (follow-up) |
| **Traces** | a per-request OTel trace with `enduser.id=<email>` and a `hermes.session_id` attribute; W3C context propagated downstream | gateway / runtime | runtime (follow-up) |
| **Cluster / Cloud changes** | `kubeagents.x-k8s.io/requested-by: <email>` label on objects the agent creates | gateway / runtime | runtime (follow-up) |

What the operator provides today (this repo):

- Agent containers are wired to the GKE Managed OpenTelemetry collector
  (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL`) and carry identifying
  resource attributes (`OTEL_RESOURCE_ATTRIBUTES`: `kubeagents.agent_type`,
  `kubeagents.agent_name`, namespace). Defaults are overridable via `spec.deployment.env`.
- A reference API-server audit policy: [`k8s-operator/config/audit/audit-policy.yaml`](../k8s-operator/config/audit/audit-policy.yaml).

> **Label/annotation key:** `kubeagents.x-k8s.io/requested-by`. Value is the requester's
> email. Optional companion `kubeagents.x-k8s.io/request-id` carries the trace ID for a direct
> jump to Cloud Trace.

## Trust boundary

- The **agent actor** (its ServiceAccount / GCP SA) is recorded tamper-proof, server-side, by
  the Kubernetes API audit log and Cloud Audit Logs — independent of any label.
- The **requester** is *asserted by the gateway*. A single trusted, audited choke point — far
  better than chat history, but not cryptographically tamper-proof. If a stronger guarantee is
  needed later, see the design doc's "stronger guarantees" section.

## Query runbook

Replace `alice@example.com` and the project/cluster as needed.

**Every LLM call a person made (Cloud Logging):**
```
resource.type="k8s_container"
labels."k8s-pod/app"="litellm"
jsonPayload.metadata.requested_by="alice@example.com"
```

**Every trace for a person (Cloud Trace filter):**
```
enduser.id:"alice@example.com"
```

**Every cluster object created for a person:**
```
kubectl get all,configmap,rolebinding -A \
  -l kubeagents.x-k8s.io/requested-by=alice@example.com
```

**Who caused a specific change (from a K8s audit entry):** read the object's
`kubeagents.x-k8s.io/requested-by` label; the audit entry's `user.username` is the agent
ServiceAccount that performed it.

**From an LLM call to the cluster change it caused:** take the `trace_id` on the LLM span and
filter Cloud Trace / the `request-id` label by it.

**From a trace to the Hermes session logs (Cloud Logging):** take the `hermes.session_id`
attribute on the trace and match it against the Hermes logs shipped via Fluent Bit:
```
jsonPayload.session_id="<hermes.session_id>"
```
