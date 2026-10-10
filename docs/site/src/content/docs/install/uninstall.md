---
title: Uninstall
description: Remove an install, its provisioned GCP resources, and what a teardown leaves behind.
---

`uninstall.sh` removes an install made by `install.sh` or the install engine: the Helm releases, the GCP resources the engine created, and the cluster when the install created it. [What a teardown leaves](#what-a-teardown-leaves) lists what survives it. Installs made another way are under [Other installs](#other-installs).

On an install made by `install.sh` the `PlatformAgent` resource and its credentials Secret (`platform-agent-secrets`) belong to the Helm release, so deleting either by hand is undone by a later upgrade that re-renders the release, and leaves the install out of step with its state until then. To remove the agent, tear the install down.

## Full teardown

```bash
./uninstall.sh
```

`terraform` must be on your `PATH` — it is the teardown engine as much as the install engine, and unlike `install.sh` this script installs nothing on your behalf. On an install made onto a cluster it did not create, put `helm` on your `PATH` too: without it the teardown warns and leaves the Helm releases to Terraform's Helm provider (see the table below). With an install to tear down and no terraform it refuses with exit 1, so a machine that only ever had the installer's auto-install cannot tear the install down. The check runs after the state lookup below, so a target with no state exits 3 without it. See [Prerequisites](/kube-agents/install/prerequisites/).

`uninstall.sh` runs the install engine in reverse: it finds the install's Terraform state in GCS (bucket `<project>-kube-agents-tfstate`, prefix `kube-agents/<cluster>` — derived from the install coordinates, so a fresh clone works), regenerates `terraform.tfvars`, and drives `terraform destroy` through the composition's [`lifecycle.sh destroy`](https://github.com/gke-labs/kube-agents/blob/main/terraform/examples/full-install/lifecycle.sh). Your `install.env` is left in place -- it is your file, not one the installer generated.

### Naming the install to tear down

Pass `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region` to name the target explicitly. Otherwise the coordinates come from `install.env`, looked for in the same order the upgrade uses: `KUBE_AGENTS_INSTALL_ENV`, then the checkout the script runs from, then the directory you run it from, then — when run from outside a checkout, such as the piped release one-liner — the install checkout the installer left in `$HOME/kube-agents`. A file reached only through that `$HOME` fallback, on a run given all three of `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region`, is read only when it records all three and they match; otherwise it is skipped with a warning and the teardown runs on the flags, rather than refusing because a file you did not name belongs to another install. When `--source-ref` hands over to an older release's `uninstall.sh`, the wrapper resolves an `install.env` in that same order and forwards those coordinates in the target release's flag dialect; a file found only by falling back to `$HOME/kube-agents/install.env` on a non-checkout run (which was written by a `>= 0.4.0` install, since pre-Terraform releases wrote no `install.env`) is read only when all three of `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region` are given on the command line and match it. Otherwise that `$HOME` fallback is skipped with a warning and only the command-line flags you gave (or nothing, on a flagless run) are forwarded, while a handover that finds no `install.env` anywhere says so and forwards only the flags given. Whichever way the coordinates were resolved, before handing over the wrapper names each of `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region` it is not forwarding — including one a loaded `install.env` does not record — because the older release falls back to its own default for it.

`KUBE_AGENTS_INSTALL_ENV` is the one part of that order that is not a fall-through: a value naming a file that is not there stops the run by the path you gave, rather than being searched past. Fix the path or unset the variable.

As in the upgrade, when both a flag and a loaded `install.env` are present and they disagree, a real teardown refuses rather than reading one install's state backend and `terraform.tfvars` settings while naming another, and `--dry-run` warns and continues.

Beyond that, and unlike the upgrade, a teardown does not refuse without configuration: naming the three coordinates is enough, and `./uninstall.sh` inside a checkout of a default install works on defaults alone. It is also not stopped by the memory question the upgrade can be stopped by — a teardown removes the store either way, and an install has to keep a working way to remove itself. What it will not do is present a default as the install's own — when the cluster or region falls back to the built-in default, or the project comes from `gcloud`'s active configuration (which on a GCE instance is the project the machine itself lives in), the run says which value it guessed before the confirmation prompt.

Several things in the stack are not symmetric — destroying them is not the inverse of applying them — and `lifecycle.sh destroy` handles each one before `terraform destroy` runs:

| Asymmetry                                                                                                              | What `lifecycle.sh destroy` does                                                                                                                                                                                                                                             |
| ---------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Cloud KMS key rings and keys can never be deleted, and destroying the key resource schedules its versions' destruction | Forgets them from Terraform state so they stay usable in GCP; the next `lifecycle.sh apply` adopts them back automatically                                                                                                                                                   |
| The `PlatformAgent` CR carries a finalizer only the operator can clear                                                 | Deletes the CR up front and waits, force-clearing the finalizer (and removing the orphaned cluster-scoped RBAC) if wedged                                                                                                                                                    |
| A GKE `BackupPlan` cannot be deleted while it still owns backups                                                       | Permanently deletes every backup the plan owns first                                                                                                                                                                                                                         |
| The cluster's `deletion_protection = true` cannot be overridden by a destroy alone                                     | Applies it as `false` first, then destroys                                                                                                                                                                                                                                   |
| Terraform's Helm provider reports a release destroyed when it cannot reach the cluster                                 | On a cluster the install did not create, uninstalls the Helm releases the install made with the `helm` CLI first, and stops if `helm` cannot list or uninstall them. Skipped with a warning when `helm` is not on your `PATH` or the cluster's credentials cannot be fetched |

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

## Other installs

An install made with Helm alone, such as the published chart or a GitOps sync, has no Terraform state, so `uninstall.sh` exits 3 and removes nothing. Remove the release (or the GitOps application that owns it). `helm uninstall` runs the chart's pre-delete hook, which deletes the `PlatformAgent` and waits for its finalizer while the operator is still running. A tool that skips Helm hooks, or a release with `platformAgent.cleanupHook.enabled=false`, needs that done by hand first:

```bash
kubectl delete platformagent platform-agent -n kubeagents-system --wait --timeout=180s
helm uninstall <release> -n kubeagents-system
```

The finalizer removes the agent's cluster-scoped RBAC, and Kubernetes garbage-collects the namespaced objects the resource owns. The namespace, the CRDs and the shell sandbox's volumes stay, as below. So does `platform-agent-secrets` when the release did not create it (`platformAgent.credentials.create=false`, the chart default); delete it with the namespace.

An install whose `PlatformAgent` was applied with `kubectl` ([Method 2 in INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#method-2-manual-kubernetes-cluster-deployment)) comes apart the same way: delete the `PlatformAgent` and wait, then remove the operator (`make undeploy` and `make uninstall` in `k8s-operator/`). Removing the operator first strands the resource on its finalizer.

A workspace registered by hand in another Hermes harness ([Manual install](/kube-agents/install/manual/)) is removed in that harness: unregister the `platform` agent, and the `chat` front door if you registered it, remove any scheduled jobs you wired by hand, and delete the copied `agents/platform` and `agents/chat` directories.

## What a teardown leaves

On GCP the teardown keeps the Cloud KMS key rings and keys (GCP cannot delete them; the next install adopts them) and the Terraform state bucket, `gs://<project>-kube-agents-tfstate`. Delete the bucket yourself if the project will not host kube-agents again:

```bash
gcloud storage rm -r gs://<project>-kube-agents-tfstate
```

The service accounts, IAM bindings, the Google Chat Pub/Sub topic and subscription, and the rest of the resources in the Terraform state are destroyed, except the project's APIs, which stay enabled. On a cluster the install did not create, the cluster-level settings it turned on stay: CMEK database encryption, the Workload Identity pool, node pools moved to the GKE metadata server, and Calico NetworkPolicy. Your `install.env` stays too, and so do the Slack app and the Google Chat app, which you configured outside the install.

A cluster the install created is deleted with everything in it. On a cluster it did not create, Helm removes its releases and leaves the namespaces it installed into, `kubeagents-system` and, when the install brought cert-manager, `cert-manager`, along with the kube-agents CRDs, since Helm never deletes a chart's `crds/`. Anything in `kubeagents-system` that Helm did not create is still there: the shell sandbox's two volumes, `data-platform-agent-shell-0` and `sshd-platform-agent-shell-0`, which its StatefulSet keeps on purpose; a registry pull Secret you created, the GitLab token Secret the installer creates after the apply, and, on the unsupported `spec.mode: next`, the gateway's `a2a-slack-principal-map` and `discord-bot` Secrets if you created them. Once the teardown has finished, remove them with the namespace; delete the CRDs only when no other install on the cluster uses them, since that deletes every `PlatformAgent` on it:

```bash
kubectl delete namespace kubeagents-system
kubectl delete crd platformagents.kubeagents.x-k8s.io agentplugins.kubeagents.x-k8s.io agentprofiles.kubeagents.x-k8s.io
```

### What `spec.mode: next` leaves behind

The objects the operator creates for `spec.mode: next` go with the `PlatformAgent`: its finalizer deletes the JetStream volume, `data-platform-agent-a2a-nats-0`, and the rest, the bus credentials Secret `platform-agent-a2a-nats-creds` among them, are owned by the resource and garbage-collected. Flipping an install back to `today` without removing it keeps that volume and that Secret, so a later flip to `next` finds the bus where it left it. The `a2a-slack-principal-map` Secret, if you created it, is yours and stays either way. To drop them on an install that stays on `today`:

```bash
kubectl delete pvc data-platform-agent-a2a-nats-0 -n kubeagents-system
kubectl delete secret platform-agent-a2a-nats-creds a2a-slack-principal-map -n kubeagents-system --ignore-not-found=true
```

## If deleting the `PlatformAgent` hangs

The teardown deletes the `PlatformAgent` first and clears its finalizer itself if the operator does not. Deleting it by hand while the operator or its webhook is offline can hang on the `kubeagents.x-k8s.io/finalizer` finalizer. Clear it:

```bash
kubectl patch platformagent platform-agent -n kubeagents-system \
  --type=merge -p '{"metadata":{"finalizers":null}}'
```

The finalizer is what deletes the agent's cluster-scoped RBAC and, under `spec.mode: next`, the JetStream volume, so a cleared finalizer leaves them behind. A teardown that clears it also deletes the `kubeagents:minimal:…` ClusterRole and binding, but not the others or the volume. Every ClusterRole and ClusterRoleBinding the operator made for the agent ends in the resource's namespace and name: `kubeagents:minimal:kubeagents-system:platform-agent` and `kubeagents:tokenreview:kubeagents-system:platform-agent`, and under `next` the callout's binding `kubeagents:a2a-callout-tokenreview:kubeagents-system:platform-agent`. This removes them all:

```bash
for o in $(kubectl get clusterrole,clusterrolebinding -o name | grep ':kubeagents-system:platform-agent$'); do
  kubectl delete "$o"
done
kubectl delete pvc data-platform-agent-a2a-nats-0 -n kubeagents-system --ignore-not-found=true
```

## Where to go next

- [Upgrade](/kube-agents/install/upgrade/) — moving an install to a newer release instead of removing it.
- [Full-install composition README](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install#teardown-and-re-apply) — the teardown asymmetries in detail, and running `terraform destroy` by hand.
- [Security & IAM](/kube-agents/reference/security-and-iam/) — the GCP service accounts and bindings the teardown removes.
