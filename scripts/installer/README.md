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
| `DEFAULT_MODEL_MAX_TOKENS`                                                | Output tokens the gateway asks for on a request that names none (`0`: no `max_tokens`) |
| `DEFAULT_LITELLM_REDACTION_ENABLED` / `_IP_ACTION`                        | Gateway redaction (`false`) and what it does with IP literals (`pseudonym`)            |
| `DEFAULT_GEMINI_API_KEY_SECRET_NAME`                                      | Secret Manager secret a Gemini key is read from when none is given (`gemini-api-key`)  |
| `DEFAULT_NAMESPACE`                                                       | Kubernetes namespace of the release (`kubeagents-system`)                              |
| `DEFAULT_PLATFORM_AGENT_GSA_NAME`                                         | The agent's GCP service account id (`kubeagents-platform-gsa`); one name per project   |
| `DEFAULT_GITHUB_MINTER_GSA_NAME`                                          | The minter's GCP service account id (`kubeagents-github-minter-gsa`); one per project  |
| `DEFAULT_LITELLM_GSA_NAME`                                                | The gateway's Vertex AI service account id (`kubeagents-litellm-gsa`); one per project |
| `DEFAULT_GKE_DB_KMS_KEYRING`                                              | Cloud KMS key ring for GKE database encryption (`platform-agent-keyring`)              |
| `DEFAULT_GKE_DB_KMS_KEY`                                                  | Cloud KMS key for GKE database encryption (`k8s-secret-encryption-key`)                |
| `DEFAULT_ENABLE_PUBSUB_PLATFORM` / `DEFAULT_ENABLE_STOCKOUT_INVESTIGATOR` | The optional AgentPlugins (`false`)                                                    |
| `DEFAULT_ENABLE_DRIFT_DETECTOR`                                           | Out-of-band change detection: the audit-log ingress and its consumer (`true`)          |
| `DEFAULT_KUBE_AGENTS_STATE_BUCKET`                                        | The `KUBE_AGENTS_STATE_BUCKET` sentinel (`auto`) that derives the state bucket         |
| `DEFAULT_TF_STATE_BUCKET_SUFFIX` / `DEFAULT_TF_STATE_PREFIX_ROOT`         | The derived bucket `<PROJECT_ID><suffix>` and prefix `<root>/<CLUSTER_NAME>`           |
| `DEFAULT_REGISTRY_PREFIX`                                                 | Container registry prefix                                                              |
| `default_model_for_provider <provider>`                                   | The default model for a provider                                                       |
| `is_valid_model_provider <provider>`                                      | Accepted providers: `gemini`, `vertex_ai`, `anthropic`, `openai`                       |
| `is_valid_permission_set <set>`                                           | Accepted GCP IAM permission sets: `read-only`, `custom`                                |
| `require_supported_permission_set <set>`                                  | The same check, reporting why a rejected value is rejected                             |
| `is_valid_cluster_mode <mode>`                                            | Accepted cluster shapes: `autopilot`, `standard`                                       |
| `derive_kms_location <region>`                                            | Region for Cloud KMS (strips a zone suffix)                                            |
| `derive_chat_sub_name [topic] [sub]`                                      | Derive Google Chat Pub/Sub subscription (`<topic>-sub`) when topic is custom           |
| `tf_state_chat_subscription_name`                                         | The subscription name managed by module.chat_pubsub, or empty                          |
| `tf_state_bucket` / `tf_state_prefix`                                     | Where the install's Terraform state lives in GCS                                       |
| `kms_key_enabled_version <key> <ring> <location> <project>`               | The minter key's first ENABLED version, or nothing; one probe for three callers        |
| `tf_state_has_cluster`                                                    | Whether that state manages THIS cluster (project, location and name all match)         |
| `check_service_account_ownership`                                         | Refuses an apply that would 409 on a service account another install owns              |
| `write_tfvars_from_state <dest> [tag]`                                    | The `terraform.tfvars` generator (reads the loaded `install.env` variable set)         |

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
agent container and Hermes profile inside that Deployment's pod, the
`platform-agent-secrets` Secret, and the sandbox StatefulSet, credential-proxy
Deployment and authorized-keys Secret the operator derives from the agent's name are
the chart's and the operator's fixed names, which no `install.env` key can change, so
they are `readonly` constants there (`KUBE_AGENTS_HELM_RELEASE`,
`KUBE_AGENTS_OPERATOR_DEPLOYMENT`, `PLATFORM_AGENT_DEPLOYMENT`,
`PLATFORM_AGENT_CONTAINER`, `PLATFORM_AGENT_HERMES_PROFILE`, `PLATFORM_AGENT_SECRET`,
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
One key ignores the environment in every front door: `install.sh`, `upgrade.sh`, and
`uninstall.sh` clear a shell-exported `NAMESPACE` before reading the file, because kubectl
tooling exports that name and the value now reaches the Helm release's namespace. The file
and `--agent-namespace` are the two routes in (`common.sh`'s `load_state` clears it the
same way). `upgrade.sh` and `uninstall.sh` also clear shell-exported `PROJECT_ID`,
`CLUSTER_NAME`, and `REGION` before reading `install.env`, so ambient GCP exports in the
caller's shell cannot steer a Day-2 run at a different cluster or be mistaken for keys
recorded in `install.env` — pass `--gcp-project-id`, `--gke-cluster-name`, and `--gcp-region`
when `install.env` omits them (or when tearing down a pre-`install.env` install); when both
a flag and `install.env` name a coordinate and disagree, `upgrade.sh` and `uninstall.sh`
refuse rather than mixing two installs' settings.
`KUBE_AGENTS_INSTALL_ENV` points at a different path, which is how CI renders one from
its own variables rather than keeping install state on an ephemeral runner.

Which file that is, for a front door that has to go and find one: `KUBE_AGENTS_INSTALL_ENV`
first, then the checkout the run's own sources came from, then the working directory,
and last — when `install.sh`, `upgrade.sh`, or `uninstall.sh` runs from outside a
checkout (such as the release-pinned one-liner, which has no checkout of its own) — the install
checkout in `$HOME/kube-agents`. A checkout run of any of the three front doors never falls
through to `$HOME/kube-agents`, and on a piped run `$HOME/kube-agents` is last rather than first
so that a workstation managing two installs acts on the one whose directory the operator is
standing in, not whichever one that shared checkout belongs to. The one additional gate on
`uninstall.sh` is `--source-ref`: because that handover exists to tear down an older release
(and pre-`0.4.0` installs wrote no `install.env`), a file found only at
`$HOME/kube-agents/install.env` is skipped unless all three of `--gcp-project-id`,
`--gke-cluster-name`, and `--gcp-region` are given on the command line and match it.

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

That precedence has a sharp edge on an install that already exists. A key missing from
`install.env` is not "leave it as it is": it resolves to the default, the default is written
into `terraform.tfvars`, and `upgrade.sh --upgrade-mode=full` then plans the destruction of
whatever the default does not mention. `ENABLE_GVISOR` absent destroys the gVisor node pool
on a Standard cluster (`write_tfvars_from_state` falls back to `false` for that key, not to
`install.defaults.env`'s `true`); `MEMORY` absent falls back to `file` unless
`write_tfvars_from_state` finds a live Hindsight deployment (`hindsight-postgresql` or
`hindsight-api`) on the target cluster, in which case `kube_agents_memory` is preserved
(`--memory=file` or `MEMORY=file` is required to tear it down);
`ENABLE_GKE_BACKUP_PLAN` absent destroys the backup plan; `ENABLE_STOCKOUT_INVESTIGATOR`
absent destroys the stockout log sink, its alerts topic and subscription, and their IAM
grants; `ENABLE_PUBSUB_PLATFORM` absent removes
the adapter plugin from the release (the
composition owns no Pub/Sub resource for it alone); `GOOGLE_CHAT_ENABLED` absent removes the
Chat topic and subscription; `PLATFORM_AGENT_PERMISSION_SET` absent falls back to `read-only`
and drops the custom roles; `SCOPE_PROJECTS`, `SCOPE_FOLDERS`, `SCOPE_ORGANIZATIONS`,
`SCOPE_SHARED_VPC_HOSTS` or `SCOPE_METRICS_SCOPES` absent
renders an empty list for it in the scope block, which revokes the read roles in every project,
folder, organisation or selector member it named and retires those projects' Cluster Agent profiles over the
reconcile's next two clean runs; `SCOPE_MAX_PROJECTS` absent writes no cap, so the default of 100
returns and the projects past it read `over-cap` (or the plan is refused, while a selector is declared);
`SCOPED_SA_POOL_ENABLED` absent disarms the scoped service account pool, deleting its accounts
and putting the broker back on the agent's own identity.
The file `install.sh` writes at the end of a first install carries every one of these, so
the hazard is a hand edit that deletes a line rather than setting it to `false`. Run
`./upgrade.sh --plan` before a full upgrade and read any `destroy` line as missing
configuration first and real drift second.

`ENABLE_DRIFT_DETECTOR` has the same sharp edge pointing the other way, because its absence
provisions rather than destroys. Two things have to hold for that, and most keys fail one of
them: `write_tfvars_from_state` has to resolve the key against `install.defaults.env` rather
than against a literal `false`, and the default there has to be `true`. `ENABLE_GVISOR`, in
the paragraph above, clears the second and fails the first — it defaults to `true` and still
destroys, because the generator hardcodes `${ENABLE_GVISOR:-false}`. `ENABLE_GKE_BACKUP_PLAN`,
`ENABLE_PUBSUB_PLATFORM` and `ENABLE_STOCKOUT_INVESTIGATOR` clear the first and fail the
second. `VERTEX_MANAGE_SERVING_PROJECT` is the only other key where both hold: absent, it
re-enables `aiplatform.googleapis.com` and re-grants `roles/aiplatform.user` in the serving
project. A full
upgrade over an `install.env` written before the key existed adds the Log Router sink,
Pub/Sub topic and subscription that carry the project's GKE audit records, and starts the
detector that reads them. That is intended — running an installer is the consent — but it
means opting out has to be a `ENABLE_DRIFT_DETECTOR=false` line in the file. Only the run
that creates `install.env` records the flag it was passed; over a file that is already
there, a `--enable-drift-detector=false` applies to the run it is passed to and is recorded
nowhere, so the next run resolves the default again, and `upgrade.sh` accepts no such flag
at all. `install.sh` warns when you pass that flag over a file that does not carry the line;
an exported `ENABLE_DRIFT_DETECTOR=false` is just as unrecorded and currently warns about
nothing, so the file is the only opt-out that survives the shell it was typed in.
Setting it to `false` is still the one boolean `write_tfvars_from_state` omits rather than
writing, so an install carrying a `TF_VAR_enable_drift_pubsub=true` line keeps its ingress
and only loses the detector — a written `false` would outrank that line and take the sink,
topic and subscription with it, up to 31 days of retained messages included.

`MEMORY` is the only one of the keys above that the generator goes and asks the cluster
about, because it is the only one whose default deletes data rather than infrastructure
Terraform can build again. That probe has three outcomes, not two. Found and confirmed
absent behave as above; the third is "could not ask" — no `kubectl`, a context pointing at
another cluster, an expired credential, a timeout — and there `install.sh` and `upgrade.sh`
stop and say so rather than read silence as "no Hindsight here". Answer the question
instead: record `MEMORY=hindsight|file|off` in `install.env` (`install.sh` also takes
`--memory=`, and `MEMORY=…` in the environment answers for one run), or restore access to
the cluster and re-run. The recording is named first because `upgrade.sh` has no `--memory`
flag and would answer it with `Unknown parameter`. `uninstall.sh` does not stop, because a
teardown removes the store either way and an install has to keep a working way to remove
itself.

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

### Projects, folders, organisations and selectors in scope

`SCOPE_PROJECTS`, `SCOPE_FOLDERS`, `SCOPE_ORGANIZATIONS`, `SCOPE_SHARED_VPC_HOSTS`,
`SCOPE_METRICS_SCOPES`, `SCOPE_MAX_PROJECTS`, `SCOPE_EXCLUDE_PROJECTS` and `SCOPE_EXCLUDE_CLUSTERS`
are the `PlatformAgent`'s `spec.scope`, declared once and reaching both halves of the install from
the same value: the generator renders them as the composition's `scope` object, the IAM module binds
the read roles in every project named, the read roles plus `roles/cloudasset.viewer` on every
folder and organisation named, and the read roles in every project a Shared VPC host or Metrics
Scope resolves to, and the chart renders the same object into the CR. The lists are space- or
comma-separated like every other list key; a folder or organisation is its bare numeric ID, and an
entry that is not one stops the run before `terraform.tfvars` is written; a Shared VPC host or
Metrics Scope is a project ID (the host's, or the scope's scoping project's); an excluded project
may be a shell-style glob; an excluded cluster is `project/location/cluster`, and an entry that
does not split into three parts stops the run the same way. The patterns, caps and repeats the CRD
enforces are checked by the module's variable validation, which fails the plan before any binding.
A folder or organisation also adds `cloudasset.googleapis.com` to the APIs the composition enables
in the host project, because the reconcile resolves a container's members through it; an install
that names explicit projects alone never enables it.

A Shared VPC host or a Metrics Scope inherits nothing, so the composition resolves it to projects
when Terraform plans (the `kube-agents-scope-resolver` module), with the same reads the reconcile
makes each run (the Compute API for a host's service projects, the Monitoring API for a scope's
monitored projects, Resource Manager to name each of those by ID) made with the google provider's
own token, and the IAM module binds the read roles in each and in the scoping project, and
`roles/compute.viewer` alone in a host not otherwise in scope, which the reconcile's lookups read. A
read that identity cannot make fails
the plan, before anything is applied, naming the selector, the status and the API's message: it
needs `compute.projects.get` on a host, to read the Metrics Scope in its scoping project with the
Monitoring API enabled there, and `resourcemanager.projects.get` on every monitored project; a
monitored project it cannot name is left out by naming its project number in
`SCOPE_EXCLUDE_PROJECTS`. That is why no shell preflight probes the two selectors' reads as
`check_scope_container_access` probes a container: a container's failure lands inside the apply,
after the Asset API is enabled and some containers are bound, while a failed read lands in the plan
with nothing changed, `upgrade.sh --plan` included. The bindings themselves are the explicit
projects' case: a resolved project the applying identity cannot set IAM policy in fails inside the
apply, as a `SCOPE_PROJECTS` entry does, and no preflight probes either. What the selectors do not have is a container's
zero-touch onboarding: a service project attached, or a project added to the scope, after the last
full upgrade reads `denied` in the reconcile's snapshot until the next one binds it. An exclude
entry that names a Shared VPC service project by ID, or a monitored project by its project number,
keeps it out of the bindings, the one place an exclusion reaches IAM, because the member has no list
to be dropped from; a monitored project excluded by ID keeps its grant, which the reconcile's naming
call needs before the exclusion can match. The reads are billed to the management project and use
its `cloudresourcemanager` and `monitoring` APIs for a Metrics Scope and its `compute` API for a
Shared VPC host, which the composition enables in the apply that follows the plan, per selector.
So before an `install.sh` apply that carries a selector, `enable_scope_selector_apis` lists the
project's enabled APIs and enables whichever of the ones the declared selectors read is off, as
gcloud's active account, like the KMS enablement beside it: nothing is called when they are on, which is every re-run and Day-2 apply of an existing install, and a failure is a
warning, since the plan reports a disabled API with the same command as its remedy. The
generate-only handoff prints the command above the apply, `install.sh --dry-run` skips its plan
with the command while an API a declared selector reads is off (a dry run enables nothing, and its plan would
otherwise be refused for a reason the real run does not have), and `upgrade.sh` does none of it,
because an existing install has them on. The reconcile lists at most `SCOPE_MAX_PROJECTS` projects (100 by default) of the resolved
set, the management project included, so a declaration whose management project, `SCOPE_PROJECTS`
and selector members together exceed that (once each, less an exact `SCOPE_EXCLUDE_PROJECTS` entry; a
project both in `SCOPE_PROJECTS` and excluded by its number stays counted, so drop it from `SCOPE_PROJECTS`)
is refused at plan rather than bound in full while a selector is declared or the cap is below its default (without a selector and at the default the count is the CRD's own, and such a plan is not refused for it), and a single selector past it is refused at its read.

The block is written on every run, empty lists included: an emptied `projects` list is the
declaration that drops projects, and a missing block would declare nothing, so removing a
project from `SCOPE_PROJECTS` and running `upgrade.sh --upgrade-mode=full` is how a project
leaves the scope. A file that lacks the keys declares an empty scope, like every absent key
(the list above). Only full mode applies the keys; `harness` and `operator` retags re-render
the release's recorded values and change nothing about the scope. `upgrade.sh`, `uninstall.sh`
and the Day-2 menu read the keys from `install.env` alone (`load_install_env` drops a value
inherited from the shell, as it does `NAMESPACE`, and `install.sh` does the same once an
`install.env` exists); `install.sh` also takes the `--scope-*` flags, and on a first install
the environment, and records them, and an empty `--scope-*=` is refused. A malformed
`SCOPE_EXCLUDE_CLUSTERS`, `SCOPE_FOLDERS`, `SCOPE_ORGANIZATIONS` or `SCOPE_MAX_PROJECTS` entry stops every front door but
`uninstall.sh`, retags included, until the line is fixed; there is no bypass. `SCOPE_MAX_PROJECTS` is
`spec.scope.maxProjects`, the most projects the reconcile lists per run (1 to 5000): empty, the
default, leaves the CRD's 100 in force and writes no `max_projects` into the scope block; a value
is rendered into the block, refused at plan time when the explicit projects and selector members
exceed it (while a selector is declared or the cap is below its default), and read by the live-scope
check as part of the declaration, so a cap set on the CR by
hand is reported like any hand edit until the key records it.

`SCOPED_SA_POOL_ENABLED` and `SCOPED_SA_POOL_MAX_ACCOUNTS` are the scoped service account pool,
derived from the same scope: with the switch on, the IAM module provisions one reader service
account in the management project per project the plan lists (the management project,
`SCOPE_PROJECTS` less an exact `SCOPE_EXCLUDE_PROJECTS` entry, and each selector's members; a
folder's or organisation's members are not listed at plan time yet, so a cluster under a declared
container is refused while the pool is armed), and the chart renders the mapping into the CR as
`spec.security.scopedServiceAccountPool` with `enabled` set from the same key, so declaring
projects arms nothing on its own. Members carry the install's identity in their description
(`Pool member of <PLATFORM_AGENT_GSA_NAME> for projects/<id>`), and `check_service_account_ownership`
lists them by it before every apply, armed or not, so a member left by a lost state is refused
beside the agent's account rather than 409ing after the agent is deleted. The generator writes `scoped_pool_enabled` on every run, `false`
by default (a member holds no IAM grant yet, so an armed pool turns every cluster read into a
`Forbidden`), and `scoped_pool_max_accounts` only when the key is set: the bound on the pool, declared from the
management project's free service-account quota, 100 (GCP's default quota, not the headroom)
when unset, with a pool past it refused at plan. `install.sh` takes `--scoped-sa-pool-enabled[=BOOL]` and
`--scoped-sa-pool-max-accounts=N` and records them on a first install; a flag on a later run
applies for that run and warns that the next full upgrade regenerates from the file. A misspelt
switch, or a cap that is not a whole number of at least 1, stops every front door but
`uninstall.sh`, which exports the switch off and the cap empty before it regenerates, so a typo
cannot refuse a teardown.

Before a full apply the front doors read the live `PlatformAgent` through the install's own
kubeconfig context and refuse when it carries a scope that neither the release record nor the
keys account for, printing the `SCOPE_*` lines that reproduce it; the scoped pool's switch
(`spec.security.scopedServiceAccountPool.enabled`) is weighed with the lists and the cap, so a
pool armed on the CR that `SCOPED_SA_POOL_ENABLED` does not record is refused the same way, with
that key among the lines, rather than disarmed by the apply. A read that cannot decide (no
context, an unreadable CR or release) refuses too, because the apply itself needs no kubeconfig
and would go ahead over a scope nobody read (`refuse_apply_over_undeclared_scope` in
`installer_common.sh`; `upgrade.sh --plan` warns instead). An `install.sh` re-run
and the menu apply the chart's CRDs before their apply, as `upgrade.sh` does, so the block lands on
every front door rather than being pruned by a served schema that predates the field.

When a folder or organisation is declared, a second check runs before every apply, first install
included (`check_scope_container_access`): that `cloudasset.googleapis.com` is enabled in the
host project or no enforced organisation policy (`constraints/gcp.restrictServiceUsage`, the
legacy `constraints/serviceuser.services`; a policy in dry run enforces nothing and is not read)
denies it, read through gcloud's active account, and that the identity Terraform applies with
holds `resourcemanager.folders.setIamPolicy` on each folder and
`resourcemanager.organizations.setIamPolicy` on each organisation, asked through Resource
Manager's `testIamPermissions` with a token minted for the credentials the google provider will
read, in its order: `GOOGLE_OAUTH_ACCESS_TOKEN`, else `GOOGLE_CREDENTIALS`,
`GOOGLE_CLOUD_KEYFILE_JSON` or `GCLOUD_KEYFILE_JSON` (an existing path is a key file, anything
else is the key's JSON, the provider's own rule), else the Application Default Credentials, which
read `GOOGLE_APPLICATION_CREDENTIALS` first, each impersonating `GOOGLE_IMPERSONATE_SERVICE_ACCOUNT`
when it is set. The messages name that identity, so a refusal points at the
principal that will apply rather than at whatever ADC the workstation holds, and a credential
variable's value is never printed. The token reaches `curl` on its stdin and an inline key
reaches `gcloud` through a file that exists only for the mint and is removed on any exit of it,
a signal included. Every container is probed and every failure named before the run
refuses; a probe that cannot decide (no `curl`, no token, a transport error) warns and lets the
apply report it, because an apply that cannot bind fails loudly, unlike the silent replace the
first check guards against. `upgrade.sh --plan`, `install.sh --generate-only` and the interactive
`g` answer warn instead of refusing, the first because it applies nothing and the other two
because the apply they hand to `lifecycle.sh` may run as an identity other than the one at the
keyboard; an interactive run is checked at the `(Y/n/g)` prompt, where its route is known, so a
`Y` refuses before anything is applied. The retag modes and `install.sh --dry-run` do not run it.
Declaring an organisation prints a warning on every run that reaches the check: the binding
reaches every project in it. gcloud's own credential overrides are kept out of every gcloud call
the check makes: the `CLOUDSDK_AUTH_*` variables are cleared for the mint and for the property
read that guards it, and a set `auth/impersonate_service_account` or `auth/access_token_file`
property in the active configuration file, which the provider does not read, makes the probe
undecided with the property named, unless `GOOGLE_IMPERSONATE_SERVICE_ACCOUNT` overrides the
first explicitly.

An install that declared a folder, organisation, Shared VPC host or Metrics Scope on the
`PlatformAgent` by hand before the installer had a key for it, and had its roles bound by hand, is
refused at its next full upgrade like any hand edit, and the lines it prints include
`SCOPE_FOLDERS`, `SCOPE_ORGANIZATIONS`, `SCOPE_SHARED_VPC_HOSTS` and `SCOPE_METRICS_SCOPES`.
Recording them hands the bindings to Terraform, which creates them with the applying credentials,
so those credentials need `setIamPolicy` on the container, or in each project a selector resolves
to, even where an administrator made the hand grant, and for a selector the reads that resolve it;
the alternatives are to obtain them for the identity that applies, or to take the entry off the
`PlatformAgent`, which retires its members over the reconcile's next two clean runs, and manage
those projects through `SCOPE_PROJECTS` instead.

The bindings live in projects, folders and organisations the applying identity has to be able to
set IAM policy in. A scoped project or container that is deleted, or whose owner revokes that
permission, fails the refresh or destroy of its bindings on every later plan, full upgrade and
uninstall (a Shared VPC host or Metrics Scope this identity can no longer read fails the plan the
same way, since the lookup runs on every plan except `uninstall.sh`'s destroy, which blanks the
selector keys). Remove it from `SCOPE_PROJECTS`, `SCOPE_FOLDERS`, `SCOPE_ORGANIZATIONS`,
`SCOPE_SHARED_VPC_HOSTS` or `SCOPE_METRICS_SCOPES`, or, for a project a selector resolved to, name
it exactly in `SCOPE_EXCLUDE_PROJECTS` (a monitored project the identity can no longer name only by
its project number, since the ID is what the plan could not read; a service project by its ID), and forget
its bindings from state, from the composition directory the last `lifecycle.sh` run initialised
against the install's backend (the address is `module.kube_agents_iam.google_project_iam_member.scope_roles`,
`module.kube_agents_iam.google_folder_iam_member.scope_roles` or
`module.kube_agents_iam.google_organization_iam_member.scope_roles`, keyed `<id>/<role>`):

```bash
cd terraform/examples/full-install
terraform state list | grep 'scope_roles\["<id>/' | while IFS= read -r address; do
  terraform state rm "$address"
done
```

The grants left in the unreachable project are orphaned, not revoked. A `custom` permission set
made of custom IAM roles cannot declare a scope: a custom IAM role is never carried into scoped
projects (only the six predefined read roles in `scope.tf`'s allowlist are, those of them the host
project holds), and the plan is refused until `PLATFORM_AGENT_CUSTOM_ROLES` carries
`roles/container.clusterViewer` or `roles/container.viewer`.

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
Enabling Calico may recreate nodes and restart workloads. `ACCEPT_NO_NETWORK_POLICY=true` (or
`--accept-no-network-policy`) is the other answer: install without enforcement and leave the cluster
as it is. The generator emits it as `accept_no_network_policy` in `terraform.tfvars`, which is what
gets the plan past the gke-cluster module's postcondition, so the key has to stay in `install.env`
for `upgrade.sh` and the Day-2 menu to regenerate an applicable file. Without either, the install
aborts before making any cluster changes.

`ALLOW_UNENCRYPTED_SECRETS=true` skips the out-of-band Cloud KMS CMEK database encryption on
pre-existing clusters (testing environments only).

### The predecessor: `vars.sh`

`k8s-operator/scripts/vars.sh` was the generated state file `install.env` replaced in 0.4.0.
No front door or Python helper reads, writes, or inspects it any more; `install.env` is the
sole install configuration input, and pre-0.4.0 checkouts without `install.env` are not
supported.

One separate file of the same name remains, and it is not an install configuration: the
dev tooling under `scripts/dev/` records whether it created the throwaway Artifact Registry
(`DEV_ARTIFACT_REGISTRY_CREATED`) through `save_var`, which lands in
`scripts/installer/vars.sh` beside these helpers. That file is developer scratch state,
git-ignored, and holds nothing an install is configured from; deleting it costs at most one
redundant registry check.

Both Python readers — `scripts/live_test_lease.py` and `admin_console/project_config.py`
— match an allowlist of assignments in `install.env` with a regex and never source it,
because it holds credentials. They accept `K=V` and `export K=V` alike, since `install.env`
is a hand-authored dotenv and a hand may well write `export`.

## File directory

- **[installer_common.sh](installer_common.sh)**: the `install.env` loader, validators,
  GitHub org checks, and the `terraform.tfvars` generator (table above). Sources the
  defaults from [`install.defaults.env`](../../install.defaults.env) rather than
  declaring any itself. The front doors run `set -E` with an ERR trap that every `$(...)`
  inherits, and bash 3.2 (macOS's `/bin/bash`) runs that trap inside the subshell even
  when the caller handles the failure. Each front door's `on_error` therefore exits a
  subshell silently and leaves the banner and the report to the parent, which prints
  them only when the failure reaches it; a probe in a front door needs no guard of its
  own. This library cannot know its caller's trap, so its tolerated probes (a release,
  deployment, ref or state object that is not there: `helm_release_status`,
  `tf_state_read`) run `trap - ERR` inside their substitution as well. Process
  substitution (`< <(...)`) leaves `BASH_SUBSHELL` at 0 on bash 3.2, so a tolerated read
  through one clears the trap inline wherever it sits.
- **[common.sh](common.sh)**: utilities the dev tooling (`scripts/dev/`) and the
  `print_instructions_*` helpers use — colour output, `init_var`/`load_state`,
  the registry prefixes, cluster connection, the step runner. Sources
  `installer_common.sh`, so nothing is defined twice. `hack/ci-deploy.sh` sources
  `scripts/release/common.sh`, not this file.
- **[gke_dns_endpoint.sh](gke_dns_endpoint.sh)**: `gke_dns_endpoint_flag`, which decides whether a given cluster should be reached with `get-credentials --dns-endpoint`. This is the roster the file's own header defers to: `common.sh`, `installer_common.sh`, `install.sh`, `upgrade.sh`, `hack/ci-env.sh`, `scripts/release/common.sh`, `scripts/release/reconcile_environment.sh`, `terraform/examples/full-install/lifecycle.sh`, and the staging-workload scripts all source it. It is kept out of `common.sh` and free of every helper in this directory so that each of them can take the predicate and nothing else — `hack/ci-env.sh` and `lifecycle.sh` want no part of the state file, and `installer_common.sh` is sourced by front doors that load no other helper. It sets `GKE_DNS_ENDPOINT_FLAG` rather than echoing, so that callers do not run it in a `$(...)` subshell that would discard its memo of whether the local gcloud offers the flag at all. That answer leaves it empty — as do a cluster with no externally reachable DNS endpoint and a describe call that fails — leaving today's IP-endpoint command untouched. `installer_common.sh` and `lifecycle.sh` fall back to a stub setting the same empty value when the file is absent, as `reconcile_environment.sh` does, so a tree without it reaches every cluster with a routable IP endpoint rather than refusing to run.
- **[min_versions.sh](min_versions.sh)**: minimum tool versions, side-effect-free so
  `install.sh` can source it standalone before any checkout exists.
- **[print_instructions_gchat.sh](print_instructions_gchat.sh)** /
  **[print_instructions_slack.sh](print_instructions_slack.sh)**: post-install manual-step
  instructions, printed by `install.sh` when the integration is enabled.
- **[../dev/dev_rebuild_agent.sh](../dev/dev_rebuild_agent.sh)**: fast local development utility
  that builds, pushes, and redeploys agent container images.
