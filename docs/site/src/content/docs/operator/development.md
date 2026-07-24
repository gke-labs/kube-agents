---
title: Development
description: Build, test, and iterate on the operator locally.
sidebar:
  order: 2
---

The operator is a standard Kubebuilder project. Standard workflow — `make generate`, `make manifests`, `make test`, `make docker-build`, `make deploy`.

Everything below runs from `k8s-operator/`.

## Prerequisites

- Go 1.25+.
- `docker` (or `podman`) for image builds.
- `kubectl` pointed at a target cluster for `make install` / `make deploy`.
- `make` — the entire workflow is Makefile-driven.

## Build

```bash
make generate      # regenerate deepcopy code
make manifests     # regenerate CRDs, ClusterRoles, WebhookConfiguration
make build         # build the manager binary
```

Generated CRDs land in `config/crd/bases/`; RBAC in `config/rbac/`; webhook config in `config/webhook/`.

`make build`, `make run`, and `make test` all run `manifests`, `generate`, `fmt`, and `vet` first, so generated code and manifests stay in sync automatically.

## Test

```bash
make test          # unit + envtest against a locally-fetched envtest binary
```

The envtest binaries are downloaded to `bin/` on first run (`make setup-envtest`).

## Run locally (against a real cluster)

```bash
make install       # install CRDs into the cluster in ~/.kube/config
make run           # run the manager binary out-of-cluster, against the target cluster
```

Kill the process with Ctrl-C. `make uninstall` removes the CRDs.

## Deploy the manager into a cluster

```bash
make docker-build IMG=<your-registry>/kube-agents-operator:dev
make docker-push  IMG=<your-registry>/kube-agents-operator:dev
make deploy        IMG=<your-registry>/kube-agents-operator:dev
```

`make undeploy` removes it.

## Fast agent iteration (dev only)

For local Platform Agent development you don't want to run the full provisioner every time. `make dev-rebuild-agent` shells out to `k8s-operator/scripts/dev/dev_rebuild_agent.sh`:

```bash
make dev-rebuild-agent ARGS="platform"
```

This builds the Platform Agent image, pushes to Artifact Registry, and restarts the Deployment. First run creates a dev Artifact Registry repo; clean it up later with `make gcp-teardown-dev-artifact-registry`.

## Integrations (Kustomize)

Integrations have dedicated deploy/undeploy targets:

```bash
make deploy-litellm             # LiteLLM Gateway
make deploy-inference-replay    # inference-replay proxy
make deploy-github              # Minty (GitHub token minter)
```

Each has a matching `undeploy-*` target. These are the same kustomize bases the provisioner uses.

## Formatting

```bash
make prettier-check    # verify Markdown/YAML formatting (**/*.{md,yaml,yml})
make prettier-write    # apply formatting
```

Prettier is enforced in CI ([`.github/workflows/prettier.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/prettier.yml)).

## CI

Relevant workflows:

- [`k8s-operator-test.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/k8s-operator-test.yml) — runs `make test`.
- [`docker-publish-k8s-operator.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-k8s-operator.yml) — publishes the manager image.
- [`e2e-gchat-test.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/e2e-gchat-test.yml) — end-to-end Google Chat test.
