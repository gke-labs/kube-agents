# An Opt-In Multi-Project Scope for the Platform Agent

> **STATUS — design of record; phase 1's mechanism is implemented: `spec.scope` on the CR, the operator's rendering of it, the reconcile's per-project outcomes and `fleet_scope.json` snapshot, the bootstrap gate's reading of it, and the event console links. Step 1's IAM bindings, installer path, the chart's rendering of `spec.scope` and the `platform_mcp_server.py` change, and steps 2 to 5, do not ship yet.** Without a declared `spec.scope` the Platform Agent discovers clusters in one GCP project, its service account holds roles in one project, and the
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
  (`_GCLOUD_FLAGS_WITH_VALUE` in `agents/platform/scripts/command_policy.py`), takes the project from the kubeconfig context
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
is no `spec.fleet` or similar today; the top-level spec has `harness`, `integration`, `mode`, `deployment`, `security`, `telemetry`,
`networkPolicy`, and now `scope`.

```yaml
spec:
  scope:
    projects: # explicit project IDs
      - payments-prod
      - payments-staging
    folders: # Resource Manager folder IDs (numeric), resolved to every project beneath them
      - "123456789012"
    organizations: # organisation IDs (numeric)
      - "987654321098"
    exclude:
      projects: # IDs or shell-style globs; dropped after resolution, even if a folder above contains them
        - payments-sandbox
        - "*-sandbox"
      clusters: # one cluster, fully qualified; replaces RECONCILE_EXCLUDE
        - projectId: payments-staging
          location: us-central1
          clusterName: scratch-cluster
```

Rules:

- **Empty scope means today's behaviour.** Selectors are set when at least one of `projects`,
  `folders`, or `organizations` (and, once phase 3 lands, the two selectors defined below) is non-empty. No `spec.scope`, `spec.scope: {}`, and a scope with
  only `exclude` populated all resolve to the management project alone, found the way `_project()`
  finds it now. Rendering is a separate question from resolution: the operator renders the scope file on every install, marked absent when the CR has no scope block and present with empty lists when the block is there and empty, so the reconcile can tell three things apart: a present block whose `projects` is empty is the declaration that drops projects and is the one case that prunes; an absent block declares nothing and marks nothing newly `retiring` (§7; a mark an earlier present block made still counts); and a scope file the pod cannot read at all reads as no declaration and as an unclean run, so nothing is marked or pruned. An install that migrates only its exclusions gets them applied like any other declaration.
- **The management project is always in scope.** It is the project the metadata server names,
  and it cannot be excluded, because the management cluster's own alerts need a profile to be
  delegated to (the reasoning in `cluster_agent_reconcile.py:17-27` still holds).
  `RECONCILE_PROJECT` is an override of that lookup, not a synonym for it: an install that runs
  with it pointed at another project today migrates by naming that project in `projects`. The operator pins the variable empty in the managed `.env` from phase 1 on, because a management identity that changes now retires the old project's profiles and a line in the agent-writable PVC `.env` must not be able to move it; the empty value reads as unset.
- **Selectors union; exclusions subtract afterwards.** A project reached through a folder and named
  explicitly appears once. An excluded project is dropped whether it was reached through a list or a
  container.
- **`exclude.projects` entries are project IDs or shell-style globs.** A glob (`*-sandbox`) is
  matched against the resolved project ID with `fnmatch`, after every selector has contributed.
  Globs are deterministic, so §5's byte-identical snapshot rule holds. An entry, ID or glob, that
  matches the management project does not exclude it, by the rule above; the run keeps the project, logs the
  match, and records the first such entry under the snapshot's `ignoredExcludes` (§5), because a silently ignored
  exclusion is the kind of outcome §4 forbids. Decided 2026-09-21, from the first enterprise request for this feature.
- **The resolved set is the management project plus every project a selector produced and no
  exclusion removed, each with an outcome.** Exclusions subtract before anything else is
  counted, so an excluded project is outside the set exactly as a project no selector produced
  is: both are the declaration speaking, and §7 retires their profiles under the same three
  conditions. A project in the set is never "absent" from it in §7's sense, whatever its outcome
  says. Two orders apply to it: the fill order below decides which projects the cap
  lets the run list, and the sorted order of the "Resolution is deterministic" rule decides how
  the snapshot is written.
- **Two caps of 100, enforced in different places.** Each declared list (`projects`, `folders`,
  `organizations`, both `exclude` lists, and the phase 3 selectors when they land) carries
  `MaxItems=100` on the CRD, so an oversized declaration is refused at admission. That cap outlives the pool's move to per-project accounts (§6) for a reason of its own: a hand-written list past a hundred entries is the shape containers exist for, and the cap is where the CRD says so; an estate with more than a hundred explicit projects declares folders rather than raising anything. The reconcile lists at most a cap's worth of projects of the resolved set, the management project included, because one folder can resolve to any number. On `main` the cap is the fixed constant `RESOLVED_SET_CAP`, 100; making it a declared value, `spec.scope.maxProjects` with 100 as its default and the listing budget (§4) scaling with it rather than staying fixed, is the follow-up §10 step 2 names. The default sits below the estate that asked for this design, about 200 projects with one cluster each grouped by folder (#1354), and that install declares its folders and a higher cap rather than splitting in two; an estate that wanted two hundred explicit projects is refused at admission and declares folders instead; what bounds an install above the default is the pod, not the reconcile (§11). The set is
  filled in a fixed order so the cap binds the same way on every run: the management project, then
  explicit projects sorted by ID, then the phase 3 selectors' projects sorted by ID, then
  containers sorted by ID; a container that does not fit is skipped and the next one is still tried,
  and the members of a frozen container carry the container's outcome for the run (§4), never the `ok` they may have read last hour. An explicit project beyond the cap (100 by default) stays in the set and reads
  `over-cap` as its project outcome (§4: profiles kept, CREATE skipped); a container whose members would cross the cap reads `over-cap` as its container outcome: its members are carried forward reading `over-cap` and get no CREATE, as under §4's freeze, but because the lookup itself succeeded and the run
  holds the full member list, `over-cap` does not hold back §7's prune the way a failed lookup
  does. Nothing is truncated silently and the snapshot
  stays deterministic. Decided 2026-09-21 as two fixed caps of 100; the resolved-set cap became a declared value on 2026-09-23, when the estate on #1354 turned out to be twice the fixed number.
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
- **Resolution is deterministic.** The resolved project set is sorted by ID before it is written
  anywhere (listing follows the fill order above), so two runs against an unchanged fleet produce byte-identical snapshots (§5) and an
  unchanged roster.
- **Two later selectors, `sharedVpcHosts` and `metricsScopes`.** Teams group projects by Shared
  VPC as often as by folder, and "every service project attached to host `H`" is answerable from
  the Compute API; a Monitoring Metrics Scope's monitored-project list answers the same question
  for teams that group by observability. Both come after folders because neither is a Resource
  Manager container: IAM cannot be granted on a VPC or a Metrics Scope, so §6's inheritance
  argument does not apply and every project they reach needs its own binding. They resolve to explicit projects, which then take the phase 1 path for IAM and creation; the lookup itself is a runtime lookup like a container's, so one that fails freezes the selector's members and the prune under §4's rule, and its outcome sits in the snapshot's `containers` array (§5). §10 places them.

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

Cloud Asset Inventory is the only resolver this design builds (decided 2026-09-21). The composition already owns
host-project API enablement (`google_project_service.required` in
`terraform/examples/full-install/main.tf`), and `cloudasset.googleapis.com` joins that list when a
folder or organisation is declared and not otherwise: an install that names explicit projects only
never calls the Asset API, and must not fail under an organisation policy that forbids it. Where a
container is declared, the installer preflights, before the apply and with the identity running
Terraform, that the API can be enabled in the host project and that this identity can set IAM
policy on the container; the agent's own `roles/cloudasset.viewer` is bound by the apply that
follows. A policy that forbids the API is reported by name (§6, §10). A Resource Manager walk (`projects list` per folder, recursing into every
sub-folder) was considered and dropped from this design: it is one call per folder plus one per
project, needs `resourcemanager.folders.list` and `resourcemanager.projects.list` at the
container on top of the viewer roles, and `parent.id` matches the immediate parent only, so a
walk that stops early misses every project in a sub-folder silently. That full recursive walk is
the fallback for an installation whose policy forbids the Asset API, filed and built when such an
installation appears rather than ahead of one; the snapshot's `resolver` field (§5) exists so the
two can be told apart.

The discovery verb is absent from the broker's read allowlist. `GCLOUD_READ_COMMANDS` in
`command_policy.py` admits `container clusters list` and `projects list` but no `asset`
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

| Outcome        | Meaning                                                                             | Effect on profiles                     |
| -------------- | ----------------------------------------------------------------------------------- | -------------------------------------- |
| `ok`           | Listed; zero or more clusters returned                                              | CREATE runs for its clusters           |
| `denied`       | 403: the service account is not granted in this project                             | Existing profiles kept; CREATE skipped |
| `api-disabled` | `container.googleapis.com` is off in this project                                   | Existing profiles kept; CREATE skipped |
| `unreachable`  | Timeout, network, quota, or a `gcloud` error not classified                         | Existing profiles kept; CREATE skipped |
| `over-cap`     | Beyond the cap on the projects the run lists (§3; 100 by default); stays in the set | Existing profiles kept; CREATE skipped |

A container carries the same vocabulary with one shift in meaning: `api-disabled` on a container
is the Asset API rather than `container.googleapis.com`, and every container outcome other than
`ok` freezes the container's members, as the next paragraph says; an `over-cap` container has not
failed its lookup and freezes only its members.

**Every container gets the same outcome, and a container whose lookup failed freezes its members
and the prune.** A folder or organisation whose resolution call failed (`denied`; `api-disabled`
on the Asset API, which the preflight catches at install time and this rule catches when a policy
lands afterwards; `unreachable`) has produced no project list, and "no projects" and "could not
list projects" must not read the same. For such a container the run carries its member projects
forward from the previous snapshot (on a first run there is none, so the container contributes no members until a lookup succeeds), writes each of them with the container's outcome for the run rather than the `ok` it read last hour, so that the table above holds for them (CREATE skipped) and the bootstrap gate's rule in §5, name every project whose outcome is not `ok`, reports them as not covered rather than as a complete roster, skips CREATE for them, and prunes nothing anywhere (§7). The rule is about a lookup that failed, not about Resource Manager: the phase 3 `sharedVpcHosts` and `metricsScopes` selectors (§3) are not containers, but each is a runtime lookup that can fail the same way, so each appears in the snapshot's `containers` array under its `via` name with the same outcome vocabulary, freezes its members on a failed lookup, and holds back the prune with the containers in §7's second condition. Without this rule one failed folder lookup would make every project beneath it "out of
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
  "ignoredExcludes": [{ "project": "ops-mgmt", "pattern": "ops-*" }],
  "containers": [
    { "id": "folders/123456789012", "outcome": "ok", "projects": 3 }
  ],
  "projects": [
    { "id": "ops-mgmt", "via": ["management"], "outcome": "ok", "state": "in-scope", "clusters": 1 },
    { "id": "payments-prod", "via": ["folders/123456789012"], "outcome": "ok", "state": "in-scope", "clusters": 4 },
    { "id": "payments-staging", "via": ["explicit"], "outcome": "denied", "state": "in-scope", "clusters": null },
    { "id": "payments-legacy", "via": [], "outcome": "ok", "state": "retiring", "clusters": 1 }
  ],
  "unmanaged": [
    { "profile": "cluster-shared-tools-ci-us-east1", "project": "shared-tools", "reason": "never in scope" }
  ],
  "profiles": {
    "cluster-ops-mgmt-kube-agents-us-central1": "ops-mgmt",
    "cluster-payments-legacy-api-us-east1": "payments-legacy",
    "cluster-shared-tools-ci-us-east1": "shared-tools"
  }
}
```

`resolver` names how containers were resolved: `explicit` when no container is declared, which is
the only value phase 1 writes, `asset-inventory` once a folder or organisation is, and a fallback resolver,
if one is ever built, names itself here. It says nothing about the other selectors: each project's
`via` carries its source, and the phase 3 selectors record theirs there (§10). The management project's `via` is `["management"]`; an explicit project's is `["explicit"]`;
a project a container produced names the container. `containers` lists every selector the run resolved at runtime, with its outcome and member count: the declared folders and organisations, and in phase 3 each `sharedVpcHosts/<host>` and `metricsScopes/<scope>` entry under its `via` name, so that a failed lookup of any of them is visible where the freeze rule (§4) and §7's second condition read it. `ignoredExcludes` lists every `exclude.projects` entry that matched the management project and was not applied (§3), with the project and the pattern, so an
exclusion the run declined to honour is visible in the snapshot rather than only in a log line.
`profiles` maps every profile on the volume to its project as the run read it, or as the last run
that could read it did: a profile whose `cluster_identity` cannot be read this run is attributed
through this map, so a `retiring` project whose remaining profile is unreadable stays `retiring`
rather than leaving the snapshot and reading as never in scope once the identity is readable again.

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
- **Shared VPC host and Metrics Scope selectors (phase 3).** Resolved at plan time and bound as
  explicit projects; nothing is inherited through either.

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
folder is zero-touch for discovery and IAM: it appears at the next hourly reconcile with no change to the CR, the tfvars, or the IAM. With the pool armed (below), its account arrives with the next apply, and its clusters are refused by the broker until then. That is the answer to "maintaining the list over time": the list is a container, and
GCP maintains it.

The same inheritance widens the blast radius of the one service account that holds these roles,
which §9 takes up.

Prerequisites the design has to state and the installer has to preflight:

- The identity running Terraform needs `resourcemanager.folders.setIamPolicy` on each folder, or `resourcemanager.organizations.setIamPolicy` for an organisation; with the pool armed it also lists the container's projects at plan time, which needs `cloudasset.googleapis.com` searchable and `roles/cloudasset.viewer` on the container for that identity too. Today it needs only
  project-level IAM admin. The installer's preflight reports which containers it cannot bind rather
  than failing on the first.
- A project in scope with `container.googleapis.com` disabled resolves to zero clusters (§4's
  `api-disabled`); Terraform must not enable the API in other people's projects.
- When a folder or organisation is declared, the identity running Terraform can enable
  `cloudasset.googleapis.com` in the host project, checked before the apply that then binds the
  agent's `roles/cloudasset.viewer` on each container. The preflight names an organisation policy
  that forbids the API rather than failing inside `google_project_service`; an install that
  declares only explicit projects skips this check and never enables the API (§4).
- `project_roles` stays the list bound in the host project, and the mirror between it and
  `read_only_roles` that `tests/test_scoped_sa_pool_iam.py` checks is unchanged. The `scope_roles`
  allowlist lives beside it with a test that every entry is also in the default `project_roles`,
  so the allowlist cannot name a role the agent does not otherwise hold.

Uninstall revokes what install granted: `terraform destroy` removes the bindings because Terraform
owns them, which is the property #588 lost when its revocation lived in a bash function.

**The scoped service account pool moves to per-project accounts.** Decided 2026-09-23. The pool is keyed on the cluster today (`scoped_pool.tf`, one account per `{project_id, location, cluster_name}` row, hand-listed in `spec.security.scopedServiceAccounts` under a cap of 100), and the broker refuses a request for a cluster with no member rather than widening. At one cluster per project that cap is the ceiling on the fleet, and a hand-maintained list of 200 rows duplicates what resolution already found. The pool therefore becomes one account per project in the resolved set: Terraform derives the members from the same `scope` input that binds `scope_roles`, without reading the runtime snapshot, so an explicit project gets its account when it gets its grant, and a folder's or organisation's members are listed at plan time, with the same Asset Inventory search the reconcile runs, and get theirs on that apply: pool membership under a container lags to the next `upgrade.sh`, as §10 step 3 says of the phase 3 selectors, while the container-level grant and discovery stay zero-touch, so a project created under the folder between applies is discovered and gets its profile, and every kubectl for it is refused until the next apply adds its account. The broker's mapping key becomes the project, the mapping the operator renders becomes one row per project (`projectId`, `serviceAccountEmail`) in place of the per-cluster triple, and the refusal rule is unchanged, a request for a cluster in a project with no account is refused, not served on the ambient credential. The blast radius of a compromised sandbox becomes the project rather than the cluster: two clusters in one project share an account. The design accepts that because the project is the IAM unit the declaration is written in and the unit the customer's estate is cut in. Arming stays a separate, explicit switch, off by default and independent of the scope: `spec.security.scopedServiceAccountPool.enabled` on the CR and `scoped_pool_enabled` in the composition (the follow-up settles the spelling), so declaring `projects` on its own arms nothing, the mode is read from that field and from the broker's `CREDENTIAL_PROXY_SCOPED_SA_POOL` as today, and the pool is turned off without touching the scope; the hand-listed `spec.security.scopedServiceAccounts` rows give way to the derived mapping. The move is a follow-up to phase 2, before `organizations` is offered in a release (§9).

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
has three conditions, all required: the project is absent from this run's resolved set (§3),
because no selector produced it or because an `exclude.projects` entry removed it -- a project in
the set is present in the snapshot with whatever outcome it read, `ok` or not, and is never pruned
by this rule; every selector the run resolves at runtime, the declared containers and the phase 3 shared-VPC-host and metrics-scope lookups alike, resolved this run, `ok` or `over-cap` (§4), no project of any kind came back `unreachable`, the management project itself resolved and listed its own clusters, and the declaration file was read, so that the absence is the declaration speaking and not a failed lookup; and the project was present in the previous snapshot's `projects` array, as `in-scope` or
`retiring`, so that removal is a transition the scope made and not a state it merely finds. A CR that carries no `scope` block at all declares nothing: the run lists the management project alone, keeps the last recorded declaration's exclusions, carries its projects forward as in scope, and marks nothing newly `retiring`; a project an earlier present block already marked `retiring`, or a management project whose identity changed, is still pruned on a clean no-block run, because that mark came from a real declaration. A block goes missing on its own when a CR write passes an older operator's webhook; dropping projects is done by emptying `projects` inside a present block. The rule therefore binds the renderer: once an install carries a scope value, the chart's `PlatformAgent` template emits `spec.scope` as a present block, empty lists included, rather than dropping the group the way its other optional groups are dropped when every value is empty, so that removing the last project from the tfvars and running `upgrade.sh` reads as the declaration that drops it and not as no declaration. The absent marker exists for CR writes the chart did not make. The prune takes two clean runs: the first clean run that finds a project absent writes it to the snapshot as `retiring`, and the next clean run that still finds it absent deletes its profiles, so a declaration edit has one clean run to be reverted before anything is removed. A run that is not clean neither marks nor counts: a project newly absent on it is carried forward as `in-scope` with no `via`, and one already `retiring` stays so. A management project that changes identity (`RECONCILE_PROJECT` re-pointed, or the metadata server naming another project) is marked `retiring` on the run the change is seen, once the new project has listed its own clusters, unless the declaration names the old one and no `exclude.projects` entry matches it; an answer from the gcloud config fallback that disagrees with the previous snapshot is treated as unresolved, not as a change. The third condition is what
protects profiles the scope never produced. The `manage-cluster` skill onboards a cluster with an
explicit `--project` today, and those profiles exist on installs that will upgrade into phase 1
with an empty scope; without it, the first tick would delete every one of them, which is the
deletion `cluster_agent_reconcile.py:11-15` exists to never do. A profile whose project is outside
the scope and was never in it is kept, verified by PRUNE against its own project as today, and
listed in the snapshot's `unmanaged` array (§5) so the operator can declare it or delete it. That is decision 3 of 2026-09-21 on #1354 as the design behaves: discovery never aborts over a stray profile; a project the scope dropped retires under the three conditions; a profile whose project was never in scope is kept and reported. The issue's wording, "pruned and recorded in the snapshot with the reason", holds for the dropped project and not for the never-in-scope one, which is recorded and kept. A project the rule has decided to retire is written to the snapshot with `state: retiring` and stays there, still
eligible for the prune, until every one of its profiles is gone; otherwise a delete that failed on
the one tick the third condition held would leave the profile `unmanaged` for good. A project that
became `denied` because a binding was revoked without editing the scope is not pruned; the
profiles stay, the outcome is reported, and an operator resolves it one way or the other.

**A project that disappears.** Deleted, or moved out from under a declared folder: the index stops
placing it under the container while the container itself still resolves `ok`. The reconcile does not
retire it on that reading alone, because the index lags a move by minutes and a run in that window
would otherwise retire a project that merely moved: the member is kept, with `absentSince` stamped on
its row, for a day (`INDEX_LAG_GRACE_SECONDS`), and only after that does it retire over the ordinary
two clean runs; the declaration speaking, an `exclude.projects` entry or the container removed from
the CR, retires it sooner, on the ordinary two runs. PRUNE's per-profile `describe --project=<P>`
meanwhile returns a 403, because the folder binding no longer covers it, which `_cluster_exists`
classifies as unknown and keeps. Moved to a different declared folder: no change, because resolution
is by project; the `via` field records the new path and any placement clears the stamp, including a
placement during the lag by the folder the project left. Dropped from `projects` in the same edit that
declares the folder it moved into, a project is held the same day, since the index may not place it yet
and its previous route names no container to keep it by.

**Never on ambiguity.** The rule at `cluster_agent_reconcile.py:11-15` holds: auth, network, quota,
and unclassified errors leave profiles untouched.

## 8. Everything else that assumes one project

Discovery and IAM are the mechanism; these are the places that will read wrong once the mechanism
works. Each is listed with whether it blocks the first phase or follows it: `1` is phase 1 of §10,
`2` means after it, and `docs` means the documents step; the column is not §10's step numbering.

| Where                                                                                                                                                                            | What it assumes                                                                                                                                                                        | Phase |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `agents/platform/scripts/session_kv_server.py:1386`                                                                                                                              | `GCP_PROJECT_ID` was the project for every event's console links; the link now takes the event's project first, the env as fallback                                                    | done  |
| `agents/platform/scripts/platform_mcp_server.py:275-300`                                                                                                                         | `get_project_id()` reads one `project:` line from `USER.md`                                                                                                                            | 1     |
| `agents/platform/skills/cluster-agent-lifecycle/SKILL.md`                                                                                                                        | Delegation needs `--project` from the requester; #953 asks for enumeration first                                                                                                       | 1     |
| `k8s-operator/cmd/drift-detector`                                                                                                                                                | The `managedFields` join drops every Cluster Agent profile whose cluster is outside `--project`, so a cross-project fleet's clusters are discarded at discovery                        | 2     |
| `terraform/modules/drift-pubsub`                                                                                                                                                 | One log sink in `var.project_id`; other projects' audit logs need a sink each into the host topic                                                                                      | 2     |
| Fleet-audit SOPs and the cost, recommender, and compliance skills                                                                                                                | Query "the project" for quotas, recommendations, and IAM; need to iterate the snapshot                                                                                                 | 2     |
| `docs/site/src/content/docs/concepts/cluster-agents.md:24`                                                                                                                       | "sweeps the project"; now "sweeps every project in scope"                                                                                                                              | done  |
| `docs/site/src/content/docs/reference/security-and-iam.md:80`                                                                                                                    | "reads Kubernetes objects in **every** cluster in the project" becomes "in every cluster in the scope"                                                                                 | docs  |
| `docs/site/src/content/docs/reference/credential-isolation.md:205`                                                                                                               | Described the metadata lookup, with `RECONCILE_PROJECT` as its override, as how the script finds its one project; the page now says the override is pinned empty in the managed `.env` | done  |
| `docs/site/src/content/docs/reference/security-and-iam.md:28`, `agents/platform/skills/manage-cluster/SKILL.md:41`, `agents/platform/skills/cluster-agent-lifecycle/SKILL.md:80` | Named `RECONCILE_EXCLUDE`, a bare cluster name matched project-blind, as the opt-out; each now names `spec.scope.exclude.clusters` with the variable as the one-release fallback       | done  |

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
`organizations` in a release: a per-project credential (§6) bounds what a compromised sandbox reads to
one project regardless of how wide discovery is. Until then the design recommends `projects` and
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

1. **Explicit projects.** `spec.scope.projects` and `spec.scope.exclude` on the CRD; the operator renders the scope file on every install, empty and marked absent when the CR has no scope block, so that a block stripped by a CR write through an older operator's webhook reads as no declaration (management project alone, nothing retired) rather than as every project dropped, while an empty `projects` list in a present block is the declaration that drops projects; `cluster_agent_reconcile.py` iterates the list,
   applies the three-condition prune, and writes `fleet_scope.json` with per-project outcomes and
   `unmanaged` and `retiring` entries; `kube-agents-iam` binds `scope_roles` per explicit project; the chart's `PlatformAgent`
   template renders `spec.scope` as a present block, empty lists included, whenever the install
   carries the value (§7 says why an omitted block must not stand in for an emptied one); the
   bootstrap gate names non-`ok` projects; `session_kv_server.py` and `platform_mcp_server.py` read the
   project from the event or the profile identity rather than one environment value; the
   `RECONCILE_EXCLUDE` mentions §8 lists point at the new field. Phase 1 also carries the
   `MaxItems=100` caps on its lists, `over-cap` as a project outcome for an explicit project past
   the resolved-set cap, glob matching in `exclude.projects`, and `resolver` and `ignoredExcludes`
   in the snapshot (§3, §5). This is the smallest change that manages two projects from one install.
2. **Folders and organisations.** Asset Inventory resolution, its allowlist entry, and its two value flags;
   `cloudasset.googleapis.com` in the composition's API list, conditional on a declared container;
   container outcomes, the freeze rule and `over-cap` as a container outcome (§3); folder- and
   organisation-level bindings of `scope_roles` plus `roles/cloudasset.viewer`; the installer
   preflight for container IAM permissions and for the Asset API under organisation policy (§6);
   `via` and `containers` in the snapshot. A follow-up to phase 2 makes the resolved-set cap a
   declared value (`spec.scope.maxProjects`, default 100) with the listing budget scaled to it, and
   runs PRUNE's per-profile `describe` under the same bounded parallel map as the listing, because
   at 200 clusters a sequential walk is minutes of every hourly tick; a second follow-up moves the
   pool to per-project accounts (§6).
3. **Shared VPC and Metrics Scope selectors.** `sharedVpcHosts` from the Compute API and
   `metricsScopes` from the Monitoring API (§3), each resolving to explicit projects with a
   per-project binding, since nothing is inherited through them. Terraform resolves the project
   list at plan time, because the bindings are Terraform's and it cannot read the runtime snapshot
   (§11), so a service project attached or a project added to the scope after the last apply reads
   `denied` until the next `upgrade.sh`: zero-touch onboarding (§6) is a property of Resource
   Manager containers and these two selectors do not have it. The reconcile resolves the same
   selectors at runtime so the snapshot names the project and its `denied` outcome rather than
   omitting it: each such project's `via` names its source (`sharedVpcHosts/<host>` or
   `metricsScopes/<scope>`), the projects fill the set after explicit projects and before containers (§3), a failed lookup freezes the selector's members and the prune exactly as a failed container lookup does (§4, §7), and the two read verbs join the broker allowlist the way §4 adds `asset`. Moved ahead of the consumers and the documents on 2026-09-21 because the first
   enterprise request named both.
4. **Downstream consumers.** The rows §8 marks 2: the drift detector's cross-project join,
   audit-log sinks per project or an aggregated sink, and the fleet-audit SOPs and cost skills
   iterating the snapshot.
5. **Documents.** The architecture edits in §9 and the site pages in §8, in one PR once
   phase 1 has merged, so the documents describe what runs.

## 11. Open questions

- **Snapshot in `.status`?** §5 keeps the resolved membership on the PVC because the pod has no
  channel to the operator. If one arrives for another reason, the snapshot should ride it.
- **Cardinality at organisation scale.** One Platform Agent for hundreds of projects means one chat
  front door, one reconcile job, and one hourly sweep for all of them. The cap question is settled (§3: a declared value with 100 as its default, the follow-up §10 step 2 names) and the pool no longer counts clusters (§6), so what bounds an
  install above the default is the pod: 200 cluster profiles means 200 sweeps per hourly tick and a
  200-profile PRUNE walk, and no install has run at that size. #1913 measures it with synthetic
  profiles before a real fleet; until it does, the design states the mechanism and not the number a
  pod can carry, nor the size at which an install wants two Platform Agents with disjoint scopes.

- **`mcp.toolUser` across projects.** If the GKE MCP server checks `roles/mcp.toolUser` in the
  project a call targets rather than in the caller's project, MCP-backed reads of a scoped project
  fail while `gcloud` reads succeed, and the role has to join `scope_roles`. One call against a
  second project settles it.
- **Who may widen the scope.** Editing `spec.scope` is a Kubernetes RBAC question on the
  management cluster; granting into a folder is a GCP IAM question. They are enforced by different
  systems and can disagree. The design assumes the tfvars is the source of both and the CR is
  rendered from it on the installer path, which holds for `install.sh` and not for a hand-applied
  CR.
