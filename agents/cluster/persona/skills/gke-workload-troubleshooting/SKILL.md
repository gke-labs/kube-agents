---
name: gke-workload-troubleshooting
description: Routes a named workload that is failing for an unknown reason to the focused skill for its symptom. Use when the question names one Deployment, StatefulSet, DaemonSet or pod as broken, down or misbehaving without saying how. For a known symptom load pod-crashloop-oom, pod-pending-scheduling, pod-container-creating or service-connectivity directly; for a cluster-wide health question load workload-triage-scan.
---

# Task

Find which symptom a named workload shows, with projected reads, and hand it to the focused skill that owns that symptom.

# Workflow

1. Project the controller's selector, replica counts and conditions (skip for a bare pod), then read the workload's pods projected to the fields that decide the symptom:

   ```bash
   kubectl get <kind> <name> -n <namespace> -o jsonpath='matchLabels={.spec.selector.matchLabels} specReplicas={.spec.replicas}{.status.desiredNumberScheduled} replicas={.status.replicas} readyReplicas={.status.readyReplicas}{.status.numberReady} conditions={.status.conditions}'
   kubectl get pods -n <namespace> -l <selector> -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,REASON:.status.reason,READY:.status.containerStatuses[*].ready,WAITING:.status.containerStatuses[*].state.waiting.reason,TERM:.status.containerStatuses[*].state.terminated.reason,TERM_EXIT:.status.containerStatuses[*].state.terminated.exitCode,LAST:.status.containerStatuses[*].lastState.terminated.reason,EXIT:.status.containerStatuses[*].lastState.terminated.exitCode,RESTARTS:.status.containerStatuses[*].restartCount,INIT:.status.initContainerStatuses[*].state.waiting.reason,INIT_TERM:.status.initContainerStatuses[*].state.terminated.reason,INIT_LAST:.status.initContainerStatuses[*].lastState.terminated.reason,INIT_EXIT:.status.initContainerStatuses[*].lastState.terminated.exitCode'
   ```

   Here `<kind>` is `deployment`, `statefulset` or `daemonset` (where `desiredNumberScheduled` and `numberReady` supply `specReplicas` and `readyReplicas`, and an omitted `readyReplicas` means `0`). For a bare pod, skip the controller read and pass `--field-selector metadata.name=<pod>`.

2. Load the skill (or follow the direct action) for the first row that matches:

   | Pod / controller state                                                                                                                                                                                                                                                                                                                                                                        | Skill / action                                   |
   | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
   | `PHASE=Failed`, `REASON=Evicted`, `CrashLoopBackOff`, `RunContainerError`, `Error`, `OOMKilled`, `TERM` set to a failure reason (other than `Completed`), non-zero `TERM_EXIT` / `EXIT`, restarts climbing, `Running` not Ready, or `PodInitializing` with `INIT` / `INIT_TERM` / `INIT_LAST` showing `CrashLoopBackOff`, `RunContainerError`, `Error`, `OOMKilled` or a non-zero `INIT_EXIT` | `pod-crashloop-oom`                              |
   | `Pending` with no container or init-container waiting/terminated state                                                                                                                                                                                                                                                                                                                        | `pod-pending-scheduling`                         |
   | `ContainerCreating`, `PodInitializing`, `ImagePullBackOff`, `ErrImagePull`, `InvalidImageName`, `ErrImageNeverPull`, `CreateContainerConfigError`, `CreateContainerError` (in `WAITING` or `INIT`)                                                                                                                                                                                            | `pod-container-creating`                         |
   | `PHASE=Succeeded` (all containers exited `0`), controller or pod `NotFound`, no pods returned, `ReplicaFailure=True`, or `readyReplicas < specReplicas` with no unhealthy pod                                                                                                                                                                                                                 | No focused skill (follow the direct steps below) |
   | At least one pod returned, all pods `Running` and `Ready` (`specReplicas > 0` and `readyReplicas == specReplicas` for a controller), callers report timeouts                                                                                                                                                                                                                                  | `service-connectivity`                           |
   | Any other non-healthy pod or controller state not matched above                                                                                                                                                                                                                                                                                                                               | `pod-crashloop-oom`                              |

   For the **No focused skill** row (`PHASE=Succeeded`, `NotFound`, no pods returned, `ReplicaFailure=True`, or `readyReplicas < specReplicas` with no unhealthy pod):

   - **`PHASE=Succeeded`:** report that the pod or Job run completed cleanly (`TERM=Completed`, exit code `0`), and record at step 3.
   - **`NotFound`:** report that `<kind>/<name>` or `<pod>` was not found in `<namespace>`.
   - **`specReplicas == 0`:** report that the workload is scaled to zero.
   - **Otherwise:** read `kubectl get events -n <namespace> --field-selector reason=FailedCreate` (plus `--field-selector involvedObject.name=<name>` for the controller or owning ReplicaSet) alongside the controller's `ReplicaFailure` and `Progressing` conditions, report the quota, webhook or policy rejection message (or an active rollout when no creation failure exists), and record at step 3.

3. Conclude that the reported fault is not in this workload only when at least one pod is returned, every pod is `Running` and `Ready` with no recent restarts, and (for a controller) `specReplicas > 0`, `readyReplicas == specReplicas` and `ReplicaFailure` is not `True`; cite the controller and pod rows you read (and for a cluster-wide scan routed through [`workload-triage-scan`](../workload-triage-scan/SKILL.md), `"No failing workloads."` requires both `unhealthy: 0` and `system unhealthy: 0`). Record the finding as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) describes.

# Rules

- Never `sleep` or `--watch`; report the state you read.
