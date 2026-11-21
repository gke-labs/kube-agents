---
title: AgentSchedule CRD
description: Custom resource that runs a prompt on an AgentProfile on a cron schedule, each run an Unattended task. Defined, off by default.
sidebar:
  order: 9
---

An `AgentSchedule` runs a prompt on an `AgentProfile` on a cron schedule. When a slot comes due, the operator submits the run through the A2A gateway as an `Unattended` task, and the profile dispatcher runs it. It is the replacement for the Hermes cron rosters ([scheduling on profiles](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/scheduling-on-profiles.md)).

**Off by default.** The operator reads schedules and submits their runs only with its `A2A_AGENT_SCHEDULES` flag set to `true`. Without it the API server stores schedules and nothing fires. The chart ships no schedules.

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentSchedule
metadata:
  name: compliance-audit
  namespace: kubeagents-system
  labels:
    kubeagents.x-k8s.io/origin: builtin # builtin | user
spec:
  schedule: "20 6 * * *" # 5 fields, UTC
  profileRef:
    name: fleet-audit # must allow the unattended tier
  prompt: Run the daily compliance audit.
  delivery:
    mode: chat # or local
    silentWhenClean: true
```

- The `kubeagents.x-k8s.io/origin` label says who owns a schedule. A schedule runs as `builtin` only when it reads `builtin`, the install's `PlatformAgent` is its controller, and its name is one of the built-in schedules that `PlatformAgent` renders. The operator renders them while schedules are on, and none ships yet. A built-in's name is reserved: a schedule you create under one is deleted and replaced by the built-in. Pause a built-in through the PlatformAgent's `spec.schedules.overrides`, keyed on the schedule's name. Only the operator may set, change or remove the `builtin` label or a controller reference, or change a built-in schedule's spec (except `spec.suspend`): an admission policy (`kube-agents-agentschedule-builtin`) refuses anyone else. Annotations such as `run-now`, and suspending a built-in schedule (`spec.suspend`), still pass. A schedule labelled `builtin` that isn't one the operator renders reads `Accepted=False` (`BuiltinNotFromOperator`). Any other schedule is a user schedule. User schedules wait on a later change, so for now one reads `Accepted=False` (`UserSchedulesDeferred`) and never fires.
- The name is a dot-free DNS-1123 label of at most 63 characters, because each run's trigger is `schedule/<name>`. A schedule with any other name reads `Accepted=False` (`InvalidName`).
- `schedule` is a standard 5-field cron expression, read in UTC, as a Kubernetes `CronJob` reads it. It takes no time zone prefix.
- `profileRef` names an `AgentProfile` in the same namespace whose `allowedTiers` includes `unattended`. A schedule whose profile is missing or doesn't allow it reads `Accepted=False` and never fires.
- `concurrencyPolicy` is `Forbid`, the only value: a slot that comes due while the last run is still going is skipped and counted in `status.skipCounts.alreadyRunning`.
- `parameters` (at most 16) go to each run as task parameters. A built-in schedule's report stream is its own name, carried on its trigger, never a parameter.
- `delivery.jobKey` groups a schedule's posts into one thread per UTC day. It's a DNS-1123 label, defaulting to the schedule's name.
- `startingDeadlineSeconds` (default 900) is how late a slot may still start. After an outage, one late slot runs if it is still inside the deadline, and the rest are counted once in `status.skipCounts.missedWindow`.
- `activeDeadlineSeconds` bounds a run, defaulting to the profile's `lifecycle.activeDeadlineSeconds`, the ceiling its Job runs under. A run that reports no end within it plus five minutes is recorded `Lost`, and the schedule moves on.
- `suspend: true` skips every slot. A slot that passes while suspended is counted, not run later. A `run-now` still runs: it is an explicit request.
- Editing the spec (a new `schedule`, say) moves the next slot to the new schedule's next one from now.

**Run it now.** Setting the annotation `kubeagents.x-k8s.io/run-now` to a new value starts one run, or adopts the run already in flight, without moving the cadence:

```sh
kubectl annotate agentschedule compliance-audit kubeagents.x-k8s.io/run-now="$(date -u +%s)" --overwrite
```

**Status is the run ledger.** No run state lives in a pod: `status` holds the next and last slots, the run in flight, the last outcome, the skip counts and the last ten runs. A slot is claimed in `status.pendingSlot` before its submission, with an idempotency key the gateway answers with the same task, so an operator restart neither runs a slot twice nor drops it. The operator also exports `kubeagents_schedule_runs_total{schedule,outcome}` and `kubeagents_schedule_skips_total{schedule,reason}`. The `Submitted` condition shows how the gateway answered the last submission. It reads `False` (`GatewayRefused`, with the gateway's reason) when the gateway refused a run, which drops that slot without retrying it.

Only the operator writes a schedule's status, and no agent ServiceAccount can write `AgentSchedule` resources.

See [AgentSchedule fields](/kube-agents/operator/agentschedule-fields/) for every field.
