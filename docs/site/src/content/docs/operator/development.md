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

`make install`, `make uninstall`, and `make deploy` deliberately do not. They apply the manifests exactly as committed, so a deploy ships what is in git rather than whatever the local tree happens to regenerate, and it leaves no modified files behind. Run `make manifests` yourself after changing the API types — CI fails if the committed output is stale.

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

### Building on a private worker pool

Cloud Build runs on the project's default pool (2 vCPU) unless you point it elsewhere. To use a [private pool](https://cloud.google.com/build/docs/private-pools/private-pools-overview) with more CPU, export its full resource name:

```bash
export CLOUD_BUILD_WORKER_POOL=projects/PROJECT/locations/REGION/workerPools/POOL
```

`dev_rebuild_agent.sh` and `hack/ci-deploy.sh` both read this variable and pass `--worker-pool` (plus the pool's region, parsed from the name) to every `gcloud builds submit`. Leave it unset to keep the default pool. The pool must allow public egress, or builds fail pulling base images and packages.

## Integrations (Kustomize)

Integrations have dedicated deploy/undeploy targets:

```bash
make deploy-litellm             # LiteLLM Gateway
make deploy-inference-replay    # inference-replay proxy
make deploy-github              # Minty (GitHub token minter)
```

Each has a matching `undeploy-*` target. These are the same kustomize bases the provisioner uses.

## RBAC Migration & Deprecation Guidelines

When modifying or deprecating RBAC roles/rolebindings in the operator:

1. **Update active role construction:** Update the builder functions (`buildPlatformLocalRole`, `buildMinimalPlatformRole`, etc.) to generate the updated role definitions.
2. **Dynamic legacy role cleanup:** Never leave old roles/rolebindings orphaned on existing clusters. `reconcileRBAC()` dynamically audits all `RoleBinding` objects in the namespace attached to the agent's ServiceAccount and deletes any non-canonical `kubeagents*` bindings.
3. **Sync controller RBAC annotations:** Ensure `// +kubebuilder:rbac` annotations on the reconciler include all permissions that the operator itself needs to grant or clean up, and run `make manifests` to regenerate `config/rbac/role.yaml`.

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
