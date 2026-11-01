# Recording a workload finding

Shared by `workload-triage-scan`, `pod-crashloop-oom`, `pod-pending-scheduling`, `pod-container-creating`, `service-connectivity`, `gke-workload-troubleshooting`, `gke-stall-detection`, `gke-load-bearing-change` and `topology-drift`.

## Boundary

You work under a strict read-only boundary. Do not apply patches to the cluster and do not open pull requests: whoever asked for the investigation owns the GitOps write path. Report in your final answer.

## Finding

1. State the root cause, grounded in the evidence you read, for example: _"payment-api exits with code 137 because its memory limit is 256Mi and the container reached it; `lastState.terminated.reason` is `OOMKilled`."_
2. Write the corrected manifest patch: a memory limit, a missing Secret or ConfigMap reference, a toleration, an `emptyDir` mount, a NetworkPolicy egress rule.
3. Put the whole finding in your final answer: the root cause, the evidence that grounds it, and the proposed patch as a fenced `yaml` block. Aim under 2,000 characters (`instructions.md` §9): tighten the prose, keep the cause, the evidence and the patch.

## Healthy scan

When `workload_triage.py` ends with `unhealthy: 0` and `system unhealthy: 0` and either no `WARNING EVENTS` row required a focused-skill investigation or every routed `gone`/`-` warning row was confirmed recovered (for example, a replacement pod is `Running` and `Ready` or the claim is now `Bound`), put `"No failing workloads."`, the scan's summary line, any initializing pods by name, any `WARNING EVENTS` rows (plus the confirming pod, controller or object read for each recovered `gone`/`-` row), the `RESTARTED EARLIER` and `FAILED EARLIER` rows when the question asks about restarts or failures, and the system-namespace lines in your final answer, with no patch and (unless the request asked about system namespaces) no `[next: ...]` line; stop your turn immediately with no follow-up command.

When `workload_triage.py` ends with `unhealthy: 0` and `system unhealthy: N` (`N > 0`), do not use `"No failing workloads."`: state that non-system namespaces have 0 unhealthy workloads while folded namespaces have `N` unhealthy pods or controllers below desired, and quote the `SYSTEM NAMESPACES AND NODES` lines as written in your final answer, with no patch.

## Several findings

Give each investigated workload its own `##` section with cause, evidence and patch. List workloads the scan found but you did not investigate in one table with their scan row.

## Cannot proceed

If an input is missing or the scope is ambiguous, say what you need in your final answer instead. Do the same when the cluster cannot be read: `workload_triage.py` exiting 2, or a kubectl read refused or unreachable. Quote the exact stderr line in your final answer.
