# GKE Cluster Module

Reusable Terraform module for provisioning the GKE cluster that hosts Kube-Agents workloads. It manages one of three cluster shapes, selected by `cluster_mode` and `create_cluster`:

- **`cluster_mode = "autopilot"`** (default) — a GKE Autopilot cluster. Autopilot clusters are regional: `location` must be a region (a zone is rejected at plan time).
- **`cluster_mode = "standard"`** — a GKE Standard cluster: a default node pool of `e2-standard-4` machines (one per zone), Dataplane V2 with FQDN NetworkPolicy, the Filestore CSI and BackupRestore addons, Workload Identity, and CMEK database encryption. `enable_gvisor_node_pool` adds a dedicated GKE Sandbox pool (Standard only — Autopilot ships the `gvisor` RuntimeClass natively). [Sizing the node pools](#sizing-the-node-pools) covers both pools' machine types, boot disks and the gVisor pool's autoscaling bounds.
- **`create_cluster = false`** — install onto a cluster somebody else made. The module reads the named cluster through a data source and creates no cluster or KMS resources. Two postconditions gate the plan: the cluster must already have Workload Identity enabled, and it must enforce NetworkPolicy — either Dataplane V2 or the legacy Calico addon, which takes two `gcloud container clusters update` calls in order, `--update-addons=NetworkPolicy=ENABLED` and then `--enable-network-policy`. `install.sh` does both for you when authorized; a bare Terraform run refuses instead. `accept_no_network_policy = true` waives the second postcondition and installs onto a cluster that enforces none, without changing it; the `network_policy_enforced` output is then `false`, and every NetworkPolicy kube-agents installs is inert.

The full-install composition passes `kube-agents-host=true` through `resource_labels` so the admin portal can discover the deployed host; standalone callers can use the same input when they install kube-agents on the cluster.

By default (`enable_database_encryption = true`, together with `create_cluster = true`), the module provisions a Cloud KMS Keyring and CryptoKey, binds `roles/cloudkms.cryptoKeyEncrypterDecrypter` to the GKE Service Agent, and enables etcd database encryption (CMEK). FQDN NetworkPolicy is also on by default (`enable_fqdn_network_policy`) — the operator's opt-in `FQDNNetworkPolicy` companion objects only enforce on clusters that have it.

Two things Terraform cannot express, both of which stay `gcloud` steps for callers that need them:

- **Managed OpenTelemetry scope.** Neither google provider (checked against 7.45) has a field for `--managed-otel-scope=COLLECTION_AND_INSTRUMENTATION_COMPONENTS`. Set it after create with `gcloud container clusters update` — the provider does not know the field, so Terraform never sees or reverts it.
- **CMEK on an existing cluster.** With `create_cluster = false` the module cannot enable database encryption on a cluster it only reads; `gcloud container clusters update --database-encryption-key` remains the way to do that, before or after the apply.

Set `allow_external_dns_traffic = true` for a cluster the Platform Agent has to reach from outside the VPC. It drives `control_plane_endpoints_config.dns_endpoint_config.allow_external_traffic`, the field the agent's endpoint detection reads before it passes `get-credentials --dns-endpoint` (see [`scripts/installer/gke_dns_endpoint.sh`](../../../scripts/installer/gke_dns_endpoint.sh)); a cluster whose IP endpoint the agent cannot route to and whose DNS endpoint serves no external traffic is unreachable. This block is why the module requires provider `>= 6.11` — it does not exist in 5.x.

The default is `false`, which is GKE's own default and therefore the value every cluster this module already manages is sitting at: the module set no `control_plane_endpoints_config` before, so upgrading to a version that does leaves an existing cluster's plan empty rather than publishing a control-plane endpoint its operator never asked for. That matters more than an ordinary default because this endpoint is governed by IAM alone — no private-endpoint or master-authorized-networks setting is holding it shut. The field is Terraform-managed either way once the module renders it, so change it here rather than with `gcloud container clusters update --enable-dns-access`: out-of-band it is drift that the next apply reverts. That cuts both ways on upgrade — a cluster whose endpoint was opened with `gcloud` while the module did not manage the field needs `allow_external_dns_traffic = true` before the next apply, which would otherwise close it and leave the agent on the IP endpoint. (install.sh's generated tfvars always set this `true`, so the agent can reach the cluster from outside the VPC.)

The `cluster_endpoint` output is whichever endpoint GKE publishes for the cluster, and on one whose IP endpoints are disabled that is the DNS hostname, not an address. `cluster_endpoint_is_dns` reports which of the two it is, because they need different trust anchors: the DNS endpoint terminates at a Google Front End holding a publicly signed certificate, while `cluster_ca_certificate` signs only the IP endpoints. A consumer that pins the cluster CA while dialling the DNS endpoint fails the handshake with `x509: certificate signed by unknown authority`; it should fall back to the system trust store instead.

> **KMS resources cannot be deleted.** Cloud KMS key rings and keys are never actually
> deleted, and a raw `terraform destroy` does not leave the key usable: destroying the
> crypto key schedules every version of it for destruction, so the key can no longer
> encrypt: a cluster created again on it fails to start, and other clusters that share
> the key lose it. Run `terraform state rm` on the key ring, the key and the key's grant
> to the GKE service agent (`gke_kms_binding`, which the other clusters need) before a
> raw destroy. A later apply with the same names fails with a 409 until you import the
> existing resources back into state
> (`terraform import module.<name>.google_kms_key_ring.gke_keyring[0] ...`) or choose new
> `kms_keyring_name`/`kms_key_name` values. The full-install composition's `lifecycle.sh`
> does both for you; its README's [Teardown and re-apply](../../examples/full-install/README.md#teardown-and-re-apply) section says how, including how to restore
> versions a raw destroy scheduled.

## Sizing the node pools

Standard mode only. `sizing_profile` picks the shape of both node pools in one setting, and each shape can be set on its own:

| Input                                    | `small` (default) and `medium` | `fleet`         |
| ---------------------------------------- | ------------------------------ | --------------- |
| `standard_machine_type`                  | `e2-standard-4`                | `n2-standard-8` |
| `standard_disk_size_gb`                  | 100                            | 200             |
| `gvisor_machine_type`                    | `e2-standard-4`                | `n2-standard-4` |
| `gvisor_disk_size_gb`                    | 100                            | 150             |
| `standard_disk_type`, `gvisor_disk_type` | `pd-balanced`                  | `pd-balanced`   |

`small` is the shape the module produced before profiles existed, so the default changes nothing on an existing cluster. `medium` resolves like `small` until a later change gives it a shape of its own. The four machine-type and disk-size inputs and the two disk-type inputs default to `null`, which means "the profile's value": an explicit value always wins, even one equal to the `small` shape, so a tfvars file that writes `standard_machine_type = "e2-standard-4"` beside `sizing_profile = "fleet"` gets `e2-standard-4`. The resolved values are the `standard_*` and `gvisor_*` outputs. Disk types accepted: `pd-standard`, `pd-balanced`, `pd-ssd` and `hyperdisk-balanced` (the last only on machine series that support it, which GKE checks at apply).

The gVisor pool autoscales between `gvisor_min_node_count` (default 1) and `gvisor_max_node_count` (default 2) nodes per zone and is created at the minimum; equal bounds keep it at a fixed size. The default pool does not autoscale: `standard_node_count` (default 1) is the per-zone count the cluster is created with, and it is the cluster's `initial_node_count`, which the google provider treats as ForceNew on the cluster itself. Changing it on a live cluster plans the cluster's destruction and recreation, with every pool and workload on it; resize a live default pool with `gcloud container clusters resize` instead. The full-install composition's `lifecycle.sh` refuses such an apply (`guard_standard_node_count`), since its front doors apply without a prompt.

Changing a machine type or a boot disk on a live cluster updates the pool in place with a rolling node recreation, on either pool, with the google providers this module admits. None of the profile, machine-type, disk or autoscaling inputs replaces a pool or the cluster: the gVisor pool's creation count is read once and ignored afterwards, so raising `gvisor_min_node_count` later moves the autoscaling bound only. `standard_node_count` is the one exception above. At the defaults the module renders the disk values GKE has created pools with since 1.24 (100 GB, `pd-balanced`), so a cluster created on those defaults plans no change when it upgrades to this version of the module, and an existing gVisor pool gains the autoscaling bounds in place; a default pool created when GKE still defaulted to `pd-standard` plans an in-place disk-type change. Read the plan before applying.

## Tests

`tests/sizing_profile.tftest.hcl` plans the module against mocked `google` and `google-beta` providers and asserts what each sizing input renders: the default and `medium` give both pools the `small` shape with the gVisor pool autoscaling from 1 to 2 per zone, `fleet` gives the two fleet shapes, an explicit machine type, disk or bound wins over `fleet`, and the validations refuse an unknown profile, a boot disk under 10 GB, an unknown disk type, a bound below 1 and a floor above the ceiling. `make terraform-test` runs it; the suite needs Terraform 1.11 or newer, above the floor the module declares for an install.

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

See the [Release versioning guide](../../../docs/site/src/content/docs/deploy/release-versioning.md) for SemVer pinning instructions.
