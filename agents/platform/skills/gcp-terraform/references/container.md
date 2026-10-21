# google_container_cluster and google_container_node_pool

From the `google` provider's source at 8.x (September 2026). In-place
support for `node_config` fields has grown release by release; the version
in each line is when the field became updatable, so check it against the
repository's `required_providers` pin. The provider's page for that version
decides.

## Node pool

- **Size.** `node_count` for a fixed pool, `autoscaling` for an autoscaled
  one; not both. Terraform reads `node_count` back from the instance groups,
  so with autoscaling on, a stated `node_count` is re-asserted on every
  apply. Inside `autoscaling`, `min_node_count`/`max_node_count` are per
  zone and `total_min_node_count`/`total_max_node_count` pool-wide; use one
  pair (the API rejects both). `location_policy` is `BALANCED` or `ANY`.
- **Updatable in place:** `node_count`, `autoscaling`, `node_locations`,
  `version`, `management`, `upgrade_settings`, and under `node_config`:
  `machine_type` (5.10.0), `disk_size_gb` (5.10.0), `disk_type` (5.10.0),
  `labels` (4.48.0), `taint` (5.4.0), `tags` (4.42.0),
  `workload_metadata_config` (3.23.0), `image_type`, and `resource_labels`
  (updatable today; the release that made it so is not recorded).
- **Forces replacement:** `name`, `location`, `cluster`,
  `initial_node_count`, and under `node_config`: `guest_accelerator`,
  `service_account`, `oauth_scopes`, `metadata`, `spot`, `preemptible`,
  local SSD settings, `shielded_instance_config`, `sandbox_config`, and
  others the provider page marks. On a provider older than the versions
  above, those fields force replacement too.
- No `deletion_protection`. Its `deletion_policy` defaults to `DELETE`;
  leave it.

### `describe` to arguments

| `gcloud container node-pools describe` field         | Argument                                                     |
| ---------------------------------------------------- | ------------------------------------------------------------ |
| `autoscaling.minNodeCount`, `maxNodeCount`           | `autoscaling { min_node_count, max_node_count }`             |
| `autoscaling.totalMinNodeCount`, `totalMaxNodeCount` | `autoscaling { total_min_node_count, total_max_node_count }` |
| `autoscaling.locationPolicy`                         | `autoscaling { location_policy }`                            |
| `locations[]`                                        | `node_locations`                                             |
| `config.machineType`, `diskSizeGb`, `diskType`       | `node_config { machine_type, disk_size_gb, disk_type }`      |
| `config.serviceAccount`, `config.oauthScopes[]`      | `node_config { service_account, oauth_scopes }`              |
| `config.labels`, `config.taints[]`                   | `node_config { labels, taint }`                              |
| `management.autoRepair`, `autoUpgrade`               | `management { auto_repair, auto_upgrade }`                   |
| `version`                                            | `version`                                                    |

## Cluster

- **Inline `node_pool` blocks replace the whole cluster.** The cluster's
  `node_pool` argument forces replacement: adding, removing or renaming an
  inline pool, or changing its `initial_node_count`, deletes and recreates
  the cluster. On a repository that writes pools inline, a new pool is a
  separate `google_container_node_pool` resource, never a new inline block;
  propose that and say why in the PR body. The provider also does not
  detect `machine_type` and disk changes on an inline pool, so those edits
  do nothing: stop and say so.
- **Forces replacement:** `name`, `location`, `network`, `subnetwork`,
  `description`, `initial_node_count`, `enable_autopilot`,
  `networking_mode`, `cluster_ipv4_cidr`, `datapath_provider`,
  `default_max_pods_per_node`, `enable_kubernetes_alpha`,
  `confidential_nodes`, `private_cluster_config.master_ipv4_cidr_block` and
  `.private_endpoint_subnetwork`, the range fields of
  `ip_allocation_policy`, some changes the provider decides at plan time
  (`enable_k8s_beta_apis`, `dns_config.additive_vpc_scope_dns_domain`,
  turning on L4 ILB subsetting or FQDN network policy), and others the
  provider page marks. A top-level `node_config` describes the default pool,
  and its replace-forcing fields (`service_account`, `oauth_scopes`,
  `metadata`, `spot`, and the rest of the node-pool list above) replace the
  whole cluster.
- **Updatable in place:** release channel, maintenance policy,
  `min_master_version`, `enable_private_nodes`, `enable_private_endpoint`,
  addons, logging and monitoring config.
- **`remove_default_node_pool`** updates in place, and setting it `true`
  deletes `default-pool` and its nodes. Treat it as a replacement (Step 3):
  write it only when the user asked for it, and call it out.
- **`deletion_protection` defaults to `true` (5.0.0 and later).** It is
  checked when Terraform deletes, against the value in state: a destroy or
  replacement plans cleanly and then fails at apply until a separate apply
  sets it `false`. Never set it `false` or remove it; a replacement the user
  approves still needs that human step, and the PR body says so.
- **`deletion_policy`** (7.33.0 and later) is checked before
  `deletion_protection`, and `ABANDON` gets past it. Never change it.
- **A new cluster**: follow the repository's existing cluster block or
  module call. With none, the kube-agents `gke-cluster` module pinned to a
  release tag is the reference layout's example; ask before introducing a
  module the repository does not already use.
