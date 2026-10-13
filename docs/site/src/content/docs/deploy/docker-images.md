---
title: Docker images
description: The images shipped from this repo and how their tags are managed.
sidebar:
  order: 2
---

Every image an install pulls or a rebuild needs, and how their tags are managed.

## Image inventory

[`images.json`](https://github.com/gke-labs/kube-agents/blob/main/images.json) at the repository root is the source of truth for this list. It is what `make mirror-images` copies from, what the chart and the dev tooling resolve their third-party pins from, and what the table below is generated from — so there is one pin per image, not one per install path.

The A2A `next` stack pulls NATS, nats-box, the gateway, the session worker, the auth callout,
the capability verifier, the console server and the Hermes bridge sidecar, and each is in the
tables below. The first-party ones (`a2a-gateway`, `a2a-worker`, `a2a-authcallout`,
`a2a-verifier`, `a2a-console`, `hermes-bridge`) are built and tagged with the rest of the
release. The operator derives the five it renders from its own image (`OPERATOR_IMAGE`, which
the chart sets beside `PLATFORM_AGENT_IMAGE`) when it carries a tag, else from the agent image it
resolves for itself:
the same registry and tag, so an install at a release pulls them at that release and a mirrored
install pulls them from the mirror. A `PlatformAgent`'s own `spec.deployment.image` does not move
them;
`A2A_GATEWAY_IMAGE`, `A2A_WORKER_IMAGE`, `A2A_CALLOUT_IMAGE`, `A2A_VERIFIER_IMAGE` and
`A2A_CONSOLE_IMAGE` on the operator override one at a time. The Hermes bridge sidecar the
operator renders into the agent pod under `next` is built from the `platform-agent` image of the
same commit, so the operator takes its registry and tag from the agent image instead, and
`A2A_BRIDGE_IMAGE` on the operator overrides it. A `PlatformAgent` that declares its own
`hermes-bridge` sidecar keeps it, and that sidecar's image is whatever the resource names. NATS and nats-box are ordinary third-party pins, in the
table as `nats` and `nats-box`, so `make mirror-images` copies them; the chart does not set their
env vars, so a mirrored `next` install still points `A2A_NATS_IMAGE` and `A2A_PROVISION_IMAGE` at
the copies by hand.

The bases those images are built from are inventory entries too. `golang`, `node` and
`distroless-static` in the build-time table below carry `a2a/Dockerfile.authcallout`,
`a2a/Dockerfile.console`, `a2a/Dockerfile.gateway`, `a2a/Dockerfile.verifier`,
`a2a/Dockerfile.worker` and `a2a/Dockerfile.hermes-bridge` alongside
every other builder, so a build in a mirrored environment can resolve them like any other.

Several images keep a second copy of their pin elsewhere in the tree — a chart value, a Dockerfile
`ARG` default, a compiled constant in the operator — and `make images-check` holds them in step with
this file. How to bump a pin is in
[`docs/pull-request-workflow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/pull-request-workflow.md#local-validation-before-committing).

<!-- BEGIN GENERATED: container-images -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

### Built and published by this repo

Tagged with the release version; `:latest` on every push to `main`.

| Image | Upstream reference | Pin | Override | Pulled by |
| ----- | ------------------ | --- | -------- | --------- |
| `platform-agent` | `ghcr.io/gke-labs/kube-agents/platform-agent` | release tag | `PLATFORM_AGENT_IMAGE` | The agent Deployment the operator renders, and its sandbox init container. |
| `credential-proxy` | `ghcr.io/gke-labs/kube-agents/credential-proxy` | release tag | `CREDENTIAL_PROXY_IMAGE` | The credential-proxy Deployment, and the agent-api-auth sidecar in the agent pod. |
| `agent-sandbox` | `ghcr.io/gke-labs/kube-agents/agent-sandbox` | release tag | `AGENT_SANDBOX_IMAGE` | The shell sandbox StatefulSet the operator renders beside every agent pod. |
| `k8s-operator` | `ghcr.io/gke-labs/kube-agents/k8s-operator` | release tag | `OPERATOR_IMAGE` | The controller-manager Deployment. |
| `replay-proxy` | `ghcr.io/gke-labs/kube-agents/replay-proxy` | release tag | `REPLAY_IMAGE` | The optional inference-replay integration. |
| `pubsub-platform` | `ghcr.io/gke-labs/kube-agents/pubsub-platform` | release tag | — | The pubsub-platform AgentPlugin. |
| `gke-stockout-investigator` | `ghcr.io/gke-labs/kube-agents/gke-stockout-investigator` | release tag | — | The gke-stockout-investigator AgentPlugin. |
| `a2a-gateway` | `ghcr.io/gke-labs/kube-agents/a2a-gateway` | release tag | `A2A_GATEWAY_IMAGE` | The A2A gateway Deployment the operator renders under spec.mode: next, and nothing on a default install. The operator derives it from its own image, else from the agent image it resolves (registry and tag), unless the override is set; the worker, the callout and the console resolve the same way. |
| `a2a-worker` | `ghcr.io/gke-labs/kube-agents/a2a-worker` | release tag | `A2A_WORKER_IMAGE` | The session pods the A2A gateway spawns under spec.mode: next; the operator passes its resolution to the gateway as A2A_WORKER_IMAGE. The image also carries the credential broker's shim as `kubectl` and `gcloud`, used only when the operator's A2A_SESSION_CLUSTER_VIEW flag gives a session pod its read-only cluster view. |
| `a2a-authcallout` | `ghcr.io/gke-labs/kube-agents/a2a-authcallout` | release tag | `A2A_CALLOUT_IMAGE` | The auth callout Deployment the operator renders under spec.mode: next, and nothing on a default install. |
| `a2a-console` | `ghcr.io/gke-labs/kube-agents/a2a-console` | release tag | `A2A_CONSOLE_IMAGE` | The console server Deployment the operator renders under spec.mode: next, and nothing on a default install. |
| `a2a-verifier` | `ghcr.io/gke-labs/kube-agents/a2a-verifier` | release tag | `A2A_VERIFIER_IMAGE` | The capability verifier Deployment the operator renders under spec.mode: next, and nothing on a default install. It resolves the same way the gateway, the worker and the callout do. |
| `hermes-bridge` | `ghcr.io/gke-labs/kube-agents/hermes-bridge` | release tag | `A2A_BRIDGE_IMAGE` | The hermes-bridge sidecar the operator renders beside the agent container under spec.mode: next, and nothing on a default install. The operator derives it from the agent image (registry and tag) unless the override is set; a CR that declares its own hermes-bridge sidecar on spec.deployment.sidecars keeps it, and that sidecar's image is the CR's. |

### Pulled by an install, built elsewhere

Pinned here so `make mirror-images` and the install ask for the same version.

| Image | Upstream reference | Pin | Override | Pulled by |
| ----- | ------------------ | --- | -------- | --------- |
| `litellm` | `ghcr.io/berriai/litellm` | `v1.100.1` | `LITELLM_IMAGE` | The LiteLLM gateway, from either the chart or the kustomize integration. |
| `fluent-bit` | `docker.io/fluent/fluent-bit` | `5.1.2` | `FLUENT_BIT_IMAGE` | The logging sidecar the operator injects into every agent pod. |
| `nats` | `docker.io/library/nats` | `2.10-alpine` | `A2A_NATS_IMAGE` | The NATS StatefulSet the operator renders under spec.mode: next, and nothing on a default install. |
| `nats-box` | `docker.io/natsio/nats-box` | `0.14.5` | `A2A_PROVISION_IMAGE` | The provision Job the operator runs against that NATS under spec.mode: next, and nothing on a default install. |
| `k8s` | `docker.io/alpine/k8s` | `1.36.5` | — | The chart's pre-delete cleanup hook Job. |
| `github-token-minter-server` | `us-docker.pkg.dev/abcxyz-artifacts/docker-images/github-token-minter-server` | `v2.7.1-amd64` | `GITHUB_MINTER_IMAGE` | The optional GitHub integration. |
| `hindsight-api` | `ghcr.io/vectorize-io/hindsight-api` | `0.10.2@sha256:1ba631f950a04460feff6b1e212bad3cdf127b0cfb3d765bdcba12f512481d7b` | `HINDSIGHT_API_IMAGE` | The chart, when the memory provider uses Hindsight (make deploy-hindsight for the kustomize dev path). |
| `hindsight-postgresql` | `docker.io/pgvector/pgvector` | `0.8.6-pg15@sha256:a947c45cdc5906a1bc951f20a8709e321256343ee0f251e4ae00b5e7def4e6da` | `HINDSIGHT_POSTGRES_IMAGE` | The chart, alongside the Hindsight API. |
| `cert-manager-controller` | `quay.io/jetstack/cert-manager-controller` | `v1.21.2` | — | cert-manager, installed by the full-install composition unless enable_cert_manager is false. |
| `cert-manager-cainjector` | `quay.io/jetstack/cert-manager-cainjector` | `v1.21.2` | — | cert-manager, installed by the full-install composition unless enable_cert_manager is false. |
| `cert-manager-webhook` | `quay.io/jetstack/cert-manager-webhook` | `v1.21.2` | — | cert-manager, installed by the full-install composition unless enable_cert_manager is false. |
| `cert-manager-acmesolver` | `quay.io/jetstack/cert-manager-acmesolver` | `v1.21.2` | — | cert-manager's controller, via its --acme-http01-solver-image flag. Never pulled by kube-agents itself; the copy exists so a mirrored cert-manager install can point the flag at it. |
| `cert-manager-startupapicheck` | `quay.io/jetstack/cert-manager-startupapicheck` | `v1.21.2` | — | cert-manager, installed by the full-install composition unless enable_cert_manager is false. Runs once per install as a post-install hook Job. |

### Base images

Needed only to rebuild the images above from source, not to run an install. Each is a build arg on its Dockerfile, so a mirrored rebuild passes the copy's reference.

| Image | Upstream reference | Pin | Override | Pulled by |
| ----- | ------------------ | --- | -------- | --------- |
| `hermes-agent` | `docker.io/nousresearch/hermes-agent` | `HERMES_AGENT_TAG` in [`tags.env`](https://github.com/gke-labs/kube-agents/blob/main/tags.env) | `HERMES_AGENT_IMAGE` | deploy/docker/Dockerfile (agent-base stage). |
| `envoy` | `docker.io/envoyproxy/envoy` | `v1.39.1` | `ENVOY_IMAGE` | deploy/docker/Dockerfile (envoy-bin stage). |
| `golang` | `docker.io/library/golang` | `1.27-alpine` | `GOLANG_IMAGE` | deploy/docker/Dockerfile, k8s-operator/Dockerfile, a2a/Dockerfile.authcallout, a2a/Dockerfile.gateway, a2a/Dockerfile.hermes-bridge, a2a/Dockerfile.verifier, a2a/Dockerfile.worker and a2a/Dockerfile.console builder stages. |
| `node` | `docker.io/library/node` | `22-slim` | `NODE_IMAGE` | a2a/Dockerfile.worker runtime stage and a2a/Dockerfile.console page build stage. |
| `python` | `docker.io/library/python` | `3.14-slim` | `PYTHON_IMAGE` | examples/inference-replay/replay-proxy/Dockerfile and deploy/sandbox/Dockerfile. |
| `distroless-static` | `gcr.io/distroless/static` | `nonroot` | `DISTROLESS_IMAGE` | k8s-operator/Dockerfile, a2a/Dockerfile.authcallout, a2a/Dockerfile.gateway, a2a/Dockerfile.verifier and a2a/Dockerfile.console runtime stages. |
| `busybox` | `docker.io/library/busybox` | `musl@sha256:ea2b9914a16a4ac1981994af97b318f7c7d4db76b580c56177f08bf76f4a0be8` | — | agentplugins/*/Dockerfile base images. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: container-images -->

## Published images

Every image below is published to `ghcr.io/gke-labs/kube-agents/<image>` on each push to `main`, and when a merge lands on a `release/<X.Y>` branch whose commit has no images yet, tagged with the pushed commit's SHA; `:latest` follows `main` alone. A push to `main` also publishes `platform-agent`, `credential-proxy`, `replay-proxy`, `agent-sandbox`, `pubsub-platform` and `gke-stockout-investigator` — not `k8s-operator`, and not the four A2A `next`-stack images, which are published to GHCR alone — to a Google Artifact Registry repository through [`docker-publish-gcp.yml`](https://github.com/gke-labs/kube-agents/blob/main/.github/workflows/docker-publish-gcp.yml), built there by Cloud Build. Production SemVer release tags (`X.Y.Z`) are promoted from the GHCR commit images without rebuilding — see [Release versioning](/kube-agents/deploy/release-versioning/).

### `platform-agent`

The agent Deployment image. Built from the `platform` target of [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile) on top of `nousresearch/hermes-agent`. It lays down the Planning Agent workspace at `/opt/defaults` (the `default` profile) plus two profile templates: the Platform Agent at `/opt/platform-template`, scaffolded into the `platform` profile at startup by the entrypoint, and the Cluster Agent at `/opt/cluster-template`, scaffolded into per-cluster `cluster-*` profiles at runtime by `cluster_agent_profile.py`.

There is no cluster or forge tooling in this image, in any form, and a build guard fails if any reappears. The agent's shell runs in the `agent-sandbox` image. `kubectl` and `gcloud` there are credential-proxy shims — symlinks to a client that forwards a command to the broker holding the credential — and `helm` and `yq` are native. Its `git` holds no credential and reaches no forge, and there is no `gh`: repositories are fetched and changed through the version-control skill's verbs, which the broker serves. Agent-pod code that needs one of them reaches the sandbox over SSH through `agents/platform/scripts/sandbox_exec.py`, and the sandbox reaches the broker at the `<name>-credential-proxy` Service.

What is installed is the debugging set the agent's own processes use: `curl`, `jq`, `dnsutils`, `iputils-ping`, `patch`, `wget`, `nano`, `vim`.

It also builds two Go binaries in a builder stage and copies them into the image: the `k8s-event-watcher` from `k8s-operator/cmd/k8s-event-watcher/` and the `drift-detector` from `k8s-operator/cmd/drift-detector/`. Both land in the shared `agent-base` stage, so the `credential-proxy` image below carries them too — which is the image that actually runs them, in the gateway pod's `agent-api-auth` sidecar.

A late build step precompiles the Python tree — `/opt/hermes`, its venv, and the stdlib — to `.pyc`. The base image ships almost none, sets `PYTHONDONTWRITEBYTECODE=1`, and `/opt/hermes` is read-only to the runtime user, so without this every short-lived process recompiled its imports from source and threw the result away. Each kanban worker is exactly such a process: a fresh `hermes -p <profile> --cli chat -q`. Shipping the bytecode costs ~170MB of image and takes about 6s off a worker's startup. It has to run after everything the Dockerfile writes into `/opt/hermes` — its patches and its bundled plugins alike — because `compileall` stamps each `.pyc` with its source's mtime and size, so bytecode written before the write would simply be discarded at import.

### `credential-proxy`

The Envoy-based credential broker runtime, which runs as the `envoy-credential-proxy` container in its own `<name>-credential-proxy` Deployment. The same image also runs the gateway pod's `agent-api-auth` sidecar, with `CREDENTIAL_PROXY_ROLE=api-proxy` starting neither Envoy nor the executor there. Built from the `credential-proxy` target of the same [`deploy/docker/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/Dockerfile), on the shared `agent-base` stage rather than on `platform`: it adds the real `gcloud`, `kubectl`, `gh` and `git` that the sandbox image deliberately lacks, the `envoy` binary and its config, and `/opt/defaults/scripts`, which is where `start-services.sh` finds `credential_proxy.py`. It carries none of what the `platform` stage adds on top — no kube-agents personas, skills, cron entries or profile templates — because nothing that runs from this image reads them.

Building it from `agent-base` rather than `platform` is also what keeps a one-file agent change cheap. The stage copies `agents/*/scripts/` itself, so an edit there still rebuilds that copy and the layers after it — but no longer the whole `platform` chain, whose output the sidecar does not use.

### `replay-proxy`

The inference replay proxy used for record/replay of model traffic. Built from [`examples/inference-replay/replay-proxy/Dockerfile`](https://github.com/gke-labs/kube-agents/blob/main/examples/inference-replay/replay-proxy/Dockerfile).

### `k8s-operator`

The Kubebuilder-generated operator manager image. Built from `k8s-operator/Dockerfile` (`make docker-build IMG=...`).

## Container entrypoint

`platform-agent` — and `credential-proxy`, which inherits it from the shared `agent-base` stage — run [`deploy/shared/docker-entrypoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/docker-entrypoint.sh) as their `ENTRYPOINT`, with `CMD ["hermes", "gateway", "run"]`. Neither container built from that image reaches it under this operator: for both `envoy-credential-proxy` and `agent-api-auth` the operator sets `command` to `/usr/local/bin/start-services`, which replaces the image's `ENTRYPOINT` outright. Before it `exec`s whatever command it was handed, the entrypoint converts Hermes' SQLite databases out of WAL when the managed config pins `database.journal_mode: delete` (see `availability.runtimeClassName` on the [CRD reference](/kube-agents/operator/platformagent-crd/#specdeployment)), seeds `$HERMES_HOME` from `/opt/defaults`, scaffolds the `platform` profile, links profile-targeted plugin volumes, merges the operator-rendered config overlays, copies the managed terminal settings into every profile's `.env`, and starts the Session KV server.

Every one of those writes to the data volume, and a Pod runs this image in more than one container against a single copy of it. Exactly one container may do the setup. A second pass from a container that lacks the plugin volumes and the overlay ConfigMap does not merely duplicate the work — it reads the first container's fresh plugin links as dangling and unlinks them, and reverts the overlay whose source it cannot see. `AGENT_SHARED_STATE_SETUP` decides which container that is:

| Value                 | Effect                                                                                                |
| --------------------- | ----------------------------------------------------------------------------------------------------- |
| `owner` (or `always`) | Run the setup, then `exec` the command.                                                               |
| `skip` (or `never`)   | Skip the setup and `exec` the command directly.                                                       |
| `auto`, or unset      | Infer from the command line: a bare `gateway` argument owns the shared state, anything else does not. |

An unrecognised value falls back to `auto` and logs a warning rather than guessing, because `Owner`, `true`, and `1` are otherwise indistinguishable from having set nothing at all. Under the operator (`HERMES_MANAGED_DIR` set) a container that skips the setup first waits, for up to `AGENT_SHARED_STATE_WAIT_SECS` (default 120), for the owner to seed `config.yaml` and to finish the WAL-to-DELETE conversion, then proceeds either way.

The operator sets the variable explicitly on every container it builds — `owner` on the gateway, `skip` on the dashboard — so `auto` never runs under a `PlatformAgent`. Auto-detection exists for deployments with no operator to ask: Compose, plain manifests, `docker run`. Set it by hand in those if the owning container's own argv does not contain `gateway`. Above one replica the operator's gateway is itself such a case: it runs `leader_elect.py`, which starts `hermes gateway run` as a child process, so the word never appears in the container's own arguments.

A Dockerfile stage built on every pull request, `entrypoint-gate-test`, verifies every case in that table against the real entrypoint (`deploy/shared/entrypoint_gate_check.sh`): it runs the entrypoint once per case against a scratch `$PLATFORM_AGENT_HOME` and checks the decision the gate announces against what it then writes to disk. A build that targets a runtime image does not run it. The script is not shipped in the runtime image, but it is safe to pipe into a running pod when diagnosing one:

```bash
kubectl exec -i deploy/platform-agent-gateway -c platform-agent -- \
  sh -s < deploy/shared/entrypoint_gate_check.sh
```

Confining it takes more than a scratch `$PLATFORM_AGENT_HOME`, because two of the setup's effects are not derived from it. Step 4 points `$HOME/.hermes/plugins/hermes_otel/config.yaml` at the config it generates — `hermes-otel` resolves its config below `~/.hermes` whatever `HERMES_HOME` says — and `$HOME` in the gateway is `/opt/data/home`, on the data PVC. Step 5 starts the Session KV server on port 8699, which is pod-wide and scoped by nothing. So each case also gets a scratch `$HOME`, and the server it spawns is killed by its scratch path as the case returns. The run ends by asserting both: that the pod's real compat symlink is byte-for-byte what it was, and that no process from the run is still alive.

Three things the entrypoint does can stop the container rather than warn: the skill manifest check, the terminal check (step 4b), and a failed move of the model's files into the shell sandbox (step 5.7). The first: before the setup copies anything to the data volume — in the container that owns the shared state, since a `skip` container has already `exec`ed the command by this point — it checks each skill tree baked into the image (`/opt/hermes/skills`, `/opt/platform-template/skills`, `/opt/cluster-template/skills`, `/opt/a2a-template/skills`) against the SHA-256 manifest the build wrote into it, and exits non-zero if a tree no longer matches — naming the offending file on stderr, with both digests when its content is what changed. Almost every other step here degrades with a `WARN` — the exceptions are step 1, which runs upstream's `stage2-hook.sh` and inherits `set -e` from the script, and the three named above. The manifest check is a deliberate exception, for the reason [Security &amp; IAM](/kube-agents/reference/security-and-iam/#change-control--safety) gives. A pod crash-looping with `does not match the manifest baked beside it at build time` is reporting a corrupted or altered image, not a misconfiguration: reinstate the image the manifest belongs to rather than looking for a setting to change.

The manifest, not the checker, is what makes the check mandatory: a tree carrying one is verified or the container refuses to start. Both sides of that pairing are root-owned in the image — the manifest inside the tree it describes, the verifier in `/opt/defaults/scripts` — so `carries a build-time manifest but nothing here can check it` is not something the agent's own uid can arrange, and it is read the same way as a mismatch: an altered or truncated image, not a setting. A tree with no manifest inside it is skipped, which is how the same entrypoint stays a no-op in images that never reached the stage where manifests are written.

The second is the terminal check, step 4b. Hermes builds the terminal a scheduled run or a kanban wake turn uses from the profile's own settings, not from the managed config the operator renders to `/etc/hermes/config.yaml`, so the entrypoint copies every `terminal:` setting Hermes maps to a `TERMINAL_*` variable (all but `workspace_root`) into the `TERMINAL_*` lines of every profile's `.env` ([`deploy/shared/terminal_env_pin.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/terminal_env_pin.py)) and then asks Hermes what each profile resolves. If a profile would resolve any of those settings differently — a backend other than the `ssh` sandbox, another host — the container exits non-zero naming the profile and the `TERMINAL_*` variables Hermes resolves differently. A cluster profile scaffolded after start-up gets the same check from `cluster_agent_profile.py`, which leaves a profile that fails it unfinished for the next reconcile to retry. In a pod crash-looping with `would not use the managed ssh terminal`, the `[TERMINAL-ENV-PIN] ERROR:` lines above it name the cause. A key in a profile's own `config.yaml` that sets one of those settings to another value, such as `terminal.backend: local`, outranks `.env`, so the step deletes it and leaves the rest of the file as it was, as Hermes' own save does; if it cannot, the error names the keys to delete by hand. Any other error names the file or directory to fix, except `this Hermes does not map terminal.backend`, which means the image ships a Hermes the copy does not support; the image build fails on that first. At start-up, a profile whose `.env` took the copy but whose `config.yaml` Hermes cannot read or parse gets a `WARN` instead: Hermes refuses that profile's terminal, so its scheduled runs fail until the file is fixed. Without `HERMES_MANAGED_DIR`, which only the operator sets, the step copies a `terminal:` block from `/etc/hermes/config.yaml` if one is there, skipping with a `WARN` any value `.env` cannot hold as a plain line and ignoring, with a `WARN`, a file it cannot read or parse, as Hermes does; with no block it does nothing. What it copies is still checked.

The third, step 5.7, stops the container with `the shell sandbox migration failed` when the model's files could not be moved into the sandbox, followed by the last lines of `logs/sandbox_mirror.log`.

## Base image pin

The Hermes base image tag is pinned in [`tags.env`](https://github.com/gke-labs/kube-agents/blob/main/tags.env) at the repo root, by tag and digest together:

```bash
HERMES_AGENT_TAG=<tag>@sha256:<digest>
```

That file holds the pin itself; this page does not repeat it, because a second copy of a digest is a second thing to forget.

Docker builds source `tags.env` via the `HERMES_AGENT_TAG` build arg:

```dockerfile
ARG HERMES_AGENT_TAG
ARG HERMES_AGENT_IMAGE=nousresearch/hermes-agent
FROM ${HERMES_AGENT_IMAGE}:${HERMES_AGENT_TAG} AS agent-base
```

The `ARG` has no default, so every build path has to pass it — the image-build workflows, `make docker-build-agents` and `make docker-build-credential-proxy`, and `dev_rebuild_agent.sh` all read it from `tags.env`. A build that omits it fails rather than falling back to `latest`.

Bumping Hermes means editing `tags.env` and rebuilding both agent images: the pin is a build-time base, so nothing changes in a cluster until `platform-agent` and `credential-proxy` are rebuilt and rolled out.

The tool-call audit trail does not depend on Hermes' log line format: the `tool_call_audit` plugin and the `chat_message_audit` hook write their records to a file of their own that the `fluent-bit` sidecar tails as JSON ([Concepts → Observability](/kube-agents/concepts/observability/#tool-call-audit)). What a bump can still change is the plugin and hook interfaces the two emitters are loaded through. Once a bumped `platform-agent` is running, has made a tool call and has answered a chat turn, query `jsonPayload.audit_event:*` in Logs Explorer: a `tool_call_start` record with a `jsonPayload.file_path` ending in `audit.jsonl` means the plugin still loads and writes, and a `chat_message_start` record means the hook does; none means the plugin or the hook no longer registers, which the agent container's startup output and `agent.log` name.

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
`quay.io/jetstack/cert-manager-webhook:v1.21.2` lands as
`<prefix>/cert-manager-webhook:v1.21.2`. The `name`, not the repository's trailing segment —
they are the same word for almost every entry, but where they differ the name wins, and
`docker.io/pgvector/pgvector` lands as `<prefix>/hindsight-postgresql`. Every consumer below
assumes that flat layout.

### 2. Point the install at it

Pick the row for how you install. Each has two prefixes: one for the images this project builds,
one for the images it does not.

| Install path                      | First-party            | Third party                      | If the second is unset              | Reaches cert-manager |
| --------------------------------- | ---------------------- | -------------------------------- | ----------------------------------- | -------------------- |
| `install.sh`                      | `--registry-prefix`    | `--third-party-registry-prefix`  | falls back to the first-party value | **yes**              |
| Helm chart                        | `global.imageRegistry` | `global.thirdPartyImageRegistry` | falls back to the first-party value | n/a                  |
| Terraform `examples/full-install` | `image_registry`       | `third_party_image_registry`     | falls back to the first-party value | **yes**              |

The rows are one path in three coats: `install.sh` generates the Terraform composition's
`terraform.tfvars` from its flags, and the composition passes both values to the chart's
`global.*` keys, so "third party" covers the same set everywhere — LiteLLM, fluent-bit, the
GitHub token minter, and Hindsight. cert-manager is the chart row's exception: the chart never
renders it — it expects one to be present already. The composition installs it as a separate
`helm_release` of the upstream chart and passes the third-party prefix to that release's image
repositories, so a mirrored install pulls every image — cert-manager's five included — from the
mirror. On a cluster whose cert-manager comes from somewhere else, set
`enable_cert_manager = false` and install it yourself; `images.json` carries all five of its
images, so `make mirror-images` has already copied them.
The composition's
[README](https://github.com/gke-labs/kube-agents/blob/main/terraform/examples/full-install/README.md)
has the detail.

`REGISTRY_PREFIX` and `THIRD_PARTY_REGISTRY_PREFIX` live in the install configuration
(`install.env`) like every other knob, so re-runs reuse them; `terraform.tfvars` is regenerated
from it on every run. Changing the registry _after_ a first run means editing `install.env` and
re-running `install.sh`, or passing the flag again — a flag beats the file.

`IMAGE_TAG` is per-run and is deliberately not recorded in `install.env`: the installer passes it into
`terraform.tfvars` as `image_tag`, which overrides both first-party image tags in the chart. The
third-party images are excluded, because their tags come from `images.json` and have nothing to
do with `IMAGE_TAG`.

### What the prefix does not cover

Three images are resolved by the operator at reconcile time rather than rendered by any install
manifest, so they need the operator's own environment set — which the chart does automatically
when a prefix is in effect:

- `PLATFORM_AGENT_IMAGE` — the agent image for a `PlatformAgent` that omits
  `spec.deployment.image`.
- `AGENT_SANDBOX_IMAGE` — the shell sandbox StatefulSet rendered beside every agent pod.
- `FLUENT_BIT_IMAGE` — the logging sidecar injected into every agent pod.

`CREDENTIAL_PROXY_IMAGE` needs nothing: the operator derives the broker image from the agent image
by swapping the trailing name (`platform-agent` to `credential-proxy`), which lands on the mirror
on its own. The A2A `next` images the operator renders (`a2a-gateway`, `a2a-worker`,
`a2a-authcallout`, `a2a-verifier`, `a2a-console`) follow `OPERATOR_IMAGE`, else `PLATFORM_AGENT_IMAGE`, the same way, so they need
no env of their own on a mirror. The sandbox is a separate repository, so it gets no such derivation. Setting it explicitly still wins, which is why `install.sh` leaves it unset — one
explicit value pins the sidecar for every agent in the cluster, and the per-CR derivation is what
otherwise keeps each sidecar in step with its own agent's image.

Per-agent, `spec.deployment.image` / `spec.deployment.tag` on a `PlatformAgent` override all of
the above for that agent's containers — see the
[PlatformAgent CRD reference](/kube-agents/operator/platformagent-crd/). The sandbox has
`spec.harness.experimental.shellSandbox.image`, which overrides `AGENT_SANDBOX_IMAGE` for that
agent. The fluent-bit sidecar has no CR-level equivalent; `FLUENT_BIT_IMAGE` is its only override.

### Rebuilding rather than copying

Every base image is a build arg, so the images can be rebuilt where the public registries are
unreachable. Each takes a full reference rather than a shared prefix, because the flat mirror
layout does not preserve the original paths:

```bash
make docker-build-agents \
  HERMES_AGENT_IMAGE=registry.example.com/mirror/hermes-agent \
  GOLANG_IMAGE=registry.example.com/mirror/golang \
  ENVOY_IMAGE=registry.example.com/mirror/envoy
```

Unset args keep their upstream defaults, so an ordinary build is unchanged. Mirror the base
images first with `INCLUDE=build-time`, and use `crane` or `skopeo` rather than `docker` — the
Hermes pin is by digest, and a `docker pull`/`push` round trip changes it. A mirrored `golang` copy
is frozen at whichever patch it was copied at, and the builder stages run with `GOTOOLCHAIN=local`,
so a later bump of the `go` directive in `k8s-operator/go.mod` past that patch fails the rebuild
instead of downloading a toolchain: re-mirror, or pass a patch-pinned `GOLANG_VERSION`.

### Registry authentication

A mirror the nodes can already read — an Artifact Registry in the same project, or a pull-through
cache — needs nothing here. One that has to be authenticated to, Harbor or Artifactory with token
auth, needs `imagePullSecrets`, set in whichever of these matches how the install was made:

- `global.imagePullSecrets` in the Helm chart, a list of Secret names — or of `{name: <secret>}`
  maps, the shape a `PodSpec` takes; any other shape fails the render. It reaches every pod the
  chart renders — the operator, the LiteLLM gateway, the `pre-delete` cleanup Job — and, through
  `IMAGE_PULL_SECRETS` on the controller manager and `spec.deployment.imagePullSecrets` on the
  `PlatformAgent` it creates, the agent pods the operator renders as well.
- `image_pull_secrets` in `terraform/examples/full-install`, which passes the same list to the
  chart.
- `spec.deployment.imagePullSecrets` on a `PlatformAgent` written by hand, or
  `IMAGE_PULL_SECRETS` (comma-separated Secret names) on the controller manager as the fleet-wide
  default for agents that do not set it. The CR **replaces** that default rather than adding to
  it, on the same terms as `spec.deployment.image` against `PLATFORM_AGENT_IMAGE`.

The list is pod-scoped, so it covers every image in an agent pod: the agent, the credential-proxy
and fluent-bit sidecars, anything in `initContainers`/`sidecars`, and the OCI image volumes
`AgentPlugin`s mount. Kubernetes has no per-container split.

The Secrets are referenced, never created. Registry credentials would otherwise live in Helm
release data and Terraform state, so each Secret has to exist in the agent's namespace before the
pod is scheduled:

```bash
kubectl create namespace kubeagents-system
kubectl create secret docker-registry regcred \
  --namespace kubeagents-system \
  --docker-server=harbor.example.com \
  --docker-username=robot\$kube-agents \
  --docker-password="$TOKEN"
```

Two things this does not cover. The provisioning scripts have no flag for it, so `install.sh`
sets no pull identity for the operator, LiteLLM, and token-minter pods it applies — those need a
mirror the nodes can read, or a hand-patched Deployment. Agent pods are reachable on that path:
set `IMAGE_PULL_SECRETS` on the controller manager yourself, the same way `INSTALL.md` documents
setting `PLATFORM_AGENT_IMAGE`. And cert-manager is a separate Helm release of an upstream chart,
unaffected by any of the above — on a cluster whose registry needs authenticating to, install it
yourself from the mirror and set `enable_cert_manager = false`.

## Local builds

`make docker-build` at the repository root builds the agent, credential-proxy and sandbox images; `make docker-build-agents`, `make docker-build-credential-proxy` and `make docker-build-sandbox` build one. The operator image builds from `k8s-operator/` (above) and `replay-proxy` from its own Dockerfile. For iterating on the agent against a running cluster, `make dev-rebuild-agent` builds, pushes to a dev Artifact Registry repository and patches the running `PlatformAgent` — or a Deployment that pulls the image directly — to the new tag; [`k8s-operator/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/README.md) covers it.
