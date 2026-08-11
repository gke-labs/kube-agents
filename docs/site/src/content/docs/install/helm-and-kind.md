---
title: Helm and Kind
description: A canonical GKE-oriented Helm chart and companion Terraform modules live in main. Kind local install is not supported.
---

- **Helm chart & Terraform modules.** A canonical GKE-oriented Helm chart (`charts/kube-agents/`) and companion Terraform modules (`terraform/modules/`) live in `main` for versioned OCI and IaC deployments. Published artifacts (the OCI chart and `?ref=vX.Y.Z` module tags) only exist from the first `vX.Y.Z` release tag onward — until then, install from a repository checkout or use the [Quick start](/kube-agents/install/quickstart-gke/). A checkout install must override both image tags — the [chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md) is canonical for the exact `--set` flags and the `appVersion`-placeholder reason.
- **No Kind or local-cluster path.** There is no `kind` workflow in the repository, and no scripted installer outside `k8s-operator/scripts/`. You need a real GKE cluster.

## Install today

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` bootstraps GKE, the operator, and the agent.
- [Helm & Terraform (GitOps)](/kube-agents/deploy/release-versioning/) — deploy via versioned OCI Helm charts and SemVer Terraform modules. For a one-command IaC install (cluster + IAM + chart in a single `terraform apply`), see [`terraform/examples/full-install/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install).
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

Check the repository's [`charts/`](https://github.com/gke-labs/kube-agents/tree/main/charts) tree for canonical Helm charts and [`terraform/modules/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules) for infrastructure modules.
