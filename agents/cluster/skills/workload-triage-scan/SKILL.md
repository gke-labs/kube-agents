---
name: workload-triage-scan
description: Answers cluster-wide workload health questions with one read-only scan. Use for "what is failing or degraded", "is the cluster healthy", "which pods are crashlooping or restarting", and "are all DaemonSet or Deployment pods scheduled and ready" when no single workload is named. Lists unhealthy pods, controllers below desired and recent Warning events, then hands each finding to pod-crashloop-oom, pod-pending-scheduling, pod-container-creating or service-connectivity.
---

# Task

Answer a cluster-wide "what is failing / is it healthy" question from one batched read, finish at once when nothing is unhealthy, and otherwise name each finding's cause through the focused skill that owns its symptom.

# Workflow

1. Run the scan once. It reads pods, Deployments, StatefulSets, DaemonSets and Warning events across all namespaces in three `kubectl get -o json` calls and prints only what needs attention:

   ```bash
   python3 /opt/data/scripts/workload_triage.py
   ```

   It ends with `unhealthy: <count>`: unhealthy pods plus controllers below desired, outside system namespaces. Its rules, for reading the objects yourself where the script cannot run: a pod is unhealthy when a container waits in `CrashLoopBackOff`, an image pull error or a config error, when it is `Pending` or not Ready for more than 10 minutes, when a container restarted in the last hour, or when it `Failed` and no ReplicaSet, StatefulSet or DaemonSet replaces it; the same starting states under 10 minutes are initializing; Warning events count from the last hour. Exit code 2 means the pod read failed or returned output it could not parse: block the card as `needs_input`, quoting the stderr line (the reference's "Cannot proceed" section covers this case).

2. If the last line is `unhealthy: 0`, stop here. Report the summary line (pods healthy, initializing, controllers below desired, warning events), any initializing pods by name, the `RESTARTED EARLIER` and `FAILED EARLIER` rows when the question is about restarts or failures, and the system-namespace lines as written (they carry restart counts for system pods). Run no further command. This is the status question with nothing failing that SOUL.md §2 answers in one sentence plus the evidence that settles it, and the scan's first line (kubeconfig context and UTC time), its summary line and the command are that evidence. Re-listing pods, events or DaemonSets to confirm a clean scan adds nothing.

3. Otherwise, group the `UNHEALTHY PODS` rows by owning workload (the pod name minus its ReplicaSet or ordinal suffix). Pods of one workload share a cause: investigate one representative pod.

4. Take up to three workloads, in this order: `CrashLoopBackOff`, `Error`, `OOMKilled` or `RecentRestart` first, then `Unschedulable`/`Pending`, then image, mount and config errors, then `NotReady`. Load the skill in the row's `SKILL` column and follow it for the representative pod until the cause is named, usually its first two steps. List any further workloads with their scan row only, and say they were not investigated.

5. `RESTARTED EARLIER` rows (Ready now, last restart before the events window) and `FAILED EARLIER` rows (pods that ended before the window and were replaced) are history. List them when the question asks about restarts or failures; do not investigate them.

6. A controller in `CONTROLLERS BELOW DESIRED` with no unhealthy pod row is still rolling out or its pods are initializing: report the counts as read, without waiting.

7. A `WARNING EVENTS` row with no matching pod row: `FailedScheduling` goes to `pod-pending-scheduling`; `FailedMount`, `ProvisioningFailed` and `FailedAttachVolume` go to `pod-container-creating`; anything else is reported as the event says.

8. Record the result as [`references/reporting.md`](references/reporting.md) describes.

# Rules

- Keep the scan itself to about six commands, the script included. Each workload step 4 selects adds its focused skill's steps on top of that, usually two commands.
- Report the current state only. Never `sleep`, `--watch`, or re-run the scan to see whether a pod comes up. A pod in `INITIALIZING` is reported as initializing.
- Do not explore system namespaces (`kube-*`, `gke-*`, `gmp-*`, `*-system`) or node events. Quote their summary lines, which name restarted system pods with count and age. Run `--include-system` only when the card names a system component, or asks for restart counts and a system line ends with `(+N more ...)`. Never run it to double-check a clean scan.
- Do not list pods, nodes or controllers again in other shapes. The scan output is the inventory.
- Do not run `gcloud container clusters get-credentials`. Preflight has already pinned the kubeconfig to this cluster.
