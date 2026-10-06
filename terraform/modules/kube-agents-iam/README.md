# Kube-Agents IAM & Workload Identity Module

Reusable Terraform module for provisioning the Platform Agent's Google Service Account (GSA), its Workload Identity binding, its project-level IAM roles, and the read grants in the projects, folders and organisations its `scope` input names and in the projects its Shared VPC host and Metrics Scope selectors resolve to.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh`) uses for the
agent's identity. The canonical identifiers also live with the installer, and the
module's defaults mirror them: the GSA `kubeagents-platform-gsa` and the namespace
`kubeagents-system` as defaults in `install.defaults.env` (an install overrides them
through `install.env`), the KSA `kubeagents-platform-agent` as a constant in
`scripts/installer/common.sh` for the dev tooling.

By default the module grants the read-only role set (the composition's
`permission_set = "read-only"`, also the installer's default). Pass `project_roles = []` to grant
nothing and manage roles yourself — but note the agent fails every GCP call until an
equivalent role set exists.

There is no admin preset to mirror: the `gke-admin` bundle was removed (see
[Security & IAM](../../../docs/site/src/content/docs/reference/security-and-iam.md)),
and this module has never had one. Passing admin roles through `project_roles` is
possible and is the module's equivalent of `permission_set = "custom"` — it puts
the grant in your Terraform, where it is reviewed.

## The scoped service account pool

`scoped_pool_enabled = true` provisions one service account per project the plan can list in
`scope` (the host project, `scope.projects` less an exact `exclude.projects` entry, and each
selector's members; a folder's or organisation's members are not listed at plan time yet), keyed
on the bare project id and created in `project_id`, plus `roles/iam.serviceAccountTokenCreator`
for the agent bound on each member as a resource (never at project level). Two clusters in one
project share an account by design (`docs/designs/multi-project-scope.md` §6). Each member's
description carries the install's identity (`Pool member of <service_account_id> for
projects/<id>`), and the installer's pre-apply ownership check lists members by it.
`scoped_pool_max_accounts` bounds how many the plan may create and refuses a pool past it at plan;
its default of 100 is GCP's default service-account quota, which the agent's own accounts share, so
set it to the headroom the project has free rather than leaving a large pool at the default. The members hold no IAM grant of their
own as of 2026-08-12 — the IAM-Condition scoping they were designed around grants nothing for
Kubernetes object operations — so the default is `false` and should stay there until
per-cluster RBAC lands. The site's
[security-and-iam reference](../../../docs/site/src/content/docs/reference/security-and-iam.md)
owns the topic, including how the mapping reaches the credential broker and
what the pool does and does not bound.

## Projects, folders, organisations and selectors in scope

`scope` mirrors `spec.scope` on the `PlatformAgent`: `projects`, `folders`, `organizations`,
`shared_vpc_hosts`, `metrics_scopes`, `max_projects` (the resolved-set cap, 1 to 5000, 100 by
default, which the whole-set precondition in `main.tf` tests the counted set against; design §6), `exclude.projects` and
`exclude.clusters`, with the same caps and patterns the CRD enforces, checked at plan time. Each
project in `projects` other than `project_id` gets the read allowlist in `scope.tf`
(`roles/container.clusterViewer`, `roles/container.viewer`, `roles/compute.viewer`,
`roles/monitoring.viewer`, `roles/logging.viewer`, `roles/iam.securityReviewer`) intersected with
`project_roles`, never `project_roles` itself, so a `custom` list that carries an admin role at
home carries none of it elsewhere; the plan is refused when the intersection leaves no role that
lists and gets clusters (`roles/container.clusterViewer` or `roles/container.viewer`;
`roles/iam.securityReviewer` lists but cannot get). `exclude` binds nothing and revokes nothing: it
travels in the object so the composition renders the CR from the same value, and a project named
in `projects` is bound even when an exclude entry removes it from the resolved set, so drop it
from `projects` instead. The one exception is a project a selector resolved to (below), which has
no list to be dropped from: an exclude entry that names a Shared VPC service project by ID, or a monitored project by its number, withholds its grant, while a monitored project excluded by ID keeps it, since the reconcile names it with that grant before the entry can match. A folder or organisation (`folders`, `organizations`: numeric IDs)
gets the same intersected allowlist plus `roles/cloudasset.viewer`, bound on the container
itself (`google_folder_iam_member`, `google_organization_iam_member`), so every project beneath
it inherits the grant, including one created after the apply, and the reconcile can search the
container's asset index for clusters; the identity running the apply needs
`resourcemanager.folders.setIamPolicy` or `resourcemanager.organizations.setIamPolicy` there.
The same manageability check applies to a container as to a project. A Shared VPC host or a
Metrics Scope's scoping project (`shared_vpc_hosts`, `metrics_scopes`: project IDs) is not a
container and inherits nothing, so it is resolved to projects at plan time and the module binds the
same intersected allowlist in each. The resolution is the
[`kube-agents-scope-resolver`](../kube-agents-scope-resolver/README.md) module's, whose `members`
output is this module's `scope_selector_members` input; the plan is refused when a declared
selector has no entry there, so a caller that skips the resolver is told so rather than getting
the host bound and its members not. Not resolved here because the composition calls this module
with a module-level `depends_on`, which would defer a data source inside it to apply time and fail
the bindings' `for_each` as unknown on a first install. Each scoping project is bound too, and a host
not otherwise in scope gets `roles/compute.viewer` alone, because the reconcile's lookups read them
(`compute.projects.get` in the host; the two Resource Manager reads in the scoping project, which
both managing roles carry), even when an exclude entry names them, and the plan is refused when
`project_roles` carries no `roles/compute.viewer` beside a host. Binding a resolved project needs
`setIamPolicy` there for the applying identity, as for an explicit project; a failure lands inside
the apply, not in the plan. An `exclude.projects` entry
that names a Shared VPC service project by ID, or a monitored project by its project number, keeps it out of the bindings, the one place `exclude` reaches IAM, and the reconcile matches a number against every row a scope named by it, so the member leaves the set whether or not the run named it before; a monitored project excluded by ID
keeps its grant, because the reconcile names every monitored project with the agent's own
credentials before it can match the entry, and without the grant the member is reported by number
as unnamed and holds the scope prune on every tick. Removing an entry revokes its bindings on the next apply, and
`terraform destroy` revokes them all. The `scope_projects`, `scope_folders`,
`scope_organizations`, `scope_shared_vpc_hosts`, `scope_metrics_scopes`, `scope_bound_projects`, `scope_lookup_only_hosts`,
`scope_roles` and `scope_container_roles` outputs surface what was bound. An organisation binding
is wide; the design is
[`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md) §6, §9 and
§10 step 3.

## Tests

`tests/*.tftest.hcl` plan the module against a mocked `google` provider and assert which bindings a
declaration plans (the intersected allowlist per project, the scoping project and a lookup-only host
included, nothing in the management project) and which declarations the preconditions refuse,
the whole-set cap's counting among them; which accounts the scoped service account pool derives
from a declaration, keyed on the project, and the `scoped_pool_max_accounts` refusal
(`tests/scoped_pool.tftest.hcl`); the role set is read from the module's own `scope_roles`
rather than spelled out. `make terraform-test` runs them, as the `validate` job in `validate.yml`
does on every pull request; `mock_provider` needs Terraform 1.7 or newer, above the floor the module declares for
an install. The repository's `tests/test_scope_iam.py` pins what a plan cannot see, the allowlist
against the default role list and the cap against the reconcile's constant among it.

## Usage

```hcl
module "kube_agents_iam" {
  source             = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/kube-agents-iam?ref=1.2.0"
  project_id         = "my-gcp-project"
  service_account_id = "kubeagents-platform-gsa"
  namespace          = "kubeagents-system"
  ksa_name           = "kubeagents-platform-agent"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
