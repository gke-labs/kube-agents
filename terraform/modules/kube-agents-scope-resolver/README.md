# Kube-Agents Scope Resolver Module

Resolves the two `spec.scope` selectors that are not Resource Manager containers, a Shared VPC host
(`sharedVpcHosts`) and a Cloud Monitoring Metrics Scope (`metricsScopes`), to the projects they
reach, at plan time, so the [`kube-agents-iam`](../kube-agents-iam/README.md) module can bind the
read roles in each; and, while the scoped service account pool is armed, lists each declared
folder's and organisation's member projects for that pool (the section below the selectors'). Nothing is inherited through either, which is why the resolution has to happen
before the bindings are planned ([`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md)
§6, §10 step 3).

## What it reads, and as whom

Three selector reads, the same the reconcile makes each run, and a fourth for the pool while it is
armed (its own section below): the Compute API's
`getXpnResources` for a host's attached service projects (a project that is not a Shared VPC host
resolves to no members, as it does at runtime); the Monitoring API's `metricsScopes.get` for a
scope's monitored projects, which it names by project number; and Resource Manager v3 to name each
number. Every read carries the google provider's own access token (`data "google_client_config"`),
so it is answered for the identity that applies, impersonation included, and not for gcloud's
active account, and names `quota_project` (the management project) as the consumer project, so the
APIs it uses and the quota it draws are that project's whichever credential type the provider
holds. Those APIs (`cloudresourcemanager` and `monitoring` for a Metrics Scope, `compute` for a Shared VPC
host) are ones the composition enables in the apply that follows the plan, per selector, so
`install.sh` enables the ones the declared selectors read before a first install's apply. A 403 that is not a grant on the project read is reported with its own remedy rather than as
one: an API off in the consumer project with the enable command, and an identity the consumer
project refuses (`USER_PROJECT_DENIED`, a plan-only or narrowly granted credential) with the
`serviceusage.services.use` it needs there, since enabling an API that is on fixes nothing. A read
that fails fails the plan, before anything is applied, with the selector,
the HTTP status and the API's message in the error: the identity needs `compute.projects.get` on a
host, to read the Metrics Scope in its scoping project (`roles/monitoring.metricsScopesViewer` is
the narrowest role) with `monitoring.googleapis.com` enabled there, and
`resourcemanager.projects.get` on each monitored project. A 200 whose body is not the document the
module reads (a JSON object of the documented shape; a list, a string or `null` decode too and are
refused as well) is refused rather than read as an empty selector, since an empty selector on the
next apply is every member's bindings revoked. A selector that resolves to more than `member_cap` projects
(the declared `spec.scope.maxProjects`, 100 by default), less the members an `exclude_projects`
entry names exactly, is refused too; a Shared VPC host with more than 500 service projects, one page
of the Compute API's answer, is refused whatever `member_cap` is, since the plan cannot follow a second
page. The reconcile lists at most `member_cap` projects of the whole resolved set, the management project included, and reads the
rest `over-cap` with nothing created under them, so a single selector past it cannot fit whatever
else is declared, and refusing it at its read spares the naming reads, one per monitored project;
the cap on the whole set, the management project, `scope.projects` and every selector's members
together, is `kube-agents-iam`'s precondition while a selector is declared or the cap is below its default, since only that module sees all three. A monitored project the identity cannot name, or whose ID
the scope cannot carry (a legacy domain-scoped ID), is left out by naming its project number in `exclude_projects`, the scope's `exclude.projects`, which the reconcile matches against the number on every row a scope named, so the member leaves the set whether or not a run had named it; that is the only entry of that list this module acts on, and the only exclusion that keeps a monitored project out of the bindings: the reconcile
has to name a monitored project, with the agent's own grant in it, before it can match an ID
entry, so `kube-agents-iam` leaves the grant of a monitored project excluded by ID in place. A service project of a Shared VPC host with such an ID has no number to be excluded by, so it is left out of `members` on its own, listed in `uncarriable_members`, and warned about by a `check` block on every plan, while the host's other service projects are bound as usual; a monitored project the Monitoring API should ever name by such an ID rather than by number takes the same path. IDs and globs are the callers': `kube-agents-iam` withholds the grant of a Shared VPC service
project an entry names by ID, the reconcile evaluates globs.

## The container read, for the scoped service account pool

While the pool is armed (`list_container_members`, the composition's `scoped_pool_enabled`), the
module also lists each declared folder's and organisation's members (`folders`, `organizations`,
numeric IDs), with the Cloud Asset Inventory search the reconcile runs for a container: one
`searchAllResources` call scoped to it, filtered to `container.googleapis.com/Cluster`, the project
read out of each asset name. The pool's accounts are Terraform's, one per project, so a member the
reconcile would discover under the container has to be known at plan time to get one. The members
get a pool account and nothing else: no binding of their own, since the container's grant is
inherited, and no place in the resolved-set cap, which counts containers at runtime after the
explicit projects and the selectors. A project created under the container between applies is
discovered and gets its profile, and every kubectl for it is refused until the next `upgrade.sh`
adds its account ([`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md)
§6). With the pool off no container is read and `container_members` is `{}`.

The read carries the same headers as the selectors' and refuses the same way: the identity needs
`cloudasset.assets.searchAllResources` on the container (`roles/cloudasset.viewer`, the role the apply
binds for the agent there) and `cloudasset.googleapis.com` enabled in `quota_project`, which
`install.sh` enables before a first plan when the pool is armed beside a container; a 403 with the
API off names the enable command, as the other reads do. A 200 that is not the search document (a
JSON object whose `results` each name a GKE cluster; an absent `results` key is a container with no
cluster and lists to an empty list) is refused, a container whose clusters are in more than
`member_cap` projects, less exact `exclude_projects` entries, is refused, and a `nextPageToken` (more
than 500 clusters under the container) is refused whatever `member_cap` is, as a Shared VPC host's
second page is. An exact `exclude_projects` entry drops a member from the pool; a glob is the
reconcile's alone. A member with a legacy domain-scoped ID is left out, listed in
`uncarriable_members` under the container's key and warned about by the same `check` block; the
management project (`quota_project`) is left out of a container's members whatever its ID, and is in
neither list, since `kube-agents-iam` seeds the pool with it itself. The search is eventually
consistent and the plan carries no state to grace an index gap with, so a container the index
answers with no cluster at all is warned about by a second `check` block (applying that answer
destroys every member's pool account; re-plan later, or pin the projects in `scope.projects`, which
does not depend on the index), while a shorter but non-empty answer is applied as read, the limit
the design records.

## Why a module of its own

The full-install composition calls `kube-agents-iam` with a module-level `depends_on` (the Workload
Identity pool has to exist before its binding). A module-level `depends_on` defers every data source
inside the module to apply time whenever a target has a planned change, which is every first
install and every upgrade that enables an API, and a `for_each` keyed on a deferred read fails the
plan as unknown. This module is called with no `depends_on` and no input a managed resource
produces, so its reads happen on every plan. `kube-agents-iam` refuses the plan when a declared
selector has no entry in its `scope_selector_members` input, so a caller that skips this module is
told so rather than getting the host bound and its members not.

## Inputs and output

`shared_vpc_hosts` and `metrics_scopes` are project IDs, with the CRD's pattern; `exclude_projects`
is the scope's exclude list; `member_cap` is the declared resolved-set cap (`spec.scope.maxProjects`,
100 by default), past which a single selector is refused. `members` maps each selector's snapshot name (`sharedVpcHosts/<host>`,
`metricsScopes/<scope>`) to the sorted project IDs it reaches, the shape `kube-agents-iam` takes and
the one the reconcile's `fleet_scope.json` `containers` array can be read beside. `folders` and
`organizations` are numeric IDs, read only while `list_container_members` is set; `container_members`
maps each one's snapshot key (`folders/<id>`, `organizations/<id>`) to the sorted project IDs its
clusters are in, the shape `kube-agents-iam` takes as `scope_container_members`.

[`lifecycle.sh`](../../examples/full-install/lifecycle.sh) in the full-install composition writes a
gitignored `scope_resolver_lifecycle_override.tf` into this directory for the duration of each
`terraform import`, pinning `data.http.scope_monitored_project` to no instances and the `members`
output to an empty list per declared selector; its unit tests fail on a rename of either, and the
composition's README says why the file exists. The script removes the file again, but one left by a
`lifecycle.sh` killed outright would be merged silently into the next `terraform test` here, where
every assertion on resolved members fails against the pin rather than the module. `make
terraform-test` refuses to run a suite beside such a file and names it; remove the file first.

## Tests

`tests/*.tftest.hcl` plan the module with both providers mocked and every HTTP read overridden, so
no case reaches an API: what each selector resolves to from the documents the APIs answer, which
answers the postconditions refuse (a read the identity cannot make, a document that is not the one
read, a second page, a selector past the per-selector cap), what an exclude entry or a legacy
ID leaves out, and the container read for the pool (`tests/container_members.tftest.hcl`): what a
folder and an organisation list to, that nothing is read with the pool off, and the answers refused. `make terraform-test` runs them, as the `validate` job in `validate.yml` does on every
pull request; the suites need Terraform 1.11 or newer (`mock_provider` from 1.7, `override_during`
from 1.11), above the floor the module declares for an
install.
