# Kube-Agents IAM & Workload Identity Module

Reusable Terraform module for provisioning the Platform Agent's Google Service Account (GSA), its Workload Identity binding, and its project-level IAM roles.

## Relationship to the install

This is the module the full-install composition (and therefore `install.sh`) uses for the
agent's identity. The canonical identifiers (GSA `kubeagents-platform-gsa`, KSA
`kubeagents-platform-agent`, namespace `kubeagents-system`) also appear in
`k8s-operator/scripts/common.sh` for the dev tooling, and the module's defaults mirror
them.

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
