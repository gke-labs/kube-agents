---
name: install-kube-agents
description: Install, set up, or deploy kube-agents (the Kubernetes Agentic Harness) and its Platform Agent onto a GKE cluster in a GCP project, interactively or non-interactively. Use when asked to install, bootstrap, or onboard kube-agents, or to plan one with a dry run.
---

# `install-kube-agents` Skill

Install `kube-agents` for an operator: detect their environment and interview them for the choices
that shape the install, preview it with `--dry-run`, and apply only after they confirm. Follow
[Install workflow](#install-workflow).

## What `install.sh` actually does

It is a front-end, not a second provisioner. It loads `install.env` (the install's
hand-authored configuration), collects anything still missing, generates
`terraform/examples/full-install/terraform.tfvars` from the result, and then runs the composition's
`lifecycle.sh apply` — the Terraform root in
[`terraform/examples/full-install/`](../../../terraform/examples/full-install/README.md) owns every
GCP resource and installs the Helm chart (`charts/kube-agents`) that owns every Kubernetes one.
The chart installs the operator and the `PlatformAgent` into the `kubeagents-system` namespace
(`DEFAULT_NAMESPACE`; `--agent-namespace` overrides it).
Terraform state goes to a GCS bucket (`<project>-kube-agents-tfstate`, versioned, prefix
`kube-agents/<cluster>`), so `uninstall.sh` and `upgrade.sh` can find the install from a fresh
clone. The installer sources
[`scripts/installer/installer_common.sh`](../../../scripts/installer/README.md) before its
first prompt, so its defaults and accepted values are the ones defined there; that file is where a
default changes.

Order of operations: resolve the image/source ref → check CLI prerequisites (including
`terraform`, which it offers to install; `make` is not needed) → put the repository on disk and
verify it against that ref → load `install.env` → interview for what is missing → generate
`terraform.tfvars` → refuse a service account another install in the project owns
(`check_service_account_ownership`, before the summary and the dry-run exit) → run
`lifecycle.sh apply`. The source check happens **before** the interview, so a bad ref fails in
seconds rather than after a dozen answers. Some steps stay `gcloud` calls outside the apply — before
it, CMEK, the Workload Identity pool and NetworkPolicy enforcement on a pre-existing cluster; after
it, the managed-OTel scope on a cluster it created — and the GitHub App PEM import runs through
the Minty CLI so the key never enters Terraform state. Re-running the installer (or its `--menu`
Day-2 panel's Save & Apply) reconciles every change through one `terraform apply`.

## Install workflow

Run the three stages in order. Stages 1 and 2 create and change nothing in the project or cluster;
nothing is applied until the operator confirms in stage 3. Pass `--non-interactive` to every
`install.sh` call: a tool call has no terminal for the installer's `/dev/tty` prompts, so the
interview happens in chat instead.

After every `install.sh` call, read the exit code before `/tmp/kube-agents-install-report.json`.
Several exits (the service-account ownership check, input validation, a missing tool under
`--dry-run`, the GitHub App PEM import) write no report and leave an earlier run's in place.

Resolve the latest stable release tag from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases)
(e.g. `0.4.0`) first, and substitute it for `<RELEASE_VERSION>` in every command you run or hand
over; never leave the placeholder unrendered.

### Stage 1: Detect and interview

Read the environment; change nothing:

```bash
gcloud config get-value project
gcloud config get-value compute/region
gcloud auth application-default print-access-token >/dev/null && echo "ADC: ok"
gcloud container clusters list --project <PROJECT_ID>
gcloud storage ls gs://<PROJECT_ID>-kube-agents-tfstate/kube-agents/
```

No `ADC: ok` means no Application Default Credentials; stage 3 cannot run without them (stage 2
says why), so have the operator run `gcloud auth application-default login` now.

A prefix in that bucket means an install already exists in the project, and so does an
`install.env` where the installer looks for one: `$KUBE_AGENTS_INSTALL_ENV`, beside `install.sh`,
the current directory, or `$HOME/kube-agents/` (where the `curl | bash` form clones). Ask whether
the operator wants [`upgrade-kube-agents`](../upgrade-kube-agents/SKILL.md) instead. A re-run reads
that `install.env`; [`scripts/installer/README.md`](../../../scripts/installer/README.md) owns the
precedence between it, flags and defaults.

Propose the detected value or the default for each decision below, and have the operator confirm or
change it. Never ask for a secret in chat: the installer reads each one from the environment it runs
in, where the operator sets it.

1. **Project and region** — `--gcp-project-id`, `--gcp-region`. Defaults: the active `gcloud`
   project, and its `compute/region` or, when that is unset, `DEFAULT_REGION`.
2. **Cluster** — create one (`--gke-cluster-name`, default `DEFAULT_CLUSTER_NAME`; Autopilot unless
   `--gke-cluster-mode=standard` or a zonal region) or install onto one from the list. An existing
   cluster can need changes; stage 2 lists them.
3. **Model provider** — `--model-provider`. `vertex_ai` authenticates through Workload Identity and
   needs no API key; `gemini`, `anthropic` and `openai` read `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`
   or `OPENAI_API_KEY`, and `gemini` also finds a key stored in Secret Manager. The default is
   `DEFAULT_MODEL_PROVIDER`; pass the flag for any other choice.
4. **How the operator reaches the agent** — a terminal when no chat flag is passed (see
   [Handing over a chat-less install](#handing-over-a-chat-less-install)), `--enable-google-chat`,
   or `--enable-slack` (tokens in `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`).
   `--enable-hermes-dashboard=true` adds the Hermes Web UI, which a port-forward cannot reach under
   gVisor; the
   [PlatformAgent reference](../../../docs/site/src/content/docs/operator/platformagent-crd.md)
   gives its access path. GitOps pull requests add `--gitops-org`, `--gitops-repo` and
   `--github-app-id`; see
   [GitOps Repository & GitHub Token Minter Configuration](#gitops-repository--github-token-minter-configuration).

Leave `--permission-set` at `read-only` unless the operator asks for `custom`. The flags in
[Adopting a cluster the operator already owns](#adopting-a-cluster-the-operator-already-owns) are
the operator's decision, never a default you fill in.

### Stage 2: Dry run

Run the installer with the agreed flags and `--dry-run`. It validates the Terraform configuration
and, when Application Default Credentials exist, runs `terraform plan` against local state; it
creates nothing, the state bucket included:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --dry-run \
  --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

A dry run regenerates `terraform.tfvars`, so back up a real deployment's copy first. It writes no
`install.env`: a dry run provisions nothing, so it has no install to record, and an existing one is
never rewritten. It also installs no missing tool — a machine without any installer prerequisite
(`terraform`, `helm`, `gh`, `gke-gcloud-auth-plugin` and the rest) fails it with
`Dry-run validation will not install missing tools`, naming the tool; install it and re-run.

Go through the output with the operator:

- `DRY_RUN_SUCCESS` in `/tmp/kube-agents-install-report.json` does not clear an existing cluster.
  The dry run exits before the consent gates, so it never reports `REFUSED_*`.
- On an existing cluster, the `Existing Cluster Mutations (Adoption)` summary and any
  `Dry-run: skipping terraform plan because ...` warning are the questions to put to the operator,
  as [Adopting a cluster the operator already owns](#adopting-a-cluster-the-operator-already-owns)
  sets out.
- `No Application Default Credentials; skipping the resource preview` means no plan ran, and on an
  existing cluster the NetworkPolicy and node-pool checks were skipped too, so their warnings are
  missing. Have the operator run `gcloud auth application-default login` and repeat stage 2. Do not
  go to stage 3 without ADC: the apply's Terraform needs it, the installer's own auth check does not
  test it, and on an existing cluster the Workload Identity, CMEK and NetworkPolicy changes run
  before Terraform starts — the install fails after making them.
- `No ... API key was provided` is a warning, not a failure: the install would finish with an agent
  that cannot call its model. Resolve the key before stage 3.

When the operator would rather run the apply themselves, use [`--generate-only`](#generate-only-mode)
instead: it runs the consent gates and reports `REFUSED_*` itself.

### Stage 3: Confirm and apply

When the operator chooses an adoption flag in stage 2, re-run stage 2 with it: the flag changes both
the adoption summary and whether `terraform plan` runs at all, so the first preview no longer shows
what will run. Summarize the project, the cluster (new or existing, and each change the run makes to
an existing one), the namespace, the model provider and how the operator reaches the agent. Wait
for an explicit go-ahead. Then run the last stage 2 command without `--dry-run`:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

- Non-zero exit: the install did not finish. Relay the error the installer printed. A `REFUSED_*`
  status leaves the cluster unchanged — relay the options and stop.
- `SUCCESS`, or `SUCCESS_PENDING_ROLLOUT` (applied, but a deployment had not reported ready; relay
  the `kubectl rollout status` command the installer printed): relay how to reach the agent — the
  terminal commands in [Handing over a chat-less install](#handing-over-a-chat-less-install), which
  also fetch credentials, or the chat platform the operator enabled — then confirm the pods with
  `kubectl get pods -n kubeagents-system`. Relay `network_policy_enforcement` when it is not
  `enforced` ([Machine-Readable Results](#machine-readable-results)).

The `kubectl` commands in this skill assume the default namespace; under `--agent-namespace`,
substitute its value.

### Local sources and pre-confirmed runs

The same invocation runs from the official release bundle (recommended) or a checkout pinned to the
release tag; add `--dry-run` for stage 2 exactly as above. Run an apply without stages 1 and 2 only
when the operator has already confirmed these exact flags, or in CI.

```bash
curl -fsSL https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz | tar -xz
cd kube-agents-<RELEASE_VERSION>
./install.sh --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

If a Git checkout is required, clone pinned to the release tag:

```bash
git clone --branch <RELEASE_VERSION> https://github.com/gke-labs/kube-agents.git
cd kube-agents
./install.sh --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

Do not clone `main` to deploy an official release: manifests and CRD schemas on `main` evolve continuously and diverge from released container images. Running install scripts against a mismatched checkout will fail `verify_local_source_ref` to prevent deploying incompatible manifests.

## Generate-Only Mode

To generate configuration files (`install.env` and `terraform.tfvars`), run pre-apply validation checks, and hand off the apply to the operator without creating or mutating cloud resources, use `--generate-only`:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --generate-only \
  --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1"
```

In `--generate-only` mode, the installer:

1. Writes `install.env` (if absent) and `terraform/examples/full-install/terraform.tfvars`.
2. Runs the same pre-apply validation checks a real run does: the GitOps organization check, the service-account ownership check, and the existing-cluster node-pool and NetworkPolicy consent gates, but not the live-scope check, which needs the install's kubeconfig context and runs only on a run that will apply; the handoff says so. The interactive `g` answer is given after the pre-flight summary, by which point that check has run too, so the two routes differ in that one check. A cluster needing `--migrate-node-pools`, or one enforcing no NetworkPolicy that was given neither `--enable-network-policy` nor `--accept-no-network-policy`, is refused (`REFUSED_MISSING_NODE_POOL_MIGRATION`, `REFUSED_MISSING_NETWORK_POLICY`), as is one that cannot be described (`FAILED_PREFLIGHT_CLUSTER_UNREADABLE`). A `REFUSED_*` status is a question for the operator, not a flag to add: see [Adopting a cluster the operator already owns](#adopting-a-cluster-the-operator-already-owns). Step 1 has already written both files by then, so a refusal exits 1 leaving `install.env` and `terraform.tfvars` on disk — unvalidated, and with no handoff printed. Do not read the presence of `terraform.tfvars` as success; read the report status.
3. Prints a checklist of out-of-Terraform prerequisites (CMEK database encryption, Workload Identity, NetworkPolicy, GitHub App PEM import, and OTel scope) and the `lifecycle.sh apply` command with remote state variables (`KUBE_AGENTS_STATE_BUCKET` and `KUBE_AGENTS_STATE_PREFIX`).
4. Exits 0 with status `GENERATE_ONLY_SUCCESS` in `/tmp/kube-agents-install-report.json`, or exits 1 with the `REFUSED_*` / `FAILED_PREFLIGHT_*` status from step 2.

The interactive wizard also offers the same choice by answering `g` at the final confirmation step.

## Adopting a cluster the operator already owns

Installing onto a cluster somebody else made can require changing that cluster, and **modifying a
cluster the operator already owns is never a decision you make on your own.** The pre-flight
summary (`Existing Cluster Mutations (Adoption)`, also printed by `--dry-run`) lists every change
the run would make; on an agent-driven run there is no interactive prompt, so the flags are the
consent and you must obtain it before passing one. Present each pending change with its cost and ask:

- **Workload Identity pool** — enabled on the control plane without a flag when missing.
  Non-revertible. Say so before running a real install.
- **CMEK database encryption** — enabled without a flag when missing: a Cloud KMS key ring and key
  (permanent), the Cloud KMS API, and a control-plane update. `ALLOW_UNENCRYPTED_SECRETS=true`
  skips it. Say so before running a real install.
- **Legacy node pools** (`REFUSED_MISSING_NODE_POOL_MIGRATION`) — two answers: `--migrate-node-pools`
  recreates every node on those pools and restarts the workloads on them, kube-agents' or not; or
  stop. There is no install without Workload Identity.
- **No NetworkPolicy enforcement** (`REFUSED_MISSING_NETWORK_POLICY`) — three answers, and you
  present all three:
  1. `--enable-network-policy` enables the legacy Calico addon: a control-plane update that may
     recreate node pools and restart workloads unrelated to kube-agents.
  2. `--accept-no-network-policy` installs without enforcement. The cluster is not modified. Every
     NetworkPolicy kube-agents ships is inert: the agent pod's egress confinement, the shell
     sandbox's deny-all (the sandbox is where model-authored commands run), and the LiteLLM,
     minter and Hindsight policies. The confinement lost is kube-agents'
     own, not the operator's workloads'; "we trust the workloads in this cluster" does not answer
     it. Recorded in the report and on the `PlatformAgent`.
  3. Stop. The cluster is unchanged.
- **gVisor node pool** — on an existing Standard cluster `--enable-gvisor=true` (the default) adds a
  billable `gvisor-pool` node pool. Say so; `--enable-gvisor=false` runs the agent unsandboxed instead.

A refusal is not a failure to route around: a `REFUSED_*` status with an unchanged cluster is the
installer doing its job. Report it, relay the options above, and pass a flag only when the operator
has chosen. When they choose `--accept-no-network-policy` and an `install.env` already exists, tell
them to add `ACCEPT_NO_NETWORK_POLICY=true` to it (the installer prints the same instruction): the
next `upgrade.sh` regenerates from that file and is refused without the key. When they later
confine the cluster, tell them to remove the key again; the installer warns while it lingers.

## Source verification

Before provisioning, the installer requires the checkout holding the Terraform configuration and
chart to be at the same commit as `--image-tag` and to have no uncommitted changes — the install
sources and the container image must come from one revision. A dirty or mismatched checkout aborts with instructions.
`--allow-unverified-source` (or `ALLOW_UNVERIFIED_SOURCE=true`) downgrades that to a warning; use it
when iterating on the installer itself, not for a deployment you intend to keep. `--dry-run` is
lenient already.

## GCP IAM permission sets

`--permission-set` chooses which GCP IAM role bundle the composition grants the agent's GSA (its
`permission_set` variable; `custom` becomes a `project_roles` list). It does **not** affect
Kubernetes RBAC, which is read-only in every set, and it does not gate the GitOps pull-request
path, which works in every set. See the site's
[security and IAM reference](../../../docs/site/src/content/docs/reference/security-and-iam.md).

| Set         | Grants                                                            |
| ----------- | ----------------------------------------------------------------- |
| `read-only` | Viewer roles only — no GCP write capability. **Default.**         |
| `custom`    | Exactly the roles passed in `--custom-roles`; no built-in bundle. |

## Machine-Readable Results

Upon completion, `install.sh` generates a machine-readable JSON status report at `/tmp/kube-agents-install-report.json`:

```json
{
  "status": "SUCCESS",
  "dry_run": false,
  "generate_only": false,
  "non_interactive": true,
  "project_id": "YOUR_GCP_PROJECT_ID",
  "cluster_name": "platform-agent-host",
  "timestamp": "2026-08-05T03:35:00Z"
}
```

The full report also carries `gvisor_enabled`, `memory_mode`, and `network_policy_enforcement`. A
report written before the run decided them says so: `gvisor_enabled` is `null` before the interview,
and the other two are empty, rather than restating a default the run never applied. For
`network_policy_enforcement` that covers a run that failed early, a `--dry-run` (it never reaches the
cluster step; only with `--accept-no-network-policy` against a cluster that enforces nothing does it
report `absent-accepted`), and a `--generate-only` run under `--enable-network-policy`, which has
not enabled anything yet.
`network_policy_enforcement` is `enforced` (Dataplane V2 or Calico, or a cluster this run created),
`enabled-by-install` (this run turned Calico on, under `--enable-network-policy`), or
`absent-accepted` (installed without enforcement, under `--accept-no-network-policy`); the last is
also stamped onto the `PlatformAgent` as `kubeagents.x-k8s.io/network-policy-enforcement`, so it
outlives the report. Relay it to the operator in either of the last two cases.

## Handing over a chat-less install

Chat is opt-in and both platforms default off, and a non-interactive run with no
`--enable-google-chat` or `--enable-slack` selects "None" — so the common agent-driven install
has no chat and the operator has no way to reach the agent unless you give them one. The
installer prints it at the chat-configuration step and again when it finishes, under
`--- [Talking to the Agent from a Terminal] ---`, with the cluster, region, project and namespace
already substituted. Relay those two commands verbatim:

```bash
gcloud container clusters get-credentials <CLUSTER_NAME> --location <REGION> --project <PROJECT_ID> --dns-endpoint
kubectl exec -it deployment/platform-agent-gateway -n kubeagents-system -c platform-agent -- hermes -p platform
```

- Take `--dns-endpoint` from what the installer printed rather than adding it: `gcloud` rejects it
  on a cluster publishing no externally reachable DNS endpoint, and the installer omits it there.
- `-p platform` reaches the Platform Agent. A bare `hermes` reaches the Planning Agent front door,
  which is where a chat message would have landed.
- `kubectl port-forward` is not an alternative: the agent is sandboxed under gVisor by default and
  the forward cannot see into the sandbox. `kubectl exec` enters it.
- Say what the operator does not get until they enable a platform: scheduled reports and
  alert-driven triage are delivered to chat only, and the first-run inventory report waits on a
  human connecting over chat — read it with
  `kubectl exec deployment/platform-agent-gateway -n kubeagents-system -c platform-agent -- cat /opt/data/INVENTORY.md`.

## GitOps Repository & GitHub Token Minter Configuration

When deploying `kube-agents` with GitOps pull-request workflows enabled, the Platform Agent creates pull requests against an infrastructure-as-code repository via the [GitHub Token Minter](https://github.com/abcxyz/github-token-minter) (`minty`). The minter runs in-cluster and signs short-lived GitHub App installation tokens using a private key securely stored in Google Cloud KMS.

### Prerequisites

The GitHub App and its permissions, the private key, the Cloud KMS signing key and its default names, and the Go toolchain the import needs are documented once, in [Token minter](https://gke-labs.github.io/kube-agents/deploy/token-minter/). Read it before running either path below. Restating those values here is how the two copies drift apart, and this file already defers the same way for flag defaults.

One input decides whether the minter can work at all, so it is worth stating where the command is: `--gitops-org` must be a GitHub **organization**. Minty resolves App installations at `/orgs/{org}/installation`, which returns 404 for a personal account, and `install.sh` refuses one rather than deploying a minter that can never mint a token.

The commands below are stage 2 dry runs showing the GitOps flags. Replace the placeholders and example values with the operator's values from stage 1, review the output as stage 2 describes, and apply through stage 3 once the operator confirms — Path 1 creates a Cloud KMS key ring and key, which cannot be deleted.

### Deployment Path 1: Automated Import via `install.sh`

In this path, `install.sh` automatically creates the Cloud KMS keyring/key (if missing) and imports the GitHub App private key using the Minty CLI before Terraform applies:

```bash
./install.sh --dry-run --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --gitops-org="YOUR_GITHUB_ORG" \
  --gitops-repo="YOUR_GITOPS_REPO" \
  --github-app-id="YOUR_GITHUB_APP_ID" \
  --github-pem-path="/path/to/app-private-key.pem"
```

Delete the `.pem` once the stage 3 apply succeeds. Cloud KMS keys cannot be destroyed, and a later run or upgrade finds the `ENABLED` version and skips the import.

### Deployment Path 2: Pre-Provisioned / Ahead-Of-Time (AOT) Key

For CI/CD pipelines and anywhere runners must not handle raw private keys, the key is imported ahead of time — the procedure, and the keyring and key names `install.sh` expects, are in [Token minter](https://gke-labs.github.io/kube-agents/deploy/token-minter/). Once the key holds an `ENABLED` version, invoke `install.sh` without `--github-pem-path`:

```bash
./install.sh --dry-run --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --gitops-org="YOUR_GITHUB_ORG" \
  --gitops-repo="YOUR_GITOPS_REPO" \
  --github-app-id="YOUR_GITHUB_APP_ID"
```

## Supported Command-Line Flags

Defaults marked "`installer_common.sh`" reach the installer through
`scripts/installer/installer_common.sh`; the values themselves are listed in
`install.defaults.env` at the repository root, not here. Run `./install.sh --help` for the authoritative list.

| Flag                                    | Description                                                                                                                                                                                                                                                           | Default                                                                                                                                            |
| :-------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-y, --non-interactive`                 | Run without blocking on `/dev/tty` prompts                                                                                                                                                                                                                            | `false`                                                                                                                                            |
| `--dry-run`                             | Output plan and `terraform.tfvars` without creating resources                                                                                                                                                                                                         | `false`                                                                                                                                            |
| `--generate-only`                       | Write `install.env` and `terraform.tfvars`, run the pre-apply checks, print the operator handoff, and exit without creating or mutating resources. Mutually exclusive with `--dry-run`                                                                                | `false`                                                                                                                                            |
| `--menu, --config`                      | Launch the Day-2 control panel instead of installing                                                                                                                                                                                                                  | `false`                                                                                                                                            |
| `--gcp-project-id=ID`                   | Target GCP Project ID                                                                                                                                                                                                                                                 | Active `gcloud` project                                                                                                                            |
| `--gcp-region=REGION`                   | Target GCP Region                                                                                                                                                                                                                                                     | `installer_common.sh` `DEFAULT_REGION`                                                                                                             |
| `--gke-cluster-name=NAME`               | GKE Cluster Name                                                                                                                                                                                                                                                      | `installer_common.sh` `DEFAULT_CLUSTER_NAME`                                                                                                       |
| `--gke-cluster-mode=MODE`               | Shape of a cluster this run creates: `autopilot` \| `standard`. Autopilot is regional: unset at a zonal `--gcp-region` builds `standard`, explicit `autopilot` there is an error. No bearing on an existing cluster, whose live shape the generator probes            | `autopilot`                                                                                                                                        |
| `--agent-namespace=NAME`                | Kubernetes namespace the agent and operator are installed into                                                                                                                                                                                                        | `installer_common.sh` `DEFAULT_NAMESPACE`                                                                                                          |
| `--image-tag=TAG`                       | Validated immutable release tag or full commit SHA (developer/CI only)                                                                                                                                                                                                | Developer and CI/CD testing only; end users must use official release installations. Default: inferred from baked release, bundle, or local `HEAD` |
| `--registry-prefix=PATH`                | Registry path (no URL scheme) for the first-party images this project builds                                                                                                                                                                                          | `installer_common.sh` `DEFAULT_REGISTRY_PREFIX`                                                                                                    |
| `--third-party-registry-prefix=PATH`    | Registry path holding the mirrored third-party images (cert-manager, LiteLLM, fluent-bit, token minter, Hindsight). Not implied by `--registry-prefix`                                                                                                                | _unset_ — upstream registries                                                                                                                      |
| `--allow-unverified-source`             | Provision from a dirty or mismatched checkout                                                                                                                                                                                                                         | `false`                                                                                                                                            |
| `--model-provider=NAME`                 | `gemini` \| `vertex_ai` \| `anthropic` \| `openai`                                                                                                                                                                                                                    | `installer_common.sh` `DEFAULT_MODEL_PROVIDER`                                                                                                     |
| `--vertex-location=LOCATION`            | Vertex AI serving location, a region or `global`. The global endpoint gives no in-region ML processing guarantee                                                                                                                                                      | `installer_common.sh` `DEFAULT_VERTEX_LOCATION`                                                                                                    |
| `--gemini-api-key=KEY`                  | Gemini API key                                                                                                                                                                                                                                                        | Looked up in Secret Manager                                                                                                                        |
| `--openai-api-key=KEY`                  | OpenAI API key                                                                                                                                                                                                                                                        | _unset_                                                                                                                                            |
| `--anthropic-api-key=KEY`               | Anthropic API key                                                                                                                                                                                                                                                     | _unset_                                                                                                                                            |
| `--permission-set=SET`                  | Agent GCP IAM set: `read-only` \| `custom`                                                                                                                                                                                                                            | `read-only`                                                                                                                                        |
| `--custom-roles=ROLES`                  | Roles for `--permission-set=custom` (space- or comma-separated)                                                                                                                                                                                                       | _unset_                                                                                                                                            |
| `--scope-projects=IDS`                  | GCP projects beyond the install's whose GKE clusters get a Cluster Agent (space- or comma-separated); the agent's service account is granted the read roles in each. Recorded in `install.env` as `SCOPE_PROJECTS`; a flag on a re-run applies for that run and warns | _unset_ (the install's project alone)                                                                                                              |
| `--scope-exclude-projects=IDS`          | Project IDs or shell-style globs (`*-sandbox`) to leave unmanaged                                                                                                                                                                                                     | _unset_                                                                                                                                            |
| `--scope-exclude-clusters=TRIPLES`      | Clusters to leave unmanaged, each as `project/location/cluster`                                                                                                                                                                                                       | _unset_                                                                                                                                            |
| `--gitops-org=ORG`                      | GitHub organization for the GitOps IaC repository                                                                                                                                                                                                                     | _unset_                                                                                                                                            |
| `--gitops-repo=REPO`                    | GitOps IaC repository name                                                                                                                                                                                                                                            | `gke-fleet-iac`                                                                                                                                    |
| `--github-app-id=ID`                    | Numeric GitHub App ID for the token minter                                                                                                                                                                                                                            | _unset_                                                                                                                                            |
| `--github-pem-path=PATH`                | Path to downloaded GitHub App private key (`.pem`) for initial Cloud KMS import                                                                                                                                                                                       | _unset_                                                                                                                                            |
| `--kms-keyring=NAME`                    | Cloud KMS Key Ring name for the token minter key                                                                                                                                                                                                                      | `installer_common.sh` `DEFAULT_KMS_KEYRING`                                                                                                        |
| `--kms-key=NAME`                        | Cloud KMS CryptoKey name for the token minter signing key                                                                                                                                                                                                             | `installer_common.sh` `DEFAULT_KMS_KEY`                                                                                                            |
| `--enable-google-chat`                  | Enable the Google Chat integration                                                                                                                                                                                                                                    | `false`                                                                                                                                            |
| `--google-chat-allowed-users=EMAILS`    | Comma-separated chat users allowed to reach the agent; empty allows everyone                                                                                                                                                                                          | _unset_                                                                                                                                            |
| `--enable-slack[=true\|false]`          | Enable the Slack socket-mode relay (non-interactive runs require `--slack-bot-token` and `--slack-app-token`)                                                                                                                                                         | `false`                                                                                                                                            |
| `--slack-bot-token=TOKENS`              | Comma-separated Slack bot tokens (`xoxb-...`), one per workspace the agent serves                                                                                                                                                                                     | _unset_                                                                                                                                            |
| `--slack-app-token=TOKEN`               | Slack socket-mode app-level token (`xapp-...`)                                                                                                                                                                                                                        | _unset_                                                                                                                                            |
| `--slack-allowed-users=USERS`           | Comma-separated Slack user IDs allowed to reach the agent; empty allows everyone                                                                                                                                                                                      | _unset_                                                                                                                                            |
| `--slack-home-channel=CHANNEL`          | Slack channel ID for unsolicited alerts/messages (e.g. `C01234567`)                                                                                                                                                                                                   | _unset_                                                                                                                                            |
| `--slack-home-channel-name=NAME`        | Display name of that channel (e.g. `#gke-alerts`)                                                                                                                                                                                                                     | _unset_                                                                                                                                            |
| `--enable-gvisor=true\|false`           | Enable GKE Sandbox (gVisor) runtime isolation                                                                                                                                                                                                                         | `true`                                                                                                                                             |
| `--enable-hermes-dashboard=true\|false` | Enable the Hermes Web UI on port 9119                                                                                                                                                                                                                                 | `false`                                                                                                                                            |
| `--enable-gke-backup-plan=true\|false`  | Provision a GKE Backup Plan for the cluster                                                                                                                                                                                                                           | `false`                                                                                                                                            |
| `--migrate-node-pools`                  | Authorize migrating legacy GCE metadata server node pools to `GKE_METADATA` (recreates those nodes, restarts their workloads). Without it such a cluster is refused unchanged. The cluster's owner decides; never pass it unasked                                     | `false`                                                                                                                                            |
| `--enable-network-policy`               | Authorize enabling the legacy Calico NetworkPolicy addon on an existing Standard cluster without Dataplane V2 (may recreate nodes). One of two answers; with neither the cluster is refused unchanged. The owner decides; never pass it unasked                       | `false`                                                                                                                                            |
| `--accept-no-network-policy`            | The other answer: install without modifying the cluster. Every NetworkPolicy kube-agents ships is then inert, the agent sandbox's included; recorded in the report and on the `PlatformAgent`. The owner decides; never pass it unasked                               | `false`                                                                                                                                            |
| `--memory=MODE`                         | Long-term agent memory engine: `file` \| `hindsight` \| `off`                                                                                                                                                                                                         | `file`                                                                                                                                             |
| `-h, --help, -?`                        | Output CLI usage banner and parameter details                                                                                                                                                                                                                         | `N/A`                                                                                                                                              |
