# Recording a workload finding

Shared by `workload-triage-scan`, `pod-crashloop-oom`, `pod-pending-scheduling`, `pod-container-creating`, `service-connectivity`, `gke-workload-troubleshooting`, `gke-stall-detection`, `gke-load-bearing-change` and `topology-drift`. Read it with `skill_view(name="workload-triage-scan", file_path="references/reporting.md")`.

## Boundary

You are a Cluster Agent under a read-only boundary. Do not apply patches to the cluster and do not open pull requests: the GitOps write path (`submit-suggestion`) belongs to the Platform Agent. Do not pass findings back through your chat reply. You communicate only through the kanban task you were spawned on.

## Finding

1. State the root cause, grounded in the evidence you read, for example: _"payment-api exits with code 137 because its memory limit is 256Mi and the container reached it; `lastState.terminated.reason` is `OOMKilled`."_
2. Write the corrected manifest patch: a memory limit, a missing Secret or ConfigMap reference, a toleration, an `emptyDir` mount, a NetworkPolicy egress rule.
3. Complete the task with the finding in `result`. It is the only field the requester receives; the gateway posts it into their chat thread as written. `metadata` is what the Platform Agent parses, not a second home for the finding. Aim under 2,000 characters (SOUL.md §6 step 4): tighten the prose, keep the cause, the evidence and the patch.

```
kanban_complete(
  result="<root cause, the evidence that grounds it, and the proposed patch>",
  summary="<one-line status, under 400 characters>",
  metadata={
    "root_cause": "...",
    "evidence": ["<quoted event, log line or spec excerpt>"],
    "proposed_patch": "<YAML>"
  }
)
```

## Healthy scan

When `workload_triage.py` ends with `unhealthy: 0` and `system unhealthy: 0` and either no `WARNING EVENTS` row required a focused-skill investigation or every routed `gone`/`-` warning row was confirmed recovered (for example, a replacement pod is `Running` and `Ready` or the claim is now `Bound`), use `metadata={"unhealthy": 0, "system_unhealthy": 0}` with no `proposed_patch`. Put the scan's summary line, any initializing pods by name, any `WARNING EVENTS` rows (plus the confirming pod, controller or object read for each recovered `gone`/`-` row), the `RESTARTED EARLIER` and `FAILED EARLIER` rows when the question asks about restarts or failures, and the system-namespace lines in `result`, with no patch and (unless the card asked about system namespaces) no `[next: ...]` line; stop your turn immediately after `kanban_complete` with no follow-up command:

```
kanban_complete(
  result="No failing workloads. <pods: N (healthy N, initializing N, unhealthy 0) ...>. <initializing pods>. <warning-event rows, if any>. <restarted-earlier rows, if asked>. <system namespace lines>. Scanned <UTC time> on <context>.",
  summary="No failing workloads: N pods healthy, N initializing",
  metadata={"unhealthy": 0, "system_unhealthy": 0}
)
```

When `workload_triage.py` ends with `unhealthy: 0` and `system unhealthy: N` (`N > 0`), do not use `"No failing workloads."`: state that non-system namespaces have 0 unhealthy workloads while folded namespaces have `N` unhealthy pods or controllers below desired, quote the `SYSTEM NAMESPACES AND NODES` lines as written, and complete with `metadata={"unhealthy": 0, "system_unhealthy": N}` and no `proposed_patch`.

## Several findings

Give each investigated workload its own `##` section with cause, evidence and patch. List workloads the scan found but you did not investigate in one table with their scan row.

## Cannot proceed

If an input is missing or the scope is ambiguous, call `kanban_block(kind="needs_input", ...)` instead. Do the same when the cluster cannot be read: `workload_triage.py` exiting 2, or a kubectl read refused or unreachable. Put the exact stderr line in the reason, as `AGENTS.md` asks under "Fail loud". Your final chat reply is a brief acknowledgement, never the finding. The Platform Agent reads the completed card and decides whether to open or update a pull request.
