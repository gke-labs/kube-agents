---
title: Helm and Kind
description: A canonical GKE-oriented Helm chart and companion Terraform modules live in main. A kind cluster is a development path, not a supported install.
---

- **Helm chart & Terraform modules.** A canonical GKE-oriented Helm chart (`charts/kube-agents/`) and companion Terraform modules (`terraform/modules/`) live in `main` for versioned OCI and IaC deployments. Published artifacts (the OCI chart and `?ref=X.Y.Z` module tags) exist for every `X.Y.Z` [release](https://github.com/gke-labs/kube-agents/releases); to run unreleased `main`, install from a repository checkout or use the [Quick start](/kube-agents/install/quickstart-gke/). A checkout install must override both image tags — the [chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md) is canonical for the exact `--set` flags and the `appVersion`-placeholder reason.
- **Kind is a development path, not an install.** The installer (`install.sh`) and the Terraform composition it drives both target GKE, and a real deployment needs a GKE cluster. Contributors to the repository can bring the operator, gateway and agent up in a local kind cluster with only a Gemini API key; [`INSTALL.md`](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md) Method 3 in the repository describes that path and what does not work off GKE.

## Install today

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `install.sh` bootstraps GKE, the operator, and the agent.
- [Helm & Terraform (GitOps)](/kube-agents/deploy/release-versioning/) — deploy via versioned OCI Helm charts and SemVer Terraform modules. For a one-command IaC install (cluster + IAM + chart in a single `terraform apply`), see [`terraform/examples/full-install/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install).
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

Check the repository's [`charts/`](https://github.com/gke-labs/kube-agents/tree/main/charts) tree for canonical Helm charts and [`terraform/modules/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules) for infrastructure modules.
