# GitHub Token Minter Identity & KMS Module

Reusable Terraform module for provisioning the GitHub token minter's Google Service Account (GSA), its Workload Identity binding, and the KMS asymmetric signing key it signs GitHub App JWTs with.

The KMS key is created **import-only and empty** (`skip_initial_version_creation = true`): importing the GitHub App private key PEM into it is a separate manual step, performed via `k8s-operator/scripts/provision_10_deploy_github_minter.sh`, which uses the Minty CLI for the cryptographic wrapping.

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> destroyed — `terraform destroy` only removes them from state, and a subsequent apply
> with the same names fails with a 409 (the provisioning scripts sidestep this by
> check-then-create). Recover by importing the existing resources back into state
> (`terraform import module.<name>.google_kms_key_ring.minter ...`) or by choosing new
> `kms_keyring_name`/`kms_key_name` values.

## Relationship to the provisioning scripts

This module creates the **same** GSA, Workload Identity binding, key ring, and key that
`k8s-operator/scripts/provision_04_gcp_iam.sh` (IAM) and
`k8s-operator/scripts/provision_10_deploy_github_minter.sh` (KMS) create — pick one path
for **resource creation**, never both. The later steps of `provision_10` (PEM import via
the Minty CLI, minter deployment) still apply after a module apply; its creation steps
skip idempotently over the module-created resources. The canonical identifiers (GSA
`kubeagents-github-minter-gsa`, KSA `kubeagents-github-minter`, namespace
`kubeagents-system`) live in `k8s-operator/scripts/common.sh`, and the module's defaults
mirror them.

## Usage

```hcl
module "github_minter" {
  source     = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/github-minter?ref=vX.Y.Z"
  project_id = "my-gcp-project"
  location   = "us-central1"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
