# CUJ 3 — Noise-filtered drift triage

## Context

Out-of-band changes to a cluster are buried in reconciliation churn. On a real cluster,
roughly 78% of mutating calls come from `system:*` controllers and another 20% from CI
service accounts, leaving about 1% that a human actually made. Argo reports
`OutOfSync: 40` with no actor and no filter, so nobody looks at it.

The Phase 0 spike established that two static filters remove ~99% of that noise, leaving
roughly seven real human changes a day on an active cluster — a volume small enough for
per-change agent judgment. This ticket implements that filter and the attribution join
behind it, and emits the result as an inject on the AutoOps pipeline.

Everything else in the drift domain waits on this. CUJs 1, 2, and 4 all consume the filtered,
attributed stream this CUJ produces.

**Read before starting:**
[`drift-detection.md`](../drift-detection.md) (design) and
[`spike-findings.md`](./spike-findings.md) (measured results, verdict GO).

## Goal

Turn raw cluster mutation activity into a small, attributed stream of real human changes,
emitted as `gitops-drift` injects on the existing pipeline. You are writing one ingestion adapter —
nothing downstream of the inject changes.

## Definition of done

- One out-of-band `kubectl` change produces **exactly one** inject within ~90s.
- One full CI deploy produces **zero** injects.
- Filter configuration is per-cluster config, not code.
- A measured noise report exists for at least one live cluster.

## Out of scope

Named explicitly, because each is a plausible-looking rabbit hole:

- **`_build_agent_query()` and the inject envelope.** Both are hardcoded k8s-event-shaped.
  Generalizing them is platform work with a separate owner.
- **Desired-state resolution and Helm/Kustomize rendering.** That is the diff work in a
  later phase, and it is unbounded.
- **Revert-or-codify judgment.** That is CUJ 1. This ticket reports what happened; it does
  not decide what to do about it.

---

## Before you start

Run `drift_attribute.sh` against a namespace, make a `kubectl patch`, and watch it land in both
halves of the output. Then make the same change as a service account and compare. You should be
able to explain why `managedFields` alone cannot separate CI from a human before writing any code —
the whole detector design rests on that.

**Placement decision to confirm with your reviewer:** the existing adapter
(`k8s-operator/cmd/k8s-event-watcher`) is Go, so the natural home is a sibling
`k8s-operator/cmd/drift-detector`. Confirm before scaffolding.

---

## T1 · Audit-log ingestion path

*Adapter — transport and parsing. Equivalent to the event watcher's informer setup.*

Log Router sink at project scope → Pub/Sub topic → pull subscription. Sink filter from the spike:

```
logName="projects/PROJECT/logs/cloudaudit.googleapis.com%2Factivity"
resource.type="k8s_cluster"
resource.labels.cluster_name="CLUSTER"
protoPayload.methodName=~"create|patch|update|delete"
```

Filter principals in the consumer, not the sink — T2 needs the unfiltered volume visible. The sink's
writer identity needs `roles/pubsub.publisher` on the topic, the consumer `roles/pubsub.subscriber`
on the subscription; pair on the IAM rather than burning days on permission errors.

Consumer is a pull loop, ack on successful parse. Extract `principalEmail`, `methodName`,
`resourceName`, `callerSuppliedUserAgent`, `timestamp` from `protoPayload`. `resourceName` arrives as
a path (`core/v1/namespaces/foo/services/web`) — decompose it into `(group, version, namespace, kind,
name)` for T3's live-object lookup. Cluster-scoped and core-group variants are the fiddly part.

**Accept:** a `kubectl patch` appears in the consumer's structured log within 90s, all five fields
populated and the path decomposed. Spike measured 30–60s ingestion delay.

---

## T2 · Principal classification and the noise profile

*Adapter — noise control. Equivalent to the event watcher's namespace rules, flapping guard, and dedup window.*

Two config-driven filters: drop `^system:` principals, then drop an automation allowlist. That
allowlist is per-cluster config, not a constant — it is the single most important tuning knob, and
getting it wrong makes every CI deploy look like drift.

```yaml
drift:
  automation_principals:
    - github-deploy-sa@PROJECT.iam.gserviceaccount.com
    - gitops-infra-sa@PROJECT.iam.gserviceaccount.com
  gitops_managers: ["argocd", "flux", "helm", "kube-controller-manager", ".*-controller$"]
```

Classify into `system` / `automation` / `human` and emit a counter per tier rather than silently
discarding — you need the counts for measurement and for debugging a bad allowlist later.
Table-driven tests over fixtures captured from the live subscription (not hand-written): a `system:`
controller, CI applying client-side, a human `kubectl patch`, a delete. Then run 24–48h against a
live cluster and report counts by tier, before and after filtering.

**Accept:** tier counts land near the spike's (~78% system, ~20% automation, ~1% human), and the
allowlist changes without a rebuild. **If the numbers are wildly off, that is a finding, not a bug** —
report it, don't tune until reality matches the document.

---

## T3 · `managedFields` attribution join

*Adapter — enrichment. No equivalent in the event watcher; drift needs a second signal to be actionable.*

For each `human` message, fetch the live object using T1's parsed components and keep `managedFields`
entries where `operation == "Update"` and the manager does not match `gitops_managers`. Direct port of
the first half of `drift_attribute.sh`, so you have known-good output to diff against.

The substantive part is flattening `fieldsV1` — nested, `f:`-prefixed keys — into dotted paths
(`spec.replicas`, `spec.template.spec.containers[0].securityContext.privileged`) readable by a human
and by the agent downstream. The lookup itself is not the work.

**Deletes have no live object**, so the join must short-circuit rather than 404 and crash the
consumer. Carry the audit record through with an explicit "object gone" marker.

**Accept:** output matches `drift_attribute.sh` for the same namespace, and deleting a NetworkPolicy
produces a clean attributed record with no live-object lookup attempted.

---

## T4 · Emit the `gitops-drift` inject

*Adapter — the pipeline boundary. Everything past this line already exists.*

Mint a session (`POST /sessions`), then `POST /sessions/{session_id}/inject`.

Match the envelope the event watcher already sends — see `injector.go`. Note that the payload is
marshalled to JSON and then **wrapped as a string** in `{"message": "<escaped JSON>"}`; it is not
posted as a nested object. Carry the principal, verb, resource, timestamp, and the attributed field
list, with `kind: gitops-drift`.

**Accept:** the ticket's definition of done — one out-of-band change produces exactly one inject
within ~90s; one full CI deploy produces zero.

---

## T5 · Daily digest *(stretch — only if T1–T4 land clean)*

*Contract 3 — judgment. The one piece of this CUJ that is not adapter work.*

A drift skill under `agents/platform/skills/` plus a minimal judgment prompt that reports the day's
real human changes in plain language. No revert-or-codify decision — that is CUJ 1. This one just
answers "what actually changed on this cluster today, and who did it."
