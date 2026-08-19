---
title: Docker images
description: The images shipped from this repo and how their tags are managed.
sidebar:
  order: 2
---

Every image an install pulls or a rebuild needs, and how their tags are managed.

## Image inventory

[`images.json`](https://github.com/gke-labs/kube-agents/blob/main/images.json) at the repository root is the source of truth for this list. It is what `make mirror-images` copies from, what the provisioning scripts resolve their third-party defaults from, and what the table below is generated from — so there is one pin per image, not one per install path.

That cuts both ways: **a version bump edits `images.json` and nothing else.** The manifest,
Dockerfile, or chart value that used to carry a pin now names a variable, so editing the old
location changes nothing. `make images-check` cross-checks the entries that still have a second
copy — LiteLLM and fluent-bit against the chart values, the build-time bases against their
Dockerfile `ARG` defaults — but for `github-token-minter-server`, both Hindsight images, and the
four cert-manager entries this file is the only copy, and nothing else in the tree is left to
disagree with it. Bump the pin here, then run `make images-check` and `make docs-generate`.

<!-- BEGIN GENERATED: container-images -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

### Built and published by this repo

Tagged with the release version; `:latest` on every push to `main`.

| Image | Upstream reference | Pin | Override | Pulled by |
| ----- | ------------------ | --- | -------- | --------- |
| `platform-agent` | `ghcr.io/gke-labs/kube-agents/platform-agent` | release tag | `PLATFORM_AGENT_IMAGE` | The agent Deployment the operator renders, and its sandbox init container. |
| `credential-proxy` | `ghcr.io/gke-labs/kube-agents/credential-proxy` | release tag | `CREDENTIAL_PROXY_IMAGE` | The credential-proxy sidecar in the agent pod. |
| `k8s-operator` | `ghcr.io/gke-labs/kube-agents/k8s-operator` | release tag | `OPERATOR_IMAGE` | The controller-manager Deployment. |
| `replay-proxy` | `ghcr.io/gke-labs/kube-agents/replay-proxy` | release tag | `REPLAY_IMAGE` | The optional inference-replay integration. |

### Pulled by an install, built elsewhere

Pinned here so `make mirror-images` and the install ask for the same version.

| Image | Upstream reference | Pin | Override | Pulled by |
| ----- | ------------------ | --- | -------- | --------- |
| `litellm` | `ghcr.io/berriai/litellm` | `v1.96.2` | `LITELLM_IMAGE` | The LiteLLM gateway, from either the chart or the kustomize integration. |
| `fluent-bit` | `docker.io/fluent/fluent-bit` | `5.1.0` | `FLUENT_BIT_IMAGE` | The logging sidecar the operator injects into every agent pod. |
| `k8s` | `docker.io/alpine/k8s` | `1.34.9` | — | The chart's pre-delete cleanup hook Job. |
| `github-token-minter-server` | `us-docker.pkg.dev/abcxyz-artifacts/docker-images/github-token-minter-server` | `v2.7.1-amd64` | `GITHUB_MINTER_IMAGE` | The optional GitHub integration. |
| `hindsight-api` | `ghcr.io/vectorize-io/hindsight-api` | `0.9.1@sha256:24a079bead8aa58e45d728bf535ea727bfe559d8784024b6b9f89d56646954ab` | `HINDSIGHT_API_IMAGE` | Provisioning step 13, when the memory provider uses Hindsight. |
| `hindsight-postgresql` | `docker.io/ankane/pgvector` | `latest@sha256:956744bd14e9cbdf639c61c2a2a7c7c2c48a9c8cdd42f7de4ac034f4e96b90f8` | `HINDSIGHT_POSTGRES_IMAGE` | Provisioning step 13, alongside the Hindsight API. |
| `cert-manager-controller` | `quay.io/jetstack/cert-manager-controller` | `v1.21.1` | — | cert-manager, installed by provision_03 unless SKIP_CERT_MANAGER is set. |
| `cert-manager-cainjector` | `quay.io/jetstack/cert-manager-cainjector` | `v1.21.1` | — | cert-manager, installed by provision_03 unless SKIP_CERT_MANAGER is set. |
| `cert-manager-webhook` | `quay.io/jetstack/cert-manager-webhook` | `v1.21.1` | — | cert-manager, installed by provision_03 unless SKIP_CERT_MANAGER is set. |
| `cert-manager-acmesolver` | `quay.io/jetstack/cert-manager-acmesolver` | `v1.21.1` | — | cert-manager's controller, via its --acme-http01-solver-image flag. Never pulled by kube-agents itself, but provision_03 rewrites the flag onto the mirror along with the rest of the manifest, so the copy has to exist. |

### Base images

Needed only to rebuild the images above from source, not to run an install. Each is a build arg on its Dockerfile, so a mirrored rebuild passes the copy's reference.

| Image | Upstream reference | Pin | Override | Pulled by |
| ----- | ------------------ | --- | -------- | --------- |
| `hermes-agent` | `docker.io/nousresearch/hermes-agent` | `HERMES_AGENT_TAG` in [`tags.env`](https://github.com/gke-labs/kube-agents/blob/main/tags.env) | `HERMES_AGENT_IMAGE` | deploy/docker/Dockerfile (agent-base stage). |
| `envoy` | `docker.io/envoyproxy/envoy` | `v1.39.0` | `ENVOY_IMAGE` | deploy/docker/Dockerfile (envoy-bin stage). |
| `golang` | `docker.io/library/golang` | `1.26-alpine` | `GOLANG_IMAGE` | deploy/docker/Dockerfile and k8s-operator/Dockerfile builder stages. |
| `python` | `docker.io/library/python` | `3.11-slim` | `PYTHON_IMAGE` | examples/inference-replay/replay-proxy/Dockerfile. |
| `distroless-static` | `gcr.io/distroless/static` | `nonroot` | `DISTROLESS_IMAGE` | k8s-operator/Dockerfile runtime stage. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: container-images -->

## Published images

Published via GitHub Actions workflows on push to `main` (tagged `:latest`) and on SemVer git tag pushes (`*.*.*`, tagged `X.Y.Z`); every publish also adds a commit-SHA tag.

### `platform-agent`

The agent Deployment image. Built from the `platform` target of [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile) on top of `nousresearch/hermes-agent`. It lays down the Planning Agent workspace at `/opt/defaults` (the `default` profile) plus two profile templates: the Platform Agent at `/opt/platform-template`, scaffolded into the `platform` profile at startup by the entrypoint, and the Cluster Agent at `/opt/cluster-template`, scaffolded into per-cluster `cluster-*` profiles at runtime by `cluster_agent_profile.py`.

- **Published by**: [`.github/workflows/docker-publish-ghcr.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-ghcr.yml)
- **Also to GAR**: [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml)

The Dockerfile installs system tooling the Platform Agent needs to inspect and remediate clusters:

- `google-cloud-cli` + `google-cloud-cli-gke-gcloud-auth-plugin`
- `kubectl`
- `gh` (GitHub CLI), `yq`, `k9s`, `helm`
- Standard debugging tools: `curl`, `jq`, `dnsutils`, `iputils-ping`, `patch`, `git`, `wget`, `nano`, `vim`

It also builds the `k8s-event-watcher` binary from `k8s-operator/cmd/k8s-event-watcher/` in a Go builder stage and copies it into the image.

A late build step precompiles the Python tree — `/opt/hermes`, its venv, and the stdlib — to `.pyc`. The base image ships almost none, sets `PYTHONDONTWRITEBYTECODE=1`, and `/opt/hermes` is read-only to the runtime user, so without this every short-lived process recompiled its imports from source and threw the result away. Each kanban worker is exactly such a process: a fresh `hermes -p <profile> --cli chat -q`. Shipping the bytecode costs ~170MB of image and takes about 6s off a worker's startup. It has to run after everything the Dockerfile writes into `/opt/hermes` — its patches and its bundled plugins alike — because `compileall` stamps each `.pyc` with its source's mtime and size, so bytecode written before the write would simply be discarded at import.

### `credential-proxy`

The Platform Agent image plus the Envoy-based credential proxy sidecar runtime. Built from the `credential-proxy` target of the same [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile) (it extends the `platform` target with the `envoy` binary and credential-proxy scripts).

- **Published by**: [`docker-publish-ghcr.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-ghcr.yml) and [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml)

### `replay-proxy`

The inference replay proxy used for record/replay of model traffic. Built from [`examples/inference-replay/replay-proxy/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/examples/inference-replay/replay-proxy/Dockerfile).

- **Published by**: [`docker-publish-ghcr.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-ghcr.yml) and [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml)

### `k8s-operator`

The Kubebuilder-generated operator manager image.

- **Published by**: [`.github/workflows/docker-publish-k8s-operator.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-k8s-operator.yml)
- **Build**: `k8s-operator/Dockerfile` (`make docker-build IMG=...`)

## Container entrypoint

`platform-agent` — and `credential-proxy`, which extends it — run [`deploy/shared/docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh) as their `ENTRYPOINT`, with `CMD ["hermes", "gateway", "run"]`. Before it `exec`s whatever command it was handed, the entrypoint seeds `$HERMES_HOME` from `/opt/defaults`, scaffolds the `platform` profile, links profile-targeted plugin volumes, merges the operator-rendered config overlays, and starts the Session KV server.

Every one of those writes to the data volume, and a Pod runs this image in more than one container against a single copy of it. Exactly one container may do the setup. A second pass from a container that lacks the plugin volumes and the overlay ConfigMap does not merely duplicate the work — it reads the first container's fresh plugin links as dangling and unlinks them, and reverts the overlay whose source it cannot see. `AGENT_SHARED_STATE_SETUP` decides which container that is:

| Value                 | Effect                                                                                                |
| --------------------- | ----------------------------------------------------------------------------------------------------- |
| `owner` (or `always`) | Run the setup, then `exec` the command.                                                               |
| `skip` (or `never`)   | Skip the setup and `exec` the command directly.                                                       |
| `auto`, or unset      | Infer from the command line: a bare `gateway` argument owns the shared state, anything else does not. |

An unrecognised value falls back to `auto` and logs a warning rather than guessing, because `Owner`, `true`, and `1` are otherwise indistinguishable from having set nothing at all.

The operator sets the variable explicitly on every container it builds — `owner` on the gateway, `skip` on the dashboard — so `auto` never runs under a `PlatformAgent`. Auto-detection exists for deployments with no operator to ask: Compose, plain manifests, `docker run`. Set it by hand in those if the owning container's own argv does not contain `gateway`. Above one replica the operator's gateway is itself such a case: it runs `leader_elect.py`, which starts `hermes gateway run` as a child process, so the word never appears in the container's own arguments.

Every case in that table is verified against the built image on each pull request, by the `entrypoint-gate-test` Dockerfile stage (`deploy/shared/entrypoint_gate_check.sh`). It runs the real entrypoint once per case against a scratch `$PLATFORM_AGENT_HOME` and checks the decision the gate announces against what it then writes to disk. That pairing is the point: the host-side unit tests in `tests/test_docker_entrypoint.py` cover the same table, but on a host every step below the gate is guarded on `/opt/defaults` or `/opt/hermes` and does nothing, so they can only prove which branch was taken. The script is not shipped in the runtime image, but it is safe to pipe into a running pod when diagnosing one:

```bash
kubectl exec -i deploy/platform-agent-gateway -c platform-agent -- \
  sh -s < deploy/shared/entrypoint_gate_check.sh
```

Confining it takes more than a scratch `$PLATFORM_AGENT_HOME`, because two of the setup's effects are not derived from it. Step 4 points `$HOME/.hermes/plugins/hermes_otel/config.yaml` at the config it generates — `hermes-otel` resolves its config below `~/.hermes` whatever `HERMES_HOME` says — and `$HOME` in the gateway is `/opt/data/home`, on the data PVC. Step 5 starts the Session KV server on port 8699, which is pod-wide and scoped by nothing. So each case also gets a scratch `$HOME`, and the server it spawns is killed by its scratch path as the case returns. The run ends by asserting both: that the pod's real compat symlink is byte-for-byte what it was, and that no process from the run is still alive.

## Base image pin

The Hermes base image tag is pinned in [`tags.env`](https://github.com/gke-labs/kube-agents/blob/main/tags.env) at the repo root:

```bash
HERMES_AGENT_TAG=v2026.8.13@sha256:68e15ae2a6d894d0ccbd9f8aacbbe13d4d28fa5dc9b6a303970b67bb2499b1a6
```

Docker builds source `tags.env` via the `HERMES_AGENT_TAG` build arg:

```dockerfile
ARG HERMES_AGENT_TAG
ARG HERMES_AGENT_IMAGE=nousresearch/hermes-agent
FROM ${HERMES_AGENT_IMAGE}:${HERMES_AGENT_TAG} AS agent-base
```

Bumping Hermes = updating `tags.env` (a single-line change) and rebuilding.

## Private / custom registry

Clusters that may only pull from an approved registry need two things: a copy of every image
above in that registry, and each install layer pointed at the copy.

### 1. Mirror the images

```bash
make mirror-images MIRROR_PREFIX=registry.example.com/kube-agents
```

The target reads `images.json`, so an image added there is copied without editing the script. It
prefers `crane` (which copies a multi-arch manifest list byte-for-byte), falls back to `skopeo`,
then `docker`, and exits non-zero listing anything that failed — an incomplete mirror must not
look like success. `./scripts/mirror_images.sh --help` documents the knobs; the ones that matter
most:

- `MIRROR_THIRD_PARTY_PREFIX` — a separate destination for images this project does not build.
  Defaults to `MIRROR_PREFIX`.
- `IMAGE_TAG` — which release tag of the first-party images to copy. Defaults to `latest`.
- `INCLUDE` — which origins to copy. Defaults to `first-party,third-party`, what a running
  install pulls; add `build-time` only if you also rebuild from source.
- `--dry-run` — print the copy plan and copy nothing.

Destinations are flat, named after the inventory entry's `name`, so
`quay.io/jetstack/cert-manager-webhook:v1.21.1` lands as
`<prefix>/cert-manager-webhook:v1.21.1`. The `name`, not the repository's trailing segment —
they are the same word for almost every entry, but where they differ the name wins, and
`docker.io/ankane/pgvector` lands as `<prefix>/hindsight-postgresql`. Every consumer below
assumes that flat layout.

### 2. Point the install at it

Pick the row for how you install. Each has two prefixes: one for the images this project builds,
one for the images it does not.

| Install path                      | First-party            | Third party                      | If the second is unset              | Reaches cert-manager |
| --------------------------------- | ---------------------- | -------------------------------- | ----------------------------------- | -------------------- |
| `install.sh`                      | `--registry-prefix`    | `--third-party-registry-prefix`  | those images stay upstream          | yes                  |
| Provisioning scripts              | `REGISTRY_PREFIX`      | `THIRD_PARTY_REGISTRY_PREFIX`    | those images stay upstream          | yes                  |
| Helm chart                        | `global.imageRegistry` | `global.thirdPartyImageRegistry` | falls back to the first-party value | n/a                  |
| Terraform `examples/full-install` | `image_registry`       | `third_party_image_registry`     | falls back to the first-party value | **no**               |

What "third party" covers differs by row, because the paths install different things. Every row
covers LiteLLM and fluent-bit; the scripts additionally cover the GitHub token minter, Hindsight,
and cert-manager. The chart never renders cert-manager at all — it expects one to be present
already — so there is nothing for its prefixes to reach. Terraform is the row to read twice: it
does install cert-manager, as a separate `helm_release` of the upstream chart, and that release
is not passed either prefix. On an approved-registry cluster set `enable_cert_manager = false`
and install cert-manager yourself from the mirror; `images.json` carries all four of its images,
so `make mirror-images` has already copied them. The composition's
[README](https://github.com/gke-labs/kube-agents/blob/main/terraform/examples/full-install/README.md)
has the detail.

The "if the second is unset" column is the one asymmetry in this page, and it is deliberate. `REGISTRY_PREFIX`
shipped long before this inventory and has always meant "the registry holding the images this
project builds" — a mirror populated against it holds those and nothing else, so inheriting it
would send an existing install after cert-manager images its registry was never given, and
provisioning would fail on ImagePullBackOff with the cluster already created. The chart and
Terraform values are new in comparison and carry no such promise, so they take the safer default
of covering everything. To mirror everything from the scripts, set both prefixes — usually to the
same value. The scripts print a warning if `REGISTRY_PREFIX` is customised while the third-party
one is not, because that is also what a half-mirrored install looks like.

`REGISTRY_PREFIX` is persisted to the scripts' state file (`vars.sh`) like every other knob, so
re-runs reuse it; the individual `OPERATOR_IMAGE`, `AGENT_IMAGE`, `REPLAY_IMAGE`,
`LITELLM_IMAGE`, and `GITHUB_MINTER_IMAGE` variables still override it. Changing the registry
_after_ a first run means editing the saved values in `vars.sh` — saved state wins over a new
export — and the scripts warn when a saved image no longer matches the effective prefix.

`IMAGE_TAG` is per-run and is deliberately not saved to `vars.sh`, so those `*_IMAGE` variables
normally hold a bare repository path. The step that consumes one attaches the current
`IMAGE_TAG` to it when it names neither a tag nor a digest — `provision_03` for the operator,
agent, and credential-proxy references, `provision_11` for the replay proxy. The third-party
images are excluded, because their tags come from `images.json` and have nothing to do with
`IMAGE_TAG`. Set a value explicitly
(`OPERATOR_IMAGE=registry.example.com/kube-agents/k8s-operator:1.4.0`) to pin a reference
independently of `IMAGE_TAG`.

cert-manager is the one install step that applies a manifest it does not own. `provision_03`
rewrites `quay.io/jetstack/` to the third-party prefix before applying it. Two escape hatches
sit alongside: `CERT_MANAGER_MANIFEST` points the step at a local or mirrored manifest instead of
the upstream URL, and `SKIP_CERT_MANAGER=1` skips it entirely where the platform team installs
cert-manager themselves (the operator's admission webhooks still need it to be present).

### What the prefix does not cover

Two images are resolved by the operator at reconcile time rather than rendered by any install
manifest, so they need the operator's own environment set — which the chart and `provision_03`
now both do automatically when a prefix is in effect:

- `PLATFORM_AGENT_IMAGE` — the agent image for a `PlatformAgent` that omits
  `spec.deployment.image`.
- `FLUENT_BIT_IMAGE` — the logging sidecar injected into every agent pod.

`CREDENTIAL_PROXY_IMAGE` needs nothing: the operator derives that sidecar from the agent image by
swapping the trailing name (`platform-agent` to `credential-proxy`), which lands on the mirror on
its own. Setting it explicitly still wins, which is why `install.sh` leaves it unset — one
explicit value pins the sidecar for every agent in the cluster, and the per-CR derivation is what
otherwise keeps each sidecar in step with its own agent's image.

Per-agent, `spec.deployment.image` / `spec.deployment.tag` on a `PlatformAgent` override all of
the above for that agent's containers — see the
[PlatformAgent CRD reference](/kube-agents/operator/platformagent-crd/). The fluent-bit sidecar
has no CR-level equivalent; `FLUENT_BIT_IMAGE` is its only override.

### Rebuilding rather than copying

Every base image is a build arg, so the images can be rebuilt where the public registries are
unreachable. Each takes a full reference rather than a shared prefix, because the flat mirror
layout does not preserve the original paths:

```bash
make docker-build-platform \
  HERMES_AGENT_IMAGE=registry.example.com/mirror/hermes-agent \
  GOLANG_IMAGE=registry.example.com/mirror/golang \
  ENVOY_IMAGE=registry.example.com/mirror/envoy
```

Unset args keep their upstream defaults, so an ordinary build is unchanged. Mirror the base
images first with `INCLUDE=build-time`, and use `crane` or `skopeo` rather than `docker` — the
Hermes pin is by digest, and a `docker pull`/`push` round trip changes it.

### Registry authentication

Out of scope: no install path renders `imagePullSecrets`. The mirror has to be readable with the
nodes' own credentials — an Artifact Registry in the same project is the simple case — or
reached through a pull-through cache.

## Local builds

For development iteration, `make dev-rebuild-agent` (from `k8s-operator/`) is the fast path — it builds and pushes to a dev Artifact Registry repo and restarts the Deployment. See [Development](/kube-agents/operator/development/#fast-agent-iteration-dev-only).

## CI

Docker builds are validated on every PR via [`.github/workflows/docker-build.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-build.yml) — the image builds but doesn't publish. Publication happens on push to `main` and on numeric SemVer `*.*.*` tag pushes (the `k8s-operator` workflow can also be dispatched manually; a non-main dispatch publishes only a commit-SHA tag).
