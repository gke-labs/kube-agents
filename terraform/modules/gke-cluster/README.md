# GKE Autopilot Cluster Module

Reusable Terraform module for provisioning a GKE Autopilot cluster configured for Kube-Agents workloads. Autopilot clusters are regional: `location` must be a region (a zone is rejected at plan time).

## Usage

```hcl
module "gke_cluster" {
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=vX.Y.Z"
  project_id   = "my-gcp-project"
  cluster_name = "production-host-01"
  location     = "us-central1"
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
