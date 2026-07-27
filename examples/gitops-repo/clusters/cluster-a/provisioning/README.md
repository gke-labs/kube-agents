# clusters/cluster-a/provisioning/

Cloud + cluster resources for `cluster-a` as **KCC YAML** or **Terraform HCL** (selected by the
proposing agent's `spec.iac.format`, default `kcc`; 06 §1.1, §4). The customer's CI/CD applies these
on merge — `kubectl apply` for KCC, `terraform apply` for HCL. Agents author here via PR only.

Examples of what lives here: `ContainerCluster`, `ContainerNodePool`, project IAM (KCC), or the
equivalent Terraform. Placeholder below is illustrative — replace with real resources per PR.
