---
title: AgentSchedule fields
description: Every spec and status field of the AgentSchedule custom resource, generated from the operator's Go API types.
sidebar:
  order: 10
---

<!-- BEGIN GENERATED: crd-fields-agentschedule -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

Every field of the `AgentSchedule` resource's `spec` and `status`, generated from the operator's Go API types. The standard Kubernetes `apiVersion`, `kind` and `metadata` are not listed. A field whose type comes from the Kubernetes API (`corev1`, `metav1`, `networkingv1`, `resource.Quantity`) names that type and is not expanded; the Kubernetes API reference describes it. Paths read `parent.child`, with `[]` for each item of a list and `{}` for each value of a map. Each description is the opening paragraph of the field's documentation, plus its deprecation notice when it has one, and names other fields in their Go spelling (`MaxSessions` for `maxSessions`); `kubectl explain agentschedule.spec.<field>` (`agentschedule.status.<field>` for a row of the Status table) prints the whole text. The Constraints column lists the schema bounds on a field: allowed values, ranges, lengths and patterns. The API server also enforces CEL rules on these fields and on whole sections, which the column does not show; `kubectl get crd agentschedules.kubeagents.x-k8s.io -o yaml` lists them under `x-kubernetes-validations`, each with the message the API server returns when a value breaks it. For what the fields do together, read the [AgentSchedule CRD](/kube-agents/operator/agentschedule-crd/) guide.

## Spec

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `schedule` | `string` | yes |  | max length `128` | Schedule is a 5-field cron expression, read in UTC. |
| `suspend` | `boolean` | no |  |  | Suspend skips every run while true. A slot that passes while suspended is counted, not run later. |
| `profileRef` | `object` | yes |  |  | ProfileRef is the AgentProfile the runs go to. It must allow the unattended tier. |
| `profileRef.name` | `string` | yes |  | min length `1`; max length `63` | Name is the AgentProfile's name, in the schedule's namespace. |
| `prompt` | `string` | yes |  | min length `1`; max length `16384` | Prompt is what each run asks. |
| `parameters` | `map[string]string` | no |  | max properties `32` | Parameters are passed to each run as task parameters. `stream` names the run's audit report stream. |
| `skills` | `[]string` | no |  | max items `16` | Skills the run asks the profile to use. |
| `delivery` | `object` | no | `{}` |  | Delivery is where each run's report goes. Omitted, it is the home channel (chat). |
| `delivery.mode` | `string` | no | `chat` | one of `chat`, `local` | Mode is chat (the home channel) or local (kept, not posted). |
| `delivery.silentWhenClean` | `boolean` | no |  |  | SilentWhenClean posts nothing for a run that found nothing. |
| `delivery.jobKey` | `string` | no |  | max length `63` | JobKey groups a schedule's posts into one thread per UTC day. It defaults to the schedule's name. |
| `concurrencyPolicy` | `string` | no | `Forbid` | one of `Forbid` | ConcurrencyPolicy is Forbid, the one policy: a slot that comes due while a run is still going is skipped and counted. |
| `startingDeadlineSeconds` | `integer` | no | `900` | min `60`; max `86400` | StartingDeadlineSeconds is how late a slot may still start. A slot missed by more (an operator outage, a gateway that stayed down) is counted and skipped. Defaults to 900. |
| `activeDeadlineSeconds` | `integer` | no |  | min `60`; max `86400` | ActiveDeadlineSeconds bounds a run. Past it, plus a grace, a run that never reported a terminal is recorded Lost and the schedule moves on. Unset, it is the profile's lifecycle.activeDeadlineSeconds, the ceiling its Job runs under. |
| `maxTurns` | `integer` | no |  | min `1`; max `1000` | MaxTurns caps the run's model turns. |

## Status

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `observedGeneration` | `integer` | no |  |  | ObservedGeneration is the .metadata.generation last reconciled. |
| `lastScheduleTime` | `metav1.Time` | no |  |  | LastScheduleTime is the last slot that was run. |
| `nextScheduleTime` | `metav1.Time` | no |  |  | NextScheduleTime is the next slot due. |
| `pendingSlot` | `object` | no |  |  | PendingSlot is a claimed run not yet accepted by the gateway. |
| `pendingSlot.slotTime` | `metav1.Time` | yes |  |  | SlotTime is the slot being run (now, for a run-now). |
| `pendingSlot.idempotencyKey` | `string` | yes |  |  | IdempotencyKey identifies this run to the gateway. |
| `pendingSlot.runNow` | `string` | no |  |  | RunNow is the run-now token this slot answers, when it is one. |
| `pendingSlot.claimedAt` | `metav1.Time` | yes |  |  | ClaimedAt is when the slot was claimed; the starting deadline is counted from it for a run-now. |
| `activeTaskID` | `string` | no |  |  | ActiveTaskID is the run in flight, if any. |
| `activeTaskStartedAt` | `metav1.Time` | no |  |  | ActiveTaskStartedAt is when the run in flight was accepted. |
| `lastTaskID` | `string` | no |  |  | LastTaskID is the most recent run's task. |
| `lastCompletionTime` | `metav1.Time` | no |  |  | LastCompletionTime is when the most recent run ended. |
| `lastOutcome` | `string` | no |  | one of `Succeeded`, `Failed`, `Cancelled`, `Lost` | LastOutcome is how the most recent run ended. |
| `lastHandledRunNow` | `string` | no |  |  | LastHandledRunNow is the last run-now token acted on. |
| `skipCounts` | `object` | no |  |  | SkipCounts counts slots that did not run. |
| `skipCounts.alreadyRunning` | `integer` | yes |  |  | AlreadyRunning: the last run was still going (Forbid). |
| `skipCounts.missedWindow` | `integer` | yes |  |  | MissedWindow: the slot was more than the starting deadline late. |
| `skipCounts.suspended` | `integer` | yes |  |  | Suspended: the schedule was suspended. |
| `skipCounts.gatewayUnavailable` | `integer` | yes |  |  | GatewayUnavailable: the gateway did not accept the run within the starting deadline. |
| `skipCounts.rateLimited` | `integer` | yes |  |  | RateLimited: the gateway refused the run under the trigger's cap. |
| `recentRuns` | `[]object` | no |  | max items `10` | RecentRuns are the last ten runs, newest first. |
| `recentRuns[].taskID` | `string` | yes |  |  | TaskID is the run's task. |
| `recentRuns[].slotTime` | `metav1.Time` | yes |  |  | SlotTime is the slot it ran for. |
| `recentRuns[].completionTime` | `metav1.Time` | no |  |  | CompletionTime is when it ended. |
| `recentRuns[].outcome` | `string` | yes |  |  | Outcome is how it ended. |
| `conditions` | `[]metav1.Condition` | no |  |  | Conditions are the latest observations of the schedule. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: crd-fields-agentschedule -->
