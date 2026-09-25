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

`uninstall.sh` runs the install engine in reverse: it finds the install's Terraform state in GCS (bucket `<project>-kube-agents-tfstate`, prefix `kube-agents/<cluster>` — derived from the install coordinates, so a fresh clone works), regenerates `terraform.tfvars`, and drives `terraform destroy` through the composition's [`lifecycle.sh destroy`](https://github.com/gke-labs/kube-agents/blob/main/terraform/examples/full-install/lifecycle.sh). Your `install.env` is left in place -- it is your file, not one the installer generated.

### Naming the install to tear down

Pass `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region` to name the target explicitly. Otherwise the coordinates come from `install.env`, looked for in the same order the upgrade uses: `KUBE_AGENTS_INSTALL_ENV`, then the checkout the script runs from, then the directory you run it from, then — when run from outside a checkout, such as the piped release one-liner — the install checkout the installer left in `$HOME/kube-agents`. A file reached only through that `$HOME` fallback, on a run given all three of `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region`, is read only when it records all three and they match; otherwise it is skipped with a warning and the teardown runs on the flags, rather than refusing because a file you did not name belongs to another install. When `--source-ref` hands over to an older release's `uninstall.sh`, the wrapper resolves an `install.env` in that same order and forwards those coordinates in the target release's flag dialect; a file found only by falling back to `$HOME/kube-agents/install.env` on a non-checkout run (which was written by a `>= 0.4.0` install, since pre-Terraform releases wrote no `install.env`) is read only when all three of `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region` are given on the command line and match it. Otherwise that `$HOME` fallback is skipped with a warning and only the command-line flags you gave (or nothing, on a flagless run) are forwarded, while a handover that finds no `install.env` anywhere says so and forwards only the flags given. Whichever way the coordinates were resolved, before handing over the wrapper names each of `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region` it is not forwarding — including one a loaded `install.env` does not record — because the older release falls back to its own default for it.

`KUBE_AGENTS_INSTALL_ENV` is the one part of that order that is not a fall-through: a value naming a file that is not there stops the run by the path you gave, rather than being searched past. Fix the path or unset the variable.

As in the upgrade, when both a flag and a loaded `install.env` are present and they disagree, a real teardown refuses rather than reading one install's state backend and `terraform.tfvars` settings while naming another, and `--dry-run` warns and continues.

Beyond that, and unlike the upgrade, a teardown does not refuse without configuration: naming the three coordinates is enough, and `./uninstall.sh` inside a checkout of a default install works on defaults alone. It is also not stopped by the memory question the upgrade can be stopped by — a teardown removes the store either way, and an install has to keep a working way to remove itself. What it will not do is present a default as the install's own — when the cluster or region falls back to the built-in default, or the project comes from `gcloud`'s active configuration (which on a GCE instance is the project the machine itself lives in), the run says which value it guessed before the confirmation prompt.

Four things in the stack are not symmetric — destroying them is not the inverse of applying them — and `lifecycle.sh destroy` handles each one before `terraform destroy` runs:

| Asymmetry                                                                                                              | What `lifecycle.sh destroy` does                                                                                           |
| ---------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Cloud KMS key rings and keys can never be deleted, and destroying the key resource schedules its versions' destruction | Forgets them from Terraform state so they stay usable in GCP; the next `lifecycle.sh apply` adopts them back automatically |
| The `PlatformAgent` CR carries a finalizer only the operator can clear                                                 | Deletes the CR up front and waits, force-clearing the finalizer (and removing the orphaned cluster-scoped RBAC) if wedged  |
| A GKE `BackupPlan` cannot be deleted while it still owns backups                                                       | Permanently deletes every backup the plan owns first                                                                       |
| The cluster's `deletion_protection = true` cannot be overridden by a destroy alone                                     | Applies it as `false` first, then destroys                                                                                 |

These steps are irreversible and run **before** Terraform's own prompt, which is why the script asks for one confirmation up front (`--non-interactive` skips it).

**No Terraform state anywhere.** With no state in the GCS bucket and none locally, the uninstaller exits **3** without touching anything and says so. Either nothing is installed against those coordinates — the ordinary answer on a clean project, and not a failure — or the install was created by a pre-Terraform release, which this uninstaller cannot take apart. For the second case, re-run with `--source-ref=<the release that installed it>` and the install's coordinates — the uninstaller fetches that release and hands over to its own `uninstall.sh`, so the code that made the install is what takes it apart:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/uninstall.sh | bash -s -- \
  --source-ref=<old release tag> \
  --gcp-project-id="my-gcp-project" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1"
```

Substitute `<RELEASE_VERSION>` with a release tag from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases). A release copy of the script tears an install down with its own release's engine, so the version you fetch is the version that runs — unless `--source-ref` names another one, as it does above, where the point is to run the engine of the release that built the install.

## Where to go next

- [Upgrade](/kube-agents/install/upgrade/) — moving an install to a newer release instead of removing it.
- [Full-install composition README](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install#teardown-and-re-apply) — the teardown asymmetries in detail, and running `terraform destroy` by hand.
- [Security & IAM](/kube-agents/reference/security-and-iam/) — the GCP service accounts and bindings the teardown removes.
