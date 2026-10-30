# The Credential Proxy Runs as One Pod

> **STATUS — design of record; implemented.** The credential proxy (the broker) runs
> as one pod, `Replicas: 1` with the `Recreate` strategy. This document records why it is not
> scaled horizontally today, what the load measurement behind that decision is, and what would
> make it change.
>
> **Proposed successor (draft, not decided):**
> [`credential-broker-exec-tier.md`](credential-broker-exec-tier.md). This decision stands until
> that design is reviewed.

**Scope:** whether the broker (`agents/platform/scripts/credential_proxy.py`, deployed by
`buildCredentialProxyDeployment` in `k8s-operator/internal/controller/credential_proxy_manifests.go`)
runs wider than one pod.
**Owns:** the replica decision, the per-pod state that blocks more replicas, and the conditions
that reopen the question.
**Does not own:** the child memory budget and the slot cap, which set how much one pod admits
([`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md)); the
container's requests and limits, which an install raises through
`spec.deployment.credentialProxy.resources`.

## 1. Decision

One pod. Concurrency is bought vertically: a larger memory limit (on Autopilot without bursting,
which clamps the limit to the request, a larger request) raises the number of requests the child memory budget
admits, up to the slot cap. More replicas behind the Service would break
the routes that keep state in the pod (§2), and the load that motivated the question now fits
one pod (§4). The single point of failure is the same one the agent pod already is.

## 2. State that lives in the pod

The exec route (brokered `kubectl`, `gcloud` and the Cloud API relay) needs nothing beyond
caches: the managed kubeconfigs and the forge refresh coalesce cache are per process, so more pods
would fill them more than once, and every cold path would run its `gcloud` more than once. The
other routes keep state in the process or in the pod's emptyDir:

- The content workspace. Handles live under `<state-dir>/workspace` in the pod's own
  emptyDir; `CREDENTIAL_PROXY_WORKSPACE_ROOT` is deliberately unset, because the sandbox's data
  claim is `ReadWriteOnce` and a separate pod cannot mount it. An `open` on one pod followed by
  a `commit` on another fails with `NoSuchHandle`.
- The Google Chat relay. A pull keeps its `receipt → ack_id` map in the process that pulled.
- The Slack relay. Each pod opens its own Socket Mode connection, and Slack spreads events
  across the connections. The relay acknowledges an event to Slack when it arrives and holds it
  in an in-process queue, and a pull keeps its `receipt → event` map in the process that pulled.
  A settle that reaches the other pod fails, so a requeue after a failed turn loses the event.

The drift detector is not in this list: it runs in the gateway pod, and the broker's watcher route
only forwards its calls and caches the endpoints. The event watcher's dedup snapshots would use the
same workspace root, but the watcher does not run in the broker role.
[`agent-shell-sandboxing.md`](agent-shell-sandboxing.md) ("One replica") gave the Slack relay as
the first reason for one replica; this document owns the decision now.

The `Recreate` strategy follows from the same state: two pods behind the Service during a
rollout split traffic that assumes one pod.

## 3. Measured: two replicas behind today's Service

A spike on 2026-10-08 on a Standard GKE install (image `main-d817b3b2`) scaled the broker to
two replicas, with the operator scaled to zero so it would not restore `Replicas: 1`:

- Exec route: works. 24 brokered `kubectl` and `gcloud` calls from the sandbox all
  succeeded. Over the window, the shell principal's exec requests (other agents' traffic
  included) split 69 and 51 between the pods, with no non-zero exits and no refusals. The shim opens one connection per call, and kube-proxy balances per connection.
- Google Chat relay: the ack crosses pods. The gateway's ack opens a new connection, so it
  reached the pod that did not pull about half the time and got `404 {"settled": false}`. Each
  turn still completed once in the run, but two missed acks in a row would pass the
  subscription's 60-second ack deadline, and the turn would run twice.
- Content workspace: breaks by construction (§2). Not run live: the install had no forge.

A bare replica count is therefore not shippable: it brings the ack failures to every install
with chat enabled and the workspace break to every install with a forge.

## 4. The load this is sized against

The question came from a 143-cluster install where the single pod sat at its CPU limit during
fleet sweeps and was OOM-killed. Since then:

- The child memory budget stops the OOM. On Standard the default limit is 2Gi: the budget
  admits nine requests, and the slot cap holds it to eight. On Autopilot without bursting the limit
  is clamped to the 1Gi request, and the budget admits four.
- The Controller Stall Watch reuses kubeconfigs across ticks, which removed about 290
  `get-credentials` calls per tick; each cluster's credentials are now fetched about once a day.
- The reconciler's and the stall watch's listing pools are four wide, sized to the four requests
  the broker admitted at the old 1Gi limit. At 2Gi the two pools listing together can hold all
  eight slots for the length of a listing.

On the eval install's presubmit runs since 2026-10-09, the budget never binds; the waits are for
the slot cap at 6 s median and 20 s at most, with no refusals; and the first run after the
proxy's memory request was raised to 1Gi had no waits in 783 commands.

## 5. Shapes considered

1. Split the exec route into its own tier. The exec route behind its own Deployment and
   Service, with a replica count on the CR, while the relays and the content workspace stay on
   one pod. (A variant keeps one Deployment and has Envoy route the stateful verbs to one stable
   pod.) The load is all on the exec route, and that route already works
   across pods (§3). The cost is a second footprint entry, a second NetworkPolicy target, and
   the shim learning a second address. This is the shape to build if the question reopens (§6).
2. Sticky routing by workspace or receipt. One Deployment with N replicas and the calls
   that need state routed to the same pod. Rejected: neither the shim nor the gateway's chat
   client has stickiness today, a pod restart still loses the workspaces it held, and the vcs
   verbs that name no workspace still need an answer.
3. A bare replica count. Rejected by the measurement in §3.
4. Stay on one pod and reduce the load. Chosen (§1, §4).

## 6. When to revisit

Reopen the question when either holds:

- Slot waits return to the saturated shape: median over 10 s, with refusals.
- A fleet install saturates one pod's CPU after the load reductions in §4.

Then build shape 1, sized against the exec route's queue-wait histogram.

## 7. Vertical Pod Autoscaler

Not used. The child memory budget design covers it
([`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md), §3): it
learns from OOM kills on a burst that lasts seconds, it is a cluster addon an install cannot
assume, and it rewrites the requests the chart's footprint and quota preflight sum. With the
budget in place it is safe only as a throughput knob.
