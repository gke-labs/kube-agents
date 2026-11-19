---
title: AgentSchedule CRD
description: Custom resource that runs a prompt on an AgentProfile on a cron schedule, each run an Unattended task. Defined, off by default.
sidebar:
  order: 9
---

An `AgentSchedule` runs a prompt on an `AgentProfile` on a cron schedule. When a slot comes due, the operator submits the run through the A2A gateway as an `Unattended` task, and the profile dispatcher runs it. It is the replacement for the Hermes cron rosters ([scheduling on profiles](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/scheduling-on-profiles.md)).

**Nothing fires yet.** The API server stores schedules, but no controller reads them: the reconciler and its submission to the gateway are wired in a later change, behind the operator's `A2A_AGENT_SCHEDULES` flag. The chart ships no schedules.

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

- The `kubeagents.x-k8s.io/origin` label says who owns a schedule. A schedule runs as `builtin` only when it reads `builtin` and the install's `PlatformAgent` is its controller, as on the schedules the operator renders. Any other schedule runs as `user`.
- `schedule` is a standard 5-field cron expression, read in UTC, as a Kubernetes `CronJob` reads it. It takes no time zone prefix.
- `profileRef` names an `AgentProfile` in the same namespace whose `allowedTiers` includes `unattended`. A schedule whose profile is missing or doesn't allow it reads `Accepted=False` and never fires.
- `concurrencyPolicy` is `Forbid`, the only value: a slot that comes due while the last run is still going is skipped and counted in `status.skipCounts.alreadyRunning`.
- `parameters` (at most 16) go to each run as task parameters. A built-in schedule's report stream is its own name, carried on its trigger, never a parameter.
- `delivery.jobKey` groups a schedule's posts into one thread per UTC day. It's a DNS-1123 label, defaulting to the schedule's name with dots turned into hyphens.
- `startingDeadlineSeconds` (default 900) is how late a slot may still start. After an outage, one late slot runs if it is still inside the deadline, and the rest are counted once in `status.skipCounts.missedWindow`.
- `activeDeadlineSeconds` bounds a run, defaulting to the profile's `lifecycle.activeDeadlineSeconds`, the ceiling its Job runs under. A run that reports no end within it plus five minutes is recorded `Lost`, and the schedule moves on.
- `suspend: true` skips every slot. A slot that passes while suspended is counted, not run later. A `run-now` still runs: it is an explicit request.
- Editing the spec (a new `schedule`, say) moves the next slot to the new schedule's next one from now.

**Run it now.** Setting the annotation `kubeagents.x-k8s.io/run-now` to a new value starts one run, or adopts the run already in flight, without moving the cadence:

```sh
kubectl annotate agentschedule compliance-audit kubeagents.x-k8s.io/run-now="$(date -u +%s)" --overwrite
```

**Status is the run ledger.** No run state lives in a pod: `status` holds the next and last slots, the run in flight, the last outcome, the skip counts and the last ten runs. A slot is claimed in `status.pendingSlot` before its submission, with an idempotency key the gateway answers with the same task, so an operator restart neither runs a slot twice nor drops it. The operator also exports `kubeagents_schedule_runs_total{schedule,outcome}` and `kubeagents_schedule_skips_total{schedule,reason}`.

Only the operator writes a schedule's status, and no agent ServiceAccount can write `AgentSchedule` resources.

See [AgentSchedule fields](/kube-agents/operator/agentschedule-fields/) for every field.
