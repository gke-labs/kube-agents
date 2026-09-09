---
name: gke-stall-detection
description: Read-only detection of controllers that stopped making progress without erroring — observedGeneration lag, stuck progress conditions, repeating warning events, and references to objects that do not exist — in one namespace or one kind.
---

# GKE Stall Detection

Use this skill when a controller has gone quiet rather than red: a Gateway that never programs, a rollout that never finishes, a custom resource whose status stopped following its spec, an HTTPRoute nobody accepted. The symptom-driven skills (`gke-workload-troubleshooting`, `gke-reliability`) start from a failing pod or a bad event; this one starts from the absence of progress, which those states never show. It is read-only: it lists objects, reads their status and events, resolves referenced names, and proposes nothing beyond naming what is missing.

## When to run it

- A user reports something "stuck", "still pending", "never came up", or "not reconciling" and nothing is CrashLooping.
- A Gateway, HTTPRoute, Certificate, Config Connector or Argo-managed object has been in the same state for longer than its controller should need.
- Before declaring a namespace healthy in a reliability review: a silent stall is what that review otherwise misses.

## Heuristics and thresholds

`scripts/stall_report.py` applies four heuristics to every object it reads. Each is gated by an age threshold, so a controller that is merely slow is not reported.

| Heuristic             | Fires when                                                                                                                                                                                                | Age measured from                                             |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `generation-lag`      | `metadata.generation` is ahead of `status.observedGeneration` (objects without `observedGeneration` are skipped)                                                                                          | newest `managedFields` entry that wrote `f:spec`              |
| `stale-condition`     | A `Progressing`, `Accepted`, `Programmed`, `ResolvedRefs`, `Ready` or `Available` condition is not `True`, including conditions nested under `status.listeners[]` or `status.parents[]`                   | the condition's `lastTransitionTime`                          |
| `repeating-warnings`  | A Warning event on the object has a count of at least 3, spans at least the threshold, and was last seen within the threshold                                                                             | the event's first to last occurrence                          |
| `dangling-reference`  | The spec names an object that does not exist: `certificateRefs`, `configMapRef`, `configMapKeyRef`, `secretRef`, `secretKeyRef`, `parentRefs`, `backendRefs`, and volume `configMap`/`secret`/`persistentVolumeClaim` sources; `optional: true` references are skipped | newest `managedFields` entry that wrote `f:spec`              |

Thresholds live at the top of the script: `DEFAULT_STALL_MINUTES = 15` for every kind, and `STALL_MINUTES_BY_KIND` for kinds whose API names its own horizon — `Deployment` is 10, matching the default `progressDeadlineSeconds`. `--threshold-minutes` replaces both for one run. Pods in `Succeeded` or `Failed` phase are skipped, and references are resolved from `kubectl get <kind> -o name` so Secret contents are never read.

A single run reads one snapshot, so `repeating-warnings` cannot watch a count move; the span and recency conditions are its stand-in. Run the script twice a few minutes apart when a rising count is the evidence you need.

## Running it

```bash
gcloud container clusters get-credentials <cluster_name> --region <cluster_location>

# Every namespaced kind in one namespace (kubectl api-resources decides which):
python3 scripts/stall_report.py --namespace <namespace>

# One or more kinds, as kubectl names them:
python3 scripts/stall_report.py --namespace <namespace> --kind gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io

# A different horizon, or machine-readable output:
python3 scripts/stall_report.py --namespace <namespace> --threshold-minutes 60 --json
```

The table has one row per object and heuristic — `OBJECT`, `HEURISTIC`, `DETAIL`, `STALLED_FOR` — and always ends with `stalled resources: <count>`, the number of distinct objects with at least one row. A healthy namespace prints the header and `stalled resources: 0`. Lines beginning `warning:` on stderr name an API the script could not read; a kind it cannot list is left out of the scan, and a referent kind it cannot list is never reported missing.

## Worked example: a Gateway waiting for a Secret that will never exist

A Gateway whose HTTPS listener names a TLS Secret that certificate automation was never going to create stays `Accepted=True` forever. The GKE Gateway controller emits `Warning SYNC` events and sets `ResolvedRefs=False` on the listener; the HTTPRoutes behind it are accepted and never programmed, and nothing goes red.

```
$ python3 scripts/stall_report.py --namespace edge
OBJECT        HEURISTIC           DETAIL                                                                   STALLED_FOR
Gateway/edge  dangling-reference  listeners[0].tls.certificateRefs -> Secret/edge-tls not found            6h12m
Gateway/edge  stale-condition     listeners[https] ResolvedRefs=False InvalidCertificateRef                6h11m
Gateway/edge  repeating-warnings  SYNC x743: error processing listener https: secret "edge-tls" not found  6h10m
stalled resources: 1
```

The report names the object, the missing Secret and the stall duration from warning events alone. The HTTPRoutes behind the listener carry no row of their own — they are accepted, and the stall is the Gateway's — so run against the namespace that holds the Gateway, not only the routes. Confirm the reference with `kubectl get gateway edge -n edge -o jsonpath='{.spec.listeners[*].tls.certificateRefs}'` and check whether the Secret is expected from automation (a `Certificate` or `ManagedCertificate` in the namespace, itself possibly a row in the same report) or was simply never created.

The same four heuristics apply without Gateway-specific code: a Deployment whose pods cannot start because `envFrom` names a missing ConfigMap shows `dangling-reference` on the Deployment and `stale-condition Ready=False` on its Pods; a Config Connector or other custom resource whose controller is wedged shows `generation-lag`.

## Recording the finding

You are a Cluster Agent under a strict read-only boundary: no patches applied, no Pull Requests opened, nothing passed back through the chat reply. Complete the kanban task you were spawned on, following the shape in `gke-workload-troubleshooting`:

```
kanban_complete(
  result="<which objects are stalled, for how long, on which heuristic, and what each is waiting for (the missing Secret, the unmet condition), grounded in the report rows and the confirming kubectl reads; or the statement that the namespace has no stalled resources>",
  summary="<one line: N stalled resources in <namespace>, or none>",
  metadata={
    "root_cause": "...",
    "evidence": ["<report rows>", "<quoted event or condition>"],
    "proposed_patch": "<YAML creating or correcting the missing reference, when one is identifiable; otherwise omit>"
  }
)
```

A report with no rows is a result, not a failure to find something: say the namespace has no stalled resources and what threshold that was measured at.
