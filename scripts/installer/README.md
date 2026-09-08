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

| Symbol                                   | What it fixes                                                                          |
| ---------------------------------------- | -------------------------------------------------------------------------------------- |
| `DEFAULT_CLUSTER_NAME`                   | GKE cluster name (`platform-agent-host`)                                               |
| `DEFAULT_REGION`                         | GCP region (`us-central1`)                                                             |
| `DEFAULT_CLUSTER_MODE`                   | Shape a fresh install creates (`autopilot`); a live cluster's probed shape always wins |
| `DEFAULT_VERTEX_LOCATION`                | Vertex AI serving location (`global`)                                                  |
| `DEFAULT_VERTEX_MANAGE_SERVING_PROJECT`  | Enable the API and grant the gateway's role in the serving project (`true`)            |
| `DEFAULT_MODEL_PROVIDER`                 | Model provider (`gemini`)                                                              |
| `DEFAULT_REGISTRY_PREFIX`                | Container registry prefix                                                              |
| `default_model_for_provider <provider>`  | The default model for a provider                                                       |
| `is_valid_model_provider <provider>`     | Accepted providers: `gemini`, `vertex_ai`, `anthropic`, `openai`                       |
| `is_valid_permission_set <set>`          | Accepted GCP IAM permission sets: `read-only`, `custom`                                |
| `require_supported_permission_set <set>` | The same check, reporting why a rejected value is rejected                             |
| `is_valid_cluster_mode <mode>`           | Accepted cluster shapes: `autopilot`, `standard`                                       |
| `derive_kms_location <region>`           | Region for Cloud KMS (strips a zone suffix)                                            |
| `tf_state_bucket` / `tf_state_prefix`    | Where the install's Terraform state lives in GCS                                       |
| `write_tfvars_from_state <dest> [tag]`   | The `terraform.tfvars` generator (reads the loaded `install.env` variable set)         |

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
`-y`/`--non-interactive`.

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

`SKIP_CERT_MANAGER=true` makes the generator emit `enable_cert_manager = false`, for a
cluster whose cert-manager comes from somewhere else.

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
