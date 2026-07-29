---
title: Helm and Kind
description: Neither a Helm chart nor a Kind-based local install ships today. Here is what to use instead.
---

Neither is supported today.

- **No Helm chart.** A GKE-oriented chart and a companion Terraform module have been proposed but are not in `main`. Installation is via the provisioning scripts plus Kustomize.
- **No Kind or local-cluster path.** There is no `kind` workflow in the repository, and no scripted installer outside `k8s-operator/scripts/`. You need a real GKE cluster.

## Install today

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` bootstraps GKE, the operator, and the agent.
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

Check the repository's [`deploy/`](https://github.com/gke-labs/kube-agents/tree/main/deploy) tree for a chart if one has landed since this page was written.
