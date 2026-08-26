---
title: Uninstall
description: Remove the Platform Agent, operator, and provisioned GCP resources.
---

There are two levels of cleanup: removing just the Platform Agent (keeping the cluster and operator), or a full teardown of everything the installer created.

## Uninstall the Platform Agent only

Use this to remove the agent while leaving the GKE cluster and operator in place.

1. **Stop the heartbeat.** Delete or disable the recurring 1-minute cron in your agent harness so no new runs fire.
2. **Delete the `PlatformAgent` CR.**

   ```bash
   kubectl delete platformagent platform-agent -n kubeagents-system --ignore-not-found=true
   ```

   If deletion hangs on a controller finalizer (e.g. the operator or its webhook is offline), clear the finalizer and retry:

   ```bash
   kubectl patch platformagent platform-agent -n kubeagents-system \
     --type=merge -p '{"metadata":{"finalizers":null}}'
   ```

   **Note:** the `kubeagents.x-k8s.io/finalizer` finalizer is what deletes the agent's **cluster-scoped** RBAC — a ClusterRole and a ClusterRoleBinding that Kubernetes cannot garbage-collect via owner references. Bypassing it leaves these behind, so delete them manually (names are derived from the CR's namespace and name):

   ```bash
   kubectl delete clusterrolebinding \
     kubeagents:minimal:kubeagents-system:platform-agent --ignore-not-found=true
   kubectl delete clusterrole \
     kubeagents:minimal:kubeagents-system:platform-agent --ignore-not-found=true
   ```

3. **Delete the agent secrets.**

   ```bash
   kubectl delete secret platform-agent-secrets github-app-credentials \
     -n kubeagents-system --ignore-not-found=true
   ```

   (`github-app-credentials` only exists if you configured the GitHub integration.)

4. **Remove the workspace** — delete the `agents/platform` directory from your harness workspace if you installed it there.

Once the CR is gone, the operator's finalizer first removes the cluster-scoped RBAC (the ClusterRole and ClusterRoleBinding above), then Kubernetes garbage-collects the namespaced resources it owns — the agent's Deployment, Service, ServiceAccount, PersistentVolumeClaims, and ConfigMaps.

## Full teardown

```bash
./uninstall.sh
```

`terraform` must be on your `PATH` — it is the teardown engine as much as the install engine, and unlike `install.sh` this script installs nothing on your behalf. With an install to tear down and no terraform it refuses with exit 1, so a machine that only ever had the installer's auto-install cannot tear the install down. The check runs after the state lookup below, so a target with no state exits 3 without it. See [Prerequisites](/kube-agents/install/prerequisites/).

`uninstall.sh` runs the install engine in reverse: it finds the install's Terraform state in GCS (bucket `<project>-kube-agents-tfstate`, prefix `kube-agents/<cluster>` — derived from the install coordinates, so a fresh clone works), regenerates `terraform.tfvars`, and drives `terraform destroy` through the composition's [`lifecycle.sh destroy`](https://github.com/gke-labs/kube-agents/blob/main/terraform/examples/full-install/lifecycle.sh). Pass `--project-id`, `--cluster-name`, and `--region` to name the target explicitly; otherwise they come from the saved `vars.sh`.

Four things in the stack are not symmetric — destroying them is not the inverse of applying them — and `lifecycle.sh destroy` handles each one before `terraform destroy` runs:

| Asymmetry                                                                                                              | What `lifecycle.sh destroy` does                                                                                           |
| ---------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Cloud KMS key rings and keys can never be deleted, and destroying the key resource schedules its versions' destruction | Forgets them from Terraform state so they stay usable in GCP; the next `lifecycle.sh apply` adopts them back automatically |
| The `PlatformAgent` CR carries a finalizer only the operator can clear                                                 | Deletes the CR up front and waits, force-clearing the finalizer (and removing the orphaned cluster-scoped RBAC) if wedged  |
| A GKE `BackupPlan` cannot be deleted while it still owns backups                                                       | Permanently deletes every backup the plan owns first                                                                       |
| The cluster's `deletion_protection = true` cannot be overridden by a destroy alone                                     | Applies it as `false` first, then destroys                                                                                 |

These steps are irreversible and run **before** Terraform's own prompt, which is why the script asks for one confirmation up front (`--non-interactive` skips it).

**No Terraform state anywhere.** With no state in the GCS bucket and none locally, the uninstaller exits **3** without touching anything and says so. Either nothing is installed against those coordinates — the ordinary answer on a clean project, and not a failure — or the install was created by a pre-Terraform release, which this uninstaller cannot take apart. For the second case, re-run with `--source-ref=<the release that installed it>` — the uninstaller fetches that release and hands over to its own `uninstall.sh`, so the code that made the install is what takes it apart:

```bash
curl -fsSL https://gke-labs.github.io/kube-agents/uninstall.sh | bash -s -- --source-ref=<old release tag>
```

## Where to go next

- [Full-install composition README](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install#teardown-and-re-apply) — the teardown asymmetries in detail, and running `terraform destroy` by hand.
- [Security & IAM](/kube-agents/reference/security-and-iam/) — the GCP service accounts and bindings the teardown removes.
