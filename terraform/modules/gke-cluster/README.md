# GKE Cluster Module

Reusable Terraform module for provisioning the GKE cluster that hosts Kube-Agents workloads. It manages one of three cluster shapes, selected by `cluster_mode` and `create_cluster`:

- **`cluster_mode = "autopilot"`** (default) — a GKE Autopilot cluster. Autopilot clusters are regional: `location` must be a region (a zone is rejected at plan time).
- **`cluster_mode = "standard"`** — a GKE Standard cluster: a default node pool of `e2-standard-4` machines (one per zone), Dataplane V2 with FQDN NetworkPolicy, the Filestore CSI and BackupRestore addons, Workload Identity, and CMEK database encryption. `enable_gvisor_node_pool` adds a dedicated GKE Sandbox pool (Standard only — Autopilot ships the `gvisor` RuntimeClass natively).
- **`create_cluster = false`** — install onto a cluster somebody else made. The module reads the named cluster through a data source and creates no cluster or KMS resources. Two postconditions gate the plan: the cluster must already have Workload Identity enabled, and it must enforce NetworkPolicy — either Dataplane V2 or the legacy Calico addon, which takes two `gcloud container clusters update` calls in order, `--update-addons=NetworkPolicy=ENABLED` and then `--enable-network-policy`. `install.sh` does both for you; a bare Terraform run refuses instead.

The full-install composition passes `kube-agents-host=true` through `resource_labels` so the admin portal can discover the deployed host; standalone callers can use the same input when they install kube-agents on the cluster.

By default (`enable_database_encryption = true`, together with `create_cluster = true`), the module provisions a Cloud KMS Keyring and CryptoKey, binds `roles/cloudkms.cryptoKeyEncrypterDecrypter` to the GKE Service Agent, and enables etcd database encryption (CMEK). FQDN NetworkPolicy is also on by default (`enable_fqdn_network_policy`) — the operator's opt-in `FQDNNetworkPolicy` companion objects only enforce on clusters that have it.

Two things Terraform cannot express, both of which stay `gcloud` steps for callers that need them:

- **Managed OpenTelemetry scope.** Neither google provider (checked against 7.45) has a field for `--managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS`. Set it after create with `gcloud container clusters update` — the provider does not know the field, so Terraform never sees or reverts it.
- **CMEK on an existing cluster.** With `create_cluster = false` the module cannot enable database encryption on a cluster it only reads; `gcloud container clusters update --database-encryption-key` remains the way to do that, before or after the apply.

Set `allow_external_dns_traffic = true` for a cluster the Platform Agent has to reach from outside the VPC. It drives `control_plane_endpoints_config.dns_endpoint_config.allow_external_traffic`, the field the agent's endpoint detection reads before it passes `get-credentials --dns-endpoint` (see [`k8s-operator/scripts/gke_dns_endpoint.sh`](../../../k8s-operator/scripts/gke_dns_endpoint.sh)); a cluster whose IP endpoint the agent cannot route to and whose DNS endpoint serves no external traffic is unreachable. This block is why the module requires provider `>= 6.11` — it does not exist in 5.x.

The default is `false`, which is GKE's own default and therefore the value every cluster this module already manages is sitting at: the module set no `control_plane_endpoints_config` before, so upgrading to a version that does leaves an existing cluster's plan empty rather than publishing a control-plane endpoint its operator never asked for. That matters more than an ordinary default because this endpoint is governed by IAM alone — no private-endpoint or master-authorized-networks setting is holding it shut. The field is Terraform-managed either way once the module renders it, so change it here rather than with `gcloud container clusters update --enable-dns-access`: out-of-band it is drift that the next apply reverts. That cuts both ways on upgrade — a cluster whose endpoint was opened with `gcloud` while the module did not manage the field needs `allow_external_dns_traffic = true` before the next apply, which would otherwise close it and leave the agent on the IP endpoint. (install.sh's generated tfvars always set this `true`, so the agent can reach the cluster from outside the VPC.)

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> destroyed — `terraform destroy` only removes them from state, and a subsequent apply
> with the same names fails with a 409. Recover by importing the existing resources back
> into state (`terraform import module.<name>.google_kms_key_ring.gke_keyring[0] ...`) or by
> choosing new `kms_keyring_name`/`kms_key_name` values. The full-install composition's
> `lifecycle.sh` automates both directions.

## Usage

```hcl
module "gke_cluster" {
  source          = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=1.2.0"
  project_id      = "my-gcp-project"
  cluster_name    = "production-host-01"
  location        = "us-central1"
  resource_labels = {
    "kube-agents-host" = "true"
  }

  # "autopilot" (default) or "standard"; standard builds an e2-standard-4
  # node pool with Dataplane V2 and FQDN NetworkPolicy, and is the only mode
  # that can carry a gVisor node pool.
  cluster_mode = "standard"

  # Reachable by a Platform Agent that does not sit in this VPC. Omit for a
  # cluster that should stay VPC-only.
  allow_external_dns_traffic = true
}
```

Installing onto a cluster somebody else made:

```hcl
module "gke_cluster" {
  source         = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/gke-cluster?ref=1.2.0"
  project_id     = "my-gcp-project"
  cluster_name   = "the-existing-cluster"
  location       = "us-central1"
  create_cluster = false
}
```

See the [Release versioning & promotion guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
