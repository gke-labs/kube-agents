---
title: Helm and Kind
description: A Helm chart for kube-agents is proposed but not yet merged; a Kind-based local install does not exist yet.
---

A Helm chart for kube-agents is proposed in [PR #230](https://github.com/gke-labs/kube-agents/pull/230) ("k8s-operator IaC deployment with Terraform and Helm") but not yet merged. There is **no** Kind (or other local, non-GKE) install path in the repo or any open PR today.

## Helm (proposed)

- Chart path in the PR: `k8s-operator/deploy/helm/kube-agents/` (`Chart.yaml`, `values.yaml`, and templates for the operator, platform agent, LiteLLM, GitHub minter, RBAC, and secrets).
- The chart is GKE/GCP-oriented: `values.yaml` expects `projectId`, `clusterName`, `clusterLocation`, and Workload Identity GSA emails, and it ships alongside a Terraform module (`k8s-operator/deploy/terraform/`) that populates those values. It is not a local/offline chart.
- Operator and agent images default to `ghcr.io/gke-labs/kube-agents/k8s-operator` and `ghcr.io/gke-labs/kube-agents/platform-agent`.

## Kind / local clusters

No Kind script or local-cluster flow exists in the codebase yet. A scripted installer, [`scripts/quick-install.sh`](https://github.com/gke-labs/kube-agents/pull/353) (proposed in [PR #353](https://github.com/gke-labs/kube-agents/pull/353)), targets GKE Autopilot only — it can create or reuse a GKE cluster but does not support Kind.

## Track progress

- [PR #230 — k8s-operator IaC deployment with Terraform and Helm](https://github.com/gke-labs/kube-agents/pull/230) — adds the Helm chart and Terraform module.
- Watch [`k8s-operator/deploy/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator/deploy) for the chart once it lands.

## Install today

Until the chart merges, use:

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` bootstraps GKE + operator + agent.
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

This page will be rewritten when the Helm chart is in `main`.
