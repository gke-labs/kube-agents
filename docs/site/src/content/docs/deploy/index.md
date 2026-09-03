---
title: Deploy overview
description: Docker, Kustomize, Minty, telemetry, and the GitOps reconciler — what actually gets deployed.
sidebar:
  order: 0
---

Everything the [installer](/kube-agents/install/quickstart-gke/) applies is standard Kubernetes: containers built via Docker, rendered by the Helm chart (with Kustomize copies for development), and wired into the cluster's telemetry stack.

Pages in this section:

- [**Kustomize**](/kube-agents/deploy/kustomize/) — what lives in `deploy/kustomize/`.
- [**Docker images**](/kube-agents/deploy/docker-images/) — the container images and their tags.
- [**Token minter (Minty)**](/kube-agents/deploy/token-minter/) — how the GitHub App identity is brokered.
- [**Release versioning & promotion**](/kube-agents/deploy/release-versioning/) — how candidate builds are promoted to SemVer releases across Docker images, Helm charts, and Terraform modules.
- [**Telemetry**](/kube-agents/deploy/telemetry/) — OpenTelemetry + Prometheus + Cloud Logging.
- [**GitOps with ArgoCD**](/kube-agents/deploy/gitops-argocd/) — standing up the reconciler that applies what the agent proposes.
- [**CI pool project prerequisites**](/kube-agents/deploy/ci-pool-projects/) — prerequisites and setup for GCP projects in the Boskos evaluation pool.
- [**Reconciling the long-lived environments**](/kube-agents/deploy/environment-reconcile/) — keeping `autopush` and `staging` in step with the composition rather than only re-tagging their images.
