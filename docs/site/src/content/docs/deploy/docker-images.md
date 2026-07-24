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

The Platform Agent Deployment image. Built from the `platform` target of [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile) on top of `nousresearch/hermes-agent` with the Platform Agent workspace, GCP tools, and `kubectl` layered in.

- **Registry**: `ghcr.io/gke-labs/kube-agents/platform-agent`
- **Published by**: [`.github/workflows/docker-publish-ghcr.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-ghcr.yml)
- **Also to GAR**: [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml)

The Dockerfile installs system tooling the Platform Agent needs to inspect and remediate clusters:

- `google-cloud-cli` + `google-cloud-cli-gke-gcloud-auth-plugin`
- `kubectl`
- `gh` (GitHub CLI), `yq`, `k9s`, `helm`
- Standard debugging tools: `curl`, `jq`, `dnsutils`, `iputils-ping`, `patch`, `git`, `wget`, `nano`, `vim`

It also builds the `k8s-event-watcher` binary from `k8s-operator/cmd/k8s-event-watcher/` in a Go builder stage and copies it into the image.

### `credential-proxy`

The Platform Agent image plus the Envoy-based credential proxy sidecar runtime. Built from the `credential-proxy` target of the same [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile) (it extends the `platform` target with the `envoy` binary and credential-proxy scripts).

- **Registry**: `ghcr.io/gke-labs/kube-agents/credential-proxy`
- **Published by**: [`docker-publish-ghcr.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-ghcr.yml) and [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml)

### `replay-proxy`

The inference replay proxy used for record/replay of model traffic. Built from [`examples/inference-replay/replay-proxy/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/examples/inference-replay/replay-proxy/Dockerfile).

- **Registry**: `ghcr.io/gke-labs/kube-agents/replay-proxy`
- **Published by**: [`docker-publish-ghcr.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-ghcr.yml) and [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml)

### `k8s-operator`

The Kubebuilder-generated operator manager image.

- **Registry**: `ghcr.io/gke-labs/kube-agents/k8s-operator`
- **Published by**: [`.github/workflows/docker-publish-k8s-operator.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-k8s-operator.yml)
- **Build**: `k8s-operator/Dockerfile` (`make docker-build IMG=...`)

## Base image pin

The Hermes base image tag is pinned in [`tags.env`](https://github.com/gke-labs/kube-agents/blob/main/tags.env) at the repo root:

```bash
HERMES_AGENT_TAG=v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40
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
