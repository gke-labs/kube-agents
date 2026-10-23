---
name: gke-workload-troubleshooting
description: Routes a named workload that is failing for an unknown reason to the focused skill for its symptom. Use when a card names one Deployment, StatefulSet, DaemonSet or pod as broken, down or misbehaving without saying how. For a known symptom load pod-crashloop-oom, pod-pending-scheduling, pod-container-creating or service-connectivity directly; for a cluster-wide health question load workload-triage-scan.
---

# Task

Find which symptom a named workload shows, with one projected read, and hand it to the focused skill that owns that symptom.

# Workflow

1. Read the workload's pods, projected to the fields that decide the symptom:

   ```bash
   kubectl get pods -n <namespace> -l <selector> -o custom-columns='NAME:.metadata.name,PHASE:.status.phase,READY:.status.containerStatuses[*].ready,WAITING:.status.containerStatuses[*].state.waiting.reason,LAST:.status.containerStatuses[*].lastState.terminated.reason,EXIT:.status.containerStatuses[*].lastState.terminated.exitCode,RESTARTS:.status.containerStatuses[*].restartCount'
   ```

   Take `<selector>` from the controller: `kubectl get deployment <name> -n <namespace> -o jsonpath='{.spec.selector.matchLabels}'` (or `statefulset`, `daemonset`). For a bare pod, use `--field-selector metadata.name=<pod>`.

2. Load the skill for the first row that matches:

   | Pod state                                                                                   | Skill                    |
   | ------------------------------------------------------------------------------------------- | ------------------------ |
   | `CrashLoopBackOff`, `Error`, `OOMKilled`, exit code set, restarts climbing, `Running` not Ready | `pod-crashloop-oom`      |
   | `Pending` with no container state                                                           | `pod-pending-scheduling` |
   | `ContainerCreating`, `ImagePullBackOff`, `ErrImagePull`, `CreateContainerConfigError`         | `pod-container-creating` |
   | All pods Running and Ready, but callers report timeouts                                    | `service-connectivity`   |

3. If every pod is Running and Ready with no recent restarts, the reported fault is not there. Say so with the row you read, and record it as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) describes.

# Rules

- Do not run `gcloud container clusters get-credentials`. Preflight has pinned the kubeconfig.
- Never `sleep` or `--watch`; report the state you read.
