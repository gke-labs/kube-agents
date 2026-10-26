---
title: Upgrade
description: Moving an existing install to a newer release with upgrade.sh — its modes, how the target version is resolved, and what a run refuses before any of the new release is applied.
---

`upgrade.sh` is the Day-2 engine for an install `install.sh` created. It re-applies the same
Terraform composition and the same Helm chart at a newer revision, re-rendering the install from
the configuration it already has. A release copy of the script carries the version it upgrades to,
so the ordinary upgrade names no version at all.

An install whose GitHub forge is a GitHub Enterprise Server uses `upgrade.sh` too: it renders the
forge from `GITOPS_FORGE=github`, `GITOPS_HOST` and `GITOPS_CA_SECRET` in `install.env`. An install
made with the Terraform + Helm method is upgraded with that method: see
[GitHub Enterprise Server in INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#upgrade-switch-and-teardown).

This site describes `main`, not a release. It changes each time a docs change merges to `main`. For
the release you upgrade to, read the same pages at that release's tag, for example
`https://github.com/gke-labs/kube-agents/tree/<RELEASE_VERSION>/docs/site/src/content/docs/install/upgrade.md`.
If this site and the page at the tag do not agree, the page at the tag describes the release. On
GitHub, the links between pages do not work: open each linked page at the same tag.

## Before you start

A GitLab install needs nothing extra for an upgrade: no token and no flag. See
[Upgrade and uninstall](/kube-agents/install/gitlab/#upgrade-and-uninstall).

Record what the install runs now, so you can tell afterwards what moved:

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.spec.mode}{"\n"}'
helm history kube-agents -n kubeagents-system
```

The third command prints the install's `spec.mode`; an empty line means `today`.

Read the release notes for every release between the one you run and the one you are moving to.

The upgrade refuses to run without the install's own configuration, because a full upgrade
re-renders the `PlatformAgent` resource from it: a file written from memory re-renders the install
with whatever it forgets. `KUBE_AGENTS_INSTALL_ENV` names the file outright, which is how an
ephemeral CI runner supplies one. Otherwise the script looks for `install.env` in the checkout it
is running from, then in the directory you run it from, and — when run from outside a checkout,
such as the piped release one-liner — last in the install checkout the installer left in
`$HOME/kube-agents`, so standing in one install's directory upgrades that install, not whichever
one the checkout in `$HOME` belongs to.
If the file is lost, rebuild it first: see [Recovering `install.env`](#recovering-installenv).

`--upgrade-mode=full`, the default, additionally needs the `terraform` CLI on `PATH`.

## Recovering `install.env`

If the machine that ran `install.sh` is gone, rebuild `install.env` from what the install recorded.
No copy of the file exists on the cluster or in Google Cloud, and the generated
`terraform.tfvars` stayed on that machine too.

The install records its settings in three places:

- **The Helm values record.** Terraform rendered these values from `install.env`, so most keys map
  to one value path. Read them with `helm get values kube-agents -n <NAMESPACE> -o json`.
- **The Terraform state**, at `gs://<PROJECT_ID>-kube-agents-tfstate/kube-agents/<CLUSTER_NAME>/default.tfstate`
  unless the install moved it. It records the keys that the values do not hold.
- **The `PlatformAgent` resource.** Use it only as a cross-check. It also shows edits made with
  `kubectl`, and a value copied from such an edit changes what the upgrade renders.

The values record and the state both contain the install's secrets. Do not print the whole state,
and do not save either one to a shared disk.

1. Find the install. `gcloud storage ls gs://<PROJECT_ID>-kube-agents-tfstate/kube-agents/` lists
   one folder for each cluster name. `gcloud container clusters list --project <PROJECT_ID>` gives
   each cluster's location, and `helm list -A --filter '^kube-agents$'` gives the namespace. If the
   state is not at the default path, set `KUBE_AGENTS_STATE_BUCKET` and `KUBE_AGENTS_STATE_PREFIX`
   to where you found it.
2. Save the values without the secrets, and read the state outputs:

   ```bash
   helm get values kube-agents -n <NAMESPACE> -o json \
     | jq 'del(.platformAgent.credentials)' > values.json
   gcloud storage cat <STATE_PATH> | jq '.outputs | map_values(.value)'
   ```

3. Write each key from the tables below into a new file named `install.env`. Write every value
   that is not empty, also when it equals a default: the upgrade does not apply every default that
   `install.sh` applied. Leave a key out only when its value is empty, unless the table says
   otherwise.
   Write a list as comma-separated values, for example
   `jq -r '.platformAgent.integration.slack.allowedUsers | join(",")' values.json`.
4. Leave the secrets out. When the file holds no `API_SERVER_KEY`, model provider API key, Slack
   token or Session KV key, the upgrade reads it from the `platform-agent-secrets` Secret. Set
   `PERSIST_SECRETS_ON_DISK=false` to keep them out of the regenerated `terraform.tfvars` as well.
5. Check the file before you upgrade. Use the `upgrade.sh` of the release the install runs now, not
   the release you are moving to. Plan at the image tag in the state output `image_tag`. If the
   output is `null`, use `.platformAgent.deployment.image.tag` from `values.json`.

   ```bash
   curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<CURRENT_RELEASE>/upgrade.sh \
     | KUBE_AGENTS_INSTALL_ENV="$PWD/install.env" bash -s -- --plan \
       --image-tag="$(gcloud storage cat <STATE_PATH> | jq -r '.outputs.image_tag.value')"
   ```

   Exit code 0 means the file renders the install as it is. If the exit code is 2, find the key
   that differs and plan again (one exception: `THIRD_PARTY_REGISTRY_PREFIX` added per the table's
   note below is expected to differ on older releases; keep it). The plan masks the Helm values,
   so compare `values.json` with the file.

Then upgrade to the new release with the same file.

### Keys in the values record

Each path is a `jq` path on `values.json`.

| Key                                              | Path                                                                                  | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------ | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `PROJECT_ID`                                     | `.platformAgent.harness.projectId`                                                    |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `CLUSTER_NAME`                                   | `.platformAgent.harness.clusterName`                                                  |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `REGION`                                         | `.platformAgent.harness.location`                                                     | Copy it exactly. It can be a zone.                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `NAMESPACE`                                      | the release namespace                                                                 |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `PLATFORM_AGENT_MODE`                            | `.platformAgent.mode`                                                                 | Empty means `today`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `PLATFORM_AGENT_STORAGE_SIZE`                    | `.platformAgent.storage.size`                                                         | Empty means the operator default.                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `MODEL_PROVIDER`                                 | `.litellm.modelProvider`                                                              |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `MODEL_DEFAULT_NAME`                             | `.litellm.modelDefaultName`                                                           |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `MODEL_MAX_TOKENS`                               | `.litellm.maxTokens`                                                                  |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `VERTEX_PROJECT_ID`, `VERTEX_LOCATION`           | `.litellm.vertex.projectId`, `.litellm.vertex.location`                               | `vertex_ai` only.                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| `LITELLM_REDACTION_ENABLED`                      | `.litellm.redaction.enabled`                                                          | If it is `true`, also copy `.litellm.redaction.ip.action` to `LITELLM_REDACTION_IP_ACTION`, `.litellm.redaction.ip.allowCidrs` to `LITELLM_REDACTION_IP_ALLOW_CIDRS` and `.litellm.redaction.rules` to `LITELLM_REDACTION_RULES` (as JSON in single quotes).                                                                                                                                                                                                                         |
| `GOOGLE_CHAT_ENABLED`                            | `.platformAgent.integration.googleChat.enabled`                                       | If it is `true`, also copy these fields of `.platformAgent.integration.googleChat`: `topicName` to `CHAT_TOPIC_NAME`, `subscriptionName` to `CHAT_SUB_NAME`, `mode` to `GOOGLE_CHAT_MODE`, `allowedUsers` to `ALLOWED_USERS` and `homeChannel` to `GOOGLE_CHAT_HOME_CHANNEL`.                                                                                                                                                                                                        |
| `SLACK_ENABLED`                                  | `.platformAgent.integration.slack.enabled`                                            | If it is `true`, also copy these fields of `.platformAgent.integration.slack`: `allowedUsers` to `SLACK_ALLOWED_USERS`, `homeChannel` to `SLACK_HOME_CHANNEL` and `homeChannelName` to `SLACK_HOME_CHANNEL_NAME`.                                                                                                                                                                                                                                                                    |
| `GITOPS_ORG`                                     | `.platformAgent.integration.github.org`                                               |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `GITOPS_REPO`                                    | `.platformAgent.integration.github.gitRepo`                                           | The value is `<org>/<repo>`. Write only the part after the `/`.                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `GITHUB_APP_ID`                                  | `.githubMinter.appId`                                                                 |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `KMS_KEYRING`, `KMS_KEY`                         | `.githubMinter.kms.keyring`, `.githubMinter.kms.key`                                  |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| GitLab keys                                      | `.platformAgent.integration.forges[0]`, `.platformAgent.integration.repositories[0]`  | If the forge's `provider` is `gitlab`, set `GITOPS_FORGE=gitlab`. Copy the forge's `host` to `GITOPS_HOST`, `credentialsRef.name` to `GITLAB_TOKEN_SECRET` and `caBundleRef.name` to `GITOPS_CA_SECRET`, and the repository's `repository` to `GITOPS_REPO`.                                                                                                                                                                                                                         |
| GitHub Enterprise Server keys                    | `.platformAgent.integration.forges[0]`, `.platformAgent.integration.repositories[0]`  | If the forge's `provider` is `github` and it has a `host`, set `GITOPS_FORGE=github`. Copy the forge's `host` to `GITOPS_HOST`, `caBundleRef.name` to `GITOPS_CA_SECRET` and `namespace` to `GITOPS_ORG`, and the repository's `repository` to `GITOPS_REPO`.                                                                                                                                                                                                                        |
| `ENABLE_GVISOR`                                  | `.platformAgent.deployment.availability.runtimeClassName`                             | `gvisor` means `true`. Always write the key: the upgrade's default is `false`, not the installer's `true`.                                                                                                                                                                                                                                                                                                                                                                           |
| `MEMORY`                                         | `.platformAgent.harness.memory.provider`                                              | `kube_agents_memory` is `hindsight`, `multiuser_memory` is `file` and `none` is `off`.                                                                                                                                                                                                                                                                                                                                                                                               |
| `USER_PROFILE_ENABLED`                           | `.platformAgent.harness.memory.userProfileEnabled`                                    |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `HERMES_DASHBOARD_ENABLED`                       | `.platformAgent.harness.hermes.dashboardEnabled`                                      | The upgrade does not read the old `ENABLE_WEBUI` name.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `ENABLE_DRIFT_DETECTOR`                          | `.platformAgent.harness.driftDetector`                                                | If `enabled` is `true`, write `true`. If the path is absent (`null`), write `false`. If it has a `subscription` but no `enabled`, write `false` and `TF_VAR_enable_drift_pubsub=true`.                                                                                                                                                                                                                                                                                               |
| `ENABLE_PUBSUB_PLATFORM`                         | `.plugins.pubsubPlatform.enabled`                                                     | When `.plugins.stockoutInvestigator.enabled` is `true`, this value is always `true` and does not show the key. Write the key only if you know its old value.                                                                                                                                                                                                                                                                                                                         |
| `ENABLE_STOCKOUT_INVESTIGATOR`                   | `.plugins.stockoutInvestigator.enabled`                                               | If it is `true`, also copy these fields of `.plugins.stockoutInvestigator.pubsub`: `topic` to `TF_VAR_stockout_pubsub_topic`, `subscription` to `TF_VAR_stockout_pubsub_subscription` and `sink` to `TF_VAR_stockout_pubsub_sink`.                                                                                                                                                                                                                                                   |
| `SCOPE_*`                                        | `.platformAgent.scope`                                                                | Copy `projects`, `folders`, `organizations`, `sharedVpcHosts`, `metricsScopes`, `maxProjects`, `exclude.projects` and `exclude.clusters` (each as `project/location/cluster`).                                                                                                                                                                                                                                                                                                       |
| `SCOPED_SA_POOL_ENABLED`                         | `.platformAgent.security.scopedServiceAccountPool.enabled`                            |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `ACCEPT_NO_NETWORK_POLICY`                       | `.platformAgent.annotations["kubeagents.x-k8s.io/network-policy-enforcement"]`        | `absent-accepted` means `true`.                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `REGISTRY_PREFIX`, `THIRD_PARTY_REGISTRY_PREFIX` | `.global.imageRegistry`, `.global.thirdPartyImageRegistry`                            | If `.global.imageRegistry` was set but `.global.thirdPartyImageRegistry` was empty: for an install created before the third-party fallback was removed whose mirror holds third-party images (the `make mirror-images MIRROR_PREFIX=` default), copy `.global.imageRegistry` to `THIRD_PARTY_REGISTRY_PREFIX` to keep third-party images on the mirror; step 5 exits 2 on this key, keep it. If the mirror holds only first-party images, leave `THIRD_PARTY_REGISTRY_PREFIX` unset. |
| `PLATFORM_AGENT_GSA_NAME`                        | `.platformAgent.security.serviceAccountAnnotations["iam.gke.io/gcp-service-account"]` | Write the part before the `@`.                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `GITHUB_MINTER_GSA_NAME`                         | `.githubMinter.gsaName`                                                               |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `LITELLM_GSA_NAME`                               | `.litellm.vertex.serviceAccountAnnotations["iam.gke.io/gcp-service-account"]`         | `vertex_ai` only. Write the part before the `@`.                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `TF_VAR_agent_ksa_name`                          | `.platformAgent.security.serviceAccountName`                                          |                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| `TF_VAR_image_pull_secrets`                      | `.global.imagePullSecrets`                                                            | Write it as a JSON list in single quotes.                                                                                                                                                                                                                                                                                                                                                                                                                                            |

### Keys outside the values record

| Key                                                                                         | Where to read it                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PLATFORM_AGENT_PERMISSION_SET`, `PLATFORM_AGENT_CUSTOM_ROLES`                              | The state output `agent_project_roles`. If it is not the [`read-only` set](/kube-agents/reference/security-and-iam/#roles-per-set), write `custom` and the roles.                                                                                                                                                                                                                                                                                                                                                                                                           |
| `ENABLE_GKE_BACKUP_PLAN`                                                                    | The state output `backup_plan_name`. A value means `true`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| `TF_VAR_drift_pubsub_topic`, `TF_VAR_drift_pubsub_subscription`, `TF_VAR_drift_pubsub_sink` | The state outputs `drift_pubsub_topic` and `drift_pubsub_subscription`. The sink has no output: `gcloud storage cat <STATE_PATH> \| jq -r '.resources[] \| select(.type=="google_logging_project_sink" and .name=="drift_audit") \| .instances[0].attributes.name'`.                                                                                                                                                                                                                                                                                                        |
| `VERTEX_MANAGE_SERVING_PROJECT`                                                             | On a `vertex_ai` install, `false` when this prints `0`: `gcloud storage cat <STATE_PATH> \| jq '[.resources[] \| select(.name=="litellm_vertex_user")] \| length'`.                                                                                                                                                                                                                                                                                                                                                                                                         |
| `SKIP_CERT_MANAGER`                                                                         | `true` when the cluster runs cert-manager and this prints `0`: `gcloud storage cat <STATE_PATH> \| jq '[.resources[] \| select(.type=="helm_release" and .name=="cert_manager")] \| length'`.                                                                                                                                                                                                                                                                                                                                                                               |
| `TF_VAR_extra_helm_values`                                                                  | `gcloud storage cat <STATE_PATH> \| jq -r '.resources[] \| select(.type=="helm_release" and .name=="kube_agents") \| .instances[0].attributes.values[1]'`. If it is not `{}`, convert it to JSON and write it in single quotes.                                                                                                                                                                                                                                                                                                                                             |
| `GKE_DB_KMS_KEYRING`, `GKE_DB_KMS_KEY`                                                      | `gcloud container clusters describe <CLUSTER_NAME> --location <REGION> --format='value(databaseEncryption.keyName)'`. Write the names after `keyRings/` and `cryptoKeys/`. Only for a cluster the install created or encrypted.                                                                                                                                                                                                                                                                                                                                             |
| `GVISOR_POOL_NAME`                                                                          | On a Standard cluster with gVisor, `gcloud container node-pools list --cluster <CLUSTER_NAME> --location <REGION> --filter='config.sandboxConfig.type=GVISOR' --format='value(name)'`.                                                                                                                                                                                                                                                                                                                                                                                      |
| `SCOPED_SA_POOL_MAX_ACCOUNTS`                                                               | No copy. If the cap is too low for the pool, the plan stops before any change.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Any other `TF_VAR_` line                                                                    | These lines have no installer key, so only the lost file had them. The composition README's [drift section](https://github.com/gke-labs/kube-agents/blob/main/terraform/examples/full-install/README.md#drift-audit-log-ingress) lists the drift ones. For `TF_VAR_drift_pubsub_source_exclude_projects`, compare the state output `drift_pubsub_source_projects` with `scope_projects`. For the writer identity overrides, read each sink's `writer_identity` in the state. If step 5 shows a change that no key in these tables explains, look for a lost `TF_VAR_` line. |

Leave out `CLUSTER_MODE`, `GITHUB_PEM_PATH`, `GEMINI_API_KEY_SECRET_NAME`, `MIGRATE_NODE_POOLS`,
`ENABLE_NETWORK_POLICY` and `ALLOW_UNENCRYPTED_SECRETS`. They do not change what the upgrade renders
for an existing install.

A missing key takes its default, and some defaults remove or add things on a full upgrade. The
installer README lists these keys in its
[`install.env` section](https://github.com/gke-labs/kube-agents/blob/main/scripts/installer/README.md#the-install-configuration-installenv),
in the paragraph that starts "That precedence has a sharp edge" and the paragraph after it. A
rebuilt file has more cases:

- Without the GitHub App keys, a full upgrade stops while the token minter runs, and prints the
  lines to set. See [When an upgrade is refused](#when-an-upgrade-is-refused).
- Without the GitLab keys, the `PlatformAgent` loses its GitLab repository.
- Without the GitHub Enterprise Server keys, the `PlatformAgent` names its repository on github.com, and the agent loses its repositories on the instance.
- Without `MODEL_PROVIDER=vertex_ai`, the upgrade deletes the LiteLLM service account and its
  Vertex AI grant.
- Without a drift or stockout `TF_VAR_` name that the install changed, the upgrade replaces that
  resource with one that has the default name.

Do not skip step 5.

## Run the upgrade

Substitute `<RELEASE_VERSION>` with the release tag you are moving to, from
[GitHub Releases](https://github.com/gke-labs/kube-agents/releases):

```bash
curl -fsSL https://raw.githubusercontent.com/gke-labs/kube-agents/<RELEASE_VERSION>/upgrade.sh | bash -s -- \
  --non-interactive \
  --gcp-project-id="my-gcp-project" \
  --gke-cluster-name="platform-agent-host" \
  --gcp-region="us-central1"
```

The release-pinned script upgrades to its own version, so you pass no tag. It reuses the install
checkout in `$HOME/kube-agents`, moving it to the release you asked for, and reads the `install.env`
in it. A checkout with uncommitted changes is left alone and the run stops rather than upgrading
from sources that do not match the release. Only a release copy is flagless: a copy built from
`main` carries no version and asks for one, so name a release tag in the URL rather than `main`.

The release bundle is the other supported source, and the one to use on a machine with no install
checkout:

```bash
curl -fsSL https://github.com/gke-labs/kube-agents/releases/download/<RELEASE_VERSION>/kube-agents-<RELEASE_VERSION>.tar.gz | tar -xz
cd kube-agents-<RELEASE_VERSION>
cp /path/to/your/install/install.env .
./upgrade.sh --non-interactive --gcp-project-id="my-gcp-project"
```

## Moving across more than one release

`upgrade.sh` does not check which release the install runs now. A full run applies the target
release's CRDs, chart and Terraform composition to the install as it is. An operator run applies
the CRDs and the chart, and a harness run applies only the chart.

- No step depends on the release that the install came from. The Secret backfills add only the
  keys that are missing. The checks that can refuse a run read the target chart and the live
  install.
- Every release serves one version of each CRD, `v1alpha1`, so no CRD conversion is necessary.
- The composition's only `moved` block, in the `gke-cluster` module, is in every release since
  0.1.0. The composition has no `removed` blocks.
- Some retired inputs still work for a time, with a warning, for example `GITHUB_REPO` and
  `TF_VAR_agent_service_account_id`. If a run prints such a warning, change `install.env` before
  the next upgrade. A later release can remove them.

The release pipeline moves the newest release to the candidate in the operator and harness modes.
Staging does a full upgrade between consecutive candidates. No CI job moves an install across
several releases, and no policy says whether such a move is supported.

Before a move across several releases:

1. Read the release notes of each release in between, and the breaking changes below.
2. Use `--upgrade-mode=full`, so that the CRDs, the chart and the Terraform composition all move.
3. Run `--plan` with the target release's `upgrade.sh`, and read the whole plan. `--plan` compares
   the target composition with the install's Terraform state. It does not know which releases you
   skipped.
4. Run the upgrade.

### Breaking changes in recent releases

The release notes list pull request titles only. A `!` before the colon marks a breaking change.
0.10.0 and 0.11.0 have no `!` changes. The table also lists changes with no `!` that turn on something new for an existing install.

| Release | Change                                                      | What an existing install meets                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| pending | #3675: the A2A bus requires TLS                             | `spec.mode: next` only. The upgrade restarts the bus once with TLS required, and every client the operator renders rolls onto `tls://` and the bus CA in the same pass. A session task running at that moment fails, as on any bus restart. Anything you run by hand against the bus, such as a `nats` CLI session, needs `tls://` and the CA from the `<name>-a2a-nats-ca-bundle` ConfigMap afterwards. If you roll back to an earlier release, delete the session pods (`kubectl delete pod -l app.kubernetes.io/component=a2a-session`): the earlier gateway would otherwise reuse them against a plaintext bus.                                                                                                                                                                                                                                                                                                              |
| 0.14.0  | #3362: the agent's data volume is 50Gi                      | The operator grows the agent's `<name>-data` claim and the shell sandbox's data claim to 50Gi, online, where the StorageClass has `allowVolumeExpansion: true` (GKE's `standard-rwo` does). Where the class cannot expand a claim, the claims keep their size and the CR carries the `DataVolumeNotExpanded` condition (reason `StorageClassCannotExpand`); see [`status`](/kube-agents/operator/platformagent-crd/#status). [`spec.storage.size`](/kube-agents/operator/platformagent-crd/#specstorage) raises the size above 50Gi. The upgrade adds 80 GiB of disk: a namespace `ResourceQuota` on `requests.storage` and the project's regional SSD quota both need room for it. A quota failure after the operator's patch leaves the claim `Bound` at its old `status.capacity`; the operator log says the resize has not landed, and the claim's events say why; see [Prerequisites](/kube-agents/install/prerequisites/). |
| 0.12.0  | #2891: the credential proxy requests 1Gi of memory          | The operator refuses a `credentialProxy.resources` that sets `limits.memory` under 1Gi without `requests.memory`. Set the request with the limit, or raise the limit. See [`spec.deployment`](/kube-agents/operator/platformagent-crd/#specdeployment).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 0.12.0  | #2862: the drift export covers the scope's other projects   | The drift export also creates sinks in other projects of the scope. See [Multi-project](/kube-agents/deploy/multi-project/) for which projects, the permission the apply needs, and how to leave a project out.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 0.9.0   | #2264: the drift detector is on by default                  | If `install.env` does not set `ENABLE_DRIFT_DETECTOR`, a full upgrade creates a Log Router sink, a Pub/Sub topic and a subscription, and starts the detector. The sink exports the GKE admin-activity audit records of every cluster in the project. The subscription keeps unacknowledged records for up to 31 days. To keep it off, write `ENABLE_DRIFT_DETECTOR=false`. In a project where another install has the drift resources, the upgrade stops until you write that line or set the `TF_VAR_drift_pubsub_*` names.                                                                                                                                                                                                                                                                                                                                                                                                     |
| 0.9.0   | #2350: the scoped service account pool is keyed per project | The upgrade turns off a pool armed under the old per-cluster field. See [the scoped service account pool](/kube-agents/reference/security-and-iam/#the-scoped-service-account-pool).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| 0.9.0   | #2706: the Slack UX is on by default                        | Slack users see new reactions, buttons and cards. See [Reactions](/kube-agents/concepts/chatops/#reactions) and [Buttons](/kube-agents/concepts/chatops/#buttons). The buttons use the Slack app's Interactivity setting. An app made from `hermes slack manifest` has it on. On an app made by hand, turn on Interactivity: Socket Mode needs no request URL. If a button does not work, a typed reply still works. To keep the old behaviour, see below. Under `spec.mode: next`, 0.9.0 also moves Slack to the A2A gateway, and `KAGE_SLACK_UX` does not turn off the gateway's reactions and buttons. See [Slack app setup](/kube-agents/install/slack-app/).                                                                                                                                                                                                                                                                |
| 0.8.0   | #2065: the `litellm` Service is renamed `inference-gateway` | Model calls can fail for a short time during the upgrade. See the chart README's [upgrade note](https://github.com/gke-labs/kube-agents/blob/main/charts/kube-agents/README.md#upgrade-notes-inference-gateway-service-rename).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| 0.8.0   | #2070: forges and repositories in `spec.integration`        | The operator refuses some repository spellings that worked before. See the upgrade notes under [`spec.integration`](/kube-agents/operator/platformagent-crd/#specintegration).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| 0.8.0   | #2010, #2042: the A2A bus reserves more consumers           | `spec.mode: next` only. If 0.7.0 or earlier created the TASKS stream, the provision Job can stop the upgrade. Do what its message says.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| 0.8.0   | #1756: the upgrade follows the release-pinned contract      | The upgrade refuses an install with only `k8s-operator/scripts/vars.sh`. See [When an upgrade is refused](#when-an-upgrade-is-refused).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |

To keep the Slack UX off across upgrades, add `KAGE_SLACK_UX=false` to the chart's
`platformAgent.deployment.env` list. `install.env` has no key for it. On an install made with
`install.sh`, put the list in a `TF_VAR_extra_helm_values` line in `install.env`. A full-mode
`install.sh` or `upgrade.sh` run applies the line:

```bash
TF_VAR_extra_helm_values='{platformAgent={deployment={env=[{name="KAGE_SLACK_UX",value="false"}]}}}'
```

`install.env` holds one such line, so put every extra value in it. If the line already has a
`platformAgent` key, put `deployment={env=[...]}` inside that key. A second `platformAgent` key
replaces the first one, with no error. If the line already has a `platformAgent.deployment.env`
list, add the entry to that list, because Helm replaces a list as a whole. See
[Operator settings](/kube-agents/install/quickstart-gke/#operator-settings) for how the line works.

## Upgrade modes

- `--upgrade-mode=harness` re-tags the Platform Agent image, the sandbox image it reaches over
  ssh, and every plugin image the release records — all are built from the same revision —
  through one `helm upgrade` that re-applies the values the release recorded over the chart's
  defaults. Before any of the new release is applied it names and stops on any recorded key the
  chart's values schema refuses as undeclared, since Helm would refuse the whole upgrade over it. On an upgrade
  the usual cause is a setting the new chart renamed or removed: take that upgrade through
  `--upgrade-mode=full`. On a rollback to a release that predates the key, pass
  `--drop-undeclared-values` to drop and name each one instead; a later release that declares the
  key renders it from that chart's default until a full-mode run there renders `install.env` onto
  the chart again.
- `--upgrade-mode=operator` applies the chart's CRDs with `kubectl` first — Helm never touches
  `crds/` on an upgrade — then re-tags the operator image the same way.
- `--upgrade-mode=full`, the default, applies the CRDs and then runs a full `terraform apply`
  through the install engine: every image tag moves, and every setting in `install.env` is
  re-rendered. It is the only mode that applies `PLATFORM_AGENT_MODE`, so it is the one that
  [switches `spec.mode`](#switching-specmode).

The operator moves before the harness because the operator owns the resources the harness runs as.
Every mode needs the `kube-agents` Helm release to exist in the target namespace; an install
without one predates the Terraform and Helm engine, and has to be re-installed to adopt it.

## Choosing what the run targets

A release copy of the script carries the version it upgrades to, which is what makes the one-liner
above flagless. That baked version is the run's target from the moment it starts, before any flag
is read, so two of the flags below behave differently depending on which copy you are holding. One
copy is the exception: `upgrade.sh` in a checkout of a release line (`release/<X.Y>`) that has moved
past its latest release still carries that release's version but is not that release, so run from
there, with the release's tag and full history fetched, it drops the baked default, says which line
and commit it is on, and asks for `--image-tag` the way a copy with no baked version does; a clone that
lacks the tag, or whose shallow history stops short of the release, is refused and told which fetch
to run.

- `--image-tag` names a revision to move to instead: a release tag or a full commit SHA. It
  overrides the baked version, and it exists for development and CI/CD testing — a candidate
  commit, or a release the script does not itself carry. It is not part of upgrading to a published
  release. Mutable refs such as `latest`, `main` and `HEAD` are rejected, so the scripts and the
  container images always name the same revision.
  A commit on `main` carries the chart's unstamped placeholder version, `0.1.0`, so
  `helm history` and the plan show the chart moving from your release to `kube-agents-0.1.0`.
  That is the placeholder, not a downgrade: only the release pipeline stamps the version.
- `--keep-image-tag` upgrades everything except the images, leaving them on the tag the install
  already serves. It refuses `--image-tag`, because the two ask for opposite things — and since a
  release copy already carries a version, it refuses this flag as well. Run it from a checkout,
  which is where the scheduled reconciles that need it run.
- `--plan` changes nothing and reports what an upgrade would do. From a release copy it plans at
  that release: the question it answers is whether moving to that version would change anything.
  From a copy with no baked version and no `--image-tag`, it plans at the tag the install's
  Terraform state records, so the report is composition drift rather than image lag. Either way it
  exits 0 when the install is in sync, 2 when there are changes, and 1 when the plan itself failed.

Given no tag and no flag, a copy of the script built from `main` has no version to default to, and
asks for one.

## Previewing

`--dry-run` and `--plan` are both previews and are deliberately not the same one. `--dry-run`
answers offline, from configuration alone, and never contacts the install. `--plan` answers from
the install's real Terraform state, so it needs credentials, and it is the only one of the two that
can report drift. The two are refused together.

Neither moves the install checkout in `$HOME/kube-agents` or edits `install.env`: a preview that
needs sources the checkout does not have reads them from a temporary copy instead, so the checkout
is still on the release the install runs when the preview is over. When a run is configured from
`$(pwd)/install.env` or `KUBE_AGENTS_INSTALL_ENV` outside `$HOME/kube-agents`, it fetches a
temporary copy as well rather than touching `$HOME/kube-agents`. When `--plan` reuses the
install's own checkout because it is already on the target release (or runs from inside it),
`--plan` refreshes the generated `terraform/examples/full-install/terraform.tfvars` and
`.terraform/` working directory there so Terraform can plan against the live backend; `--dry-run`
exits before Terraform runs and writes nothing.

Neither is refused by an edited checkout either. A preview of what an uncommitted change would
apply is the one report that answers "what have I edited here", so both previews warn and continue
where a real upgrade stops. When the configuration the run loaded records a different install than
the flags name, only `--dry-run` warns and continues — `--plan` refuses alongside a real upgrade
because planning writes `terraform.tfvars` and reconfigures `.terraform/` in the working sources
(see [Naming the install](#naming-the-install)).

What a preview will not do is report on a run that could not happen: with no `install.env` anywhere
it stops with the same refusal a real upgrade gives, rather than printing a plan you could not
execute.

## Naming the install

The lookup order above is what keeps the configuration and the target install in step, because
standing in an install's directory upgrades that install. The piped one-liner has nowhere to stand,
so on a machine with two installs it loads `$HOME/kube-agents/install.env` whichever cluster the
flags name — and a full upgrade re-renders the `PlatformAgent` resource from that file, so the
mismatch writes one install's chat space, allowed users, model provider and namespace into the
other.

So the run compares them. When `--gcp-project-id`, `--gke-cluster-name` or `--gcp-region` names
something the loaded `install.env` records differently, both a real upgrade and `--plan` refuse,
while `--dry-run` reports the mismatch and goes on. Point `KUBE_AGENTS_INSTALL_ENV` at the
`install.env` of the install you are upgrading, run from its checkout, or drop the flag that
disagrees.

## Checking the result

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}{"\n"}'
kubectl get pods -n kubeagents-system
helm history kube-agents -n kubeagents-system
```

Both images end in `:<RELEASE_VERSION>`, the `Ready` condition reads `True`, the gateway pod is
`Running`, and the newest Helm revision is `deployed`. `kubeagents-system` is the default
namespace; an install that set `NAMESPACE` in `install.env` uses that one. `platform-agent` is the
chart's default `platformAgent.name` value.

The run also writes a machine-readable report to `/tmp/kube-agents-upgrade-report.json`.

### Slack

On an install with Slack enabled, check that Slack still answers. Which process holds the Slack
connection depends on `spec.mode` (the third command under [Before you start](#before-you-start)):

- **Empty or `today`**, or `next` with Google Chat also enabled: the credential broker.

  ```bash
  kubectl logs deploy/platform-agent-credential-proxy -n kubeagents-system \
    | grep -E 'Slack relay enabled|Slack relay initialization failed|Slack bot token authentication failed'
  ```

  The pattern matches exactly three kinds of line. `Slack relay enabled workspaces=1` as the last line is
  the healthy reading: the number counts the bot tokens Slack accepted, one per workspace.
  `Slack bot token authentication failed` means Slack refused one of the bot tokens; it gives the
  error type and Slack's error, not which token. `Slack relay initialization failed; retrying`
  means no bot token was accepted or the Socket Mode connection failed, and the broker tries again
  every 30 seconds. No output has no reading of its own (on a broker that has run for a while the
  startup lines may have rotated out of the log): go by the DM test below.

- **`next`**: the A2A gateway.

  ```bash
  kubectl logs deploy/platform-agent-a2a-gateway -c gateway -n kubeagents-system \
    | grep -E 'slack connected|chat backend stopped'
  ```

  The last line contains `"msg":"slack connected"` and names the bot user and the workspace
  (`team`). The gateway logs it once it has joined the bus and Slack has accepted the bot token,
  and then opens the Socket Mode connection. A `chat backend stopped` line after it means that
  connection failed. One with no `slack connected` before it means the gateway's first call to
  Slack failed: its `err` reads `invalid_auth` when Slack refused the bot token, and anything else
  (a DNS error or a timeout, say) is the gateway not reaching Slack. The gateway retries either
  way. No output means the gateway has not tried Slack yet: it first joins the bus, retrying for up
  to 45 seconds, and exits if that fails. Wait a minute and run the command again. If the pod's
  restart count is climbing, `kubectl logs ... --previous` shows why the last run exited.

Either way, DM the app a question such as `what clusters can you see?` and wait for the answer (an
AI agent asks a person to do this): the log line does not prove that messages reach the agent. If
nothing comes back, [Slack app setup → Verify](/kube-agents/install/slack-app/#verify) has the next
checks.

## Switching `spec.mode`

`PLATFORM_AGENT_MODE` in `install.env` sets the install's `spec.mode`: `next`, which a first install
gets, or `today`, the previous stack, kept as a rollback. A file with no such line means `today` in
`upgrade.sh`, so upgrading an install made before `next` became the default leaves it on `today`;
`install.sh` keeps the mode the cluster runs and records it there, and a full `upgrade.sh` over such
a file is refused when the install may run `next`. Of `upgrade.sh`'s modes only
full applies the key; a re-run of `install.sh` applies it too. Only the `install.sh` and
`upgrade.sh` of releases after 0.9.0 read it, so upgrade to such a release first.
[`scripts/installer/README.md`](https://github.com/gke-labs/kube-agents/blob/main/scripts/installer/README.md)
has the rules for the key, and
[`docs/designs/spec-mode-switch.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/spec-mode-switch.md)
what a switch changes inside the install. This section covers what a switch does to chat: Google Chat first, then
Slack, in order.

### Google Chat

With Google Chat enabled, Google Chat takes the A2A gateway under `next`. The thing that changes for
it is the home channel, where alerts and scheduled reports land. Under `next` the gateway posts them
only to `GOOGLE_CHAT_HOME_CHANNEL` in `install.env` (the `PlatformAgent`'s
`spec.integration.googleChat.homeChannel`), and it must be a space name, `spaces/<id>`. A home
channel set from chat with `/sethome`, or by the install's first chat message, is kept in the
Planning Agent's profile and does not count. With the key unset or not a space name, the agent posts
nothing to Google Chat unprompted. So before switching to `next`, set `GOOGLE_CHAT_HOME_CHANNEL` in
the same `install.env` as `PLATFORM_AGENT_MODE`, so the one full run applies both
([ChatOps → Home channel](/kube-agents/concepts/chatops/#home-channel)). After the switch, check
that a scheduled report or an alert reaches that space.

With Slack enabled too, Slack stays on the credential broker under `next`, as under `today`. Then
none of the Slack steps below apply: switch, check Slack as in [Checking the result](#slack), and
leave the Slack app as it is. The rest of this section is for an install whose Slack takes the
gateway.

### Before the switch

1. Check that Slack answers now, as in [Checking the result](#slack).
2. Check the Slack settings `next` depends on. A full upgrade refuses a switch to `next` that
   would hand the gateway a setting it cannot use, and names it
   ([When an upgrade is refused](#when-an-upgrade-is-refused)); `--plan` warns of the same
   ones. Checking first saves a refused run:
   - The bot token is one `xoxb-` token, not a comma-separated list: the A2A gateway takes a
     single workspace's token
     ([Get the tokens](/kube-agents/install/slack-app/#get-the-tokens)). This counts the
     `xoxb-` tokens in the installer's Secret without printing them, and should print `1`:

     ```bash
     kubectl get secret platform-agent-secrets -n kubeagents-system \
       -o jsonpath='{.data.SLACK_BOT_TOKEN}' | base64 -d | tr ',' '\n' | grep -c '^xoxb-'
     ```

     `0` means nothing was read: check that the `PlatformAgent`'s
     `spec.integration.slack.botTokenSecretRef`, if set, names this Secret and key.

   - `SLACK_ALLOWED_USERS` in `install.env` holds member IDs such as `U0123ABCD`, not emails
     ([Allowed users](/kube-agents/install/slack-app/#allowed-users)).
   - `SLACK_HOME_CHANNEL` in `install.env` is the ID of the channel for alerts and scheduled
     reports: `C…`, or `G…` for some private channels. In Slack, the ID is at the bottom of the
     channel's details. Under `next` the gateway posts the agent's proactive messages to this
     channel only. A home channel set from Slack with `/sethome` is kept in the Planning Agent's
     profile, not in `install.env`, so it does not carry over. A DM ID (`D…`) or a channel name
     turns off the gateway's notify route, and with it the reports of board cards
     ([Slack app setup → Home channel](/kube-agents/install/slack-app/#home-channel)). With no home
     channel those posts are dropped while conversations keep working: the run warns, and the
     `PlatformAgent` carries the `HomeChannelUnset` condition.

3. Check the board for cards in flight:

   ```bash
   kubectl exec deploy/platform-agent-gateway -c platform-agent -n kubeagents-system -- \
     hermes kanban ls --status running
   kubectl exec deploy/platform-agent-gateway -c platform-agent -n kubeagents-system -- \
     hermes kanban ls --status blocked
   ```

   Wait until neither command lists a card: answer each blocked card in its Slack thread, and let
   running ones finish ([What carries over](#what-carries-over) says why). If scheduled jobs keep
   filing cards, switch between their runs.

4. Switch when nobody is using the agent, and tell its users that Slack will not answer for a
   while.

### The switch

Edit the `install.env` the upgrade reads ([Before you start](#before-you-start) says where it
looks). Set the key, adding the line if the file has none:

```bash
PLATFORM_AGENT_MODE=next
```

[Run the upgrade](#run-the-upgrade) again at the release the install runs, in the default full
mode: pass no `--upgrade-mode`. Before it applies anything, the run warns:

```text
This apply switches the install from spec.mode today to next (PLATFORM_AGENT_MODE in install.env): a mode switch, not a settings change.
```

It does not stop to ask. To stay on `today`, press Ctrl-C at that warning and set the key back.

`--upgrade-mode=harness` and `--upgrade-mode=operator` leave the mode as it is and say that a full
upgrade would switch it. If the run says instead that the `PlatformAgent` carries a `spec.mode` set
outside the installer, the resource was patched by hand (`kubectl patch`, as 0.9.0 needed). That
mode survives upgrades, because Helm sees no change in a value the release already renders, so the
run leaves the resource as it is. To bring the two together, patch `spec.mode` on the resource to
the mode you want, or apply once with `PLATFORM_AGENT_MODE` set to the mode the resource carries and
then again with the one you want.

### During the switch

The operator moves Slack in three steps:

1. It restarts the credential broker without the Slack tokens, stopping the old broker pod before
   it starts the new one, and restarts the agent's pod without its Slack listener. From here, no
   process holds the app's Socket Mode connection.
   - Messages the broker had taken from Slack but not yet handed to the agent are lost: the
     broker acknowledges each event to Slack when it arrives and holds it in memory.
   - A question the agent was answering stops with its pod and gets no answer.
   - A board card that was running is put back in the queue once the board reclaims its stale
     claim, which can take up to 30 minutes (`kanban.dispatch_stale_timeout_seconds`), and runs
     again from the start.
2. It creates the NATS bus and the auth callout. It creates the A2A gateway once a callout
   replica is ready, which needs the bus to be running. The provisioning Job, started at the same
   point, creates the bus's streams.
3. The gateway joins the bus, retrying for up to 45 seconds while the streams do not exist and
   restarting after that. It then checks the bot token with Slack, logs `slack connected`, and
   opens the Socket Mode connection.

The broker's connection stops when the switch starts, and the gateway exists only once the bus is
up, so in practice the two never take the same message. How long Slack goes unanswered depends on
how fast the cluster schedules and pulls the new pods. Slack has no connection to deliver a message
sent in that gap to, and nothing in kube-agents fetches it later: treat it as lost, and ask its
sender to send it again. Wait for the gateway, for up to 15 minutes:

```bash
for i in $(seq 90); do
  kubectl logs deploy/platform-agent-a2a-gateway -c gateway -n kubeagents-system 2>/dev/null \
    | grep -q 'slack connected' && { echo connected; break; }
  sleep 10
done
```

It prints `connected` once the gateway has logged `slack connected`. The operator then restarts
the agent's pod once more, to add the bridge that runs the gateway's tasks, and that can take a few
minutes if the cluster has to add a node for it. Wait for the `PlatformAgent` to report `Ready`,
then check Slack as in [Checking the result](#slack):

```bash
kubectl wait platformagent/platform-agent -n kubeagents-system --for=condition=Ready --timeout=15m
```

If the loop prints nothing, the `Ready` condition's message names what the
operator is waiting on, and
[Troubleshooting](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#6-tracing-where-a-chat-message-stops)
has the checks for the bus and the gateway. Slack stays unanswered until the gateway connects or
you [switch back](#switching-back-to-today).

```bash
kubectl get platformagent platform-agent -n kubeagents-system \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].message}{"\n"}'
```

### What carries over

- **The same app, tokens and allowlist.** The gateway reads the tokens from the same Secret.
- **The board and the agent's data volume.** The new pod mounts the same volume.
- **Not the conversations.** Under `next` the agent starts every Slack conversation with no
  history from `today`. In a channel, a reply without a mention in a thread started before the
  switch gets no answer: mention the app.
- **Not the cards in flight, outside the home channel.** A card filed before the switch from a
  DM or from any channel other than the home channel posts nothing after it: the gateway refuses
  the post and, with a home channel set, logs `thread "<channel>/<ts>" is not a thread of the home channel`. A card filed
  in a home-channel thread still posts its progress and report there. Either way its buttons stop
  working: the gateway answers only the buttons it drew itself.
- **The home channel only from `install.env`**, as in step 2 above.

Slash commands, buttons, cards and the other Slack features that do not carry over are listed in
[ChatOps → What carries over from `today`](/kube-agents/concepts/chatops/#what-carries-over-from-today).

### After the switch: the Slack app

Once a DM gets an answer as in [Checking the result](#slack), edit the app as
[Slack app setup → Moving an existing app to `next`](/kube-agents/install/slack-app/#moving-an-existing-app-to-next)
describes: delete the slash commands from its manifest and leave interactivity on, since the gateway
answers the buttons it draws. Edit the app rather than pasting the `next` manifest over it: a
manifest replaces the app's whole configuration, including its name. Do it after the switch rather
than before, because the `today` listener answers slash commands until then. Until you do, a slash
command gets Slack's timeout error.

### Switching back to `today`

1. Set `PLATFORM_AGENT_MODE=today` in `install.env`, or delete the line, and run the upgrade again
   in full mode. The run warns
   `This apply switches the install from spec.mode next to today (PLATFORM_AGENT_MODE in install.env): a mode switch, not a settings change.`
2. The operator restarts the credential broker with the Slack tokens and deletes the A2A stack,
   the gateway with it. For the seconds the gateway pod takes to stop, both can hold a
   connection, and a message the stopping gateway takes gets no answer. The bus volume and its
   credentials Secret stay; [Uninstall](/kube-agents/install/uninstall/#what-specmode-next-leaves-behind)
   says how to remove them. Wait for the broker, for up to 5 minutes:

   ```bash
   for i in $(seq 30); do
     kubectl logs deploy/platform-agent-credential-proxy -n kubeagents-system 2>/dev/null \
       | grep -q 'Slack relay enabled' && { echo connected; break; }
     sleep 10
   done
   ```

   It prints `connected` once the broker has connected to Slack. If it prints nothing, read the
   broker's log as in [Checking the result](#slack).

3. Put the slash commands back. Print the `today` manifest:

   ```bash
   kubectl exec deploy/platform-agent-gateway -c platform-agent -n kubeagents-system -- \
     hermes slack manifest
   ```

   Before you paste it into the app's **App Manifest** page, change its `name`, `description` and
   `display_name` to the app's current values: the manifest replaces the app's whole
   configuration. [Slack app setup](/kube-agents/install/slack-app/#create-the-app) covers its
   options; read its warning about agent view first. Reinstall the app if Slack asks.

4. Check Slack as in [Checking the result](#slack).

Conversations and cards started under `next` do not carry back, for the same reasons as above.

To roll back to an older release, switch to `today` first:
[Rolling back a release](/kube-agents/deploy/rollback/#before-you-start) says why.

## The gateway's own ServiceAccount

From the release in which the gateway Pod runs as its own ServiceAccount, an upgrade moves the
gateway from the agent ServiceAccount (`spec.security.serviceAccountName`, `kubeagents-platform-agent`
by default) to `<name>-gateway-agent`, which the operator creates with no annotation. The agent
ServiceAccount, its Workload Identity annotation and its IAM binding stay where they are, with the
credential broker, so neither Terraform nor IAM changes. The new ServiceAccount is bound to the same
read-only `kubeagents:minimal` ClusterRole and the `kubeagents:local` and `kubeagents:leader` Roles
the gateway already used.

Two things an install may have added by hand do not follow the gateway:

- **RBAC bound to your own ServiceAccount.** If you set `serviceAccountName` and bound extra roles to
  it expecting the gateway to use them, the gateway no longer runs as it. Bind them to
  `<name>-gateway-agent` if the gateway needs them; the broker keeps whatever is bound to yours.
- **IAM granted to the gateway's Kubernetes identity directly.** A `principal://…/subject/ns/<ns>/sa/<sa>`
  or namespace-wide `principalSet://` grant reaches whichever Pod runs as that ServiceAccount, and the
  gateway's metadata path is closed, so such a grant no longer reaches the gateway. Anything in the
  gateway that needs Google goes through the broker.

To check: `kubectl get deployment platform-agent-gateway -n kubeagents-system -o
jsonpath='{.spec.template.spec.serviceAccountName}'` prints `platform-agent-gateway-agent` for the
chart's default name.

## When an upgrade is refused

Every one of these stops the run before any of the new release is applied. The first four are
settled before the run touches the cluster at all. The last six need the cluster: they are settled
after `kubectl` has been pointed at it, and except for the breaking-change check, after a real run's
pre-flight Secret backfills — a plan skips those — but still before any CRD, chart or Terraform
change of the new release.

- **The sources do not match the release.** The checkout's `HEAD` is not the release's commit, the
  tree has uncommitted changes, or an unpacked tree names a release other than the one asked for —
  either in its `.release-bundle` marker or in the version stamped into its root scripts. A release
  script carries its version with it, so standing in an older unpacked tree and running a newer one
  is refused rather than silently applying the older tree. The stamp is read from `install.sh` and
  `uninstall.sh` before `upgrade.sh`, because saving a newer `upgrade.sh` into an older tree
  overwrites the one file that would otherwise be asked. Start again from a clean checkout or from
  the bundle of the release you want.
- **`KUBE_AGENTS_INSTALL_ENV` names a file that is not there.** An explicit pointer is taken
  literally, not fallen back from, so a stale or mistyped value is reported with the path you gave
  rather than searched past. Fix the path or unset the variable.
- **No install configuration.** Neither `KUBE_AGENTS_INSTALL_ENV` nor an `install.env` was found in
  any of the configuration locations described at the top of this page. An install predating 0.4.0,
  which kept its settings in the retired `k8s-operator/scripts/vars.sh` and never gained an
  `install.env`, is refused here: copy those settings into an `install.env` first.
  If the machine that held the file is gone, see
  [Recovering `install.env`](#recovering-installenv).
- **`GITHUB_APP_ID` is not a number.** The token minter needs the GitHub App's numeric ID, the "App
  ID" on the App's General settings page, not its name or slug. `--dry-run` refuses it too.
- **The move leaves behind a commit marked as a breaking change.** When the target does not
  contain the installed commit — a rollback to an older release, or a commit build moved to a
  release that lacks some of its commits — the run reads the commits the target lacks. It stops
  before the Secret backfills if any has a `type!:` subject or a `BREAKING CHANGE:` footer, and
  names each one. Read them, then re-run with `--allow-breaking-downgrade`, in any mode. `--plan`
  warns instead. A move between two release tags to the newer one is not checked, and neither is a
  run whose tag is read off the install. See [Rolling back a release](/kube-agents/deploy/rollback/).
- **No Helm release.** The target namespace has no `kube-agents` release to upgrade.
- **The memory store cannot be checked and nothing named one.** When the configuration records no
  `MEMORY`, the upgrade asks the cluster whether it runs the Hindsight memory store, so that a store
  that is there is kept rather than planned away. If the cluster cannot be asked — `kubectl` is
  pointed elsewhere, the credentials have expired, the API server times out — the run stops instead
  of guessing. Record `MEMORY=hindsight|file|off` in `install.env`, or restore access to the cluster
  and re-run.
- **The chart does not declare a value the release recorded.** The operator and harness modes
  re-apply the values the release recorded, and name each one the new chart's values schema refuses
  as undeclared. On an upgrade that is usually a setting the new chart renamed or removed: run
  `--upgrade-mode=full`, which renders `install.env` onto the chart instead. On a rollback to a
  release that predates the value, re-run with `--drop-undeclared-values`. A full upgrade refuses
  that flag, since it has no recorded values to drop.
- **A Slack setting the A2A gateway cannot use.** A full upgrade applies `PLATFORM_AGENT_MODE` from
  `install.env`. With `next`, Slack on and Google Chat off, the A2A gateway takes Slack, and the run
  refuses three settings it cannot use, naming each one:
  - a `SLACK_ALLOWED_USERS` entry with an `@`: the list matches Slack member IDs exactly, so an
    email matches nobody. Replace it with the person's member ID (in Slack, their profile, then
    **⋮** (More), then **Copy member ID**).
  - a `SLACK_HOME_CHANNEL` that is not a channel ID (`C...` or `G...`): the gateway's notify route
    does not arm, so proactive posts and board-card reports stop. The ID is at the bottom of the
    channel details' About tab.
  - a `SLACK_BOT_TOKEN` that holds several tokens, or one with a comma or space left in it: the
    gateway takes one workspace's token, whole. The run gives the count, never the token. Keep a
    multi-workspace install on `today`.

  `--plan` warns of the same three without refusing.

- **The apply would remove GitOps.** The PlatformAgent opens pull requests through the GitHub token
  minter, but `install.env` does not carry that repository in `GITOPS_ORG`, `GITOPS_REPO` and
  `GITHUB_APP_ID`, so the run would render GitOps off and destroy the minter's Cloud KMS key, key
  ring and service account. This is what an install whose GitOps came from `install.sh`'s flags looks like when it
  was set up by a release that did not record them in `install.env`. The run prints the lines to
  set, with the live values filled in where it can read them; set them and re-run the same command. To remove GitOps on purpose, re-run with `KUBE_AGENTS_REMOVE_GITOPS=true` in the
  environment. `--plan` warns instead. A minter that this install did not make (one made by hand,
  by `make deploy-github`, or by another Helm release) is not removed by the apply, so the run does
  not ask for `GITHUB_APP_ID`. It refuses when `GITOPS_ORG`, `GITOPS_REPO` and `GITOPS_HOST` do not
  name the PlatformAgent's GitOps repository, or when it cannot read the PlatformAgent or the
  repository it names. The run decides that the minter is not this install's only when the
  `kube-agents` release record has `githubMinter.enabled` false, the minter's Deployment carries no
  Helm annotations for that release, and the install's Terraform state holds no minter module (its
  service account and Cloud KMS key); with no release (a first install), or a state it cannot
  read, it applies the check above.

- **The chart's minter would land on another minter.** This run renders the chart's own token
  minter, but a minter that this install did not make runs under the same names, and Helm does not
  take over objects it does not own. Remove all of the other minter's objects first: run
  `make -C k8s-operator undeploy-github` with the same `GITHUB_MINTER_KSA_NAME` for the kustomize
  path, or turn the minter off in the Helm release that made it, and delete a
  `github-app-credentials` Secret that you made by hand. Or keep the other minter: set
  `GITHUB_APP_ID` empty in `install.env` (and, with `install.sh`, give no `--github-app-id`).
  `--plan` warns instead.

## What stops during an upgrade

This section describes `spec.mode: today`, which an install made before `next` became the first-install default keeps.

At the default of one replica, the agent gateway and the credential broker each run one pod with
the `Recreate` strategy. Kubernetes stops the old pod before it starts the new one, so chat stops
until the new pods are ready. The operator and LiteLLM keep serving. The operator starts its new
pod first, and LiteLLM runs two replicas and keeps one serving.

| Mode       | What restarts                                                                                                                                                                                                         |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `operator` | The operator. The agent gateway also restarts if the run added keys to `platform-agent-secrets`. The agent gateway, the credential broker and the shell sandbox restart if the new operator renders them differently. |
| `harness`  | The agent gateway, the credential broker, whose image tag follows the agent's, and the shell sandbox.                                                                                                                 |
| `full`     | The workloads of both other modes, and each object that a changed setting touches.                                                                                                                                    |

In each mode, a chart workload such as LiteLLM also restarts if the target release's chart changed
it. In CI runs, a harness upgrade made the agent unavailable for 41 seconds to about 3 minutes. A
full upgrade on staging took about 8 minutes. These are examples, not limits. In the harness and full modes, and in an
operator run that added Secret keys, the script waits up to 25 minutes for the agent gateway. That
allows for a cold image pull. An operator run that re-renders the gateway does not wait for it, so
the script can end before chat is back.

During the restart:

- **Slack.** The broker confirms each Slack event to Slack when it arrives, and keeps it in memory
  until the agent takes it. If the broker restarts, the events it holds are lost. If only the agent
  restarts, the broker gives its events to the new agent. The sender must send an event again in
  two cases: the old agent took it but did not finish it, or it arrived up to 20 seconds after
  the old agent stopped.
- **Google Chat.** A message stays in the Pub/Sub subscription until the agent confirms it. If the
  agent took a message but did not confirm it, Pub/Sub delivers it again after 60 seconds.
- **Board cards.** The board is on the agent's volume, so the cards stay. A card that was running
  usually goes back to `ready` soon after the new pod starts, and runs again from the start. This
  does not count against the card's retries. If the old worker's process ID is in use in the new
  pod, the card waits until its claim times out.

## Where to go next

- [Rolling back a release](/kube-agents/deploy/rollback/) — the reverse move, and what each mode
  leaves behind when it goes backwards.
- [Release versioning and promotion](/kube-agents/deploy/release-versioning/) — what a release tag
  guarantees about the artifacts it names.
- [Uninstall](/kube-agents/install/uninstall/) — removing the install instead of moving it.
