# Design: Audit Logging & User Attribution for kube-agents

**Status:** Draft for review
**Priority:** P0 — addresses external concern (Waze #2) and the internal "Audit log all agents" objective

---

## TL;DR

Today we can see *that an agent acted*, but not *which human asked for it* — that link lives
only in chat history, which is not durable, queryable, or joinable to what the agent did.

We don't need to build a new system. The telemetry already exists: LiteLLM and GKE Managed
OpenTelemetry already capture LLM calls, traces, and audit logs. The only thing missing is the
**requester's identity** in those records.

The fix: **stamp the requester onto records we already produce.** The gateway already knows
who the user is, so it tags each request, and that tag rides into the existing logs and audit.
We use only things the stack already understands — the LLM `user` field, OpenTelemetry trace
context, and Kubernetes labels. No new resource, nothing new for the agent to learn.

---

## 1. Problem

We need an audit trail that links **every agent action back to the human who requested it**,
and that is **durable** and **queryable** — independent of chat history.

Chat history can't be that trail:
- **Not durable** — messages age out; spaces get deleted; history is editable.
- **Not joinable** — nothing connects a chat message to the cluster changes, PRs, or LLM
  calls the agent then makes.
- **Not queryable** — you can't ask "what did Alice cause last week?" or "who triggered this
  deletion?"

**Root cause.** A request crosses two identity domains, and nothing links them:

| | Human → Agent | Agent → LLM / Kubernetes / Cloud |
|---|---|---|
| **Who** | Google Chat email | the agent's service account |
| **Recorded in** | chat history | LiteLLM logs, K8s audit, Cloud audit |

Every action record names the *agent*, never the *human*. The fix is a single tag, set where
the human is known and carried into the records where actions are logged.

### Goals
- Attribute every agent action (LLM call, cluster change, tool run) to the requesting human.
- Keep the trail durable and queryable, independent of chat history.
- Reuse existing infrastructure; use concepts the agent already understands.

### Non-goals (this version)
- Tamper-proof, non-repudiable proof of the *human* claim — see §5. v1 trusts the gateway,
  which already authenticates the user.
- Per-action authorization / policy enforcement.
- Changing the agent runtime's internals.

---

## 2. What we already have

The expensive part — a pipeline that durably ships telemetry to a queryable store — is already
running. That's why this fix is small.

| Asset | What it does today | Why it matters |
|------|--------------------|----------------|
| **GKE Managed OpenTelemetry** | In-cluster collector exporting traces/metrics/logs to Cloud Observability | A managed telemetry pipeline we don't run or secure |
| **LiteLLM proxy** | Every agent's LLM calls route through it; it logs prompt/output/cost and exports to the collector | LLM activity is already captured — it just lacks the human's identity |
| **Per-agent service account** | The operator gives each agent its own SA | The *agent* actor is already recorded tamper-proof by K8s/Cloud audit |
| **Agent OTEL wiring** | `OTEL_SERVICE_NAME` already set on the agent Deployment | Agent traces are one config step from the collector |

The agent's own identity is already captured server-side. We only need to add the **human**
half, as a tag.

---

## 3. Solution: stamp the requester

The gateway authenticates the chat user (it already does, via the allow-list). It tags each
request with that identity, and the tag flows into the three places actions are recorded.

| Plane | Already records | We add | Using |
|------|-----------------|--------|-------|
| **LLM calls** | LiteLLM logs every call | `user` and `requested_by` on each call | the standard LLM `user` field |
| **Traces** | OTel collector, deployed | a per-request trace tagged with the requester | standard OpenTelemetry trace context |
| **Cluster / Cloud changes** | K8s + Cloud audit record the SA actor | a `requested-by` label on objects the agent creates | standard Kubernetes labels |

**How you use it.** Filter any of these by the requester's email to see everything a person
caused; or start from one action and read its tag to find who asked. The trace ID ties an LLM
call to the cluster change that followed from the same request.

### Trust model (the honest limit)
- The **agent actor** is always recorded tamper-proof by K8s/Cloud audit, server-side.
- The **human tag** is *asserted by the gateway*. The gateway is a single, trusted, audited
  choke point — far better than editable chat history, but not cryptographically tamper-proof.
  A compromised agent could mislabel an object it creates; even then the real actor and
  timestamp remain in the audit log, so the action is never *unattributable* — only its human
  tag is suspect.
- If stronger proof is ever needed, §5 is the upgrade. v1 stops here on purpose: most of the
  value for a small fraction of the effort.

---

## 4. Plan

Each step is independently shippable and useful. Ordered by return on effort.

| Step | Delivers | Work | Effort |
|------|----------|------|--------|
| **1. LLM attribution** | Every prompt/output/cost tied to a human in LiteLLM logs | Gateway sets `user` + `requested_by` on each LLM call; document the contract | **Small** (mostly config) |
| **2. Trace identity** | One trace per request, tagged with the human, across agent + LiteLLM | Operator adds the OTLP endpoint + agent identity env; gateway starts a tagged trace and propagates context; enable Managed OTel | **Small–Medium** |
| **3. Cluster tag + queries** | `requested-by` on cluster objects; a reference audit policy; a query runbook | Helper to stamp the label on created objects; reference `AuditPolicy`; document the queries | **Medium** |

Step 1 alone already attributes the most sensitive data — LLM prompts and outputs — to the
requesting human, almost entirely through configuration. Steps 2–3 extend the same tag to the
trace and cluster planes.

---

## 5. If we need stronger guarantees later

Only if gateway-asserted attribution proves insufficient (e.g. for compliance or to gate
policy). None of this is needed for v1:

- **A request record stamped at admission.** A small resource the *gateway code* creates per
  request (not the agent), stamped with the authenticated submitter's identity and made
  immutable — making the "who asked" claim tamper-evident. Creation stays in deterministic
  code, so the agent never has to understand a new concept.
- **Signed requests** — the gateway signs the requester, verified before action.
- **Long-term export** — a BigQuery / Log Analytics sink for retention and reporting.
