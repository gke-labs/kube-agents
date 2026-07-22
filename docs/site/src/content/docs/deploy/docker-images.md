---
title: Docker images
description: The images shipped from this repo and how their tags are managed.
sidebar:
  order: 2
---

Images published by this repo, plus the base Hermes image (pulled from Docker Hub).

## Published images

Published on push to `main` via GitHub Actions workflows.

### `platform-agent`

The Platform Agent Deployment image. Built from [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile) on top of `nousresearch/hermes-agent` with the Platform Agent workspace, GCP tools, and `kubectl` layered in.

- **Registry**: `ghcr.io/gke-labs/kube-agents/platform-agent`
- **Published by**: [`.github/workflows/docker-publish-ghcr.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-ghcr.yml)
- **Also to GAR**: [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml)

The Dockerfile installs system tooling the Platform Agent needs to inspect and remediate clusters:

- `google-cloud-cli` + `google-cloud-cli-gke-gcloud-auth-plugin`
- `kubectl`
- Standard debugging tools: `curl`, `jq`, `dnsutils`, `iputils-ping`, `patch`, `git`

### `k8s-operator`

The Kubebuilder-generated operator manager image.

- **Registry**: `ghcr.io/gke-labs/kube-agents/k8s-operator`
- **Published by**: [`.github/workflows/docker-publish-k8s-operator.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-k8s-operator.yml)
- **Build**: `k8s-operator/Dockerfile` (`make docker-build IMG=...`)

## Base image pin

The Hermes base image tag is pinned in [`tags.env`](https://github.com/gke-labs/kube-agents/blob/main/tags.env) at the repo root:

```bash
HERMES_AGENT_TAG=v2026.7.7.2@sha256:9c841866021c54c4596849f6135717e8a4d52ba510b7f52c50aef1de1a283973
```

Docker builds source `tags.env` via the `HERMES_AGENT_TAG` build arg:

```dockerfile
ARG HERMES_AGENT_TAG
FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG} AS agent-base
```

Bumping Hermes = updating `tags.env` (a single-line change) and rebuilding.

## Local builds

For development iteration, `make dev-rebuild-agent` (from `k8s-operator/`) is the fast path — it builds and pushes to a dev Artifact Registry repo and restarts the Deployment. See [Development](/kube-agents/operator/development/#fast-agent-iteration-dev-only).

## CI

Docker builds are validated on every PR via [`.github/workflows/docker-build.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-build.yml) — the image builds but doesn't publish. Publication happens only on push to `main`.
