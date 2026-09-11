# Installer Helper Scripts

The install engine is Terraform + Helm: `terraform/examples/full-install` driven through
its `lifecycle.sh`, with the repository-root `install.sh` / `uninstall.sh` / `upgrade.sh`
as the front doors. This directory holds the helpers those front doors (and the dev
tooling) share.

These lived under `k8s-operator/scripts/` until they moved here. That was the address of
the fourteen numbered `provision_*.sh` scripts #748 deleted when Terraform + Helm became
the only engine, and the helpers stayed behind at it — serving three repository-root
scripts from inside the Go operator's directory, which is not where anyone looks for
them. `vars.sh` was the piece of that residue #1081 noticed first.

## Shared defaults live in `installer_common.sh`

`installer_common.sh` is where every installer front-end picks up the values it must
agree on; it reads them from [`install.defaults.env`](../../install.defaults.env) and
declares none itself. `install.sh`, `uninstall.sh`, and `upgrade.sh` source it rather than keeping
their own copies:

| Symbol                                                                    | What it fixes                                                                          |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `DEFAULT_CLUSTER_NAME`                                                    | GKE cluster name (`platform-agent-host`)                                               |
| `DEFAULT_REGION`                                                          | GCP region (`us-central1`)                                                             |
| `DEFAULT_CLUSTER_MODE`                                                    | Shape a fresh install creates (`autopilot`); a live cluster's probed shape always wins |
| `DEFAULT_VERTEX_LOCATION`                                                 | Vertex AI serving location (`global`)                                                  |
| `DEFAULT_VERTEX_MANAGE_SERVING_PROJECT`                                   | Enable the API and grant the gateway's role in the serving project (`true`)            |
| `DEFAULT_MODEL_PROVIDER`                                                  | Model provider (`gemini`)                                                              |
| `DEFAULT_MODEL_GEMINI` / `_OPENAI` / `_ANTHROPIC`                         | The model each provider serves by default; the chart's `litellm.yaml` mirrors them     |
| `DEFAULT_GEMINI_API_KEY_SECRET_NAME`                                      | Secret Manager secret a Gemini key is read from when none is given (`gemini-api-key`)  |
| `DEFAULT_NAMESPACE`                                                       | Kubernetes namespace of the release (`kubeagents-system`)                              |
| `DEFAULT_PLATFORM_AGENT_GSA_NAME`                                         | The agent's GCP service account id (`kubeagents-platform-gsa`); one name per project   |
| `DEFAULT_GITHUB_MINTER_GSA_NAME`                                          | The minter's GCP service account id (`kubeagents-github-minter-gsa`); one per project  |
| `DEFAULT_LITELLM_GSA_NAME`                                                | The gateway's Vertex AI service account id (`kubeagents-litellm-gsa`); one per project |
| `DEFAULT_GKE_DB_KMS_KEYRING`                                              | Cloud KMS key ring for GKE database encryption (`platform-agent-keyring`)              |
| `DEFAULT_GKE_DB_KMS_KEY`                                                  | Cloud KMS key for GKE database encryption (`k8s-secret-encryption-key`)                |
| `DEFAULT_ENABLE_PUBSUB_PLATFORM` / `DEFAULT_ENABLE_STOCKOUT_INVESTIGATOR` | The optional AgentPlugins (`false`)                                                    |
| `DEFAULT_KUBE_AGENTS_STATE_BUCKET`                                        | The `KUBE_AGENTS_STATE_BUCKET` sentinel (`auto`) that derives the state bucket         |
| `DEFAULT_TF_STATE_BUCKET_SUFFIX` / `DEFAULT_TF_STATE_PREFIX_ROOT`         | The derived bucket `<PROJECT_ID><suffix>` and prefix `<root>/<CLUSTER_NAME>`           |
| `DEFAULT_REGISTRY_PREFIX`                                                 | Container registry prefix                                                              |
| `DEFAULT_HELM_TIMEOUT`                                                    | Timeout in seconds for Helm rollouts (`600`)                                           |
| `default_model_for_provider <provider>`                                   | The default model for a provider                                                       |
| `is_valid_model_provider <provider>`                                      | Accepted providers: `gemini`, `vertex_ai`, `anthropic`, `openai`                       |
| `is_valid_permission_set <set>`                                           | Accepted GCP IAM permission sets: `read-only`, `custom`                                |
| `require_supported_permission_set <set>`                                  | The same check, reporting why a rejected value is rejected                             |
| `is_valid_cluster_mode <mode>`                                            | Accepted cluster shapes: `autopilot`, `standard`                                       |
| `derive_kms_location <region>`                                            | Region for Cloud KMS (strips a zone suffix)                                            |
| `tf_state_bucket` / `tf_state_prefix`                                     | Where the install's Terraform state lives in GCS                                       |
| `kms_key_enabled_version <key> <ring> <location> <project>`               | The minter key's first ENABLED version, or nothing; one probe for three callers        |
| `tf_state_has_cluster`                                                    | Whether that state manages THIS cluster (project, location and name all match)         |
| `check_service_account_ownership`                                         | Refuses an apply that would 409 on a service account another install owns              |
| `write_tfvars_from_state <dest> [tag]`                                    | The `terraform.tfvars` generator (reads the loaded `install.env` variable set)         |
| `check_existing_cluster_capacity_preflight`                               | Preflights schedulable capacity on untainted nodes for adopted Standard clusters       |

The values themselves live in [`install.defaults.env`](../../install.defaults.env) at the
repository root, which `installer_common.sh` sources. That file does one job and holds
nothing else: every default an install gets for saying nothing, and no configuration.
Change a default there and every front door follows. Do **not** restate one in
`install.sh`, in a chart, in a `${VAR:-value}` at a point of use, or in prose — link to
this table instead. A second copy of a default is how the installer's permission-set
default once disagreed with the provisioner's. One case is not a copy and stays: a
fallback that deliberately differs from the fresh-install default because it reads an
install that already exists, as `${ENABLE_GVISOR:-false}` does in the control panel and
in `write_tfvars_from_state`. Those carry the argument beside them.

It is sourced **without** `set -a`, unlike `install.env`: these are the project's
defaults, not the install's configuration, so they stay shell variables rather than
entering the environment Terraform and the agent see.

`terraform/examples/full-install/lifecycle.sh` sources the same file. It never sees
`install.env` — it reads its inputs from the generated `terraform.tfvars` — but it has
to agree with the front doors on where the state lives and on the agent GSA's default
name, and reading those from the one file is what makes a hand-driven run and an
installer-driven one name the same objects.

`installer_common.sh` does declare constants of its own, and the distinction is the
point: the Helm release name, the LiteLLM, operator and agent Deployment names, the
`platform-agent-secrets` Secret, and the sandbox StatefulSet, credential-proxy
Deployment and authorized-keys Secret the operator derives from the agent's name are
the chart's and the operator's fixed names, which no `install.env` key can change, so
they are `readonly` constants there (`KUBE_AGENTS_HELM_RELEASE`,
`KUBE_AGENTS_OPERATOR_DEPLOYMENT`, `PLATFORM_AGENT_DEPLOYMENT`, `PLATFORM_AGENT_SECRET`,
`LITELLM_DEPLOYMENT`, `PLATFORM_AGENT_SHELL_STATEFULSET`,
`PLATFORM_AGENT_CREDENTIAL_PROXY_DEPLOYMENT`, `PLATFORM_AGENT_SHELL_AUTHORIZED_KEYS_SECRET`)
rather than defaults an install could override. So are the Helm timeouts
(`HELM_OPERATION_TIMEOUT`, `HELM_LOCK_POLL_INTERVAL`, `HELM_ROLLBACK_TIMEOUT`, each
overridable from the environment for one run) and `IMAGE_TAG_FALLBACK`, which only a
direct caller of the generator reaches because every front door rejects `latest`.
Three things a front door needs before it has a checkout to read anything from — the
clone URL, the clone directory and the Minty CLI tag — are named at the top of the
front door that needs them, and `tests/test_install_script.py` pins the URL equal
across the three.

## The install configuration: `install.env`

An install has one hand-authored input and one derived artifact, and the difference
between them is the whole model.

**`<repo>/install.env`** (git-ignored, `chmod 600`, from the checked-in
`install.env.example`) is the input. Every front door loads it — `install.sh` before its
parameter block, `upgrade.sh` and `uninstall.sh` through `load_install_env`, the Day-2
menu, and `common.sh`'s `load_state` for the dev scripts — with `set -a` so the values
reach `write_tfvars_from_state` and the `TF_VAR_*` handoff, both of which read the
environment. Order of authority is **flag, then file, then an exported variable, then
the defaults above** — `set -a` sourcing means a key the file carries overwrites an
export of the same name, so a flag is what overrides a recorded value for one run.
One key is file-only: the front doors clear a shell-exported `NAMESPACE` before reading
the file, because kubectl tooling exports that name and the value now reaches the Helm
release's namespace. The dev tooling's `load_state` clears it the same way.
`KUBE_AGENTS_INSTALL_ENV` points at a different path, which is how CI renders one from
its own variables rather than keeping install state on an ephemeral runner.

`install.sh` reads it and does not rewrite it. It creates one at the end of a first
install, when there is nothing there, and never touches it again; the Day-2 menu's
"Save & Apply" is the one path that edits it, one key at a time, leaving comments and
ordering intact. That asymmetry is deliberate: a file the documentation tells you to edit
and the next run overwrites is what made the old `vars.sh` confusing.

**`terraform/examples/full-install/terraform.tfvars`** is the derived artifact,
regenerated on every run from the loaded environment. Nobody edits it.

**`<repo>/install.defaults.env`** is checked in and holds the defaults, nothing else. It
is not configuration and not something an operator edits per install; it is where this
project decides what an install gets for saying nothing. Full precedence:

```
install.defaults.env  →  an exported environment variable  →  install.env  →  a command-line flag
```

Loading the input first is also what fixes non-interactive re-runs (#1060). Every
`PARAM_X="${VAR:-}"` seed already knew how to inherit from the environment; giving it a
file to inherit from makes inheritance the default path rather than something each flag
has to remember, so the next flag added inherits too.

### What is deliberately not in it

Derived values are recomputed every run rather than stored, because a stored copy can
only disagree with the live answer. `PROJECT_NUMBER` comes from `gcloud projects
describe` and `KMS_LOCATION` from `derive_kms_location`. `create_cluster` and the
**effective** `CLUSTER_MODE` come from `write_tfvars_from_state`'s own probe of the live
cluster. `NO_CONFIRM` describes an invocation, not an install, and comes from
`-y`/`--non-interactive`. The identity keys (`PLATFORM_AGENT_GSA_NAME`,
`GITHUB_MINTER_GSA_NAME`, `LITELLM_GSA_NAME`, `GKE_DB_KMS_KEYRING`, `GKE_DB_KMS_KEY`) are
written into a new `install.env` only when the run set them — a default copied in
would freeze at that release, and a custom name that went missing would replace the
account — and `NAMESPACE` is never copied in from the environment.

`CLUSTER_MODE` in `install.env` therefore supplies one thing: the shape of a cluster that
does not exist yet. Whenever the probe finds a cluster, that cluster's own shape wins and
the configured value is discarded — which is what stops a hand-written
`CLUSTER_MODE=standard` against a live Autopilot cluster from taking its resource count
to 0 and turning the next apply into a replacement. Nothing writes the probe's answer
back, so the file never becomes an input and an output at once.

### Credentials

`PERSIST_SECRETS_ON_DISK=false` keeps them out of every file the installer writes: the
generator omits them from `terraform.tfvars` and exports them as `TF_VAR_*` for the apply
instead, and later runs recover them from the live `platform-agent-secrets` Secret (only
when kubectl's current context is this install's cluster). `API_SERVER_KEY` is generated
once, when the configuration carries none and none can be recovered — not on every run,
which used to replace the Secret and restart every pod holding it.

### Cluster adoption and component toggles

`SKIP_CERT_MANAGER=true` makes the generator emit `enable_cert_manager = false`, for a
cluster whose cert-manager comes from somewhere else. Without it, the generator probes an
existing cluster for a `cert-manager` Deployment and emits `false` when it finds one that
is not the composition's own; one whose release is in this install's Terraform state keeps
`true`, so a retry after a failed apply, or an `upgrade.sh` run, does not have Terraform
destroy the cert-manager it installed. A state that cannot be read also keeps `true`: the
wrong `true` fails the apply on the existing CRDs, the wrong `false` destroys silently.

A retry has one more leftover to clear. An apply that dies inside the kube-agents release
leaves it in Helm's `failed` status, with no revision that ever served and no entry in
Terraform state, and Helm refuses the retry's create with "cannot re-use a name that is
still in use". When the cluster already exists, `install.sh` uninstalls exactly that
release before the apply (`clear_failed_initial_helm_release`), and only while kubectl's
current context is that cluster's; a failed release that served before, one the state
manages, or one whose state cannot be read is left as it is.

`MIGRATE_NODE_POOLS=true` (or `--migrate-node-pools`) authorizes migrating existing node pools
using the legacy GCE metadata server to `GKE_METADATA`, which recreates the pool's nodes and restarts
workloads. Without opt-in, the install aborts before making any cluster changes because kube-agents
requires Workload Identity (`GKE_METADATA`).

`ENABLE_NETWORK_POLICY=true` (or `--enable-network-policy`) authorizes enabling the legacy Calico
NetworkPolicy addon and enforcement on pre-existing GKE Standard clusters lacking Dataplane V2.
Enabling Calico may recreate nodes and restart workloads. Without opt-in, the install aborts before
making any cluster changes because kube-agents requires NetworkPolicy enforcement.

`ALLOW_UNENCRYPTED_SECRETS=true` skips the out-of-band Cloud KMS CMEK database encryption on
pre-existing clusters (testing environments only).

### The predecessor: `vars.sh`

`k8s-operator/scripts/vars.sh` was the generated state file `install.env` replaces. No
front door writes one any more. Every reader still accepts one so that an install
predating the change keeps working with no action from its owner: each loads `vars.sh`
first and `install.env` over the top, so the input wins. `install.sh` additionally
migrates — it reads a legacy `vars.sh` and warns, and a full run that has no `install.env`
yet writes those values into one on the way out, after which the old file can be deleted.
A run that already has an `install.env` does not: `bootstrap_install_env_file` treats an
existing file as the operator's, so the legacy values are loaded for that run and recorded
nowhere. Delete `vars.sh` only once `install.env` carries what you need from it.

One writer is left, and it is not an install one. The dev tooling under `scripts/dev/`
records whether it created the throwaway Artifact Registry (`DEV_ARTIFACT_REGISTRY_CREATED`)
through `save_var`, which lands in `scripts/installer/vars.sh` beside these helpers. That
file is developer scratch state, git-ignored, and holds nothing an install is configured
from; deleting it costs at most one redundant registry check.

Both Python readers — `scripts/live_test_lease.py` and `admin_console/project_config.py`
— match an allowlist of assignments with a regex and never source either file, because
both hold credentials. They accept `K=V` and `export K=V` alike, since `install.env` is a
dotenv and `vars.sh` was generated with `printf %q`.

## File directory

- **[installer_common.sh](installer_common.sh)**: the `install.env` loader, validators,
  GitHub org checks, and the `terraform.tfvars` generator (table above). Sources the
  defaults from [`install.defaults.env`](../../install.defaults.env) rather than
  declaring any itself.
- **[common.sh](common.sh)**: utilities the dev tooling and the Prow CI scripts
  (`hack/ci-deploy.sh`) use — colour output, `init_var`/`load_state`,
  registry and third-party-image resolution, cluster connection helpers. Sources
  `installer_common.sh`, so nothing is defined twice.
- **[gke_dns_endpoint.sh](gke_dns_endpoint.sh)**: `gke_dns_endpoint_flag`, which decides whether a given cluster should be reached with `get-credentials --dns-endpoint`. Kept out of `common.sh` and free of its helpers so `hack/ci-env.sh`, `scripts/release/common.sh`, `upgrade.sh`, and the staging-workload scripts can source the one predicate without also taking on the state file. It sets `GKE_DNS_ENDPOINT_FLAG` rather than echoing, so that callers do not run it in a `$(...)` subshell that would discard its memo of whether the local gcloud offers the flag at all. That answer leaves it empty — as do a cluster with no externally reachable DNS endpoint and a describe call that fails — leaving today's IP-endpoint command untouched.
- **[min_versions.sh](min_versions.sh)**: minimum tool versions, side-effect-free so
  `install.sh` can source it standalone before any checkout exists.
- **[print_instructions_gchat.sh](print_instructions_gchat.sh)** /
  **[print_instructions_slack.sh](print_instructions_slack.sh)**: post-install manual-step
  instructions, printed by `install.sh` when the integration is enabled.
- **[../dev/dev_rebuild_agent.sh](../dev/dev_rebuild_agent.sh)**: fast local development utility
  that builds, pushes, and redeploys agent container images.
