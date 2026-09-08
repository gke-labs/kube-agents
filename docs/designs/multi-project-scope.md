# An Opt-In Multi-Project Scope for the Platform Agent

> **STATUS — design of record; not implemented.** Nothing below ships today. The Platform Agent
> discovers clusters in one GCP project, its service account holds roles in one project, and the
> architecture documents define it as one agent per project. This document proposes replacing that
> single project with a declared scope, and gives the order the change has to land in. Each section
> says what is true on `main` now and what the design changes.

**Scope:** Which GCP projects a single kube-agents install manages, how that set is declared,
resolved, granted, and kept current as projects come and go, and what in the codebase assumes there
is only one.
**Owns:** the scope model on the `PlatformAgent` resource, the discovery path that resolves it to
clusters, the IAM that makes the discovery readable, the membership snapshot and its drift signal,
and the sequencing. What a credential may do once it reaches a cluster belongs to
[`../credential-isolation-design.md`](../credential-isolation-design.md); the per-cluster service
account pool it would eventually feed is `terraform/modules/kube-agents-iam/scoped_pool.tf`; how
per-cluster profiles are scheduled once they exist is
[`spec-subagent-profiles.md`](spec-subagent-profiles.md).

---

## 1. The problem

The cluster profile sync is single-project by construction. `cluster_agent_reconcile.py`, the hourly
job that gives every GKE cluster a Cluster Agent profile, resolves exactly one project and lists it
exactly once:

- `_project()` (`agents/platform/scripts/cluster_agent_reconcile.py:87-96`) returns one string:
  `RECONCILE_PROJECT` from the environment, else the GCE metadata server's `project/project-id`,
  else `gcloud config get-value project`. All three answer "the project the management cluster runs
  in".
- `_all_clusters(project)` (`:99-132`) runs one `gcloud container clusters list --project <P>` and
  tags every row with that project.
- `reconcile()` (`:261-262`) calls it once. When the project cannot be resolved, or that one list
  call fails, the CREATE direction is skipped for the run. The hourly job then exits 0 in prune-only
  mode; only the bootstrap gate, which passes `--require-create-pass`, sees a non-zero exit. #566 is
  that path, with the broker refusing the list call.
- The header (`:7-10`) states the policy: "every cluster in the project gets a Cluster Agent
  profile". The only opt-out is `RECONCILE_EXCLUDE`, a list of cluster names (`:63`).

IAM matches the code. `terraform/modules/kube-agents-iam/main.tf:57-68` binds `project_roles` to the
agent's service account with `google_project_iam_member` in `var.project_id` and nowhere else, and
`terraform/examples/full-install/main.tf:229-234` passes the install's one `project_id`. Widening the
list call without widening IAM would produce a 403 per extra project, which `_all_clusters` reports
and then treats as "skip create this run".

The documents agree with both. `docs/architecture/01-vision-scope.md:75` gives the Platform Agent a
cardinality of "1 per project"; `02-agent-personas.md:280` says it is "scoped to its one project" and
"cannot read or reach another project"; `03-security-model.md:114` lists "any other project" under
what it is forbidden to touch; `06-api-and-data-contracts.md:82` keys the `platform` tier on a single
`projectId`. Single-project is the documented end-state, so this is a scope change to the
architecture, not a gap in the implementation of it.

The cost today is that an organisation with clusters in several projects installs kube-agents
several times: one management cluster, one operator, one Pub/Sub topic, one chat front door per
project, with no view across them. A question like "which of our clusters run a version behind" has
no single agent that can answer it.

**Prior art.** PR #588 added `--monitored-projects` to `install.sh` and per-project IAM to the
bash provisioning scripts. The provisioning scripts were replaced by Terraform in #797 while that
branch was open; a force-push then dropped the IAM code without porting it, and the branch was
closed on 2026-08-28 with a comment that `main` handles multi-project IAM and reconciliation
natively through Terraform and Helm. It does not: the IAM module above takes one project, and no
reconciler reads more than one. Epic #618 filed multi-project onboarding as its phase 3 and pointed it at #588.
#953 (the agent cannot route a request that does not name a cluster) and #1126 (the broker's read
allowlist withheld discovery reads a leaf read needs) are the same problem seen from the agent's
side: the fleet it can enumerate is narrower than the fleet it is asked about.

## 2. What already generalises

The profile model is project-qualified end to end, so multi-project discovery does not change how a
Cluster Agent is named, stored, or driven:

- Profile names are `cluster-{project}-{cluster}-{location}`, derived in `profile_name()`
  (`agents/platform/scripts/cluster_agent_profile.py:66`), and the profile's `config.yaml` carries a
  `cluster_identity: {project, cluster, location}` block (`:99`). `read_cluster_identity()` reads it
  back (`:103-124`).
- PRUNE works per stamped identity, not per resolved project: `_cluster_exists`
  (`cluster_agent_reconcile.py:135-163`) runs `describe --project=<identity.project>`, so a profile
  for a cluster in another project is verified against the right project today.
- `create_profile()` fetches credentials with `--project=<P>` (`cluster_agent_profile.py:235-243`).
- The credential broker passes `--project` through as a value-taking flag
  (`agents/platform/scripts/command_policy.py:392`), takes the project from the kubeconfig context
  name (`credential_proxy.py:1168-1190`), and re-issues `get-credentials` with the target's project
  (`:2496`). It does not pin a project. Only IAM stops a cross-project call.
- The scoped service account pool is already keyed on a per-row project. `scoped_clusters`
  (`terraform/modules/kube-agents-iam/variables.tf:71` onward) is a list of
  `{project_id, location, cluster_name}` objects, with the comment that "a cluster in another
  project is a row in this list rather than a second module"; the CRD mirror is
  `spec.security.scopedServiceAccounts[]` (`k8s-operator/api/v1alpha1/common_types.go:498-540`),
  whose `projectId` "need not be the project the agent runs in" (`:808-809`).

What changes is therefore confined to four places: how the set of projects is declared, how it is
resolved to clusters, how the service account is granted into it, and which documents describe the
boundary.

## 3. The scope model

A new block on `PlatformAgent`, `spec.scope`, declares an opt-in set. The name is provisional; there
is no `spec.fleet` or similar today; the top-level spec has `harness`, `integration`, `mode`,
`deployment`, `security`, `telemetry`, and `networkPolicy`.

```yaml
spec:
  scope:
    projects: # explicit project IDs
      - payments-prod
      - payments-staging
    folders: # Resource Manager folders, resolved to every project beneath them
      - folders/123456789012
    organizations: # an entire organisation; see §9 before using this
      - organizations/987654321098
    exclude:
      projects: # never resolved, even if a folder above contains them
        - payments-sandbox
      clusters: # one cluster, fully qualified; replaces RECONCILE_EXCLUDE
        - projectId: payments-staging
          location: us-central1
          clusterName: scratch-cluster
```

Rules:

- **Empty scope means today's behaviour.** Selectors are set when at least one of `projects`,
  `folders`, or `organizations` is non-empty. No `spec.scope`, `spec.scope: {}`, and a scope with
  only `exclude` populated all resolve to the management project alone, found the way `_project()`
  finds it now. Rendering is a separate question from resolution: the operator renders the scope
  file whenever any list in `spec.scope` is non-empty, `exclude` included, so an install that
  migrates only its exclusions still gets them applied.
- **The management project is always in scope.** It is the project the metadata server names,
  and it cannot be excluded, because the management cluster's own alerts need a profile to be
  delegated to (the reasoning in `cluster_agent_reconcile.py:17-27` still holds).
  `RECONCILE_PROJECT` is an override of that lookup, not a synonym for it: an install that runs
  with it pointed at another project today migrates by naming that project in `projects`, and the
  variable retires on the same schedule as `RECONCILE_EXCLUDE`.
- **Selectors union; exclusions subtract afterwards.** A project reached through a folder and named
  explicitly appears once. An excluded project is dropped whether it was reached through a list or a
  container.
- **`exclude.clusters` names one cluster, not one name.** Entries are the triple of `projectId`,
  `location`, and `clusterName` that `scopedServiceAccounts` already uses, because cluster names
  are unique only within a project and location; `prod` and `cluster-1` recur across a folder, and
  an exclusion prunes. `RECONCILE_EXCLUDE` is project-blind today: `cluster_agent_reconcile.py`
  compares the bare name against every stamped profile on the PVC (`:266`, `:296`), so a
  hand-onboarded cross-project namesake is already pruned by it. That is today's behaviour, not a
  hypothetical, and it is what the triple replaces. The variable keeps that project-blind meaning
  for one release, and nothing an operator relies on today is dropped by adding a scope: the script
  applies the union of the file's `exclude.clusters` and the variable's bare names for that
  release, logs every exclusion that came from the variable so the operator can move it to the
  triple, and the variable is then removed. This matters because the security page tells an operator on a
  `custom` role set to exclude the management cluster by that variable; a scope that silently
  ended the exclusion would recreate the one profile they were told to prevent. The places that
  name the variable as the opt-out (§8) change with it.
- **Resolution is deterministic.** The resolved project set is sorted before it is listed or written
  anywhere, so two runs against an unchanged fleet produce byte-identical snapshots (§5) and an
  unchanged roster.
- **A later selector, `sharedVpcHosts`.** Teams group projects by Shared VPC as often as by folder,
  and "every service project attached to host `H`" is answerable from the Compute API. It is
  deferred because a VPC is a network grouping, not a Resource Manager container: IAM cannot be
  granted on it, so §6's inheritance argument does not apply and every attached project would need
  its own binding. It fits the model as a fourth list once the first three work.

## 4. Resolution

Resolution turns the declared scope into a set of `(project, cluster, location)` tuples, plus a
per-project outcome. It runs inside the existing reconcile job under the agent's identity, through
the credential broker, because that is the only process in the install that talks to GCP on a
schedule and the operator deliberately holds no GCP credential.

**Explicit projects** use the call the script makes today, once per project:
`gcloud container clusters list --project <P> --format=value(name,location)`.

**Folders and organisations** use Cloud Asset Inventory rather than walking the tree:

```bash
gcloud asset search-all-resources \
  --scope=folders/123456789012 \
  --asset-types=container.googleapis.com/Cluster \
  --format='value(name,location)'
```

One call returns every cluster under the container, including in projects created since the last
run, and needs `roles/cloudasset.viewer` on the container plus the Cloud Asset API enabled in the
host project only. The project ID is the segment after `projects/` in the asset `name`, and the location is the
`location` field, not the path segment: regional clusters render as
`//container.googleapis.com/projects/<ID>/locations/<L>/clusters/<C>` and zonal ones as
`.../projects/<ID>/zones/<Z>/clusters/<C>` (both measured), so a parser written to one shape drops
the other with the container still reading `ok`. The ID is not read from the
`project` field: that field carries the project _number_ (`projects/757207957170`, measured against
`bhoekstra-gkedemos`), and a cluster keyed by number would get a second profile beside the one its
explicit project ID produces, and would never match an `exclude.projects` entry. Everything
downstream keys on the ID.

There is no second resolver. The composition already owns host-project API enablement
(`google_project_service.required` in `terraform/examples/full-install/main.tf`), and
`cloudasset.googleapis.com` joins that list, so "Asset API not enabled" is not a state an install
can be in. A Resource Manager walk (`projects list` per folder, recursing) was considered and
dropped: it is one call per folder plus one per project, needs `resourcemanager.folders.list` and
`resourcemanager.projects.list` at the container on top of the viewer roles, and `parent.id`
matches the immediate parent only, so a walk that stops early misses every project in a sub-folder
silently. An organisation policy that forbids the Asset API is an open question (§11), not a code
path.

The discovery verb is absent from the broker's read allowlist. `GCLOUD_READ_COMMANDS`
(`command_policy.py:277-377`) admits `container clusters list` and `projects list` but no `asset`
command. This is the class of gap #1126 describes: a discovery read the leaf reads depend on,
refused fail-closed with no signal. Adding `("asset", "search-all-resources")` is part of phase 2, with the resolver that needs it,
and so is adding `--scope` and `--asset-types` to `_GCLOUD_FLAGS_WITH_VALUE`: the broker refuses a
flag it does not know the arity of before it matches the command path, so a verb whose flags are
not listed is admitted and unreachable at once, which the set's own comment records as having
happened to `logging read`.

**Every project gets an outcome, and no outcome is silent.** For an explicit project the outcome
comes from its `clusters list`. For a project reached through a container, Asset Inventory has
listed its clusters without any per-project call, so the outcome starts as `ok` and is revised by
the two per-cluster calls the run already makes: a 403 from CREATE's `get-credentials` or from
PRUNE's `describe` for any cluster in the project sets the project to `denied` (an IAM deny policy
on a member project blocks the inherited grant without hiding the cluster from the asset index).
PRUNE is the one that matters in the steady state, because CREATE runs only for clusters without a
profile, and a binding revoked after every profile exists would otherwise never be observed. The
`create_failed` and `skipped_error` buckets the script already keeps name the clusters. The
outcome is one of:

| Outcome        | Meaning                                                     | Effect on profiles                          |
| -------------- | ----------------------------------------------------------- | ------------------------------------------- |
| `ok`           | Listed; zero or more clusters returned                      | CREATE runs for its clusters                |
| `denied`       | 403: the service account is not granted in this project     | Existing profiles kept; CREATE skipped      |
| `api-disabled` | `container.googleapis.com` is off in this project           | Treated as zero clusters; nothing to manage |
| `unreachable`  | Timeout, network, quota, or a `gcloud` error not classified | Existing profiles kept; CREATE skipped      |

**Every container gets the same outcome, and a container that is not `ok` freezes its members.**
A folder or organisation whose resolution call failed (`denied`, `api-disabled` on the Asset API
with no working fallback, `unreachable`) has produced no project list, and "no projects" and
"could not list projects" must not read the same. For a container that is not `ok` the run carries
its member projects forward from the previous snapshot, skips CREATE for them, and prunes nothing
under it. Without this rule one failed folder lookup would make every project beneath it "out of
scope" and §7's prune would delete every profile under the folder in a single tick, which is the
one thing `cluster_agent_reconcile.py:11-15` exists to never do.

`denied` and `unreachable`, for projects and containers alike, are counted in the report the job
already prints (`report` at `cluster_agent_reconcile.py:228-236`). The report already carries one
bit of this kind, `create_pass_ran`, and the bootstrap gate already acts on it: it runs the script
with `--require-create-pass`, treats a non-zero exit as "roster not reconciled", and retries up to
a ceiling. Per-project outcomes extend that from one bit for the whole run to one per project, so
the gate can hand the sweep a roster that is partial in a named way rather than a roster it can
only call reconciled or not. This is the lesson of #566: a project the agent was told to manage and
cannot list is a finding, and folding it into an empty list turns a permission gap into a clean
fleet.

## 5. Where the resolved membership lives

The `PlatformAgent` resource carries the declaration only. The resolved set lives with the
profiles, on the data PVC, in a snapshot the reconcile run rewrites every hour:

```json
{
  "resolvedAt": "2026-09-03T14:11:07Z",
  "declared": { "projects": [...], "folders": [...], "organizations": [...], "exclude": {...} },
  "resolver": "asset-inventory",
  "containers": [
    { "id": "folders/123456789012", "outcome": "ok", "projects": 3 }
  ],
  "projects": [
    { "id": "payments-prod", "via": ["folders/123456789012"], "outcome": "ok", "state": "in-scope", "clusters": 4 },
    { "id": "payments-staging", "via": ["explicit"], "outcome": "denied", "state": "in-scope", "clusters": null },
    { "id": "payments-legacy", "via": [], "outcome": "ok", "state": "retiring", "clusters": 1 }
  ],
  "unmanaged": [
    { "profile": "cluster-shared-tools-ci-us-east1", "project": "shared-tools", "reason": "never in scope" }
  ]
}
```

A project entry carries two fields that answer different questions. `outcome` (§4) says whether
the run could read the project this tick. `state` says what the declaration wants: `in-scope` for
a project the current scope resolves, `retiring` for one the scope has dropped and whose profiles
§7 is still removing. `unmanaged` is a separate list, per profile rather than per project, of
profiles on the PVC whose project the scope never produced.

Today the roster is the set of profile directories under `$HERMES_HOME/profiles/`, read by the
bootstrap gate (`agents/chat/scripts/bootstrap_scan_gate.py`) through one `hermes profile list`
call (`_roster_command()`, `:148`). The gate keeps reading that; the snapshot sits beside it as
`$HERMES_HOME/fleet_scope.json` and the gate's instructions to the sweep worker name any project
whose outcome is not `ok`, so a partial roster is reported as partial rather than audited as
complete.

The operator renders `spec.scope` to the pod the way it renders other agent configuration, as a
mounted file rather than an environment variable: the lists are unbounded and the CRD already
carries `spec.deployment.env` (`common_types.go:368-372`) only as a generic passthrough. The
rendered file's hash joins the ConfigMap hash that rolls the agent workload, so editing the scope
takes effect at the next pod start and the next reconcile tick, whichever is later.

Whether the snapshot should also be lifted into `.status` is open (§11). It would make `kubectl get
platformagent -o yaml` answer "which projects does this install manage" without a pod exec, but the
pod has no channel to the operator today and building one for this alone is out of proportion.

## 6. IAM

`kube-agents-iam` gains a `scope` input mirroring `spec.scope`, and the full-install composition
generates it from the same `terraform.tfvars` the installer front doors already write. Grants
follow the selector type:

- **Host project.** Unchanged: `google_project_iam_member` for each role in `project_roles`.
- **Explicit project.** `google_project_iam_member` for each role in `scope_roles`, in that
  project.
- **Folder.** `google_folder_iam_member` for each role in `scope_roles`, plus
  `roles/cloudasset.viewer`, on the folder.
- **Organisation.** `google_organization_iam_member`, same roles, on the organisation.

`scope_roles` is a fixed allowlist of read roles intersected with `project_roles`, never
`project_roles` itself, and it is what every grant outside the host project carries, whether the
project was named or reached through a container. The allowlist is `container.clusterViewer`,
`container.viewer`, `compute.viewer`, `monitoring.viewer`, `logging.viewer`, and
`iam.securityReviewer`: the read roles in the list the composition binds, `local.read_only_roles`
in `terraform/examples/full-install/main.tf`, which the module default
(`terraform/modules/kube-agents-iam/variables.tf:59-68`) mirrors. The intersection matters on the
`custom` permission set, where the operator names `project_roles` outright: a list that carries
`roles/container.admin` for the host project must not carry it to another project, where
`container.clusters.impersonate` would apply to every cluster, and a
quota-consuming role such as `roles/serviceusage.serviceUsageConsumer` must not consume quota in
projects the agent only reads. Widening `project_roles` widens the host project alone; widening
what the scope carries is an edit to the allowlist, in one file, on purpose.

The default roles outside the allowlist are outside it by design, and so is any role a later
change adds to the default list that is not a read role. `roles/iam.serviceAccountUser` is
`iam.serviceAccounts.actAs`, held so the agent can run jobs as service accounts in its own project;
inherited across a folder it would let the one agent identity act as every service account in every
project beneath, including ones created tomorrow. `roles/mcp.toolUser` lets the agent call the GKE
MCP server; whether that check runs in the host project or in the project a call targets is not
measured here, so it stays host-only until it is (§11).

Inheritance is the point of offering containers at all. A folder-level binding reaches every
project beneath it, including one created tomorrow, so onboarding a new project under a declared
folder is zero-touch: it appears at the next hourly reconcile with no change to the CR, the tfvars,
or the IAM. That is the answer to "maintaining the list over time": the list is a container, and
GCP maintains it.

The same inheritance widens the blast radius of the one service account that holds these roles,
which §9 takes up.

Prerequisites the design has to state and the installer has to preflight:

- The identity running Terraform needs `resourcemanager.folders.setIamPolicy` on each folder, or
  `resourcemanager.organizations.setIamPolicy` for an organisation. Today it needs only
  project-level IAM admin. The installer's preflight reports which containers it cannot bind rather
  than failing on the first.
- A project in scope with `container.googleapis.com` disabled resolves to zero clusters (§4's
  `api-disabled`); Terraform must not enable the API in other people's projects.
- `project_roles` stays the list bound in the host project, and the mirror between it and
  `read_only_roles` that `tests/test_scoped_sa_pool_iam.py` checks is unchanged. The `scope_roles`
  allowlist lives beside it with a test that every entry is also in the default `project_roles`,
  so the allowlist cannot name a role the agent does not otherwise hold.

Uninstall revokes what install granted: `terraform destroy` removes the bindings because Terraform
owns them, which is the property #588 lost when its revocation lived in a bash function.

## 7. The onboarding lifecycle

**Adding a project.** Under a declared folder or organisation: nothing to do; it is discovered at
the next tick. As an explicit project: add it to `scope.projects` in the tfvars and run
`upgrade.sh`, which binds the IAM and renders the CR from the same value (a hand-applied CR is
edited separately, and §11 says why that split is the weak point). The binding then exists before
the reconcile tries the list, and the project's
outcome goes from `denied` to `ok` at the following tick. The order matters and the snapshot shows
it: a project added to the CR before Terraform has run reads `denied`, which is correct and visible,
not an error to suppress.

**Removing a project from scope.** Its clusters' profiles are pruned the way `RECONCILE_EXCLUDE`
prunes a cluster today, on the strength of the declaration rather than of a cloud error. The rule
has three conditions, all required: the project is absent from this run's resolved set; every
declared container resolved `ok` this run, so that the absence is the declaration speaking and not
a failed lookup (§4); and the project was present in the previous snapshot's resolved set, so that
removal is a transition the scope made and not a state it merely finds. The third condition is what
protects profiles the scope never produced. The `manage-cluster` skill onboards a cluster with an
explicit `--project` today, and those profiles exist on installs that will upgrade into phase 1
with an empty scope; without it, the first tick would delete every one of them, which is the
deletion `cluster_agent_reconcile.py:11-15` exists to never do. A profile whose project is outside
the scope and was never in it is kept, verified by PRUNE against its own project as today, and
listed in the snapshot's `unmanaged` array (§5) so the operator can declare it or delete it. A
project the rule has decided to retire is written to the snapshot with `state: retiring` and stays there, still
eligible for the prune, until every one of its profiles is gone; otherwise a delete that failed on
the one tick the third condition held would leave the profile `unmanaged` for good. A project that
became `denied` because a binding was revoked without editing the scope is not pruned; the
profiles stay, the outcome is reported, and an operator resolves it one way or the other.

**A project that disappears.** Deleted, or moved out from under a declared folder: its clusters
stop appearing in the resolved set, and PRUNE's per-profile `describe --project=<P>` now returns a
403, because the folder binding no longer covers it, which `_cluster_exists` classifies as unknown
and keeps (`cluster_agent_reconcile.py:135-163`). It is the out-of-scope rule above, not NotFound,
that retires those profiles, and only once the containers have resolved `ok`. Moved to a different
declared folder: no change, because resolution is by project and the `via` field merely records
the new path.

**Never on ambiguity.** The rule at `cluster_agent_reconcile.py:11-15` holds: auth, network, quota,
and unclassified errors leave profiles untouched.

## 8. Everything else that assumes one project

Discovery and IAM are the mechanism; these are the places that will read wrong once the mechanism
works. Each is listed with whether it blocks the first phase or follows it.

| Where                                                                                                                                                                                                                                                                            | What it assumes                                                                                                                                                                                                                                                 | Phase |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `agents/platform/scripts/session_kv_server.py:1155`                                                                                                                                                                                                                              | `GCP_PROJECT_ID` is the project for every event's console links                                                                                                                                                                                                 | 1     |
| `agents/platform/scripts/platform_mcp_server.py:275-300`                                                                                                                                                                                                                         | `get_project_id()` reads one `project:` line from `USER.md`                                                                                                                                                                                                     | 1     |
| `agents/platform/skills/cluster-agent-lifecycle/SKILL.md`                                                                                                                                                                                                                        | Delegation needs `--project` from the requester; #953 asks for enumeration first                                                                                                                                                                                | 1     |
| `terraform/modules/drift-pubsub`                                                                                                                                                                                                                                                 | One log sink in `var.project_id`; other projects' audit logs need a sink each into the host topic                                                                                                                                                               | 2     |
| Fleet-audit SOPs and the cost, recommender, and compliance skills                                                                                                                                                                                                                | Query "the project" for quotas, recommendations, and IAM; need to iterate the snapshot                                                                                                                                                                          | 2     |
| `docs/site/src/content/docs/concepts/cluster-agents.md:24`                                                                                                                                                                                                                       | "sweeps the project"                                                                                                                                                                                                                                            | docs  |
| `docs/site/src/content/docs/reference/security-and-iam.md:84`                                                                                                                                                                                                                    | "an IAM role grants privileges across all clusters in the project" becomes "in the scope"                                                                                                                                                                       | docs  |
| `docs/site/src/content/docs/reference/credential-isolation.md:218`, `k8s-operator/api/v1alpha1/common_types.go:708` (rendered into both CRD YAMLs)                                                                                                                               | Describe the metadata lookup, with `RECONCILE_PROJECT` as its override, as how the script finds its one project; phase 1 because the override retires with `RECONCILE_EXCLUDE`                                                                                  | 1     |
| `docs/site/src/content/docs/reference/security-and-iam.md:28`, `agents/platform/skills/manage-cluster/SKILL.md:41`, `agents/platform/skills/cluster-agent-lifecycle/SKILL.md:80`, `agents/platform/governance/inventory.md:70`, `agents/chat/scripts/bootstrap_scan_gate.py:331` | Name `RECONCILE_EXCLUDE`, a bare cluster name matched project-blind, as the opt-out; becomes `spec.scope.exclude.clusters`. Phase 1 rather than docs because the variable is deprecated in that release and these pages must point at the triple before it goes | 1     |

Event delivery from other projects is the largest of these. The event watcher watches through each
profile's kubeconfig and already labels every metric with `project` and `location`, so Kubernetes
events fan in as soon as profiles exist. Reaching a private cluster in another VPC needs the DNS
endpoint, which `create_profile()` already selects per cluster; that is why cross-project reach
works, not an assumption that breaks. Cloud audit-log drift,
which `drift-pubsub` exports through a log sink, is per project by construction; a Shared VPC or a
folder-level aggregated sink can replace N per-project sinks, and that is its own design.

## 9. The boundary changes, and what does not

The architecture documents move from "its one project" to "its declared scope":

- `01-vision-scope.md:75` and `:121`: cardinality becomes "1 per scope (one or more projects)".
- `02-agent-personas.md:16`, `:31`, `:262`, `:280-282`, `:477`: the persona is scoped to the projects
  in `spec.scope`, and the containment sentence becomes "it cannot read or reach a project outside
  its declared scope".
- `03-security-model.md:114`, `:123`, and `:381`: the forbidden column and the containment sentence
  read "any project outside its scope".
- `06-api-and-data-contracts.md:82`: the `platform` tier's scope field becomes the resolved project
  set, with `projectId` kept as the management project.

What does not change: read-only stays read-only. Every grant outside the host project carries the
`scope_roles` allowlist of §6 and nothing else; the non-viewer roles in `project_roles`
(`iam.serviceAccountUser`, `mcp.toolUser`, and whatever a `custom` set adds) stay in the host
project, and nothing here grants a write anywhere. What does change is how much one credential can
read. The
agent's service account carries `roles/container.viewer`, which "lets an identity read Kubernetes
objects in every cluster in the project" (`kube-agents-iam/main.tf:30-31`); bound on a folder it
reads every cluster in every project beneath, and the `asset search-all-resources` allowlist entry
lets the agent, not only the reconcile job, read the metadata of every resource type in the
container's asset index, since the allowlist matches the verb and not its arguments. Both are
reads, and both are wider than today. That is the argument for landing the scoped service
account pool's authority (`scoped_pool.tf`, currently granting nothing) before offering
`organizations` in a release: a per-cluster credential bounds what a compromised sandbox reads to
one cluster regardless of how wide discovery is. Until then the design recommends `projects` and
`folders` for a fleet an operator would be comfortable reading with one account, and documents
`organizations` as available but wide.

### A scope is one trust domain, by operator assertion

The widening above is about what a credential may read. The scope change also decides what
happens to what it has read, and the design's answer is that declaring a scope is the
operator asserting these projects may share findings.

Cross-project reach is not itself new: the broker never pinned a project (§2), the
`manage-cluster` skill takes `--project` today (§7), and §5's own snapshot example carries
a profile in another project. What the stock IAM grant did was make an operator arrange
each one deliberately. A scope makes reading across projects the supported default, and
the aggregation stage is where that lands — the sweep fans out per cluster, so each Cluster
Agent's context holds one, but the roll-up in the Platform Agent holds all of them and
nothing there keeps them apart. For the case this document motivates, one organisation and
several of its own projects, that is what the operator wants. For a scope drawn across
projects whose findings should not meet, it is not.

The check is one sentence: _every project in this scope may see every other project's
findings._ An install that cannot say yes wants two Platform Agents with disjoint scopes,
which is where §11's cardinality question also points — though disjointness is a
declaration too, and nothing prevents two installs from declaring overlapping scopes.

Nothing in `spec.scope` verifies any of this. The platform enforces the scope; the trust
boundary is the operator's claim that the scope is drawn where the trust is. Where those
diverge, the divergence is silent.

## 10. Implementation order

Each step is shippable alone and live-testable on a shared install by granting its service account
into a second project the tester controls.

1. **Explicit projects.** `spec.scope.projects` and `spec.scope.exclude` on the CRD; the operator
   renders the scope file whenever any `spec.scope` list is non-empty; `cluster_agent_reconcile.py` iterates the list,
   applies the three-condition prune, and writes `fleet_scope.json` with per-project outcomes and
   `unmanaged` and `retiring` entries; `kube-agents-iam` binds `scope_roles` per explicit project;
   the bootstrap
   gate names non-`ok` projects; `session_kv_server.py` and `platform_mcp_server.py` read the
   project from the event or the profile identity rather than one environment value; the
   `RECONCILE_EXCLUDE` mentions §8 lists point at the new field. This is the smallest change that
   manages two projects from one install.
2. **Folders and organisations.** Asset Inventory resolution, its allowlist entry, and its two value flags;
   `cloudasset.googleapis.com` in the composition's API list; container outcomes and the freeze
   rule; folder- and organisation-level bindings of `scope_roles` plus `roles/cloudasset.viewer`;
   the installer preflight for container IAM permissions; `via` and `containers` in the snapshot.
3. **Downstream consumers.** The phase-2 rows of §8: audit-log sinks per project or an aggregated
   sink, and the fleet-audit SOPs and cost skills iterating the snapshot.
4. **Documents.** The architecture edits in §9 and the site pages in §8, in one PR once
   phase 1 has merged, so the documents describe what runs.
5. **Shared VPC selector.** After the first three selectors have been used by someone other than
   the author.

## 11. Open questions

- **Snapshot in `.status`?** §5 keeps the resolved membership on the PVC because the pod has no
  channel to the operator. If one arrives for another reason, the snapshot should ride it.
- **Cardinality at organisation scale.** One Platform Agent for an organisation of hundreds of
  projects means one chat front door, one reconcile job, and one hourly sweep for all of them. The
  reconcile's per-profile `describe` in PRUNE is already O(clusters); at what fleet size does an
  install want two Platform Agents with disjoint scopes, and does anything need to prevent overlap?
- **A ceiling on projects per install.** The CRD caps `scopedServiceAccounts` at 100 entries; the
  scope lists should carry a cap for the same reason, and the number is a guess until phase 2 runs
  against a real folder.
- **Deriving `scopedServiceAccounts` from scope.** Once the pool grants authority, hand-listing
  every cluster in `spec.security.scopedServiceAccounts` duplicates what resolution already found.
  Terraform cannot read the snapshot, so either the pool moves to per-project accounts or the
  snapshot becomes a Terraform input through a data source; neither is settled.
- **`mcp.toolUser` across projects.** If the GKE MCP server checks `roles/mcp.toolUser` in the
  project a call targets rather than in the caller's project, MCP-backed reads of a scoped project
  fail while `gcloud` reads succeed, and the role has to join `scope_roles`. One call against a
  second project settles it.
- **An organisation policy against the Asset API.** §4 has one resolver and assumes the host
  project can enable `cloudasset.googleapis.com`. An organisation that forbids it would need the
  Resource Manager walk §4 rejected, or would be told folders and organisations are unavailable to
  it. Which of those is right depends on whether such a policy exists among the installs that want
  this.
- **Who may widen the scope.** Editing `spec.scope` is a Kubernetes RBAC question on the
  management cluster; granting into a folder is a GCP IAM question. They are enforced by different
  systems and can disagree. The design assumes the tfvars is the source of both and the CR is
  rendered from it on the installer path, which holds for `install.sh` and not for a hand-applied
  CR.
