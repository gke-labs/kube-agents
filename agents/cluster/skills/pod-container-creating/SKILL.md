---
name: pod-container-creating
description: Finds why a pod's containers never start. Use for ContainerCreating or PodInitializing that does not finish, FailedMount and FailedAttachVolume, a missing Secret, ConfigMap or PersistentVolumeClaim, ProvisioningFailed, ImagePullBackOff or ErrImagePull, a wrong image tag, registry authentication errors, and CreateContainerConfigError. Proposes the manifest fix.
---

# Task

Name what blocks one pod's containers from starting, quote the event, and propose the fix.

# Workflow

1. Read the pod's images, volumes and configuration references in one projected call:

   ```bash
   kubectl get pod <pod> -n <namespace> -o json | jq '{owner: .metadata.ownerReferences, images: [.spec.containers[] | {name, image}], imagePullSecrets: .spec.imagePullSecrets, volumes: .spec.volumes, envFrom: [.spec.containers[].envFrom[]?], secretKeyRefs: [.spec.containers[].env[]? | .valueFrom.secretKeyRef // empty], configMapKeyRefs: [.spec.containers[].env[]? | .valueFrom.configMapKeyRef // empty], waiting: [.status.containerStatuses[]?, .status.initContainerStatuses[]? | {name, waiting: .state.waiting}]}'
   ```

2. Read the pod's events:

   ```bash
   kubectl get events -n <namespace> --field-selector involvedObject.name=<pod> -o custom-columns=LAST:.lastTimestamp,REASON:.reason,COUNT:.count,MESSAGE:.message
   ```

3. Confirm a referenced object exists, only for the kind the event names:

   ```bash
   kubectl get configmap,pvc -n <namespace> -o custom-columns=KIND:.kind,NAME:.metadata.name,STATUS:.status.phase,CLASS:.spec.storageClassName
   ```

   The default Cluster Agent identity cannot list Secrets. A `Secret "<name>" not found` event is the evidence; do not try to read the Secret.

4. Record the finding as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) describes. Stop once the cause, the evidence and the patch are written.

# Signatures

| Event                                                            | Cause                                     | Patch                                                                               |
| ---------------------------------------------------------------- | ----------------------------------------- | ----------------------------------------------------------------------------------- |
| `FailedMount`: `persistentvolumeclaim "<name>" not found`        | Missing claim                             | Create the claim or correct `claimName`                                             |
| `ProvisioningFailed`: `storageclass ... not found`               | Claim names a StorageClass that is absent | Correct `storageClassName`                                                          |
| `FailedMount`: `secret "<name>" not found`                       | Missing Secret                            | Create the Secret or correct the reference                                          |
| `FailedMount`: `configmap "<name>" not found`                    | Missing ConfigMap                         | Create the ConfigMap or correct the reference                                       |
| `CreateContainerConfigError`: `secret`/`configmap` key not found | Missing key in an existing object         | Add the key or correct `key:`                                                       |
| `Failed`/`BackOff`: `manifest unknown`, `not found` on pull      | Wrong image tag or name                   | Restore the last working tag (below)                                                |
| `Failed`: `unauthorized`, `403`, `denied` on pull                | Registry access                           | Grant the node or Workload Identity read on the registry, or add `imagePullSecrets` |
| `FailedAttachVolume`, `Multi-Attach error`                       | Disk still attached elsewhere             | Use `ReadWriteOnce` with one replica, or a regional/RWX class                       |

For a wrong tag, find the last working one in the manifest repository's history when it is in your workspace: `git log -p -S "<image_name>" -- <manifest_path>`. Propose that tag, or the corrected one.

# Rules

- Budget about five commands.
- A pod under the scan's grace period is initializing: report it, do not wait. No `sleep`, no `--watch`.
- Do not run `gcloud container clusters get-credentials`. Preflight has pinned the kubeconfig.
