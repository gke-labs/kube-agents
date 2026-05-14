# Demo: Collaborative Upgrade Handshake

Implements Scenario 1 of `docs/demo.md` on the Scion-based GKE Platform Team. A `platform-coordinator` agent orchestrates an `upgrade-coordinator` and a `dev-workload-guardian` to negotiate a workload-aware upgrade plan, with human approval at each write-path boundary. `node-pool-provisioner` and `workload-deployer` are also linked into the project so the team can fan out to them as needed (e.g., scaling a workload before upgrade).

## Prerequisites

On the host (laptop):

1. **Scion CLI** installed and `scion init --machine` already run.
2. **`gcloud`** with ADC: `gcloud auth application-default login`. The active account needs `container.developer` on the in-scope cluster (plus `container.admin` if the demo will execute the upgrade).
3. **`gke-mcp`** binary on PATH (see `../../tools/README.md`).
4. **Python 3** with `venv` (proxy bootstraps its own `aiohttp` deps).
5. **A GitHub PAT** with read access to this repo. In Hub mode, agents populate their workspace via `git clone`; without a PAT the clone fails. Register it as a Hub secret (see below) before the first run.

## Hub secrets (register once, persist across server restarts)

These are stored in the Hub's sqlite at `~/.scion/` and survive workstation reboots. Register with the Hub running:

```sh
# 1. ADC for Vertex auth (the agent's gemini harness uses Vertex AI, not the
#    public Gemini API, so we don't need GEMINI_API_KEY).
scion hub secret set --type file \
  --target /home/scion/.config/gcloud/application_default_credentials.json \
  gcloud-adc @/home/user/.config/gcloud/application_default_credentials.json

scion hub secret set GOOGLE_CLOUD_PROJECT <your-gcp-project>
scion hub secret set GOOGLE_CLOUD_LOCATION global   # or us-central1, etc.

# 2. GitHub PAT for the workspace clone.
scion hub secret set GITHUB_TOKEN <your-pat>

# Verify:
scion hub secret list
```

## One-time host services

In separate terminals (or `tmux` panes):

```sh
# Terminal 1 — Scion server in workstation mode (Hub + Broker + Web on 8080)
cd ~ && source /home/user/projects/kube-agents/gcpenv.sh
scion server start --workstation --foreground

# Terminal 2 — local gke-mcp HTTP server (port 9080)
source /home/user/projects/kube-agents/gcpenv.sh
../../tools/start-local-mcp.sh

# Terminal 3 — remote MCP token-refreshing proxies (8081 full, 8082 read-only)
source /home/user/projects/kube-agents/gcpenv.sh
../../tools/start-remote-mcp-proxy.sh
```

## Running the demo

Set the cluster identity and scenario specifics, then bootstrap:

```sh
export GKE_PROJECT=my-sandbox-project       # GCP project ID
export GKE_LOCATION=us-central1             # region or zone
export GKE_CLUSTER=mercury-01               # cluster name
export GKE_NAMESPACES_IN_SCOPE=prod-checkout
export PROD_NAMESPACE=prod-checkout
export PRIMARY_WORKLOAD=payment-api         # the demo's "single replica" workload
export GKE_TARGET_VERSION=1.29.x            # upgrade target

./bootstrap.sh
```

`bootstrap.sh` will:
- refuse to run if a stray `.scion/` exists at the kube-agents repo root (it would shadow the demo's own `.scion/` during Scion's project resolution and templates would land in the wrong place)
- run `scion init` if needed
- stage a symlink-free copy of `templates/` into a temp dir (Scion's importer doesn't follow symlinks, and our per-template `skills/<name>` entries are symlinks into the shared `skills/` tree), then run `scion templates import --all --force <staging-dir>` to register the role templates with the local Hub
- render `MEMORY.md` and `opening-prompt.rendered.md` with your env vars filled in
- best-effort check that the host MCP services are reachable

After editing any template under `templates/`, re-run `./bootstrap.sh` to re-import (the `--force` overwrites the previously-imported copy).

Then start the coordinator:

```sh
scion start coordinator --type platform-coordinator \
  --branch gari/spawning-fixes \
  "$(cat opening-prompt.rendered.md)" --attach
```

The `--branch` flag tells Scion which branch of this repo to clone into the agent's workspace. `scion-prototype` and `scion-prototype-work` are protected (push-disabled), so working commits live on `gari/spawning-fixes` for now; pin to whichever branch has the latest agent definitions you want the coordinator to clone.

The coordinator will spawn the specialists it needs and start the negotiation. The human (you) approves write-path actions via `ask_user` prompts that surface in the attached terminal (and in the Scion web dashboard's Inbox Tray).

The container name is `kube-agents--coordinator` (Scion treats the kube-agents repo as the project, not the demo dir). To detach: `Ctrl-P Ctrl-Q`. To reattach: `scion attach coordinator`. To inspect spawned specialists: `scion list`.

## Stand-in workload

The demo expects a workload at `${PROD_NAMESPACE}/${PRIMARY_WORKLOAD}` with a single replica (so the team can detect the resilience gap). The bootstrap does **not** deploy this for you in phase 1 — either pick an existing workload in your sandbox that matches, or apply a stub:

```sh
kubectl create namespace "$PROD_NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl create deployment "$PRIMARY_WORKLOAD" \
  --image=registry.k8s.io/echoserver:1.10 \
  --namespace="$PROD_NAMESPACE" --replicas=1
```

(Eventually the bootstrap will offer to deploy a stub for you.)

## Resetting between runs

```sh
./bootstrap.sh --cleanup       # cleanup + re-bootstrap in one step
```

`--cleanup` deletes the coordinator agent (in this grove and the kube-agents grove if accidentally created there), removes `.scion/`, the rendered files, and any stray `.scion/` at the repo root, then proceeds with the normal bootstrap. If env vars are not set, `--cleanup` runs the teardown only and exits cleanly.

The coordinator's worktree state (under `~/.scion_worktrees/<project>/coordinator/`) is removed when `scion delete` runs.

## Mapping to docs/demo.md

| Demo script element | Scion implementation |
|---|---|
| Cluster operator role | `upgrade-coordinator` template (referred to by template name in narration) |
| Dev team role | `dev-workload-guardian` template |
| Cross-agent @-mentions in shared chat | `scion message` between coordinator and specialists; the coordinator narrates the back-and-forth in the human-facing terminal |
| `MEMORY.md` constraint persistence | Same — `/workspace/MEMORY.md`, single-writer = coordinator |
| Readiness Score | Produced by `dev-workload-guardian` per its `agents.md` workflow |
| Upgrade-window negotiation | Driven by the coordinator's prose workflow; human's constraints captured in MEMORY.md |
| Human approval gates | `sciontool status ask_user` from the responsible specialist; coordinator surfaces |
