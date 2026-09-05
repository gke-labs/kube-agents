# Phase 0 findings — drift attribution spike (Spike A)

**Date:** 2026-07-28
**Environment:** cluster `platform-agent-host` (us-east4), project `kube-agents-autopush`, GKE 1.35
**Run by:** jayantid@google.com
**Artifacts:** [`drift_attribute.sh`](./drift_attribute.sh) (reusable join), [`baseline-manifests.yaml`](./baseline-manifests.yaml)

## Verdict: GO — build as designed (two-signal model)

Attribution works. `managedFields` cleanly separates a GitOps owner from a human out-of-band
change, and audit logs supply who/when (and catch deletes that `managedFields` can't). The one
real challenge is audit-log noise, and it is trivially filtered by principal.

## Environment note (important for the eng)

- **No in-cluster GitOps controller** on this cluster (no Argo / Flux / Config Sync pods or CRDs).
  Existing workloads are deployed via **Helm using server-side apply** (`manager: helm, operation: Apply`),
  so real SSA managers do exist here.
- For the spike I **simulated the GitOps owner** with `kubectl apply --server-side --field-manager=argocd-controller`,
  which produces the exact `managedFields` entry Argo/Flux SSA would (`operation: Apply`).
- To validate on a true GitOps setup, re-run on a cluster where Argo/Flux is actually syncing.

## Spike A results

### 1. Attribution works — `managedFields` separates GitOps from human

| Field changed | Method used | `managedFields` owner | operation |
|---|---|---|---|
| Service `spec.type` (open) | `kubectl patch` | `kubectl-patch` | Update |
| Deployment `securityContext` (privileged) | `kubectl patch` | `kubectl-patch` | Update |
| Deployment `replicas` (hotfix scale) | `kubectl scale` | `kubectl` | Update |
| (baseline, all fields) | SSA `--field-manager=argocd-controller` | `argocd-controller` | **Apply** |

**Discriminator:** GitOps owner = `operation: Apply` under a stable manager; human out-of-band =
`operation: Update` under a `kubectl*` manager. The `operation` field + manager-name prefix is robust.

### 2. `managedFields` alone is NOT sufficient — deletes need audit logs

Deleting the NetworkPolicy left **no `managedFields`** (object is gone). The delete appeared
**only in the audit log**. Confirms the two-signal design: `managedFields` = "what field / who owns
it" on live objects; audit log = "who/when + deletes".

### 3. Manager-name stability matrix (clean, distinguishable)

| Method | manager | operation |
|---|---|---|
| `apply --server-side --field-manager=X` | `X` (e.g. argocd-controller, helm) | **Apply** |
| `kubectl patch` | `kubectl-patch` | Update |
| `kubectl scale` | `kubectl` | Update |
| `kubectl apply` (client-side) | `kubectl-client-side-apply` | Update |
| `kubectl annotate` | `kubectl-annotate` | Update |

### 4. Audit log NOISE is the real work (preview of Spike B)

A single `scale 1 -> 5` triggered a **storm** of controller events (endpoint-controller,
endpointslice-controller, replicaset-controller, kube-scheduler bindings, node pod-deletes). With
`--limit 20` newest-first, the human change was buried under reconciliation churn.

**Filter that fixes it:** exclude `principalEmail =~ "^system:"`. Doing so isolates the human
out-of-band change instantly (all showed `jayantid@google.com`, UA `kubectl/v1.35.6`).

### 5. Caveat — real GitOps principal is even cleaner

My simulated GitOps apply showed principal `jayantid@google.com` in the audit log (I ran it
myself). A real GitOps controller runs as a distinct service account
(`system:serviceaccount:argocd:...`), so the audit-log discriminator is *stronger* in production:
GitOps SA vs human user vs `system:` controllers are three separable buckets.

### 6. GKE audit-log facts confirmed

- Admin Activity logs are **on by default**, queryable directly (no sink needed to prototype).
- Ingestion delay observed ~30–60s.
- Each entry carries `principalEmail`, `methodName`, `resourceName`, `timestamp`,
  `callerSuppliedUserAgent` — everything attribution needs.

## Implications for Phase 1 (detection + attribution)

- **Detection trigger:** an audit-log subscription filtered to `NOT principal =~ ^system:` + mutating
  verbs is a high-signal trigger (reuse the `pubsub-platform` adapter). On each hit, read the live
  object's `managedFields` for field-level attribution.
- **Noise filter (CUJ #3) is two-layer:**
  1. audit side — drop `system:*` principals;
  2. `managedFields` side — keep `operation==Update` fields whose manager is NOT in the
     GitOps/controller allowlist (`argocd|flux|helm|kube-controller-manager|*-controller`).
- **Config:** the GitOps owner identity (manager name + SA) must be configurable per cluster
  (here it's `helm`/SSA; customer clusters will be argocd/flux).
- **Deletes:** handle as audit-log-only events — no object to read.

## Reusable join script

`./drift_attribute.sh <namespace> <project> <cluster> [freshness]` prints, for a namespace:
non-GitOps-owned fields (from `managedFields`) + non-system mutations (from audit logs). Verified
working against the spike namespace.

---

# Spike B — Noise profile

**Method:** no wait needed. The audit log already holds history, so signal-to-noise was measured
**retroactively over 7 days** of real cluster activity, plus a `managedFields` manager census.

## Verdict: noise is very high but trivially filterable (~99% reduction with 2 static rules)

## Signal-to-noise, 7 days of mutating calls

| Tier | Principal class | Events (7d) | Share | Action |
|---|---|---|---|---|
| 1 | `system:*` controllers | **≥4,707** | ~78% | **Drop** (prefix match) |
| 2 | CI / automation service accounts | **1,222** | ~20% | **Drop** (the desired-state applier) |
| 3 | **Real humans** (`@google.com`) | **63** | **~1%** | **THE SIGNAL** |

Tier 1 is dominated by heartbeat/reconciliation churn: `cloud-controller-manager` (421),
`pdcsi-controller` (337), `cluster-autoscaler` (278), `filestorecsi-controller` (252),
`gke-common-webhooks` (237), plus ~15 more controllers at ~210 each.

Tier 2 breakdown: `github-deploy-sa` **1,141**, `gitops-infra-sa` 43, `container-engine-robot` 32,
`kubeagents-platform-gsa` 3, `kubelet-nodepool-bootstrap` 3.

Tier 3 breakdown: three individual engineers, at 42, 18 (14 of which were this spike), and 3 changes.
**Organic human changes ≈ 49 in 7 days — roughly 7/day on an active cluster.** Very tractable
for an agent to triage.

## THE KEY FINDING: `managedFields` alone cannot separate CI from humans

On this cluster the CI service account (`github-deploy-sa`) applies **client-side**, so it records
the generic manager **`kubectl-client-side-apply`** — the *exact same manager string a human
running `kubectl apply` would produce*.

Verified by overlap: `cert-manager/cert-manager`, `kubeagents-system/litellm-config-*`, and
`kubeagents-system/github-token-minter-config` all show `kubectl-client-side-apply` in
`managedFields` **and** appear as `github-deploy-sa` targets in the audit log.

**Implication:** the audit-log principal is **required**, not optional. `managedFields` gives
field-level "what changed"; only the audit log can say whether the actor was CI or a human. This
validates the two-signal design decisively — a `managedFields`-only detector would flag every CI
deploy as human drift.

## `managedFields` manager census (241 field-ownership entries cluster-wide)

| Manager | Op | Entries | Class |
|---|---|---|---|
| `kube-addon-manager` | Apply | 106 | platform |
| `kube-controller-manager` | Update | 72 | controller churn |
| **`kubectl-client-side-apply`** | Update | **20** | **ambiguous: CI *or* human** |
| `kube-apiserver` | Update | 10 | platform |
| `platformagent-controller` | Apply | 7 | our controller |
| `kubectl-rollout` / `kubectl-patch` / `kubectl` | Update | 8 | human-ish |
| `helm` | Apply | 3 | deploy tool |

Only ~30 of 241 entries (12%) carry a `kubectl*` manager, so the `managedFields` side is small
and cheap to scan — but 20 of those 30 are the ambiguous generic manager.

## Implications for Phase 1 (updated)

1. **Filter in two static layers** (cheap, no ML, ~99% reduction):
   - drop `principalEmail =~ "^system:"`;
   - drop a **configurable automation-SA allowlist** (`github-deploy-sa`, `gitops-infra-sa`, …).
2. **The automation allowlist is per-customer config and is the single most important tuning
   knob.** Get it wrong and every CI deploy looks like drift.
3. **Audit log is the primary trigger; `managedFields` is the enrichment.** Inverting that order
   produces false positives on this cluster.
4. **Volume is low after filtering** (~7 human changes/day), so per-drift agent judgment is
   affordable; no sampling needed.
5. **Prefer SSA-based GitOps** where possible: an Argo/Flux controller using SSA records a distinct
   manager, which would make `managedFields` self-sufficient. Client-side CI is the muddy case.

## Caveat

Measured on `platform-agent-host`, a control-plane/admin cluster (its human activity is operator
work on kube-agents itself, not app-team drift). A production app cluster will have a different
mix and likely more Tier-3 volume. Re-run the census there before finalizing filter defaults.
