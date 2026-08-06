# clusters/cluster-a/provisioning/

Cloud + cluster resources for `cluster-a` as **KCC YAML** or **Terraform HCL** (selected by the
proposing agent's `spec.iac.format`, default `kcc`; 06 §1.1, §4). The customer's CI/CD applies these
on merge — `kubectl apply` for KCC, `terraform apply` for HCL. Agents author here via PR only.

Examples of what lives here: `ContainerCluster`, `ContainerNodePool`, project IAM (KCC), or the
equivalent Terraform. HCL that consumes the kube-agents Terraform modules pins them to an immutable
release tag (`?ref=vX.Y.Z`, never a branch ref) — illustrative, replace with real resources per PR:

```hcl
module "gke_cluster" {
  source       = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=vX.Y.Z"
  project_id   = "my-gcp-project"
  cluster_name = "cluster-a"
  location     = "us-central1"
}
```

The same pin applies to the other modules (`kube-agents-iam`, `chat-pubsub`, `github-minter`);
[`terraform/examples/full-install/`](../../../../../terraform/examples/full-install/README.md) is the
canonical single-apply composition of all four, and the
[release versioning & promotion guide](../../../../../docs/site/src/content/docs/deploy/release-versioning.md)
owns the pinning rules.
