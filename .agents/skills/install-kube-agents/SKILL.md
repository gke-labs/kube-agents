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
(e.g. `0.4.0`; a GitLab install needs `0.9.0` or later) first, and substitute it for `<RELEASE_VERSION>` in every command you run or hand
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

No `ADC: ok` means no Application Default Credentials. Stage 3 cannot run without them unless one
of the google provider's credential variables is set — `GOOGLE_OAUTH_ACCESS_TOKEN`,
`GOOGLE_CREDENTIALS`, `GOOGLE_CLOUD_KEYFILE_JSON` or `GCLOUD_KEYFILE_JSON`, which Terraform reads
before ADC (stage 2 says why). With none of them set, have the operator run
`gcloud auth application-default login` now.

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
   gives its access path.
5. **Where the GitOps repository lives**, only if the operator wants GitOps pull requests. Ask
   which forge holds it. On GitHub, pass `--gitops-org`, `--gitops-repo` and `--github-app-id`; see
   [GitOps Repository & GitHub Token Minter Configuration](#gitops-repository--github-token-minter-configuration).
   On GitLab (gitlab.com or a self-managed instance), pass `--gitops-forge=gitlab`, `--gitops-repo`
   and, for a self-managed instance, `--gitops-host`; see
   [GitOps Repository on GitLab](#gitops-repository-on-gitlab). Never combine the GitHub App flags
   with `--gitops-forge=gitlab`: the installer refuses them. On a GitHub Enterprise Server, pass the
   GitHub flags and `--gitops-host` (and `--gitops-ca-file` for a private CA); see
   [GitHub Enterprise Server](#github-enterprise-server).

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
  go to stage 3 without ADC or one of the provider's credential variables
  (`GOOGLE_OAUTH_ACCESS_TOKEN`, `GOOGLE_CREDENTIALS`, `GOOGLE_CLOUD_KEYFILE_JSON`,
  `GCLOUD_KEYFILE_JSON`): the apply's Terraform needs one, the installer's own auth check tests
  neither, and on an existing cluster the Workload Identity, CMEK and NetworkPolicy changes run
  before Terraform starts — the install fails after making them. A credential variable does not
  bring the preview back, since the dry run's plan gate checks ADC alone.
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

1. Writes `install.env` (if absent) and `terraform/examples/full-install/terraform.tfvars`. One refusal lives inside this step rather than after it: if the cluster already exists, nothing stated a memory mode (no `--memory=`, no `MEMORY` in `install.env`), and the run cannot ask the cluster whether it is running the Hindsight memory store, the generator stops instead of defaulting to a value whose apply would delete the store's database. That applies under `--generate-only` and `--dry-run` as well, because both write the same real `terraform.tfvars` and `--generate-only` exists to hand it to `lifecycle.sh apply`. It exits 1 having written no `terraform.tfvars` at all — the opposite of the step-2 refusals below. Pass `--memory=hindsight|file|off`, record `MEMORY` in `install.env`, or restore access to the cluster.
2. Runs the same pre-apply validation checks a real run does: the GitOps organization check, the service-account ownership check, the scope container preflight when a folder or organisation is declared, which on this route, and on the interactive `g` answer, only warns (the apply it hands off may run as another identity; a run that applies, `-y` or the interactive `Y`, refuses, exit 1 with no report status like the ownership check, when the credentials Terraform applies with cannot set IAM policy on a container or an enforced organisation policy forbids the Cloud Asset API), the scope export sink preflight when the drift ingress is on and `SCOPE_PROJECTS` lists a project the drift sinks will cover, which warns and refuses on the same routes when those credentials cannot create a Log Router sink (`logging.sinks.create`) in one of them (a project whose sink the install's Terraform state already holds warns instead when the credentials can read it), and the existing-cluster node-pool and NetworkPolicy consent gates, but not the live-scope check, which needs the install's kubeconfig context and runs only on a run that will apply; the handoff says so. The interactive `g` answer is given after the pre-flight summary, by which point that check has run too, so the two routes differ in that one check. A cluster needing `--migrate-node-pools`, or one enforcing no NetworkPolicy that was given neither `--enable-network-policy` nor `--accept-no-network-policy`, is refused (`REFUSED_MISSING_NODE_POOL_MIGRATION`, `REFUSED_MISSING_NETWORK_POLICY`), as is one that cannot be described (`FAILED_PREFLIGHT_CLUSTER_UNREADABLE`). A `REFUSED_*` status is a question for the operator, not a flag to add: see [Adopting a cluster the operator already owns](#adopting-a-cluster-the-operator-already-owns). Step 1 has already written both files by then, so a refusal exits 1 leaving `install.env` and `terraform.tfvars` on disk — unvalidated, and with no handoff printed. Do not read the presence of `terraform.tfvars` as success; read the report status.
3. Prints a checklist of out-of-Terraform prerequisites (CMEK database encryption, Workload Identity, NetworkPolicy, GitHub App PEM import, and OTel scope) and the `lifecycle.sh apply` command with remote state variables (`KUBE_AGENTS_STATE_BUCKET` and `KUBE_AGENTS_STATE_PREFIX`).
4. Exits 0 with status `GENERATE_ONLY_SUCCESS` in `/tmp/kube-agents-install-report.json`, or exits 1 with the `REFUSED_*` / `FAILED_PREFLIGHT_*` status from step 2.

On a GitLab install, step 2 skips the GitOps organization check: there is no GitHub organization.
Generate-only never creates the Secrets (the token Secret, and the CA Secret for a private CA). On a
first install it writes the GitLab keys into `install.env`. Over an existing `install.env` it does
not change them: have the operator set `GITOPS_FORGE`, `GITOPS_HOST`, `GITOPS_REPO`,
`GITLAB_TOKEN_SECRET` and, for a private CA, `GITOPS_CA_SECRET` there before
`lifecycle.sh apply`. Otherwise the next `upgrade.sh` removes the GitLab forge. After
`lifecycle.sh apply`, the person who runs the install creates the Secrets by hand (Terraform only
names them), as
[Dry run and generate-only](https://gke-labs.github.io/kube-agents/install/gitlab/#dry-run-and-generate-only)
shows.

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

| Set         | Grants                                                                           |
| ----------- | -------------------------------------------------------------------------------- |
| `read-only` | Auditing and observability: read roles, no write to infrastructure. **Default.** |
| `custom`    | Exactly the roles passed in `--custom-roles`; no built-in bundle.                |

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
  `kubectl exec platform-agent-shell-0 -n kubeagents-system -c shell -- cat /opt/data/INVENTORY.md`
  (the report is on the shell sandbox pod, not the agent pod).

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

### GitHub Enterprise Server

If the operator's GitOps repository is on a GitHub Enterprise Server (a GitHub instance at a host they run, not github.com), `install.sh`, `upgrade.sh` and `uninstall.sh` install, upgrade and remove it, with `--gitops-host` and, for a private CA, `--gitops-ca-file`. Follow [GitHub Enterprise Server in INSTALL.md](../../../INSTALL.md#github-enterprise-server) with them. Its steps and their order:

1. Decide the cluster. A new cluster goes into the project's `default` VPC network (the installer has no network setting). If the instance is reachable only from another VPC or a Shared VPC, the operator creates the cluster in that VPC, and you install onto it with `--gke-cluster-name`.
2. On an existing cluster, check the network path now (DNS and HTTPS on port 443) with the two `kubectl run` commands in INSTALL.md. On a new cluster, run them right after the install. A private instance needs a route, firewall rules and, for a private DNS zone, a Cloud DNS forwarding zone; those are the operator's to set up.
3. The operator creates the GitHub App on the instance (not on github.com), with the same permissions as on github.com and no webhook, installed on the organization, the GitOps repository and each context repository, and downloads its private key. The repository must exist and belong to an organization.
4. Run `install.sh --dry-run` with `--gitops-host=<host>`, `--gitops-org`, `--gitops-repo` (the name, not a URL), `--github-app-id`, `--github-pem-path` and, only for a private CA, `--gitops-ca-file=<regular PEM file>`. Show the operator the summary. Then run it without `--dry-run`. The installer imports the App's key into Cloud KMS before the apply, writes the CA into the Secret `github-forge-ca` after the apply, and restarts the token minter so that it reads the CA.
5. Check the minter's `SOURCE_SYSTEM_API_BASE_URL` and the gitops-state ConfigMap's `github_host` with the commands in INSTALL.md.

Rules for the agent:

- `--gitops-host` is a lowercase hostname with no scheme, path or port. Do not give it for github.com, except to switch an install back to github.com. The installer refuses a host under `github.com`, `githubusercontent.com`, `ghe.com` or another domain where GitHub runs its own services, and `gitlab.com`.
- Do not give `--gitops-ca-file` when a public CA signed the certificate. Do not use `/dev/stdin` or a process substitution for it.
- The organization check asks the instance with the CA and sends no credential. On an instance in private mode it warns that it cannot verify the organization and goes on.
- To upgrade, run `upgrade.sh`. To remove, run `uninstall.sh`. Both read the forge from `install.env` (`GITOPS_FORGE=github`, `GITOPS_HOST`, `GITOPS_CA_SECRET`).
- A switch between github.com and an instance needs the App on the new host and a new `--kms-key`, because `install.sh` imports a key only into a key with no `ENABLED` version. The installer refuses a change of host that keeps the recorded App or key. INSTALL.md, "Switch between github.com and a GitHub Enterprise Server", gives the command. `--gitops-host=github.com` switches back.
- With `--generate-only`, the operator creates the CA Secret and restarts the minter after `lifecycle.sh apply`; INSTALL.md gives the commands. Over an existing `install.env`, generate-only does not record the forge keys: have the operator set `GITOPS_FORGE=github`, `GITOPS_HOST` and, for a private CA, `GITOPS_CA_SECRET` there before `lifecycle.sh apply`, or the next `upgrade.sh` renders github.com.
- When `install.env` records `GITOPS_FORGE=gitlab`, also pass `--gitops-forge=github` to switch to an instance.

Ask the operator for: the instance's hostname (port 443 only), the organization and repository on it, the App ID and the path of its private key, the CA file if a private CA signed the certificate, whether the cluster exists, and which VPC reaches the instance. An install serves one GitHub host, so this install has no github.com repositories. If the operator does not want `install.sh`, INSTALL.md also has the Terraform + Helm method.

## GitOps Repository on GitLab

A GitLab install has no GitHub App and no token minter. The credential broker holds a GitLab access
token in a Kubernetes Secret, and the agent and its shell sandbox never get it.
[GitLab as the GitOps forge](https://gke-labs.github.io/kube-agents/install/gitlab/) has the
procedure, the network, DNS and certificate rules, a reachability test, the connection check,
token expiry and rotation, and the `install.env` keys. Read it before the dry run.

Rules for the agent:

- Use release 0.9.0 or later. An earlier release has no `--gitops-forge` flag.
- The operator creates the GitOps project first. The installer does not create it.
- `--gitops-repo` is the project's full path with its nested groups (`group/subgroup/project`), or
  its `https://` URL on that host. Put the host in `--gitops-host`, not in the path. Leave
  `--gitops-host` out for gitlab.com.
- The broker uses the token only for the declared repositories. Another project in the
  same group gets no token; `inspect-repository` reads it without one, so only a public project
  is readable. A project outside those groups is refused. Tell the operator to use the token with the smallest reach. That is a project access
  token on the GitOps project, or a group access token on the project's own group. For
  `platform/infra/gitops`, that group is `platform/infra`, not `platform`. Tell the operator to
  record the token's expiry date, and to check the token before the install, as the GitLab page
  shows.
- Do not ask the operator to paste the token. Do not put the token in a flag value, a variable or
  the transcript. Use one of these:
  - Do not give `--gitlab-token-file`. The install completes and prints the command that creates
    the Secret, and the operator runs it. Until then, GitLab calls answer
    `FORGE_CREDENTIAL_UNAVAILABLE`. No restart is necessary afterwards.
  - The operator gives a file path or a password-manager command, which you pass as
    `--gitlab-token-file=<(…)`. The installer reads the whole source and removes all whitespace,
    newlines inside it too. The source must hold only the token. For a `pass` entry with more
    lines, use `<(pass show gitlab/agent-token | head -n1)`. The operator unlocks `gpg-agent`
    before the run. Do not use `/dev/stdin` under `curl … | bash`, because standard input is the
    script there.
- Only when a private CA signed the instance's certificate, add `--gitops-ca-file` to the dry run
  and to the stage 3 run. Do not give it for gitlab.com or for a public CA. Ask the operator
  for the CA certificates (public; never a private key) as a regular PEM file: the root CA, and the
  intermediate CAs if the server does not send them. Do not use `/dev/stdin` or a process
  substitution for the CA. The installer refuses the flag without a self-managed `--gitops-host`.
  Use a release whose `install.sh --help` lists `--gitops-ca-file`.
- The dry run checks that the token source is readable, and stops if a regular token file is
  empty. It stores no token, and `install.sh` never connects to GitLab. Only the stage 3 run
  reads the token into the Secret, after the apply.
- On a cluster that already exists, have the operator run the network-path test from the GitLab
  page before stage 3, in the `default` namespace: `kubeagents-system` does not exist yet. On a
  new cluster, run it after stage 3, in `kubeagents-system`. After stage 3, run the connection
  check from the same page.

The command below is a stage 2 dry run. Apply through stage 3 once the operator confirms:

```bash
./install.sh --dry-run --non-interactive \
  --gcp-project-id="YOUR_GCP_PROJECT_ID" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1" \
  --model-provider="gemini" \
  --gitops-forge=gitlab \
  --gitops-host="gitlab.example.com" \
  --gitops-repo="platform/infra/gitops" \
  --gitlab-token-file=<(pass show gitlab/agent-token | head -n1)
```

Pass the same `--gitlab-token-file` source to the stage 3 run. The dry run only checks it.

## Supported Command-Line Flags

Defaults marked "`installer_common.sh`" reach the installer through
`scripts/installer/installer_common.sh`; the values themselves are listed in
`install.defaults.env` at the repository root, not here. Run `./install.sh --help` for the authoritative list.

- Over an existing `install.env`, do not pass a chat flag (`--enable-slack`, `--enable-google-chat`, `--slack-*`, `--google-chat-allowed-users`, `--google-chat-home-channel`, `--google-chat-mode`, `--chat-topic-name`) to change a key the file sets: it is refused. Have the operator edit the key, then re-run without the flag. A key the file lacks is recorded with the value the run applies; Slack tokens never are. [`scripts/installer/README.md`](../../../scripts/installer/README.md#the-install-configuration-installenv) has the rule.

| Flag                                    | Description                                                                                                                                                                                                                                                                                                                                                                       | Default                                                                                                                                            |
| :-------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------- |
| `-y, --non-interactive`                 | Run without blocking on `/dev/tty` prompts                                                                                                                                                                                                                                                                                                                                        | `false`                                                                                                                                            |
| `--dry-run`                             | Output plan and `terraform.tfvars` without creating resources                                                                                                                                                                                                                                                                                                                     | `false`                                                                                                                                            |
| `--generate-only`                       | Write `install.env` and `terraform.tfvars`, run the pre-apply checks, print the operator handoff, and exit without creating or mutating resources. Mutually exclusive with `--dry-run`                                                                                                                                                                                            | `false`                                                                                                                                            |
| `--menu, --config`                      | Launch the Day-2 control panel instead of installing                                                                                                                                                                                                                                                                                                                              | `false`                                                                                                                                            |
| `--gcp-project-id=ID`                   | Target GCP Project ID                                                                                                                                                                                                                                                                                                                                                             | Active `gcloud` project                                                                                                                            |
| `--gcp-region=REGION`                   | Target GCP Region                                                                                                                                                                                                                                                                                                                                                                 | `installer_common.sh` `DEFAULT_REGION`                                                                                                             |
| `--gke-cluster-name=NAME`               | GKE Cluster Name                                                                                                                                                                                                                                                                                                                                                                  | `installer_common.sh` `DEFAULT_CLUSTER_NAME`                                                                                                       |
| `--gke-cluster-mode=MODE`               | Shape of a cluster this run creates: `autopilot` \| `standard`. Autopilot is regional: unset at a zonal `--gcp-region` builds `standard`, explicit `autopilot` there is an error. No bearing on an existing cluster, whose live shape the generator probes                                                                                                                        | `autopilot`                                                                                                                                        |
| `--agent-namespace=NAME`                | Kubernetes namespace the agent and operator are installed into                                                                                                                                                                                                                                                                                                                    | `installer_common.sh` `DEFAULT_NAMESPACE`                                                                                                          |
| `--mode=MODE`                           | The `PlatformAgent`'s `spec.mode`: `today` \| `next` (unsupported dev stack). Recorded as `PLATFORM_AGENT_MODE` when `install.env` sets none; later, switch by editing that key, since a disagreeing `--mode` is refused                                                                                                                                                          | `installer_common.sh` `DEFAULT_PLATFORM_AGENT_MODE`                                                                                                |
| `--image-tag=TAG`                       | Validated immutable release tag or full commit SHA (developer/CI only)                                                                                                                                                                                                                                                                                                            | Developer and CI/CD testing only; end users must use official release installations. Default: inferred from baked release, bundle, or local `HEAD` |
| `--registry-prefix=PATH`                | Registry path (no URL scheme) for the first-party images this project builds                                                                                                                                                                                                                                                                                                      | `installer_common.sh` `DEFAULT_REGISTRY_PREFIX`                                                                                                    |
| `--third-party-registry-prefix=PATH`    | Registry path holding the mirrored third-party images (cert-manager, LiteLLM, fluent-bit, token minter, Hindsight). Not implied by `--registry-prefix`                                                                                                                                                                                                                            | _unset_ — upstream registries                                                                                                                      |
| `--allow-unverified-source`             | Provision from a dirty or mismatched checkout                                                                                                                                                                                                                                                                                                                                     | `false`                                                                                                                                            |
| `--model-provider=NAME`                 | `gemini` \| `vertex_ai` \| `anthropic` \| `openai`                                                                                                                                                                                                                                                                                                                                | `installer_common.sh` `DEFAULT_MODEL_PROVIDER`                                                                                                     |
| `--vertex-location=LOCATION`            | Vertex AI serving location, a region or `global`. The global endpoint gives no in-region ML processing guarantee                                                                                                                                                                                                                                                                  | `installer_common.sh` `DEFAULT_VERTEX_LOCATION`                                                                                                    |
| `--gemini-api-key=KEY`                  | Gemini API key                                                                                                                                                                                                                                                                                                                                                                    | Looked up in Secret Manager                                                                                                                        |
| `--openai-api-key=KEY`                  | OpenAI API key                                                                                                                                                                                                                                                                                                                                                                    | _unset_                                                                                                                                            |
| `--anthropic-api-key=KEY`               | Anthropic API key                                                                                                                                                                                                                                                                                                                                                                 | _unset_                                                                                                                                            |
| `--permission-set=SET`                  | Agent GCP IAM set: `read-only` \| `custom`                                                                                                                                                                                                                                                                                                                                        | `read-only`                                                                                                                                        |
| `--custom-roles=ROLES`                  | Roles for `--permission-set=custom` (space- or comma-separated)                                                                                                                                                                                                                                                                                                                   | _unset_                                                                                                                                            |
| `--scope-projects=IDS`                  | Projects beyond the install's whose GKE clusters get a Cluster Agent (space- or comma-separated); the agent's service account gets the read roles in each. Recorded as `SCOPE_PROJECTS`; a flag on a re-run applies for that run and warns; the preflight checks the credentials Terraform applies with can create a sink in each                                                 | _unset_ (the install's project alone)                                                                                                              |
| `--scope-folders=IDS`                   | Numeric GCP folder IDs: every project beneath, at any depth; the read roles plus `roles/cloudasset.viewer` bound on the folder; the Cloud Asset API enabled. Recorded as `SCOPE_FOLDERS`; the preflight checks the credentials Terraform applies with can set IAM policy on each                                                                                                  | _unset_ (no container)                                                                                                                             |
| `--scope-organizations=IDS`             | Numeric GCP organisation IDs, bound the same way. Wide; prefer folders. Recorded as `SCOPE_ORGANIZATIONS`                                                                                                                                                                                                                                                                         | _unset_ (no container)                                                                                                                             |
| `--scope-shared-vpc-hosts=IDS`          | Shared VPC host project IDs: every attached service project, resolved when Terraform plans (as the identity it applies with) and granted the read roles, with `roles/compute.viewer` in the host for the lookup; a lookup that identity cannot make fails the plan before anything is applied. Recorded as `SCOPE_SHARED_VPC_HOSTS`                                               | _unset_ (no selector)                                                                                                                              |
| `--scope-metrics-scopes=IDS`            | Metrics Scope scoping-project IDs: every project the scope monitors, resolved and granted the same way, the scoping project too for the lookup; a monitored project the planning identity cannot name is left out by naming its number in `--scope-exclude-projects`. Recorded as `SCOPE_METRICS_SCOPES`                                                                          | _unset_ (no selector)                                                                                                                              |
| `--scope-max-projects=N`                | The most projects the reconcile lists per run, the management project included (`spec.scope.maxProjects`, 1 to 5000); a project past it reads over-cap. Recorded as `SCOPE_MAX_PROJECTS`                                                                                                                                                                                          | _unset_ (the CRD's 100)                                                                                                                            |
| `--scope-exclude-projects=IDS`          | Project IDs or shell-style globs (`*-sandbox`) to leave unmanaged                                                                                                                                                                                                                                                                                                                 | _unset_                                                                                                                                            |
| `--scope-exclude-clusters=TRIPLES`      | Clusters to leave unmanaged and out of fleet answers and the per-cluster audits, each as `project/location/cluster`                                                                                                                                                                                                                                                               | _unset_                                                                                                                                            |
| `--scoped-sa-pool-enabled[=BOOL]`       | Arm the scoped service account pool: one reader service account per project the plan lists in scope, created in the install's project; the credential broker selects from them and refuses a cluster in a project with none. Members hold no IAM grant yet, so leave it off. Recorded as `SCOPED_SA_POOL_ENABLED`                                                                 | `false`                                                                                                                                            |
| `--scoped-sa-pool-max-accounts=N`       | The most pool accounts the plan may create in the install's project, set from its free service-account quota (a whole number of at least 1); a pool past it is refused at plan. Recorded as `SCOPED_SA_POOL_MAX_ACCOUNTS`                                                                                                                                                         | _unset_ (the module's 100)                                                                                                                         |
| `--gitops-org=ORG`                      | GitHub organization for the GitOps IaC repository. Not used with `--gitops-forge=gitlab`                                                                                                                                                                                                                                                                                          | _unset_                                                                                                                                            |
| `--gitops-repo=REPO`                    | GitOps IaC repository name. With `--gitops-forge=gitlab`, the project's full path (`group/subgroup/project`) or its `https://` URL on the GitLab host                                                                                                                                                                                                                             | `gke-fleet-iac`                                                                                                                                    |
| `--gitops-forge=FORGE`                  | Forge that holds the GitOps repository: `github` or `gitlab` (gitlab.com or a self-managed instance). A GitLab install has no GitHub App and no token minter                                                                                                                                                                                                                      | `github`                                                                                                                                           |
| `--gitops-host=HOST`                    | The forge's hostname, with no scheme, port or path. With `github`: a GitHub Enterprise Server host; a host that GitHub operates and `gitlab.com` are refused, and `github.com` switches back to github.com. With `gitlab`: a self-managed GitLab host; a GitHub hostname is refused                                                                                               | github.com or gitlab.com                                                                                                                           |
| `--gitlab-token-file=PATH`              | File, `/dev/stdin` (not under `curl … \| bash`) or `<(command)` that holds the GitLab access token (scopes `api` and `write_repository`). A regular file is checked before the apply. Read into the Secret after the apply and never recorded. Without it, a non-interactive run prints the command that creates the Secret                                                       | _unset_                                                                                                                                            |
| `--gitlab-token-secret=NAME`            | Kubernetes Secret that holds the GitLab token                                                                                                                                                                                                                                                                                                                                     | `gitlab-forge-token`                                                                                                                               |
| `--gitops-ca-file=PATH`                 | Regular PEM file with the CA certificates of a self-managed GitLab or a GitHub Enterprise Server behind a private CA. Stored in Secret `gitlab-forge-ca` or `github-forge-ca`, and trusted by the credential broker for that host only. Needs a self-managed `--gitops-host`. On a GitHub Enterprise Server the token minter trusts it too, and the installer restarts the minter | _unset_                                                                                                                                            |
| `--github-app-id=ID`                    | Numeric GitHub App ID for the token minter                                                                                                                                                                                                                                                                                                                                        | _unset_                                                                                                                                            |
| `--github-pem-path=PATH`                | Path to downloaded GitHub App private key (`.pem`) for initial Cloud KMS import                                                                                                                                                                                                                                                                                                   | _unset_                                                                                                                                            |
| `--kms-keyring=NAME`                    | Cloud KMS Key Ring name for the token minter key                                                                                                                                                                                                                                                                                                                                  | `installer_common.sh` `DEFAULT_KMS_KEYRING`                                                                                                        |
| `--kms-key=NAME`                        | Cloud KMS CryptoKey name for the token minter signing key                                                                                                                                                                                                                                                                                                                         | `installer_common.sh` `DEFAULT_KMS_KEY`                                                                                                            |
| `--enable-google-chat`                  | Enable the Google Chat integration                                                                                                                                                                                                                                                                                                                                                | `false`                                                                                                                                            |
| `--google-chat-allowed-users=EMAILS`    | Comma-separated chat users allowed to reach the agent; empty allows everyone                                                                                                                                                                                                                                                                                                      | _unset_                                                                                                                                            |
| `--enable-slack[=true\|false]`          | Enable the Slack socket-mode relay (non-interactive runs require `--slack-bot-token` and `--slack-app-token`)                                                                                                                                                                                                                                                                     | `false`                                                                                                                                            |
| `--slack-bot-token=TOKENS`              | Comma-separated Slack bot tokens (`xoxb-...`), one per workspace the agent serves                                                                                                                                                                                                                                                                                                 | _unset_                                                                                                                                            |
| `--slack-app-token=TOKEN`               | Slack socket-mode app-level token (`xapp-...`)                                                                                                                                                                                                                                                                                                                                    | _unset_                                                                                                                                            |
| `--slack-allowed-users=USERS`           | Comma-separated Slack member IDs (`U0123ABCD`, not emails) allowed to reach the agent; empty allows everyone                                                                                                                                                                                                                                                                      | _unset_                                                                                                                                            |
| `--slack-home-channel=CHANNEL`          | Slack channel ID for unsolicited alerts/messages (e.g. `C01234567`)                                                                                                                                                                                                                                                                                                               | _unset_                                                                                                                                            |
| `--slack-home-channel-name=NAME`        | Display name of that channel (e.g. `#gke-alerts`)                                                                                                                                                                                                                                                                                                                                 | _unset_                                                                                                                                            |
| `--enable-gvisor=true\|false`           | Enable GKE Sandbox (gVisor) runtime isolation                                                                                                                                                                                                                                                                                                                                     | `true`                                                                                                                                             |
| `--enable-hermes-dashboard=true\|false` | Enable the Hermes Web UI on port 9119                                                                                                                                                                                                                                                                                                                                             | `false`                                                                                                                                            |
| `--enable-gke-backup-plan=true\|false`  | Provision a GKE Backup Plan for the cluster                                                                                                                                                                                                                                                                                                                                       | `false`                                                                                                                                            |
| `--migrate-node-pools`                  | Authorize migrating legacy GCE metadata server node pools to `GKE_METADATA` (recreates those nodes, restarts their workloads). Without it such a cluster is refused unchanged. The cluster's owner decides; never pass it unasked                                                                                                                                                 | `false`                                                                                                                                            |
| `--enable-network-policy`               | Authorize enabling the legacy Calico NetworkPolicy addon on an existing Standard cluster without Dataplane V2 (may recreate nodes). One of two answers; with neither the cluster is refused unchanged. The owner decides; never pass it unasked                                                                                                                                   | `false`                                                                                                                                            |
| `--accept-no-network-policy`            | The other answer: install without modifying the cluster. Every NetworkPolicy kube-agents ships is then inert, the agent sandbox's included; recorded in the report and on the `PlatformAgent`. The owner decides; never pass it unasked                                                                                                                                           | `false`                                                                                                                                            |
| `--memory=MODE`                         | Long-term agent memory engine: `file` \| `hindsight` \| `off`                                                                                                                                                                                                                                                                                                                     | `file`                                                                                                                                             |
| `-h, --help, -?`                        | Output CLI usage banner and parameter details                                                                                                                                                                                                                                                                                                                                     | `N/A`                                                                                                                                              |
