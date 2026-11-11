---
title: Prerequisites
description: What you need in place before running the kube-agents installer.
---

The shipping install path targets GKE. You'll need one working GCP project plus the standard command-line tools, and cert-manager on the cluster so the operator's admission webhooks come up cleanly, which the installer puts there for you. If the cluster already exists, it also has to meet the [cluster requirements](#cluster-requirements) below.

## Local tooling

- **Google Cloud SDK** (`gcloud`) — **576.0.0 or newer**, [install](https://cloud.google.com/sdk/docs/install), authenticated: `gcloud auth login && gcloud auth application-default login`. The installer sets Managed OpenTelemetry with `--managed-otel-scope`, which reached GA in gcloud 576.0.0 (2026-07-14); on an older SDK the flag exists only on the alpha and beta tracks. The installer checks this before it touches any cloud resource — `gcloud components update` if it complains.
- **`gke-gcloud-auth-plugin`** — required for `kubectl` to authenticate to GKE clusters (`gcloud components install gke-gcloud-auth-plugin` or your OS package manager). Pre-flighted by the installer (`install.sh`).
- **Terraform** — **1.5 or newer, below 2.0** (the configurations pin `~> 1.5`, which `terraform init` enforces; run from a checkout or release archive, the installer also checks for 1.5.0 before it touches any cloud resource). The install engine is the [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) composition; the installer (`install.sh`) pre-flights `terraform` and offers to install it from HashiCorp's tap (Homebrew) or apt repository. It is equally the _teardown_ engine, and `./uninstall.sh` installs nothing on your behalf — with an install to tear down and no terraform, it refuses with exit 1, so a machine that has only ever run the installer's auto-install cannot tear the install down. (Against a target with no Terraform state there is nothing to destroy, so it exits 3 without needing terraform at all.)
- **`kubectl`** — [install](https://kubernetes.io/docs/tasks/tools/). The installer points it at the GKE cluster it creates.
- **Docker or Podman** — required for manual deployments that build images (`make -C k8s-operator docker-build`, Method 2) and the local development workflow (`make dev-rebuild-agent`, Method 3). Not required for stock installs (Methods 0 & 1).
- **Go** (`1.27+`) — required for development workflows (running tests, building binaries, bootstrapping `controller-gen`/`kustomize` for Method 2). Importing a GitHub App private key (`.pem`) during GitOps installation also needs Go on the host machine, but only `1.21+`: that path builds the Minty CLI, not the operator. Not required if the Cloud KMS key is pre-provisioned, GitOps is not enabled, or the GitOps repository is on GitLab, Gitea or Bitbucket.
- **Bash** — the installer scripts are bash (including the `/bin/bash` macOS ships).
- **`jq`, `gh`, `helm`, `git`, `python3`** — the rest of the CLI set the installer pre-flights up front and offers to install when missing. The installer checks only that these and `kubectl` are present. It holds three tools to a minimum version: gcloud and Terraform, above, and Go 1.21 when it imports a GitHub App key. The `curl … | bash` one-liner skips the gcloud and Terraform checks, because there is no checkout yet to read the minimums from.
- **`gcloud beta` component** — required when adopting an existing unencrypted cluster for CMEK (`gcloud beta services identity create`) or purging backup plans during teardown (`gcloud beta container backup-restore`). Not required for standard fresh installs.
- **`envsubst`** — only for the development Kustomize path (`make -C k8s-operator deploy-*`); usually shipped with `gettext`.
- **`ssh-keygen`** — for Method 2, which mints the shell sandbox SSH keypair by hand in [INSTALL.md Step 2](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#step-2-create-api-key--access-secrets), and for `upgrade.sh`'s backfill of that pair; the installer and the Terraform composition mint it without it (`tls_private_key`). Ships with the OpenSSH client.

## GCP project

- A GCP project you can enable APIs on and where you can create GKE clusters, Pub/Sub topics and subscriptions, Log Router sinks, KMS keyrings, and IAM service accounts. An install that will declare other projects in its scope needs, in each of them, the right to set IAM policy, to read the project, to mint its Logging service agent and to create a Log Router sink (`roles/owner` carries all of them) ([Multi-project](/kube-agents/deploy/multi-project/)).
- Billing enabled on that project.
- The `Owner` role for the user running the installer, or `Editor` plus the roles [below](#the-role-the-installer-needs). `Editor` alone is not enough.

The installer enables APIs and creates all resources itself, including the cluster. You do not need to pre-provision one; if you do, the [cluster requirements](#cluster-requirements) are what it has to look like.

### The role the installer needs

The installer runs Terraform and Helm under your own `gcloud` credentials (`gcloud auth application-default login`), so you need every permission the apply uses. It does not check your role first: a missing permission fails the apply partway through, with some resources already created. Check your roles on the project before you start:

```bash
gcloud projects get-iam-policy <PROJECT_ID> --flatten=bindings \
  --filter="bindings.members:user:$(gcloud config get account)" --format='value(bindings.role)'
```

That lists only roles bound to your account directly on the project. A role you hold through a Google group, or one inherited from a folder or the organisation, does not show, so empty output does not mean you lack it. The Policy Troubleshooter reads the effective policy, groups and inheritance included, one permission at a time; for example, the first row of the table below:

```bash
gcloud policy-intelligence troubleshoot-policy iam \
  //cloudresourcemanager.googleapis.com/projects/<PROJECT_ID> \
  --principal-email="$(gcloud config get account)" \
  --permission=resourcemanager.projects.setIamPolicy
```

`roles/owner` covers a stock install. `roles/editor` does not, because a stock install grants access as well as creating resources, and Editor holds none of the permissions that do it. Each line below is something a stock install does, the permission it needs that Editor lacks, and a predefined role that carries it:

| What the install does                                                                                                                                                                           | Permission Editor lacks                                                                                                                                           | Role that has it                        |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| Grants the agent's service account its roles on the project (`google_project_iam_member`)                                                                                                       | `resourcemanager.projects.setIamPolicy`                                                                                                                           | `roles/resourcemanager.projectIamAdmin` |
| Binds the agent's Kubernetes ServiceAccount to its service account (Workload Identity, `google_service_account_iam_member`)                                                                     | `iam.serviceAccounts.setIamPolicy`                                                                                                                                | `roles/iam.serviceAccountAdmin`         |
| Lets the GKE service agent use the Secret-encryption key (CMEK, on by default for a cluster the install creates or adopts)                                                                      | `cloudkms.cryptoKeys.setIamPolicy`                                                                                                                                | `roles/cloudkms.admin`                  |
| Grants the drift detector's topic and subscription to the Logging service agent and the agent (on by default; `--enable-drift-detector=false` on the first install skips this row and the next) | `pubsub.topics.setIamPolicy`, `pubsub.subscriptions.setIamPolicy`                                                                                                 | `roles/pubsub.admin`                    |
| Creates the drift detector's Log Router sink                                                                                                                                                    | `logging.sinks.create`                                                                                                                                            | `roles/logging.configWriter`            |
| Installs the chart's ClusterRoles, Roles and their bindings on the cluster                                                                                                                      | `container.clusterRoles.create`, `.bind` and `.escalate`, `container.clusterRoleBindings.create`, and the same for `container.roles` and `container.roleBindings` | `roles/container.admin`                 |

So `Editor` plus those six roles covers what a stock install needs that Editor lacks. Options add to it. Google Chat grants its topic and subscription (`pubsub.*.setIamPolicy` again), `--enable-stockout-investigator` creates its own sink, topic and subscription grants (the same permissions as the drift detector's), and the GitHub token minter grants its signing key and service account (`cloudkms.cryptoKeys.setIamPolicy`, `iam.serviceAccounts.setIamPolicy`). `--model-provider=vertex_ai` with `--vertex-project-id` naming another project grants the model gateway's service account `roles/aiplatform.user` there, which needs `resourcemanager.projects.setIamPolicy` in that project, or `--vertex-manage-serving-project=false` and the grant made by hand. `--scope-projects`, `--scope-shared-vpc-hosts` and `--scope-metrics-scopes` grant the agent read roles in every project they name or resolve to, which needs `resourcemanager.projects.setIamPolicy` in each, and nothing tests for it first. `--scope-folders` and `--scope-organizations` grant read roles on the folder or organisation, which needs `resourcemanager.folders.setIamPolicy` or `resourcemanager.organizations.setIamPolicy` there; the installer tests for those two before it applies anything.

What the install creates in your project:

- The agent's service account, `kubeagents-platform-gsa`, always. Its roles are the [permission set](/kube-agents/reference/security-and-iam/#gcp-iam-permission-sets) you choose.
- `kubeagents-litellm-gsa`, the model gateway's, with `--model-provider=vertex_ai` only.
- `<cluster>-minter`, the GitHub token minter's, with the GitHub integration only. An install from an earlier release keeps `kubeagents-github-minter-gsa`.
- One reader service account per project in scope, with `--scoped-sa-pool-enabled` only.
- The Google-managed service agents the grants above name: GKE's for CMEK, Logging's for the drift sink, and Google Chat's with Chat.
- A versioned Cloud Storage bucket, `<PROJECT_ID>-kube-agents-tfstate`, for the Terraform state.

The APIs it enables are `container`, `cloudkms`, `iam`, `cloudresourcemanager`, `monitoring`, `logging`, `gkebackup` and `developerknowledge` (each `<name>.googleapis.com`), `pubsub` with the drift detector, Google Chat, `--enable-stockout-investigator` or `--enable-pubsub-platform`, `chat` and `gsuiteaddons` with Google Chat, `cloudasset` with `--scope-folders` or `--scope-organizations`, `compute` with `--scope-shared-vpc-hosts`, and `aiplatform` in the serving project with `--model-provider=vertex_ai`. Enabling them is in Editor already.

`spec.mode: next`, a first install's default, adds no service account, no IAM grant and no API. Its bus identities are Kubernetes ServiceAccounts the operator creates, and the one cluster-scoped grant, the auth callout's ClusterRoleBinding to `system:auth-delegator`, is made by the operator under its own permissions, not yours.

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

| Requirement                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | What to look for                                                                                                                                        | When it is not met                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **GKE 1.29 or newer.** The chart's `kubeVersion` floor, for native sidecar containers. The optional `ValidatingAdmissionPolicy` needs 1.30 and `AgentPlugin` OCI volumes 1.35, but both degrade rather than fail below that.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | `currentMasterVersion`                                                                                                                                  | Nothing checks it up front. The Helm release fails after the GCP resources exist.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| **Workload Identity pool** `PROJECT.svc.id.goog`. Every KSA-to-GSA binding the install creates rides it; without it the pods authenticate as the node's service account.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | `workloadIdentityConfig.workloadPool`                                                                                                                   | `install.sh` enables it, a control-plane update of several minutes. A bare Terraform run refuses the plan.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| **GKE metadata server on every node pool.** Enabling the pool does not migrate node pools; pods on a pool still using the legacy metadata server get the node's identity. Standard only; Autopilot manages it.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Every pool's `MODE` reads `GKE_METADATA`                                                                                                                | `install.sh` migrates each pool that is not when authorized via `--migrate-node-pools` (or `MIGRATE_NODE_POOLS=true`), **which recreates that pool's nodes**, including pools running workloads unrelated to kube-agents. Without opt-in, the install aborts before making any cluster changes (`REFUSED_MISSING_NODE_POOL_MIGRATION`). Terraform does not check, so refusing beforehand prevents pods silently running under the wrong identity.                                                                                                                                                                                                                                             |
| **NetworkPolicy enforcement**: Dataplane V2, or the legacy Calico addon. Every NetworkPolicy the install ships is accepted and inert without one; GKE Standard's default has neither.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | `datapathProvider: ADVANCED_DATAPATH`, or `networkPolicy.enabled: true`                                                                                 | `--enable-network-policy`: `install.sh` enables the Calico addon, then enforcement; can recreate node pools. `--accept-no-network-policy`: install without enforcement, cluster untouched ([what that costs](#installing-without-networkpolicy-enforcement)). Neither: the install aborts unchanged. Bare Terraform refuses unless `accept_no_network_policy = true`. No migration to Dataplane V2; `FQDNNetworkPolicy` companions stay inert on Calico.                                                                                                                                                                                                                                      |
| **A control plane reachable from where Terraform runs, and from the agent.** Terraform's Helm provider dials whatever endpoint GKE reports for the cluster: the IP endpoint when there is one, otherwise the DNS endpoint. The agent, and every credential fetch the installer and teardown make, prefer the DNS endpoint whenever it allows external traffic, whether or not an IP endpoint exists.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | `ipEndpointsConfig.enabled` and `enablePrivateEndpoint` for Terraform; for the agent, either of those or `dnsEndpointConfig.allowExternalTraffic: true` | Nothing checks any of it. Terraform run outside the VPC fails on a private IP endpoint, which GKE keeps reporting even once a DNS endpoint exists; disabling IP access is what moves Terraform onto the DNS endpoint, and that answers 403 unless external traffic is allowed on it. On a cluster the install did not create, `allow_external_dns_traffic` does nothing; open the DNS endpoint with `gcloud container clusters update --enable-dns-access` instead. [`gke_dns_endpoint.sh`](https://github.com/gke-labs/kube-agents/blob/main/scripts/installer/gke_dns_endpoint.sh) explains why the flag is never passed blind.                                                             |
| **cert-manager**, present or absent, but declared either way.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | `kubectl get deployment cert-manager -n cert-manager`                                                                                                   | `install.sh` probes for it (in that namespace only) and sets `enable_cert_manager` for you; a bare Terraform run against a cluster that already has it fails on the existing CRDs unless you set it to `false`. Details [below](#cert-manager-on-the-target-cluster).                                                                                                                                                                                                                                                                                                                                                                                                                         |
| **A gVisor RuntimeClass**, because the agent runs sandboxed by default (`--enable-gvisor=false` opts out).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | Autopilot: `currentMasterVersion` at or above `1.27.4-gke.800`. Standard: nothing to read; the install adds it.                                         | `install.sh` refuses an older Autopilot cluster before applying anything. On Standard the composition creates a `gvisor-pool` node pool of one `e2-standard-4` per zone, autoscaling to two: new billable capacity, and it carries GKE's sandbox taint, so only the sandboxed agent lands there. The module's [sizing inputs](https://github.com/gke-labs/kube-agents/blob/main/terraform/modules/gke-cluster/README.md#sizing-the-node-pools) resize it; `install.sh` writes none of them, so set them as `TF_VAR_` lines in `install.env`. A cluster that already has a pool of that name needs `gvisor_pool_name` changed; the module never adopts one.                                    |
| **Schedulable capacity for the workloads the install adds.** [Footprint](#footprint) gives what each install profile requests. A today install's pods include the dashboard, shell sandbox, kube-proxy, and cleanup hook, and the agent pod alone needs its whole request free on a single node. On GKE Autopilot the credential proxy requests more, which the Autopilot rows count. Hindsight memory and the GitHub minter each add more. Those are the defaults for a small fleet. [Fleet and host-cluster sizing](/kube-agents/deploy/fleet-sizing/) sums them per pod with the minter on and the hook left out, and says what a fleet of 10 or more clusters asks of the host cluster. The per-workload figures live in `charts/kube-agents/files/footprint.yaml` (operator-rendered pods) and `charts/kube-agents/values.yaml` (the chart's own). Under `spec.mode: next` (the new-install mode), the A2A stack adds standing workloads: NATS, the auth callouts, the capability verifiers, the A2A gateways, the console, and a `hermes-bridge` container in the agent pod, along with a provisioning Job. NATS runs three servers by default, each on its own node; `spec.harness.tuning.busReplicas: 1` runs one. In addition, each `delegate:` from chat, and each conversation moved to `/session` (one pod for the whole conversation, kept until it has been idle 30 minutes or for 4 hours at most), spawns an `a2a-worker` session pod, up to `maxSessions` at once. The footprint's per-pod table gives each one's requests and limits. | `kubectl describe nodes` allocatable minus requests, on the pools the agent can use                                                                     | Nothing checks it, and the failure depends on which pod is stuck. LiteLLM or the operator Pending: the Helm release waits ten minutes and fails with `context deadline exceeded`, naming nothing. Only the agent pod Pending: the operator creates it after the Helm release, so the apply succeeds and `install.sh` prints a warning naming `platform-agent-gateway` after five minutes, then reports the install complete.                                                                                                                                                                                                                                                                  |
| **Namespace ResourceQuota headroom.** When the release namespace enforces a baseline ResourceQuota, it needs room for the whole release: the requests in the row above, plus the limits [Footprint](#footprint) gives for the same profile.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | `kubectl describe resourcequota -n <release-namespace>`                                                                                                 | The chart checks un-scoped ResourceQuotas in the release namespace at render time (`quotaPreflight.enabled=true`; `--set quotaPreflight.enabled=false` skips it) and fails with a diagnosis and a ready-to-run `kubectl patch`. With no cluster to query (`helm template`) or no ResourceQuota in the namespace it passes silently, so a clean render is not proof a quota fits; without `get`/`list` on `resourcequotas` there it fails the render instead, because Helm's `lookup` raises on anything but a NotFound. The [chart README](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#quota-preflight) is canonical for what it sums and how it compares. |

The chart's quota check counts the `spec.mode: next` additions in the capacity row above when `platformAgent.mode` is `next`, but not the session pods. Autopilot sizes nodes to fit; on a Standard cluster, check the free allocatable against those figures, or a session pod stays `Pending` when one is asked for.

Two next-mode workloads run only behind an operator flag, and the check counts each while its flag is set. With `A2A_MCP_DOOR=true` (the chart's `operator.mcpServer.enabled`, or an `operator.extraEnv` entry), the MCP server adds one pod. With `A2A_DISPATCHER_IMAGE` set through `operator.extraEnv`, the profile dispatcher adds one pod. The footprint's per-pod table gives both. Both run in the release namespace. AgentProfile workers don't: they run in the profile namespace, `<release-namespace>-profiles`, under a pod quota the operator renders there.

With `platformAgent.deployment.credentialProxy.exec.replicas` above 0, each exec-tier replica is one more pod with 2 CPU and 2 GiB in requests and limits, plus the broker's ephemeral storage, and the chart's check counts them. It does not check node room. On a Standard cluster whose nodes lack 2 free CPU, a replica stays `Pending`. Turning the tier on also restarts the shell sandbox, which then needs its own room again.

The scope publisher's Job runs in the release namespace too, one pod at a time every 15 minutes. It runs where the gateway pod admission policy is on and `platformAgent.scope` names a folder, organization, Shared VPC host or Metrics Scope, and the check counts it then.

The `install.sh` pre-flight summary prints the release's requested CPU, memory, pods and persistent storage, with a monthly cost modeled from us-central1 list prices, model tokens excluded, before anything is applied. The apply shows no chart notes; `helm get notes kube-agents -n <NAMESPACE>` prints the same figures afterwards.

A new bus runs three NATS servers on separate nodes, so that one node drain or upgrade takes one server and the bus stays up. With fewer than three nodes, the third waits `Pending` until a node joins. Autopilot and an autoscaled pool add one. A bus keeps its server count once it exists. Changing it is not supported yet, so a `spec.harness.tuning.busReplicas` that asks an existing bus for another count is refused, and the bus runs on unchanged.

An install whose `next` bus predates the three-server default keeps its one server. A `today` install has no bus, so its first switch to `next` creates one with three servers, like a new install. The chart's quota check cannot see the bus that runs, so it counts three servers unless `platformAgent.harness.tuning.busReplicas` is `1`. An install that keeps one server under a namespace quota should set that value before it upgrades. Otherwise the check asks for room for two more pods and two more claims.

A Standard cluster that cannot grow to three nodes should run one server, set before the first install: the chart value `platformAgent.harness.tuning.busReplicas: 1`, which `install.sh` and the Terraform full install take through `extra_helm_values` (see [Operator settings](/kube-agents/install/quickstart-gke/#operator-settings)). One server is down for as long as its pod restarts. Once the bus exists, the field on the PlatformAgent cannot change its count.

The Terraform full install sets it to 1 on a zonal Standard cluster it creates, so that bus runs one server from the start.

A quota is more than CPU and memory, and so is the check: it also counts pods, persistent volume claims, `requests.storage` and ephemeral storage, which [Footprint](#footprint) gives for each profile. Two of the claims, the agent's data claim and the shell sandbox's, follow [`spec.storage.size`](/kube-agents/operator/platformagent-crd/#specstorage), so each GiB added to it adds 2 GiB of `requests.storage`, and the chart's check counts it. [`spec.storage.expectedClusterProfiles`](/kube-agents/operator/platformagent-crd/#specstorage) raises the agent's claim alone, and the check counts that too. A namespace quota that caps pod or claim counts below what the release needs refuses the install the same way; note that constraining ephemeral storage additionally requires every container to declare it (or a `LimitRange` to default it), as the chart README explains. Under `spec.mode: next`, each NATS server adds a JetStream persistent volume claim, and the operator renders a namespace-wide `ResourceQuota` on `pods`, the footprint's session quota, sized for the session pods plus the rest of the release; anything else you run in
the namespace counts against it. The claims
are also disks: on GKE's default `standard-rwo` class they are balanced persistent
disks, which count against the project's regional SSD quota (`SSD_TOTAL_GB`) with the
node boot disks. The chart does not check that quota. A new claim that exceeds it stays
`Pending` with a `QUOTA_EXCEEDED` event. A claim the operator grows on an upgrade stays
`Bound` at its old `status.capacity` under the new `spec`, with a `Resizing` condition and
`VolumeResizeFailed` events, until the quota frees.

Three more things the installer sets on a cluster it creates, and treats differently on one it adopts:

- **CMEK database encryption.** `install.sh` enables it on an adopted cluster too, creating a KMS keyring and key and updating the control plane, unless `ALLOW_UNENCRYPTED_SECRETS=true`. `uninstall.sh` does not revert it. Bare Terraform does not require or enable it.
- **Managed OpenTelemetry collection scope.** Set only on clusters the install created; on an adopted one, follow [Enable Managed OpenTelemetry on an existing cluster](/kube-agents/reference/attribution/#enable-managed-opentelemetry-on-an-existing-cluster).
- **The Backup for GKE agent.** Only needed with `enable_gke_backup_plan = true`, which is off by default; the module cannot enable the agent on a cluster it only reads.

Run the installer with `--dry-run` to see the plan against the cluster without changing it. The installer's own cluster changes happen only on a real run, so a dry run against a cluster still missing NetworkPolicy enforcement or legacy node pool migration skips the resource preview (`terraform plan`) with a warning; a real run, and `--generate-only`, abort unless told which way to go with `--enable-network-policy` or `--accept-no-network-policy`, and `--migrate-node-pools`.

### Footprint

These figures are rendered from the chart's quota preflight and the operator's own pod definitions, so they match what the check counts. A profile not listed adds up the same way: `install.sh` prints the totals for the values it is about to apply.

<!-- BEGIN GENERATED: install-footprint -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

Totals by install profile, as the chart's quota preflight sums them (the operator's pods included, the session pods not):

| Profile | Pods | CPU requests | Memory requests | CPU limits | Memory limits | Ephemeral storage requests / limits | Claims | `requests.storage` | Session quota `pods` |
| ------- | ---- | ------------ | --------------- | ---------- | ------------- | ----------------------------------- | ------ | ------------------ | -------------------- |
| `today`, GKE Standard | 9 | 2756m | 12096Mi | 13200m | 25472Mi | 14912Mi / 14912Mi | 4 | 102Gi | — |
| `today`, GKE Autopilot | 9 | 4256m | 13120Mi | 13200m | 25472Mi | 14912Mi / 14912Mi | 4 | 102Gi | — |
| `next`, GKE Standard | 20 | 4236m | 14768Mi | 21050m | 32224Mi | 15232Mi / 15232Mi | 7 | 222Gi | 38 |
| `next`, GKE Autopilot | 20 | 5736m | 15792Mi | 21050m | 32224Mi | 15232Mi / 15232Mi | 7 | 222Gi | 38 |
| `next` with one NATS server, GKE Standard | 18 | 3716m | 13712Mi | 18950m | 30112Mi | 15104Mi / 15104Mi | 5 | 142Gi | 36 |
| `next` with Hindsight and the GitHub minter, GKE Standard | 24 | 5086m | 17200Mi | 27050m | 37856Mi | 16Gi / 16Gi | 8 | 230Gi | 44 |

The session quota is `spec.harness.tuning.maxSessions` (10 by default) plus the pods the operator runs for the agent, the chart's pods in the namespace with their rollout surges (5 when the operator cannot read the chart's count), and a margin of 7 for the operator's rollout surges, its Jobs and the gateway's count-then-create overshoot.

Under `spec.mode: next`, per pod:

| Workload | Pods | CPU request | Memory request | CPU limit | Memory limit | Ephemeral storage request / limit | Runs |
| -------- | ---- | ----------- | -------------- | --------- | ------------ | --------------------------------- | ---- |
| NATS server | 3 | 260m | 528Mi | 1050m | 1056Mi | 64Mi / 64Mi | Standing, each on its own node, with one JetStream claim each (40Gi); one with `spec.harness.tuning.busReplicas: 1` |
| Auth callout | 2 | 50m | 64Mi | 500m | 256Mi | — | Standing |
| Capability verifier | 2 | 50m | 64Mi | 500m | 256Mi | — | Standing |
| A2A gateway | 2 | 50m | 64Mi | 500m | 512Mi | — | Standing; one fewer with the operator's `A2A_GATEWAY_ELECTION=false` |
| Console | 1 | 100m | 128Mi | 500m | 256Mi | — | Standing |
| Bus provisioning Job | 1 | 50m | 64Mi | 200m | 256Mi | 128Mi / 128Mi | At install, again whenever its spec changes, and about daily |
| `hermes-bridge` container | in the agent pod | 250m | 512Mi | 1 | 1Gi | — | Under the default `api` executor; `A2A_BRIDGE_EXECUTOR=cli` raises it to 1 CPU and 4Gi memory (limits 3 and 8Gi), and `A2A_BRIDGE_RESOURCES` overrides either |
| MCP server | 1 | 100m | 128Mi | 500m | 256Mi | — | With the operator's `A2A_MCP_DOOR=true` |
| Profile dispatcher | 1 | 50m | 64Mi | 500m | 256Mi | — | With the operator's `A2A_DISPATCHER_IMAGE` set |
| Scope publisher Job | 1 | 50m | 192Mi | 500m | 512Mi | — | One pod at a time, every 15 minutes, where `platformAgent.scope` names a folder, organization, Shared VPC host or Metrics Scope |
| `a2a-worker` session pod | up to `maxSessions` | 250m | 512Mi | 2 | 2Gi | 1088Mi / 1088Mi | One per `delegate:` from chat and per conversation moved to `/session` |

<!-- prettier-ignore-end -->
<!-- END GENERATED: install-footprint -->

### Installing without NetworkPolicy enforcement

`--accept-no-network-policy` installs onto a cluster that has neither Dataplane V2 nor the Calico addon
without modifying it (unavailable when `--enable-web-console` / `WEB_CONSOLE_ENABLED=true` is enabled,
because the web console requires NetworkPolicy enforcement to confine access to `kubectl port-forward`).
The install works: every NetworkPolicy it ships is accepted by the API server
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
- **Slack** (opt-in): a Slack workspace where you can install a bot app and generate bot + app tokens. Many workspaces need an admin to approve a new app, so arrange that first. [Slack app setup](/kube-agents/install/slack-app/) covers the app, for either mode. Slack is configured only if you enable it in the installer's chat menu or with `--enable-slack` (or set `enable_slack = true` in `terraform.tfvars`).

**A GCP project holds one Chat app.** Google's rule is that "each Google Chat app that you create
requires its own Google Cloud project with the Chat API enabled" —
[Configure the Chat API](https://developers.google.com/workspace/chat/configure-chat-api). So a
project already running a Chat app cannot also run kube-agents' Chat integration.

This only applies if Chat is on, and whether it is on by default depends on which front door you
use. Terraform and Helm both default it off — `enable_google_chat` and the chart's
`googleChat.enabled` are `false`, so neither takes a slot unless you ask. The interactive installer
goes the other way: its chat menu pre-selects **Google Chat**, so pressing enter at that prompt
provisions the Chat backend. Pick "None" or Slack there if you want the project's Chat slot left
alone. Pick Slack alone, too, if you install `next` (a first install's default) and want Slack on the A2A gateway: with both platforms enabled, Google Chat holds the gateway and Slack stays on the
Hermes Slack platform. **Slack is unaffected** — it provisions no GCP resource at all, so a project whose Chat slot
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
- `OPENAI_COMPATIBLE_API_KEY`, for an OpenAI-compatible server you run (`openai_compatible`), and only if that server checks a key. A gateway behind OAuth2 client credentials, SAP AI Core (`sap`) and any other LiteLLM provider take theirs from a Secret you create and name with `--litellm-env-secret` instead. See [Inference gateway](/kube-agents/concepts/inference-gateway/#a-credentials-secret-for-the-gateway).

Or route one of these keys through a self-hosted LiteLLM gateway — see [`examples/litellm-gemini/`](https://github.com/gke-labs/kube-agents/tree/main/examples/litellm-gemini) for a Gemini API-key template.

## GitOps repo & GitHub token minter (for `submit-suggestion`)

This section is for a GitOps repository on GitHub only. For GitLab, see [GitOps repo on GitLab](#gitops-repo-on-gitlab); for Bitbucket, see [GitOps repo on Bitbucket](#gitops-repo-on-bitbucket); for Gitea, see [GitOps repo on Gitea](#gitops-repo-on-gitea).

The declarative workflow routes infrastructure mutations through pull requests via the GitHub token minter (Minty). When GitOps is enabled, prepare:

- **GitHub Organization:** A GitHub repo **owned by an organization**. Minty queries `/orgs/{org}/installation`, so personal accounts fail token minting with HTTP 404 — see [Token minter](/kube-agents/deploy/token-minter/). A free organization is sufficient.
- **GitHub App:** A GitHub App with `contents:write`, `pull_requests:write`, and `issues:write` permissions, installed on that repo and on any repository you later register under `context_repos` (see upstream [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter#readme) and [Read-only tokens for context repositories](/kube-agents/deploy/token-minter/#read-only-tokens-for-context-repositories)).
- **GitHub App ID:** Numeric ID of the created App.
- **Cloud KMS Key & Private Key Import:** The App's private key imported into a Cloud KMS asymmetric signing key in your cluster's region (default on a new install `<cluster>-minter-keyring` / `<cluster>-minter-key`). Because Cloud KMS keys cannot be deleted in GCP, this import is a permanent one-time step.
  - **Option 1 (Automated import via `install.sh`):** Provide `--github-pem-path="/path/to/app-private-key.pem"` during install. `install.sh` creates the KMS key, runs the Minty CLI to import the key, and you can delete the local `.pem` file immediately after. Needs Go 1.21+ on the installer host — the Minty CLI asks for Go 1.24, and 1.21 onwards downloads that toolchain on demand. Distribution packages are often older (Debian 12 ships 1.19, Ubuntu 22.04 ships 1.18); `install.sh` checks before it builds and tells you if yours cannot work. This is unrelated to the Go version needed to build the operator.
  - **Option 2 (Pre-provisioned Ahead-Of-Time):** Pre-provision the KMS key and import the private key upfront following the [upstream guide](https://github.com/abcxyz/github-token-minter#readme) and [Google Cloud KMS documentation](https://cloud.google.com/kms/docs/importing-a-key) (recommended for CI/CD and release pipelines). `install.sh` detects the existing `ENABLED` version and skips `.pem` handling.

**A GitOps repository on a GitHub Enterprise Server** needs the App on the instance, a network path from the cluster to the instance on port 443, and, for a private CA, the CA's PEM file for `--gitops-ca-file`. `install.sh` installs it with `--gitops-host`: see [GitHub Enterprise Server in INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#github-enterprise-server).

See [Deploy → Token minter](/kube-agents/deploy/token-minter/) and the upstream [`abcxyz/github-token-minter`](https://github.com/abcxyz/github-token-minter#readme) guide for complete setup details.

## GitOps repo on GitLab

When the GitOps repository is on GitLab (gitlab.com or a self-managed instance), you need none of the GitHub items above: a GitLab install has no GitHub App, no token minter and no Cloud KMS key. Prepare these:

- **GitLab project:** the GitOps project, created before the install. Note its full path, with its nested groups (`group/subgroup/project`).
- **Access token:** a group or project access token with the `api` and `write_repository` scopes and the Developer role. On a GitLab tier without group or project access tokens, use a personal access token of a dedicated account that is a Developer member of the project.
- **Network access:** the credential broker pod must reach the GitLab host over HTTPS. A self-managed instance on a private network needs a route from the cluster.
- **Certificate:** a self-managed instance must present a certificate that the broker trusts. See [Before you start](/kube-agents/install/gitlab/#before-you-start).

See [GitLab as the GitOps forge](/kube-agents/install/gitlab/) for the installer flags and where the token goes.

## GitOps repo on Bitbucket

When the GitOps repository is on Bitbucket Cloud or on a Bitbucket Data Center instance, you need none of the GitHub items above: a Bitbucket install has no GitHub App, no token minter and no Cloud KMS key. Prepare these:

- **Bitbucket repository:** the GitOps repository, created before the install, with one commit on its default branch. Note its path: `workspace/repo-slug` on Cloud, `PROJECTKEY/repo-slug` on Data Center.
- **Token:** on Cloud, the API token (with scopes) of an account that exists only for this install, a workspace access token (Premium), or a repository access token for an install with one repository. On Data Center, a project HTTP access token with Repository write.
- **Identity:** a Cloud workspace, project or repository access token needs the account UUID that it acts as.
- **Network access:** the credential broker pod must reach `bitbucket.org` and `api.bitbucket.org`, or the Data Center host, over HTTPS on port 443. A Data Center instance must serve at the root path on port 443.
- **Certificate:** a Data Center instance behind a private CA needs the CA's PEM file.

See [Bitbucket as the GitOps forge](/kube-agents/install/bitbucket/) for the installer flags and where the token goes.

## GitOps repo on Gitea

When the GitOps repository is on a self-managed Gitea instance (in-cluster or on-premises), you need none of the GitHub items above either. `install.sh` has no Gitea flag: the forge is declared on the `PlatformAgent` CR, or through the chart, after the install. Prepare these:

- **Gitea repository:** the GitOps repository, created before the install, and a dedicated automation account that can write to it.
- **Access token:** a token of that account with the `write:repository`, `write:issue` and `read:user` scopes, stored in a Secret in the agent's namespace under the key `token`.
- **Network access:** the credential broker pod must reach the Gitea host; an in-cluster Service or a route from the cluster.
- **Certificate:** an HTTPS instance signed by a private CA needs its chain in a Secret under the key `ca.crt`, referenced from the forge entry.

See [Gitea as the GitOps forge](/kube-agents/install/gitea/) for the token Secret, the CA bundle and the forge declaration.

## Ready to install

- [Quick start (GKE)](/kube-agents/install/quickstart-gke/) — run the installer end-to-end.
- [Manual install](/kube-agents/install/manual/) — step-by-step, no wrapper script.
