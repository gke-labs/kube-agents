---
title: Provisioning scripts
description: The modular sub-scripts that make up `./provision.sh` and their teardown counterparts.
sidebar:
  order: 3
---

The provisioner in [`k8s-operator/scripts/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/scripts) is composed of one orchestrator (`provision.sh`) and a set of idempotent step scripts (plus their teardown mirrors and an optional gVisor step). This page catalogs each step; the [quick start](/kube-agents/install/quickstart-gke/) shows the operator's-eye view.

Shared state — cluster name, region, project ID, model provider, GitOps repo — lives in `k8s-operator/scripts/vars.sh` (git-ignored). Each script sources `common.sh`, which loads that state and provides the shared helpers (prompting, retries, step runner); missing values prompt the user and get appended to `vars.sh`.

## Orchestrators

- **[`provision.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/provision.sh)** — runs the numbered steps in order (skipping opt-in steps unless enabled).
- **[`teardown.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/teardown.sh)** — runs the steps in reverse.

Both accept `--dry-run` to print planned actions without applying them.

## Provisioning steps

The numbered steps run in order; opt-in steps no-op unless their flag is set. Rather than
restate them here, the catalogue lives next to the code so it cannot drift from it:

**[`k8s-operator/scripts/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/README.md)**
— what each `provision_NN_*.sh` does, which variables it reads, and which are opt-in.

For the current target list, run `cd k8s-operator && make help`.

## Teardown steps

Mirror the provisioning steps in reverse. Full table on [Uninstall](/kube-agents/install/uninstall/).

## Utilities

- **[`update_cluster_name.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/update_cluster_name.sh)** — Patches the target GKE cluster name into the deployed `platform-agent` `PlatformAgent` spec (`spec.harness.clusterName`), triggering the operator to reconcile.

## Development helpers (`dev/`)

- **[`dev/dev_rebuild_agent.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/dev_rebuild_agent.sh)** — Fast local iteration on the Platform Agent workspace image.
- **[`dev/setup-gcp-github-wif.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/setup-gcp-github-wif.sh)** — Sets up GCP Workload Identity Federation (pool + OIDC provider + service account) so GitHub Actions can deploy to the project keylessly. Requires `PROJECT_ID`, `SA_NAME`, and `GITHUB_REPO` env vars.
- **[`dev/teardown_dev_01_gcp_artifact_registry.sh`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/scripts/dev/teardown_dev_01_gcp_artifact_registry.sh)** — Deletes the dev-only Artifact Registry created by `dev_rebuild_agent.sh`.

## Common gotchas

- **cert-manager.** Step 03 auto-installs cert-manager (v1.14.4) unless a `cert-manager-webhook` Deployment is already available in the `cert-manager` namespace, so you normally don't need to install it yourself. All steps are idempotent, so you can safely re-run.
- **`vars.sh` collision.** If you rerun the provisioner against a different project without wiping `vars.sh`, you'll target the previous project. Delete `vars.sh` to reset.
- **Autopilot leader election.** On GKE Autopilot, step 03 installs cert-manager with leader election disabled automatically (kube-system restrictions) — see [Prerequisites](/kube-agents/install/prerequisites/#gke-autopilot-install).
