---
title: Prerequisites
description: What you need in place before running the kube-agents installer.
---

The shipping install path targets GKE. You'll need one working GCP project plus the standard command-line tools, and cert-manager on the cluster so the operator's admission webhooks come up cleanly, which the installer puts there for you. If the cluster already exists, it also has to meet the [cluster requirements](#cluster-requirements) below.

## Local tooling

- **Google Cloud SDK** (`gcloud`) — **576.0.0 or newer**, [install](https://cloud.google.com/sdk/docs/install), authenticated: `gcloud auth login && gcloud auth application-default login`. The installer sets Managed OpenTelemetry with `--managed-otel-scope`, which reached GA in gcloud 576.0.0 (2026-07-14); on an older SDK the flag exists only on the alpha and beta tracks. The installer checks this before it touches any cloud resource — `gcloud components update` if it complains.
- **`gke-gcloud-auth-plugin`** — required for `kubectl` to authenticate to GKE clusters (`gcloud components install gke-gcloud-auth-plugin` or your OS package manager). Pre-flighted by the installer (`install.sh`).
- **Terraform** — the install engine is the [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) composition; the installer (`install.sh`) pre-flights `terraform` and offers to install it from HashiCorp's tap (Homebrew) or apt repository. It is equally the _teardown_ engine, and `./uninstall.sh` installs nothing on your behalf — with an install to tear down and no terraform, it refuses with exit 1, so a machine that has only ever run the installer's auto-install cannot tear the install down. (Against a target with no Terraform state there is nothing to destroy, so it exits 3 without needing terraform at all.)
- **`kubectl`** — [install](https://kubernetes.io/docs/tasks/tools/). The installer points it at the GKE cluster it creates.
- **Docker or Podman** — required for manual deployments that build images (`make -C k8s-operator docker-build`, Method 2) and the local development workflow (`make dev-rebuild-agent`, Method 3). Not required for stock installs (Methods 0 & 1).
- **Go** (`1.27+`) — required for development workflows (running tests, building binaries, bootstrapping `controller-gen`/`kustomize` for Method 2). Importing a GitHub App private key (`.pem`) during GitOps installation also needs Go on the host machine, but only `1.21+`: that path builds the Minty CLI, not the operator. Not required if the Cloud KMS key is pre-provisioned or GitOps is not enabled.
- **Bash** — the installer scripts are bash (including the `/bin/bash` macOS ships).
- **`jq`, `gh`, `helm`, `git`, `python3`** — the rest of the CLI set the installer pre-flights up front and offers to install when missing.
- **`gcloud beta` component** — required when adopting an existing unencrypted cluster for CMEK (`gcloud beta services identity create`) or purging backup plans during teardown (`gcloud beta container backup-restore`). Not required for standard fresh installs.
- **`envsubst`** — only for the development Kustomize path (`make -C k8s-operator deploy-*`); usually shipped with `gettext`.
- **`ssh-keygen`** — for Method 2, which mints the shell sandbox SSH keypair by hand in [INSTALL.md Step 2](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#step-2-create-api-key--access-secrets), and for `upgrade.sh`'s backfill of that pair; the installer and the Terraform composition mint it without it (`tls_private_key`). Ships with the OpenSSH client.

## GCP project

- A GCP project you can enable APIs on and where you can create GKE clusters, Pub/Sub topics, KMS keyrings, and IAM service accounts.
- Billing enabled on that project.
- The `Editor` or `Owner` role for the user running the installer (or a scoped set covering the resources above).

The installer enables APIs and creates all resources itself, including the cluster. You do not need to pre-provision one; if you do, the next section is what it has to look like.

## Cluster requirements

A cluster the installer creates meets every requirement here by construction, so on a fresh install there is nothing to check. A cluster somebody else made is a different matter. The composition installs onto one when `create_cluster = false`, and `install.sh` selects that on its own whenever the cluster it is pointed at already exists and this install did not create it. Some requirements are then refused at plan time, some `install.sh` meets by changing the cluster, and the rest fail after the GCP resources exist; the third column says which. Read the cluster before you point the installer at it:

```bash
CLUSTER=my-cluster LOCATION=us-central1 PROJECT=my-project
gcloud container clusters describe "$CLUSTER" --location "$LOCATION" --project "$PROJECT" \
  --format='yaml(autopilot.enabled,currentMasterVersion,workloadIdentityConfig.workloadPool,networkConfig.datapathProvider,networkPolicy.enabled,privateClusterConfig.enablePrivateEndpoint,controlPlaneEndpointsConfig,databaseEncryption.state)'
gcloud container node-pools list --cluster "$CLUSTER" --location "$LOCATION" --project "$PROJECT" \
  --format='table(name,config.workloadMetadataConfig.mode,config.machineType)'
```

The first command omits a key whose value is unset, so a requirement that is not met usually shows as a missing key rather than a `false`; the second prints an empty `MODE` cell. `autopilot: {}` means a Standard cluster; `autopilot: {enabled: true}` means Autopilot.

| Requirement                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | What to look for                                                                                                                                        | When it is not met                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GKE 1.29 or newer.** The chart's `kubeVersion` floor, for native sidecar containers. The optional `ValidatingAdmissionPolicy` needs 1.30 and `AgentPlugin` OCI volumes 1.35, but both degrade rather than fail below that.                                                                                                                                                                                                                                                        | `currentMasterVersion`                                                                                                                                  | Nothing checks it up front. The Helm release fails after the GCP resources exist.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| **Workload Identity pool** `PROJECT.svc.id.goog`. Every KSA-to-GSA binding the install creates rides it; without it the pods authenticate as the node's service account.                                                                                                                                                                                                                                                                                                            | `workloadIdentityConfig.workloadPool`                                                                                                                   | `install.sh` enables it, a control-plane update of several minutes. A bare Terraform run refuses the plan.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| **GKE metadata server on every node pool.** Enabling the pool does not migrate node pools; pods on a pool still using the legacy metadata server get the node's identity. Standard only; Autopilot manages it.                                                                                                                                                                                                                                                                      | Every pool's `MODE` reads `GKE_METADATA`                                                                                                                | `install.sh` migrates each pool that is not when authorized via `--migrate-node-pools` (or `MIGRATE_NODE_POOLS=true`), **which recreates that pool's nodes**, including pools running workloads unrelated to kube-agents. Without opt-in, the install aborts before making any cluster changes (`REFUSED_MISSING_NODE_POOL_MIGRATION`). Terraform does not check, so refusing beforehand prevents pods silently running under the wrong identity.                                                                                                                                                                                                                                             |
| **NetworkPolicy enforcement**: Dataplane V2, or the legacy Calico addon. Every NetworkPolicy the install ships is accepted and inert without one; GKE Standard's default has neither.                                                                                                                                                                                                                                                                                               | `datapathProvider: ADVANCED_DATAPATH`, or `networkPolicy.enabled: true`                                                                                 | `--enable-network-policy`: `install.sh` enables the Calico addon, then enforcement; can recreate node pools. `--accept-no-network-policy`: install without enforcement, cluster untouched ([what that costs](#installing-without-networkpolicy-enforcement)). Neither: the install aborts unchanged. Bare Terraform refuses unless `accept_no_network_policy = true`. No migration to Dataplane V2; `FQDNNetworkPolicy` companions stay inert on Calico.                                                                                                                                                                                                                                      |
| **A control plane reachable from where Terraform runs, and from the agent.** Terraform's Helm provider dials whatever endpoint GKE reports for the cluster: the IP endpoint when there is one, otherwise the DNS endpoint. The agent and the installer's post-apply `kubectl` prefer the DNS endpoint whenever it allows external traffic, whether or not an IP endpoint exists.                                                                                                    | `ipEndpointsConfig.enabled` and `enablePrivateEndpoint` for Terraform; for the agent, either of those or `dnsEndpointConfig.allowExternalTraffic: true` | Nothing checks any of it. Terraform run outside the VPC fails on a private IP endpoint, which GKE keeps reporting even once a DNS endpoint exists; disabling IP access is what moves Terraform onto the DNS endpoint, and that answers 403 unless external traffic is allowed on it. On a cluster the install did not create, `allow_external_dns_traffic` does nothing; open the DNS endpoint with `gcloud container clusters update --enable-dns-access` instead. [`gke_dns_endpoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/scripts/installer/gke_dns_endpoint.sh) explains why the flag is never passed blind.                                                             |
| **cert-manager**, present or absent, but declared either way.                                                                                                                                                                                                                                                                                                                                                                                                                       | `kubectl get deployment cert-manager -n cert-manager`                                                                                                   | `install.sh` probes for it (in that namespace only) and sets `enable_cert_manager` for you; a bare Terraform run against a cluster that already has it fails on the existing CRDs unless you set it to `false`. Details [below](#cert-manager-on-the-target-cluster).                                                                                                                                                                                                                                                                                                                                                                                                                         |
| **A gVisor RuntimeClass**, because the agent runs sandboxed by default (`--gvisor=false` opts out).                                                                                                                                                                                                                                                                                                                                                                                 | Autopilot: `currentMasterVersion` at or above `1.27.4-gke.800`. Standard: nothing to read; the install adds it.                                         | `install.sh` refuses an older Autopilot cluster before applying anything. On Standard the composition creates a `gvisor-pool` node pool of one `e2-standard-4` per zone: new billable capacity, and it carries GKE's sandbox taint, so only the sandboxed agent lands there. A cluster that already has a pool of that name needs `gvisor_pool_name` changed; the module never adopts one.                                                                                                                                                                                                                                                                                                    |
| **Schedulable capacity for the workloads the install adds.** A stock install requests about 2.5 vCPU and 8.1 GiB across 7 pods — including the dashboard, shell sandbox, and cleanup hook — and the agent pod alone needs more than 1 vCPU free on a single node. Hindsight memory and the GitHub minter each add more. The per-workload figures live in `charts/kube-agents/files/footprint.yaml` (operator-rendered pods) and `charts/kube-agents/values.yaml` (the chart's own). | `kubectl describe nodes` allocatable minus requests, on the pools the agent can use                                                                     | Nothing checks it, and the failure depends on which pod is stuck. LiteLLM or the operator Pending: the Helm release waits ten minutes and fails with `context deadline exceeded`, naming nothing. Only the agent pod Pending: the operator creates it after the Helm release, so the apply succeeds and `install.sh` prints a warning naming `platform-agent-gateway` after five minutes, then reports the install complete.                                                                                                                                                                                                                                                                  |
| **Namespace ResourceQuota headroom.** When the release namespace enforces a baseline ResourceQuota, it needs room for the whole release: the requests in the row above, plus ~10.2 CPU and ~21.5 GiB in limits.                                                                                                                                                                                                                                                                     | `kubectl describe resourcequota -n <release-namespace>`                                                                                                 | The chart checks un-scoped ResourceQuotas in the release namespace at render time (`quotaPreflight.enabled=true`; `--set quotaPreflight.enabled=false` skips it) and fails with a diagnosis and a ready-to-run `kubectl patch`. With no cluster to query (`helm template`) or no ResourceQuota in the namespace it passes silently, so a clean render is not proof a quota fits; without `get`/`list` on `resourcequotas` there it fails the render instead, because Helm's `lookup` raises on anything but a NotFound. The [chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#quota-preflight) is canonical for what it sums and how it compares. |

A quota is more than CPU and memory, and so is the check: a stock install also needs 7
pods, 4 persistent volume claims totalling 22 GiB of `requests.storage`, and 5 GiB of
ephemeral-storage requests against 5 GiB of limits. A namespace quota that caps pod or
claim counts below what the release needs refuses the install the same way; note that
constraining ephemeral storage additionally requires every container to declare it (or
a `LimitRange` to default it), as the chart README explains.

Three more things the installer sets on a cluster it creates, and treats differently on one it adopts:

- **CMEK database encryption.** `install.sh` enables it on an adopted cluster too, creating a KMS keyring and key and updating the control plane, unless `ALLOW_UNENCRYPTED_SECRETS=true`. `uninstall.sh` does not revert it. Bare Terraform does not require or enable it.
- **Managed OpenTelemetry collection scope.** Set only on clusters the install created; on an adopted one, follow [Enable Managed OpenTelemetry on an existing cluster](/kube-agents/reference/attribution/#enable-managed-opentelemetry-on-an-existing-cluster).
- **The Backup for GKE agent.** Only needed with `enable_gke_backup_plan = true`, which is off by default; the module cannot enable the agent on a cluster it only reads.

Run the installer with `--dry-run` to see the plan against the cluster without changing it. The installer's own cluster changes happen only on a real run, so a dry run against a cluster still missing NetworkPolicy enforcement or legacy node pool migration skips the resource preview (`terraform plan`) with a warning; a real run, and `--generate-only`, abort unless told which way to go with `--enable-network-policy` or `--accept-no-network-policy`, and `--migrate-node-pools`.

### Installing without NetworkPolicy enforcement

`--accept-no-network-policy` installs onto a cluster that has neither Dataplane V2 nor the Calico addon
without modifying it. The install works: every NetworkPolicy it ships is accepted by the API server
and enforced by nothing, so nothing crashes and nothing degrades functionally. What is lost is
confinement, and it is the confinement of kube-agents' own workloads rather than yours:

- The agent pod's ingress restriction and its egress confinement. Enforced, external egress is port
  443 outside the private ranges plus named in-cluster peers; unenforced, the pod can reach anything
  routable in your VPC.
- The shell sandbox's policy, which is deny-all apart from cluster DNS and the credential proxy.
  This is where model-authored commands run, and the policy is the only thing between it and the VPC.
- The policies around the LiteLLM gateway, the GitHub token minter, and Hindsight.

The usual argument for accepting is "we trust the workloads in this cluster". That is a statement
about your workloads; the one whose network boundary is at stake is ours. An operator who reads
that and still says yes has made an informed decision, and the installer records it so the next
person can find it: the install report carries `"network_policy_enforcement": "absent-accepted"`,
and the `PlatformAgent` carries the annotation
`kubeagents.x-k8s.io/network-policy-enforcement: absent-accepted`, stamped by the Terraform
composition from what it read on the cluster and dropped on the next apply once the cluster
enforces. The pre-flight summary shows the choice as `NetworkPolicy Enforcement: Absent, accepted`.

The choice has to survive the run. `install.sh` writes `ACCEPT_NO_NETWORK_POLICY=true` into the
`install.env` it creates; if you already had one, add the key yourself, as the installer tells you
to. `upgrade.sh` and the Day-2 menu regenerate `terraform.tfvars` from that file, and without the
key the next apply is refused for the enforcement you already accepted. To confine the agent later,
enable Dataplane V2 or the Calico addon on the cluster (or re-run with `--enable-network-policy`,
which overrides the recorded key for that run), then remove `ACCEPT_NO_NETWORK_POLICY` from
`install.env`: the installer warns while the line remains, because it keeps every later apply
waiving the check that would refuse the install if enforcement were lost again. The annotation
goes away on its own. The installer records the key only when the cluster enforced nothing at the
time and the run accepted that; the flag against a cluster that already enforces records nothing.

Both `--accept-no-network-policy` and `--enable-network-policy` are decisions for whoever owns the
cluster. An agent installing on your behalf is told to present the three options and ask, not to
choose.

**No extra firewall rule is needed on private clusters.** The operator's webhook server listens on
`10250`, one of the two ports GKE's automatic control-plane-to-node rule already permits — see
[Admission webhooks](/kube-agents/operator/#admission-webhooks). A cluster that hardens `10250`
beyond the GKE default (scoping it to node IPs, say) still needs a rule for the webhook, or a move to
a port it does allow — which is a Kustomize patch across the `--webhook-port` flag, the manager
`containerPort`, and the Service `targetPort` together, not a single flag. Changing one of the three
leaves the API server dialing a port nothing is listening on; see
[Serving on a different port](/kube-agents/operator/#serving-on-a-different-port).

## cert-manager on the target cluster

The operator's admission webhooks need TLS certificates managed by [cert-manager](https://cert-manager.io) (v1.13.0+).

**You usually do not need to install this yourself.** The Terraform composition `terraform/examples/full-install` installs cert-manager as its own `helm_release`, pinned in its `cert_manager_version` variable, including the leader-election relocation Autopilot needs. On an existing cluster that already runs cert-manager, set `enable_cert_manager = false` — the composition does not detect an existing install and the apply fails on the existing CRDs. The installer (`install.sh`) probes for a `cert-manager` Deployment on the existing-cluster path and writes that variable for you, keeping `true` when the Deployment is the composition's own release recorded in this install's Terraform state. (An existing cert-manager installed under a different namespace or release name is not detected.)

Install it by hand only if you are:

- deploying into an existing cluster without the installer or Terraform ([Manual install](/kube-agents/install/manual/)), or
- pinning a specific cert-manager version.

The Helm chart on its own is the one path that never installs cert-manager: a chart that shipped a `Certificate` into a cluster without the CRDs would fail at apply time for everyone. It therefore leaves the operator's admission webhooks off (`operator.webhooks.enabled=false`) until you install cert-manager and turn them on. See the [chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md).

### Standard install (recommended)

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true
```

### GKE Autopilot install

Autopilot blocks leader-election Leases in `kube-system`. Disable leader election during install:

```bash
helm repo add jetstack https://charts.jetstack.io
helm repo update
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true \
  --set controller.leaderElection.enabled=false \
  --set cainjector.leaderElection.enabled=false
```

### Manifest fallback

If Helm isn't available:

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.2/cert-manager.yaml
```

On Autopilot you'll additionally need to patch the deployments to append `--leader-elect=false`. Because argument indices vary by cert-manager version, verify the arg list before patching — a positional JSON patch (`/args/1`) will silently corrupt an unexpected version.

## Chat platform

- **Google Chat** (opt-in, but the interactive installer pre-selects it): a GCP project with the Chat API enabled and a Chat app configured to publish events to Pub/Sub. The composition's [`chat-pubsub` module](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/chat-pubsub) creates the topic and subscription (`enable_google_chat = true`, or the installer's `--enable-google-chat`); you configure the Chat app itself in the [Chat API console](https://console.cloud.google.com/apis/api/chat.googleapis.com).
- **Slack** (opt-in): a Slack workspace where you can install a bot app and generate bot + app tokens. Follow the [Hermes Slack setup guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack). Slack is configured only if you enable it in the installer's chat menu (or set `enable_slack = true` in `terraform.tfvars`).

**A GCP project holds one Chat app.** Google's rule is that "each Google Chat app that you create
requires its own Google Cloud project with the Chat API enabled" —
[Configure the Chat API](https://developers.google.com/workspace/chat/configure-chat-api). So a
project already running a Chat app cannot also run kube-agents' Chat integration.

This only applies if Chat is on, and whether it is on by default depends on which front door you
use. Terraform and Helm both default it off — `enable_google_chat` and the chart's
`googleChat.enabled` are `false`, so neither takes a slot unless you ask. The interactive installer
goes the other way: its chat menu pre-selects **Google Chat**, so pressing enter at that prompt
provisions the Chat backend. Pick "None" or Slack there if you want the project's Chat slot left
alone. **Slack is unaffected** — it provisions no GCP resource at all, so a project whose Chat slot
is already spoken for can still run kube-agents with Slack in it.

If you do want Chat and the slot is taken, moving just the Chat backend elsewhere is not available
through the supported paths: the chart renders the CR's `projectId` from
`platformAgent.harness.projectId`, so the installer and the chart always put the Chat topic in the
cluster's project. That leaves installing into a project whose Chat slot is free — a choice worth
weighing against how you want the agent's fleet scoped, not against Chat alone.

## LLM credentials

Pick one at least:

- `GEMINI_API_KEY` (recommended default; get one at [aistudio.google.com](https://aistudio.google.com)).
- `ANTHROPIC_API_KEY`.
- `OPENAI_API_KEY`.

Or route one of these keys through a self-hosted LiteLLM gateway — see [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) for a Gemini API-key template.

## GitOps repo & GitHub token minter (for `submit-suggestion`)

The declarative workflow routes infrastructure mutations through pull requests via the GitHub token minter (Minty). When GitOps is enabled, prepare:

- **GitHub Organization:** A GitHub repo **owned by an organization**. Minty queries `/orgs/{org}/installation`, so personal accounts fail token minting with HTTP 404 — see [Token minter](/kube-agents/deploy/token-minter/). A free organization is sufficient.
- **GitHub App:** A GitHub App with `contents:write`, `pull_requests:write`, and `issues:write` permissions, installed on that repo and on any repository you later register under `context_repos` (see upstream [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter#readme) and [Read-only tokens for context repositories](/kube-agents/deploy/token-minter/#read-only-tokens-for-context-repositories)).
- **GitHub App ID:** Numeric ID of the created App.
- **Cloud KMS Key & Private Key Import:** The App's private key imported into a Cloud KMS asymmetric signing key in your cluster's region (default `github-token-minter-keyring` / `github-token-minter-key`). Because Cloud KMS keys cannot be deleted in GCP, this import is a permanent one-time step.
  - **Option 1 (Automated import via `install.sh`):** Provide `--github-pem-path="/path/to/app-private-key.pem"` during install. `install.sh` creates the KMS key, runs the Minty CLI to import the key, and you can delete the local `.pem` file immediately after. Needs Go 1.21+ on the installer host — the Minty CLI asks for Go 1.24, and 1.21 onwards downloads that toolchain on demand. Distribution packages are often older (Debian 12 ships 1.19, Ubuntu 22.04 ships 1.18); `install.sh` checks before it builds and tells you if yours cannot work. This is unrelated to the Go version needed to build the operator.
  - **Option 2 (Pre-provisioned Ahead-Of-Time):** Pre-provision the KMS key and import the private key upfront following the [upstream guide](https://github.com/abcxyz/github-token-minter#readme) and [Google Cloud KMS documentation](https://cloud.google.com/kms/docs/importing-a-key) (recommended for CI/CD and release pipelines). `install.sh` detects the existing `ENABLED` version and skips `.pem` handling.

See [Deploy → Token minter](/kube-agents/deploy/token-minter/) and the upstream [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter#readme) guide for complete setup details.

## Ready to install

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — run the installer end-to-end.
- [Manual install](/kube-agents/install/manual/) — step-by-step, no wrapper script.
