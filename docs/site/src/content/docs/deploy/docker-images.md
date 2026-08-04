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

The agent Deployment image. Built from the `platform` target of [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile) on top of `nousresearch/hermes-agent`. It lays down the Chat Agent workspace at `/opt/defaults` (the `default` profile) plus two profile templates: the Platform Agent at `/opt/platform-template`, scaffolded into the `platform` profile at startup by the entrypoint, and the Cluster Agent at `/opt/cluster-template`, scaffolded into per-cluster `cluster-*` profiles at runtime by `cluster_agent_profile.py`.

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

## Private / custom registry

Installs that cannot pull from public registries (behind-the-firewall clusters) can mirror the
images into their own registry and point every layer of the install at it. Mirror the four
published images above, plus the `fluent/fluent-bit` logging sidecar the operator injects into
agent pods (version pinned in `k8s-operator/internal/controller/manifest_helpers.go`).

Not every image in the install path has an override yet. The following are pulled from their
upstream registries regardless of the settings below:

- **cert-manager** — `provision_03` applies the upstream cert-manager manifest, which pulls
  `quay.io/jetstack/*` images. Behind a firewall this step fails before the operator deploys;
  mirror the manifest and images manually.
- **LiteLLM** — the optional LiteLLM integration deploys `ghcr.io/berriai/litellm`
  (`k8s-operator/config/integrations/litellm/`).
- **GitHub token minter** — the optional GitHub integration deploys the
  `github-token-minter-server` image from `us-docker.pkg.dev`
  (`k8s-operator/config/integrations/github/`).

The registry is configurable at three layers, from broadest to most specific:

1. **Provisioning scripts** — export `REGISTRY_PREFIX` (e.g.
   `registry.example.com/kube-agents`) before the first `provision_*.sh` run. It replaces
   `ghcr.io/gke-labs/kube-agents` as the default for the operator image (`provision_03`), the
   agent image (`provision_08`), and the replay proxy (`provision_11`), and is persisted to the
   state file (`vars.sh`) like every other knob, so re-runs reuse it. The individual
   `OPERATOR_IMAGE`, `AGENT_IMAGE`, and `REPLAY_IMAGE` variables still override the prefix.
   `provision_03` also sets `PLATFORM_AGENT_IMAGE` on the operator Deployment whenever an
   explicit `PLATFORM_AGENT_IMAGE`, a custom `AGENT_IMAGE`, or a custom `REGISTRY_PREFIX` is in
   effect, so CRs that omit `spec.deployment.image` follow the mirror too. Changing the
   registry _after_ a first run requires editing the saved `REGISTRY_PREFIX` and `*_IMAGE`
   values in `vars.sh` (saved state wins over a new export); the scripts warn when an export
   is ignored or a saved image no longer matches the effective prefix.
2. **Operator environment** — the controller manager reads three optional env vars (see the
   commented block in `k8s-operator/config/manager/manager.yaml`):
   - `PLATFORM_AGENT_IMAGE` — default agent image when a `PlatformAgent` CR omits
     `spec.deployment.image`.
   - `CREDENTIAL_PROXY_IMAGE` — explicit credential-proxy sidecar image. When unset, the proxy
     image is derived from the agent image (same registry and tag, image name `platform-agent`
     mapped to `credential-proxy`), so mirrors that keep the image names only need
     `PLATFORM_AGENT_IMAGE`.
   - `FLUENT_BIT_IMAGE` — replaces the Docker Hub `fluent/fluent-bit` sidecar image.
3. **Per-agent CR** — `spec.deployment.image` / `spec.deployment.tag` on a `PlatformAgent`
   override the defaults above for that agent's containers, and the credential-proxy image is
   derived from them — unless an explicit `CREDENTIAL_PROXY_IMAGE` is set, which always wins
   for the sidecar. The fluent-bit sidecar has no CR-level equivalent; `FLUENT_BIT_IMAGE` is
   its only override. See the
   [PlatformAgent CRD reference](/kube-agents/operator/platformagent-crd/).

If the private registry requires authentication, configure node-level pull credentials (or
mirror through a pull-through cache); the operator does not currently manage `imagePullSecrets`.

## Local builds

For development iteration, `make dev-rebuild-agent` (from `k8s-operator/`) is the fast path — it builds and pushes to a dev Artifact Registry repo and restarts the Deployment. See [Development](/kube-agents/operator/development/#fast-agent-iteration-dev-only).

## CI

Docker builds are validated on every PR via [`.github/workflows/docker-build.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-build.yml) — the image builds but doesn't publish. Publication happens only on push to `main`.
