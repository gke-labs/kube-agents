---
name: install-kube-agents
description: Install, set up, or deploy kube-agents (the Kubernetes Agentic Harness) and its Platform Agent onto a GKE cluster in a GCP project, interactively or non-interactively. Use when asked to install, bootstrap, or onboard kube-agents, or to plan one with a dry run.
---

# `install-kube-agents` Skill

This skill provides step-by-step instructions for AI Agents to non-interactively provision Google Cloud GKE infrastructure and deploy the `kube-agents` Platform Agent.

## What `install.sh` actually does

It is a front-end, not a second provisioner. It loads `install.env` (the install's
hand-authored configuration), collects anything still missing, generates
`terraform/examples/full-install/terraform.tfvars` from the result, and then runs the composition's
`lifecycle.sh apply` — the Terraform root in
[`terraform/examples/full-install/`](../../../terraform/examples/full-install/README.md) owns every
GCP resource and installs the Helm chart (`charts/kube-agents`) that owns every Kubernetes one.
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

## Quick Execution for AI Agents

For production installations, AI Agents and automated pipelines must target an official release version. When recommending commands to users or executing deployments, AI Agents must resolve the latest stable release tag from [GitHub Releases](https://github.com/gke-labs/kube-agents/releases) (e.g. `0.4.0`) and provide executable commands with that exact release version substituted, rather than leaving an unrendered `<RELEASE_VERSION>` placeholder:

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --permission-set="read-only"
```

If deploying from local sources instead of piping the script via curl, AI Agents should unpack the official release bundle (recommended):

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

Alternatively, if a Git checkout is specifically required, clone pinned to the target release tag:

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

1. Writes `install.env` (if absent) and `terraform/examples/full-install/terraform.tfvars`. One refusal lives inside this step rather than after it: if the cluster already exists, nothing stated a memory mode (no `--memory=`, no `MEMORY` in `install.env`), and the run cannot ask the cluster whether it is running the Hindsight memory store, the generator stops instead of defaulting to a value whose apply would delete the store's database. That applies under `--generate-only` and `--dry-run` as well, because both write the same real `terraform.tfvars` and `--generate-only` exists to hand it to `lifecycle.sh apply`. It exits 1 having written no `terraform.tfvars` at all — the opposite of the step-2 refusals below. Pass `--memory=hindsight|file|off`, record `MEMORY` in `install.env`, or restore access to the cluster.
2. Runs the same pre-apply validation checks a real run does: the GitOps organization check, the service-account ownership check, and the existing-cluster node-pool and NetworkPolicy consent gates. A cluster needing `--migrate-node-pools`, or one enforcing no NetworkPolicy that was given neither `--enable-network-policy` nor `--accept-no-network-policy`, is refused (`REFUSED_MISSING_NODE_POOL_MIGRATION`, `REFUSED_MISSING_NETWORK_POLICY`), as is one that cannot be described (`FAILED_PREFLIGHT_CLUSTER_UNREADABLE`). A `REFUSED_*` status is a question for the operator, not a flag to add: see [Adopting a cluster the operator already owns](#adopting-a-cluster-the-operator-already-owns). Step 1 has already written both files by then, so a refusal exits 1 leaving `install.env` and `terraform.tfvars` on disk — unvalidated, and with no handoff printed. Do not read the presence of `terraform.tfvars` as success; read the report status.
3. Prints a checklist of out-of-Terraform prerequisites (CMEK database encryption, Workload Identity, NetworkPolicy, GitHub App PEM import, and OTel scope) and the `lifecycle.sh apply` command with remote state variables (`KUBE_AGENTS_STATE_BUCKET` and `KUBE_AGENTS_STATE_PREFIX`).
4. Exits 0 with status `GENERATE_ONLY_SUCCESS` in `/tmp/kube-agents-install-report.json`, or exits 1 with the `REFUSED_*` / `FAILED_PREFLIGHT_*` status from step 2.

The interactive wizard also offers the same choice by answering `g` at the final confirmation step.

## Dry-Run Inspection

To validate prerequisites and preview the install without creating GCP resources, AI Agents must use `--dry-run` with the official release installer (substituting `<RELEASE_VERSION>` with the resolved release version):

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/install.sh | bash -s -- \
  --dry-run \
  --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID"
```

A dry run regenerates `terraform.tfvars`, so back that up first if a real deployment's copy is
already there. It writes no `install.env`: a dry run provisions nothing, so it has no install to
record, and an existing one is never rewritten.

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

### Deployment Path 1: Automated Import via `install.sh`

In this path, `install.sh` automatically creates the Cloud KMS keyring/key (if missing) and imports the GitHub App private key using the Minty CLI before Terraform applies:

```bash
./install.sh --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --gitops-org="YOUR_GITHUB_ORG" \
  --gitops-repo="YOUR_GITOPS_REPO" \
  --github-app-id="YOUR_GITHUB_APP_ID" \
  --github-pem-path="/path/to/app-private-key.pem"
```

Delete the `.pem` once the run succeeds. Cloud KMS keys cannot be destroyed, and a later run or upgrade finds the `ENABLED` version and skips the import.

### Deployment Path 2: Pre-Provisioned / Ahead-Of-Time (AOT) Key

For CI/CD pipelines and anywhere runners must not handle raw private keys, the key is imported ahead of time — the procedure, and the keyring and key names `install.sh` expects, are in [Token minter](https://gke-labs.github.io/kube-agents/deploy/token-minter/). Once the key holds an `ENABLED` version, invoke `install.sh` without `--github-pem-path`:

```bash
./install.sh --non-interactive \
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

| Flag                                    | Description                                                                                                                                                                                                                                                | Default                                                                                                                                            |
| :-------------------------------------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-y, --non-interactive`                 | Run without blocking on `/dev/tty` prompts                                                                                                                                                                                                                 | `false`                                                                                                                                            |
| `--dry-run`                             | Output plan and `terraform.tfvars` without creating resources                                                                                                                                                                                              | `false`                                                                                                                                            |
| `--generate-only`                       | Write `install.env` and `terraform.tfvars`, run the pre-apply checks, print the operator handoff, and exit without creating or mutating resources. Mutually exclusive with `--dry-run`                                                                     | `false`                                                                                                                                            |
| `--menu, --config`                      | Launch the Day-2 control panel instead of installing                                                                                                                                                                                                       | `false`                                                                                                                                            |
| `--gcp-project-id=ID`                   | Target GCP Project ID                                                                                                                                                                                                                                      | Active `gcloud` project                                                                                                                            |
| `--gcp-region=REGION`                   | Target GCP Region                                                                                                                                                                                                                                          | `installer_common.sh` `DEFAULT_REGION`                                                                                                             |
| `--gke-cluster-name=NAME`               | GKE Cluster Name                                                                                                                                                                                                                                           | `installer_common.sh` `DEFAULT_CLUSTER_NAME`                                                                                                       |
| `--gke-cluster-mode=MODE`               | Shape of a cluster this run creates: `autopilot` \| `standard`. Autopilot is regional: unset at a zonal `--gcp-region` builds `standard`, explicit `autopilot` there is an error. No bearing on an existing cluster, whose live shape the generator probes | `autopilot`                                                                                                                                        |
| `--agent-namespace=NAME`                | Kubernetes namespace the agent and operator are installed into                                                                                                                                                                                             | `installer_common.sh` `DEFAULT_NAMESPACE`                                                                                                          |
| `--image-tag=TAG`                       | Validated immutable release tag or full commit SHA (developer/CI only)                                                                                                                                                                                     | Developer and CI/CD testing only; end users must use official release installations. Default: inferred from baked release, bundle, or local `HEAD` |
| `--registry-prefix=PATH`                | Registry path (no URL scheme) for the first-party images this project builds                                                                                                                                                                               | `installer_common.sh` `DEFAULT_REGISTRY_PREFIX`                                                                                                    |
| `--third-party-registry-prefix=PATH`    | Registry path holding the mirrored third-party images (cert-manager, LiteLLM, fluent-bit, token minter, Hindsight). Not implied by `--registry-prefix`                                                                                                     | _unset_ — upstream registries                                                                                                                      |
| `--allow-unverified-source`             | Provision from a dirty or mismatched checkout                                                                                                                                                                                                              | `false`                                                                                                                                            |
| `--model-provider=NAME`                 | `gemini` \| `vertex_ai` \| `anthropic` \| `openai`                                                                                                                                                                                                         | `installer_common.sh` `DEFAULT_MODEL_PROVIDER`                                                                                                     |
| `--vertex-location=LOCATION`            | Vertex AI serving location, a region or `global`. The global endpoint gives no in-region ML processing guarantee                                                                                                                                           | `installer_common.sh` `DEFAULT_VERTEX_LOCATION`                                                                                                    |
| `--gemini-api-key=KEY`                  | Gemini API key                                                                                                                                                                                                                                             | Looked up in Secret Manager                                                                                                                        |
| `--openai-api-key=KEY`                  | OpenAI API key                                                                                                                                                                                                                                             | _unset_                                                                                                                                            |
| `--anthropic-api-key=KEY`               | Anthropic API key                                                                                                                                                                                                                                          | _unset_                                                                                                                                            |
| `--permission-set=SET`                  | Agent GCP IAM set: `read-only` \| `custom`                                                                                                                                                                                                                 | `read-only`                                                                                                                                        |
| `--custom-roles=ROLES`                  | Roles for `--permission-set=custom` (space- or comma-separated)                                                                                                                                                                                            | _unset_                                                                                                                                            |
| `--gitops-org=ORG`                      | GitHub organization for the GitOps IaC repository                                                                                                                                                                                                          | _unset_                                                                                                                                            |
| `--gitops-repo=REPO`                    | GitOps IaC repository name                                                                                                                                                                                                                                 | `gke-fleet-iac`                                                                                                                                    |
| `--github-app-id=ID`                    | Numeric GitHub App ID for the token minter                                                                                                                                                                                                                 | _unset_                                                                                                                                            |
| `--github-pem-path=PATH`                | Path to downloaded GitHub App private key (`.pem`) for initial Cloud KMS import                                                                                                                                                                            | _unset_                                                                                                                                            |
| `--kms-keyring=NAME`                    | Cloud KMS Key Ring name for the token minter key                                                                                                                                                                                                           | `installer_common.sh` `DEFAULT_KMS_KEYRING`                                                                                                        |
| `--kms-key=NAME`                        | Cloud KMS CryptoKey name for the token minter signing key                                                                                                                                                                                                  | `installer_common.sh` `DEFAULT_KMS_KEY`                                                                                                            |
| `--enable-google-chat`                  | Enable the Google Chat integration                                                                                                                                                                                                                         | `false`                                                                                                                                            |
| `--google-chat-allowed-users=EMAILS`    | Comma-separated chat users allowed to reach the agent; empty allows everyone                                                                                                                                                                               | _unset_                                                                                                                                            |
| `--enable-slack[=true\|false]`          | Enable the Slack socket-mode relay (non-interactive runs require `--slack-bot-token` and `--slack-app-token`)                                                                                                                                              | `false`                                                                                                                                            |
| `--slack-bot-token=TOKENS`              | Comma-separated Slack bot tokens (`xoxb-...`), one per workspace the agent serves                                                                                                                                                                          | _unset_                                                                                                                                            |
| `--slack-app-token=TOKEN`               | Slack socket-mode app-level token (`xapp-...`)                                                                                                                                                                                                             | _unset_                                                                                                                                            |
| `--slack-allowed-users=USERS`           | Comma-separated Slack user IDs allowed to reach the agent; empty allows everyone                                                                                                                                                                           | _unset_                                                                                                                                            |
| `--slack-home-channel=CHANNEL`          | Slack channel ID for unsolicited alerts/messages (e.g. `C01234567`)                                                                                                                                                                                        | _unset_                                                                                                                                            |
| `--slack-home-channel-name=NAME`        | Display name of that channel (e.g. `#gke-alerts`)                                                                                                                                                                                                          | _unset_                                                                                                                                            |
| `--enable-gvisor=true\|false`           | Enable GKE Sandbox (gVisor) runtime isolation                                                                                                                                                                                                              | `true`                                                                                                                                             |
| `--enable-hermes-dashboard=true\|false` | Enable the Hermes Web UI on port 9119                                                                                                                                                                                                                      | `false`                                                                                                                                            |
| `--enable-gke-backup-plan=true\|false`  | Provision a GKE Backup Plan for the cluster                                                                                                                                                                                                                | `false`                                                                                                                                            |
| `--migrate-node-pools`                  | Authorize migrating legacy GCE metadata server node pools to `GKE_METADATA` (recreates those nodes, restarts their workloads). Without it such a cluster is refused unchanged. The cluster's owner decides; never pass it unasked                          | `false`                                                                                                                                            |
| `--enable-network-policy`               | Authorize enabling the legacy Calico NetworkPolicy addon on an existing Standard cluster without Dataplane V2 (may recreate nodes). One of two answers; with neither the cluster is refused unchanged. The owner decides; never pass it unasked            | `false`                                                                                                                                            |
| `--accept-no-network-policy`            | The other answer: install without modifying the cluster. Every NetworkPolicy kube-agents ships is then inert, the agent sandbox's included; recorded in the report and on the `PlatformAgent`. The owner decides; never pass it unasked                    | `false`                                                                                                                                            |
| `--memory=MODE`                         | Long-term agent memory engine: `file` \| `hindsight` \| `off`                                                                                                                                                                                              | `file`                                                                                                                                             |
| `-h, --help, -?`                        | Output CLI usage banner and parameter details                                                                                                                                                                                                              | `N/A`                                                                                                                                              |
