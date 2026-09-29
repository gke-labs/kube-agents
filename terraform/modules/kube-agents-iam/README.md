# Kube-Agents IAM & Workload Identity Module

Reusable Terraform module for provisioning the Platform Agent's Google Service Account (GSA), its Workload Identity binding, its project-level IAM roles, and the read grants in the projects, folders and organisations its `scope` input names.

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

`scoped_clusters` provisions one service account per named GKE cluster, plus
`roles/iam.serviceAccountTokenCreator` for the agent bound on each member as a
resource (never at project level). The members hold no IAM grant of their own
as of 2026-08-12 — the IAM-Condition scoping they were designed around grants
nothing for Kubernetes object operations — so the default is `[]` and should
stay there until per-cluster RBAC lands. The site's
[security-and-iam reference](../../../docs/site/src/content/docs/reference/security-and-iam.md)
owns the topic, including how the mapping reaches the credential broker and
what the pool does and does not bound.

## Projects, folders and organisations in scope

`scope` mirrors `spec.scope` on the `PlatformAgent`: `projects`, `folders`, `organizations`,
`exclude.projects` and `exclude.clusters`, with the same caps and patterns the CRD enforces,
checked at plan time. Each
project in `projects` other than `project_id` gets the read allowlist in `scope.tf`
(`roles/container.clusterViewer`, `roles/container.viewer`, `roles/compute.viewer`,
`roles/monitoring.viewer`, `roles/logging.viewer`, `roles/iam.securityReviewer`) intersected with
`project_roles`, never `project_roles` itself, so a `custom` list that carries an admin role at
home carries none of it elsewhere; the plan is refused when the intersection leaves no role that
lists and gets clusters (`roles/container.clusterViewer` or `roles/container.viewer`;
`roles/iam.securityReviewer` lists but cannot get). `exclude` binds nothing and revokes nothing: it
travels in the object so the composition renders the CR from the same value, and a project named
in `projects` is bound even when an exclude entry removes it from the resolved set, so drop it
from `projects` instead. A folder or organisation (`folders`, `organizations`: numeric IDs)
gets the same intersected allowlist plus `roles/cloudasset.viewer`, bound on the container
itself (`google_folder_iam_member`, `google_organization_iam_member`), so every project beneath
it inherits the grant, including one created after the apply, and the reconcile can search the
container's asset index for clusters; the identity running the apply needs
`resourcemanager.folders.setIamPolicy` or `resourcemanager.organizations.setIamPolicy` there.
The same manageability check applies to a container as to a project. Removing an entry revokes
its bindings on the next apply, and `terraform destroy` revokes them all. The `scope_projects`,
`scope_folders`, `scope_organizations`, `scope_roles` and `scope_container_roles` outputs surface
what was bound. An organisation binding is wide; the design is
[`docs/designs/multi-project-scope.md`](../../../docs/designs/multi-project-scope.md) §6 and §9.

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
