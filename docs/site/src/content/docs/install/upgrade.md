---
title: Upgrade
description: Moving an existing install to a newer release with upgrade.sh — its modes, how the target version is resolved, and what a run refuses before any of the new release is applied.
---

`upgrade.sh` is the Day-2 engine for an install `install.sh` created. It re-applies the same
Terraform composition and the same Helm chart at a newer revision, re-rendering the install from
the configuration it already has. A release copy of the script carries the version it upgrades to,
so the ordinary upgrade names no version at all.

An install whose GitHub forge is a GitHub Enterprise Server does not use `upgrade.sh`: it
regenerates `terraform.tfvars` from `install.env`, which has no keys for the instance, and it can
run from a checkout or a temporary copy of the engine that does not hold `ghes.auto.tfvars`. Move
the checkout to the new release (change a pinned `image_tag` in `terraform.tfvars` to it), apply
`charts/kube-agents/crds/`, and run `KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh apply` in
`terraform/examples/full-install`, with the forge kept in `ghes.auto.tfvars`: see
[GitHub Enterprise Server in INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#upgrade-switch-and-teardown).

## Before you start

A GitLab install needs nothing extra for an upgrade: no token and no flag. See
[Upgrade and uninstall](/kube-agents/install/gitlab/#upgrade-and-uninstall).

Record what the install runs now, so you can tell afterwards what moved:

```bash
kubectl get deployment platform-agent-gateway -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="platform-agent")].image}{"\n"}'
kubectl get deployment kube-agents-controller-manager -n kubeagents-system \
  -o jsonpath='{.spec.template.spec.containers[*].image}{"\n"}'
helm history kube-agents -n kubeagents-system
```

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
   that differs and plan again. The plan masks the Helm values, so compare `values.json` with the
   file.

Then upgrade to the new release with the same file.

### Keys in the values record

Each path is a `jq` path on `values.json`.

| Key                                              | Path                                                                                  | Notes                                                                                                                                                                                                                                                                         |
| ------------------------------------------------ | ------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PROJECT_ID`                                     | `.platformAgent.harness.projectId`                                                    |                                                                                                                                                                                                                                                                               |
| `CLUSTER_NAME`                                   | `.platformAgent.harness.clusterName`                                                  |                                                                                                                                                                                                                                                                               |
| `REGION`                                         | `.platformAgent.harness.location`                                                     | Copy it exactly. It can be a zone.                                                                                                                                                                                                                                            |
| `NAMESPACE`                                      | the release namespace                                                                 |                                                                                                                                                                                                                                                                               |
| `PLATFORM_AGENT_MODE`                            | `.platformAgent.mode`                                                                 | Empty means `today`.                                                                                                                                                                                                                                                          |
| `MODEL_PROVIDER`                                 | `.litellm.modelProvider`                                                              |                                                                                                                                                                                                                                                                               |
| `MODEL_DEFAULT_NAME`                             | `.litellm.modelDefaultName`                                                           |                                                                                                                                                                                                                                                                               |
| `MODEL_MAX_TOKENS`                               | `.litellm.maxTokens`                                                                  |                                                                                                                                                                                                                                                                               |
| `VERTEX_PROJECT_ID`, `VERTEX_LOCATION`           | `.litellm.vertex.projectId`, `.litellm.vertex.location`                               | `vertex_ai` only.                                                                                                                                                                                                                                                             |
| `LITELLM_REDACTION_ENABLED`                      | `.litellm.redaction.enabled`                                                          | If it is `true`, also copy `.litellm.redaction.ip.action` to `LITELLM_REDACTION_IP_ACTION`, `.litellm.redaction.ip.allowCidrs` to `LITELLM_REDACTION_IP_ALLOW_CIDRS` and `.litellm.redaction.rules` to `LITELLM_REDACTION_RULES` (as JSON in single quotes).                  |
| `GOOGLE_CHAT_ENABLED`                            | `.platformAgent.integration.googleChat.enabled`                                       | If it is `true`, also copy these fields of `.platformAgent.integration.googleChat`: `topicName` to `CHAT_TOPIC_NAME`, `subscriptionName` to `CHAT_SUB_NAME`, `mode` to `GOOGLE_CHAT_MODE`, `allowedUsers` to `ALLOWED_USERS` and `homeChannel` to `GOOGLE_CHAT_HOME_CHANNEL`. |
| `SLACK_ENABLED`                                  | `.platformAgent.integration.slack.enabled`                                            | If it is `true`, also copy these fields of `.platformAgent.integration.slack`: `allowedUsers` to `SLACK_ALLOWED_USERS`, `homeChannel` to `SLACK_HOME_CHANNEL` and `homeChannelName` to `SLACK_HOME_CHANNEL_NAME`.                                                             |
| `GITOPS_ORG`                                     | `.platformAgent.integration.github.org`                                               |                                                                                                                                                                                                                                                                               |
| `GITOPS_REPO`                                    | `.platformAgent.integration.github.gitRepo`                                           | The value is `<org>/<repo>`. Write only the part after the `/`.                                                                                                                                                                                                               |
| `GITHUB_APP_ID`                                  | `.githubMinter.appId`                                                                 |                                                                                                                                                                                                                                                                               |
| `KMS_KEYRING`, `KMS_KEY`                         | `.githubMinter.kms.keyring`, `.githubMinter.kms.key`                                  |                                                                                                                                                                                                                                                                               |
| GitLab keys                                      | `.platformAgent.integration.forges[0]`, `.platformAgent.integration.repositories[0]`  | If the forge's `provider` is `gitlab`, set `GITOPS_FORGE=gitlab`. Copy the forge's `host` to `GITOPS_HOST`, `credentialsRef.name` to `GITLAB_TOKEN_SECRET` and `caBundleRef.name` to `GITOPS_CA_SECRET`, and the repository's `repository` to `GITOPS_REPO`.                  |
| `ENABLE_GVISOR`                                  | `.platformAgent.deployment.availability.runtimeClassName`                             | `gvisor` means `true`. Always write the key: the upgrade's default is `false`, not the installer's `true`.                                                                                                                                                                    |
| `MEMORY`                                         | `.platformAgent.harness.memory.provider`                                              | `kube_agents_memory` is `hindsight`, `multiuser_memory` is `file` and `none` is `off`.                                                                                                                                                                                        |
| `USER_PROFILE_ENABLED`                           | `.platformAgent.harness.memory.userProfileEnabled`                                    |                                                                                                                                                                                                                                                                               |
| `HERMES_DASHBOARD_ENABLED`                       | `.platformAgent.harness.hermes.dashboardEnabled`                                      | The upgrade does not read the old `ENABLE_WEBUI` name.                                                                                                                                                                                                                        |
| `ENABLE_DRIFT_DETECTOR`                          | `.platformAgent.harness.driftDetector`                                                | If `enabled` is `true`, write `true`. If the path is absent (`null`), write `false`. If it has a `subscription` but no `enabled`, write `false` and `TF_VAR_enable_drift_pubsub=true`.                                                                                        |
| `ENABLE_PUBSUB_PLATFORM`                         | `.plugins.pubsubPlatform.enabled`                                                     | When `.plugins.stockoutInvestigator.enabled` is `true`, this value is always `true` and does not show the key. Write the key only if you know its old value.                                                                                                                  |
| `ENABLE_STOCKOUT_INVESTIGATOR`                   | `.plugins.stockoutInvestigator.enabled`                                               | If it is `true`, also copy these fields of `.plugins.stockoutInvestigator.pubsub`: `topic` to `TF_VAR_stockout_pubsub_topic`, `subscription` to `TF_VAR_stockout_pubsub_subscription` and `sink` to `TF_VAR_stockout_pubsub_sink`.                                            |
| `SCOPE_*`                                        | `.platformAgent.scope`                                                                | Copy `projects`, `folders`, `organizations`, `sharedVpcHosts`, `metricsScopes`, `maxProjects`, `exclude.projects` and `exclude.clusters` (each as `project/location/cluster`).                                                                                                |
| `SCOPED_SA_POOL_ENABLED`                         | `.platformAgent.security.scopedServiceAccountPool.enabled`                            |                                                                                                                                                                                                                                                                               |
| `ACCEPT_NO_NETWORK_POLICY`                       | `.platformAgent.annotations["kubeagents.x-k8s.io/network-policy-enforcement"]`        | `absent-accepted` means `true`.                                                                                                                                                                                                                                               |
| `REGISTRY_PREFIX`, `THIRD_PARTY_REGISTRY_PREFIX` | `.global.imageRegistry`, `.global.thirdPartyImageRegistry`                            |                                                                                                                                                                                                                                                                               |
| `PLATFORM_AGENT_GSA_NAME`                        | `.platformAgent.security.serviceAccountAnnotations["iam.gke.io/gcp-service-account"]` | Write the part before the `@`.                                                                                                                                                                                                                                                |
| `GITHUB_MINTER_GSA_NAME`                         | `.githubMinter.gsaName`                                                               |                                                                                                                                                                                                                                                                               |
| `LITELLM_GSA_NAME`                               | `.litellm.vertex.serviceAccountAnnotations["iam.gke.io/gcp-service-account"]`         | `vertex_ai` only. Write the part before the `@`.                                                                                                                                                                                                                              |
| `TF_VAR_agent_ksa_name`                          | `.platformAgent.security.serviceAccountName`                                          |                                                                                                                                                                                                                                                                               |
| `TF_VAR_image_pull_secrets`                      | `.global.imagePullSecrets`                                                            | Write it as a JSON list in single quotes.                                                                                                                                                                                                                                     |

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
  re-rendered.

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

## When an upgrade is refused

Every one of these stops the run before any of the new release is applied. The first four are
settled before the run touches the cluster at all. The last five need the cluster: they are settled
after `kubectl` has been pointed at it, and after a real run's pre-flight Secret backfills — a plan
skips those — but still before any CRD, chart or Terraform change of the new release.

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
  environment. `--plan` warns instead.

## Where to go next

- [Rolling back a release](/kube-agents/deploy/rollback/) — the reverse move, and what each mode
  leaves behind when it goes backwards.
- [Release versioning and promotion](/kube-agents/deploy/release-versioning/) — what a release tag
  guarantees about the artifacts it names.
- [Uninstall](/kube-agents/install/uninstall/) — removing the install instead of moving it.
