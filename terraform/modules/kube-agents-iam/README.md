# Kube-Agents IAM & Workload Identity Module

Reusable Terraform module for provisioning the Platform Agent's Google Service Account (GSA), its Workload Identity binding, and its project-level IAM roles.

## Relationship to the provisioning scripts

This module and `k8s-operator/scripts/provision_04_gcp_iam.sh` create the **same** GSA and
Workload Identity binding — use one or the other for a given project, never both. The
canonical identifiers (GSA `kubeagents-platform-gsa`, KSA `kubeagents-platform-agent`,
namespace `kubeagents-system`) live in `k8s-operator/scripts/common.sh`, and the module's
defaults mirror them.

By default the module grants the same read-only role set the script grants (its
`read-only` permission set, also the script's default). Pass `project_roles = []` to grant
nothing and manage roles yourself — but note the agent fails every GCP call until an
equivalent role set exists. The script's `gke-admin` set can be reproduced by passing those
roles explicitly.

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
