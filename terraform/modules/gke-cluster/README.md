# GKE Autopilot Cluster Module

Reusable Terraform module for provisioning a GKE Autopilot cluster configured for Kube-Agents workloads. Autopilot clusters are regional: `location` must be a region (a zone is rejected at plan time). The full-install composition passes `kube-agents-host=true` through `resource_labels` so the admin portal can discover the deployed host; standalone callers can use the same input when they install kube-agents on the cluster.

By default (`enable_database_encryption = true`), the module provisions a Cloud KMS Keyring and CryptoKey, binds `roles/cloudkms.cryptoKeyEncrypterDecrypter` to the GKE Service Agent, and enables etcd database encryption (CMEK).

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> destroyed — `terraform destroy` only removes them from state, and a subsequent apply
> with the same names fails with a 409 (the provisioning scripts sidestep this by
> check-then-create). Recover by importing the existing resources back into state
> (`terraform import module.<name>.google_kms_key_ring.gke_keyring ...`) or by choosing new
> `kms_keyring_name`/`kms_key_name` values.

## Usage

```hcl
module "gke_cluster" {
  source          = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=vX.Y.Z"
  project_id      = "my-gcp-project"
  cluster_name    = "production-host-01"
  location        = "us-central1"
  resource_labels = {
    "kube-agents-host" = "true"
  }
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
