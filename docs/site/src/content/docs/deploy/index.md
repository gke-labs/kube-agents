---
title: Deploy overview
description: Docker, Kustomize, Minty, and telemetry — what actually gets deployed.
sidebar:
  order: 0
---

Everything the [provisioner](/kube-agents/operator/provisioning-scripts/) applies is standard Kubernetes: containers built via Docker, layered with Kustomize, and wired into the cluster's telemetry stack.

Pages in this section:

- [**Kustomize**](/kube-agents/deploy/kustomize/) — what lives in `deploy/kustomize/`.
- [**Docker images**](/kube-agents/deploy/docker-images/) — the container images and their tags.
- [**Token minter (Minty)**](/kube-agents/deploy/token-minter/) — how the GitHub App identity is brokered.
- [**Telemetry**](/kube-agents/deploy/telemetry/) — OpenTelemetry + Prometheus + Cloud Logging.
