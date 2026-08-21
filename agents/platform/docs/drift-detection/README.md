# Drift Detection

Design, spike results, and tooling for the drift-detection domain.

| Document | What it is |
|---|---|
| [`../drift-detection.md`](../drift-detection.md) | Design doc: what drift is, the two-signal model, the MVP CUJs |
| [`spike-findings.md`](./spike-findings.md) | Phase 0 spike results (A: attribution, B: noise profile). Verdict: **GO** |
| [`drift_attribute.sh`](./drift_attribute.sh) | Reusable join: `managedFields` + audit log for one namespace |
| [`baseline-manifests.yaml`](./baseline-manifests.yaml) | Baseline manifests used to stage the spike |
| [`cuj3-task-breakdown.md`](./cuj3-task-breakdown.md) | Ticket: CUJ 3 (noise-filtered triage), the enabling implementation |

## What the spike settled

Four findings that shape the build, and that are counterintuitive enough to be worth
re-reading before changing the detector:

1. **The audit log is the primary trigger; `managedFields` is enrichment.** Inverting that
   order produces false positives on a real cluster.
2. **The audit principal is required, not optional.** CI applying client-side records
   `kubectl-client-side-apply` — the exact manager string a human running `kubectl apply`
   produces. A `managedFields`-only detector flags every CI deploy as drift.
3. **Two static filters remove ~99% of the noise** — drop `principalEmail =~ "^system:"`,
   then drop a configurable automation-service-account allowlist. The allowlist is
   per-cluster config and is the single most important tuning knob.
4. **Volume after filtering is low** (~7 human changes/day on an active cluster), so
   per-change agent judgment is affordable. No sampling needed.

## Caveats on the measurements

Measured on `platform-agent-host`, a control-plane/admin cluster whose human activity is
operator work on kube-agents itself, not app-team drift. It also runs no in-cluster GitOps
controller — the GitOps owner was simulated with
`kubectl apply --server-side --field-manager=argocd-controller`, which produces the same
`managedFields` entry Argo or Flux SSA would. Re-run the census on a production app cluster
with a real GitOps controller before finalizing filter defaults.
