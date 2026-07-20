---
title: Helm and Kind
description: A Helm chart for the Platform Agent and a Kind-based local install are proposed but not yet merged.
---

A Helm chart at `deploy/helm/platform-agent/` and a local development flow at `local-dev/setup-kind.sh` are proposed in [PR #353](https://github.com/gke-labs/kube-agents/pull/353) but not yet merged.

## Track progress

- [PR #353 — README overhaul + Helm + Kind](https://github.com/gke-labs/kube-agents/pull/353) — the umbrella change adding the chart and the Kind script.
- Watch [`deploy/`](https://github.com/gke-labs/kube-agents/tree/main/deploy) and [`local-dev/`](https://github.com/gke-labs/kube-agents/tree/main/local-dev) for the artifacts once they land.

## Install today

Until those merge, use:

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — `./provision.sh` bootstraps GKE + operator + agent.
- [Manual install](/kube-agents/install/manual/) — for other Hermes-compatible harnesses.

This page will be rewritten when the chart and Kind flow are in `main`.
