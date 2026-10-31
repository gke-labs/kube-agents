---
name: pod-crashloop-oom
description: Finds why a named pod or workload keeps restarting. Use for CrashLoopBackOff, Error, OOMKilled or exit code 137, a non-zero exit code, failing liveness or readiness probes (NotReady), Evicted or Failed pods, and high restart counts on one workload. Reads the last termination, previous logs and limits, and proposes the manifest fix.
---

# Task

Name the cause of one workload's restarts, quote the evidence, and propose the manifest patch. Investigate one representative pod: replicas of one controller share a cause.

# Workflow

1. Read the container state, limits, probes, command and owner in one projected call:

   ```bash
   kubectl get pod <pod> -n <namespace> -o json | jq '{owner: .metadata.ownerReferences, node: .spec.nodeName, initContainers: [.spec.initContainers[]? | {name, image, command, args, resources, env: [.env[]? | {name, valueFrom}], envFrom}], containers: [.spec.containers[] | {name, image, command, args, resources, env: [.env[]? | {name, valueFrom}], envFrom, livenessProbe, readinessProbe}], initStatus: [.status.initContainerStatuses[]? | {name, ready, restartCount, state, lastState}], status: [.status.containerStatuses[]? | {name, ready, restartCount, state, lastState}], reason: .status.reason, message: .status.message}'
   ```

2. Read the previous container's log tail, or pass `-c <container>` without `-p` when step 1 shows `restartCount: 0` (or `-c <init-container> -p` when an init container is restarting while app containers wait in `PodInitializing`):

   ```bash
   kubectl logs <pod> -n <namespace> --all-containers -p --tail=50
   ```

3. Read this pod's events only:

   ```bash
   kubectl get events -n <namespace> --field-selector involvedObject.name=<pod> -o custom-columns=LAST:.lastTimestamp,TYPE:.type,REASON:.reason,COUNT:.count,MESSAGE:.message
   ```

   Events last about an hour. For a failure reported as earlier ("since this morning"), read the window from Cloud Logging instead, centred on the last `finishedAt` from step 1 (30 minutes either side):

   ```bash
   gcloud logging read "resource.type=\"k8s_cluster\" AND logName=\"projects/<project_id>/logs/events\" AND jsonPayload.involvedObject.namespace=\"<namespace>\" AND timestamp>=\"<start_time>\" AND timestamp<=\"<end_time>\"" --project=<project_id> --limit=50 --format="table(timestamp,jsonPayload.reason,jsonPayload.message)"
   ```

4. Match the evidence, then record the finding as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) describes. Stop once the cause, the evidence and the patch are written.

# Signatures

| Evidence                                                         | Cause                                      | Patch                                                                     |
| ---------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------------------------- |
| `exitCode: 137`, `reason: OOMKilled`                             | Memory limit reached                       | Raise `resources.limits.memory`, or fix the leak the log and command show |
| `exitCode: 1` or another non-zero code, stack trace in the log   | Application crash                          | Name the exception; propose the config or image fix it points to          |
| Log names a missing variable, password, file or key              | Missing configuration                      | Add the `env`, `envFrom` or Secret/ConfigMap reference                    |
| `Read-only file system` or `Permission denied` on `/tmp`, `/var` | `readOnlyRootFilesystem` with no scratch   | Mount an `emptyDir` at that path                                          |
| `Liveness probe failed` or `Readiness probe failed` events       | Probe path, port or timing wrong           | Correct the probe, or raise `initialDelaySeconds`                         |
| `Connection timed out`, `dial tcp: i/o timeout` in the log       | Dependency unreachable                     | Load `service-connectivity` for the target service                        |
| `reason: Evicted`, node pressure message                         | Node memory or disk pressure               | Set requests near real usage; check the node in the message               |
| `exitCode: 143` with no other error                              | SIGTERM: stopped by the kubelet or a probe | Read the probe events; extend `terminationGracePeriodSeconds` if needed   |

For an OOM, tell an application leak or loop (unbounded growth in the log, a command that loads everything into memory) from a limit set below real demand. When a GKE event reason or exit code is unfamiliar, search `mcp-developer_knowledge` before a web search.

# Rules

- Budget about five commands. Do not `describe` or log every replica.
- Never `sleep` or `--watch` for the next restart.
- Do not run `gcloud container clusters get-credentials`. Preflight has pinned the kubeconfig.
