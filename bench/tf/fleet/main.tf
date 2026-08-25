# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The seeded dirty fleet: three small standing clusters whose defects are the
# fixtures the Phase 2 presubmit scenarios assert on. Everything here is
# planted on purpose; "fixing" any of it breaks a test. README.md is the
# defect-to-scenario map, and the scheduled re-apply of this stack is what
# keeps the defects planted (GKE auto-upgrade would otherwise quietly heal
# seeded-b's version lag, and a well-meaning cleanup would delete the orphan
# disks).
#
# The label is deliberately NOT managed-by=kube-agents-bench: that label is
# what the eval orphan sweep in modules/cluster/gke deletes clusters by, and
# a standing fleet must never match it. kube-agents-host is the deploy
# pipeline's marker and is equally off limits.
locals {
  fleet_labels = {
    "managed-by" = "kube-agents-seeded-fleet"
  }

  # Cluster resource labels carry the cohort confinement on top: the drift
  # SOP resolves a cluster's environment from `.resourceLabels.environment`
  # FIRST, before any name-keyword inference, and cohorts are keyed on
  # (mode, environment) with unknown-environment clusters kept in their own
  # cohort, never merged. Labeling all three `environment = seeded` --
  # a literal the SOP's synonym table passes through unchanged -- makes the
  # drift cohort exactly {seeded-a, seeded-b, seeded-c} no matter what else
  # exists in the project: platform-agent-host and the transient eval-pr*
  # clusters carry no environment label, land in the unknown cohort, and
  # never vote on this fleet's baseline. Without this, a 2/2 authorized-
  # networks split against an unlabeled neighbour has no majority and the
  # consistency scenario has no finding. Label-resolved also means no
  # "inferred environment" severity step, which the r = 2/3 math cannot
  # spare. The value "seeded" is reserved for this fleet: labeling any
  # other cluster in the project with it would add a fourth voter and
  # change the arithmetic.
  cluster_labels = merge(local.fleet_labels, {
    "environment" = "seeded"
  })
}

provider "google" {
  project = var.project_id
  zone    = var.zone
}

# One dedicated minimal service account for every fleet node, mirroring the
# sibling eval module (modules/cluster/gke). The alternative -- the Compute
# Engine default SA -- would let any pod on these standing, deliberately
# defective clusters mint whatever that account holds, in the same project
# that runs platform-agent-host. The roles are the node-agent minimum.
resource "google_service_account" "fleet_nodes" {
  account_id   = "seeded-fleet-nodes"
  display_name = "Node service account for the seeded fleet"
}

resource "google_project_iam_member" "fleet_nodes_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.fleet_nodes.email}"
}

resource "google_project_iam_member" "fleet_nodes_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.fleet_nodes.email}"
}

resource "google_project_iam_member" "fleet_nodes_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.fleet_nodes.email}"
}

resource "google_project_iam_member" "fleet_nodes_metadata_writer" {
  project = var.project_id
  role    = "roles/stackdriver.resourceMetadata.writer"
  member  = "serviceAccount:${google_service_account.fleet_nodes.email}"
}

resource "google_project_iam_member" "fleet_nodes_artifact_registry_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.fleet_nodes.email}"
}

# ---------------------------------------------------------------------------
# The evaluation READ credential.
#
# Every open pull request's presubmit reads this fleet at the same time, and a
# check that can write to it spoils a fixture for whoever is running minutes
# later -- non-deterministically, and in someone else's build. The runner's own
# identity has to create GKE clusters for the eval tasks, so handing it to the
# verifiers would be handing them cluster-admin on the shared fleet.
#
# roles/container.viewer is the whole grant, verified against the live role
# rather than assumed: get/list only, no create/update/delete/patch on any
# Kubernetes object (its one *.create is container.tokenReviews.create, the
# read-shaped token validation API), and no container.secrets.* at all -- so
# this account cannot read a Secret on platform-agent-host either. It is
# project-scoped because IAM is where GKE authorization starts; the fleet
# stack's kubernetes provider only reaches seeded-a, so an RBAC-level grant
# could not cover b and c.
#
# hack/fleet-kubeconfigs.sh binds it by minting an access token
# (FLEET_READONLY_SA), which is why the token-creator grant below exists.
# Impersonation on `gcloud container clusters get-credentials` would NOT do:
# get-credentials writes an exec entry for gke-gcloud-auth-plugin, and that
# plugin has no impersonation, so the flag changes who read the cluster
# metadata and nothing about who kubectl talks to the API server as.
resource "google_service_account" "fleet_reader" {
  account_id   = "seeded-fleet-reader"
  display_name = "Read-only credential for seeded-fleet evaluation checks"
}

resource "google_project_iam_member" "fleet_reader_container_viewer" {
  project = var.project_id
  role    = "roles/container.viewer"
  member  = "serviceAccount:${google_service_account.fleet_reader.email}"
}

# Who may mint a token AS the reader. Empty by default: an eval project with no
# entry here still gets the account, and its runs fall back to the runner's own
# credential with a loud warning from fleet-kubeconfigs.sh, rather than failing
# to read the fleet at all. Populate it with the project's Prow runner identity
# to actually close the write path.
resource "google_service_account_iam_member" "fleet_reader_token_creators" {
  for_each           = toset(var.fleet_reader_token_creators)
  service_account_id = google_service_account.fleet_reader.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = each.value
}

# seeded-b is held one minor version behind whatever the REGULAR channel
# currently defaults to, derived live rather than hardcoded so the pin does
# not rot: each apply re-computes "current default minus one". The cluster
# is ENROLLED in REGULAR -- required, because the upgrade SOP's
# master-behind check compares a master's minor against its own channel's
# default, so a channel-less cluster falls out of the comparison and the
# lag is invisible. What stops enrollment from healing the lag between
# reconciles is the rolling NO_MINOR_UPGRADES maintenance exclusion on the
# seeded_b resource below, re-stamped each apply and capped by the API at
# the held minor's end of life.
#
# min_master_version is NOT a creation-time floor, and the pin depends on
# that: the field is not ForceNew and not ignored, and the provider's
# update path answers a raised value with an operator-initiated
# clusters.update carrying desiredMasterVersion -- "Only upgrade the master
# if the current version is lower than the desired version"
# (resource_container_cluster.go, the min_master_version branch). So the
# reconcile CARRIES THE MASTER FORWARD, which is exactly what keeps the pin
# from rotting: a new patch inside the held minor moves the master to it,
# and the day REGULAR's default rolls a minor, local.lagging_version
# recomputes and the next apply walks the master to the new
# default-minus-one. Both are manual upgrades in GKE's sense, and
# "manually-initiated upgrades begin immediately and ignore any maintenance
# windows" -- the exclusion never had to gate them, and does not.
#
# The provider only ever upgrades (cur < des) and a control plane cannot be
# downgraded, so the pin is one-way: if the master ever gets AHEAD of it --
# reconciles lapse past the exclusion window, or the minor reaches EOL and
# GKE auto-upgrades -- the lag is gone for good and the fix is replacing
# the cluster (`tofu apply -replace=google_container_cluster.seeded_b`).
# That replacement is not free: the drift SOP drops a cluster under 24h old
# from every cohort, which takes the comparable cohort from three to two,
# under its 3-cluster floor -- so the drift audit emits nothing for the
# WHOLE fleet for a day and consistency-drift-outlier goes red everywhere.
# Schedule it accordingly; the README's activation timeline has the detail.
# The seeded_b comment and the README carry the full account.
data "google_container_engine_versions" "fleet" {
  location = var.zone
  project  = var.project_id
}

locals {
  regular_default = data.google_container_engine_versions.fleet.release_channel_default_version["REGULAR"]
  default_minor   = tonumber(split(".", local.regular_default)[1])
  lagging_versions = [
    for v in data.google_container_engine_versions.fleet.valid_master_versions :
    v if startswith(v, "1.${local.default_minor - 1}.")
  ]
  # valid_master_versions is newest-first, so [0] is the freshest patch of the
  # previous minor: lagging by exactly one minor, not by every CVE since.
  # try(): an empty candidate list must surface as the precondition's message
  # on seeded_b, not as an index error that masks it.
  lagging_version = try(local.lagging_versions[0], null)
}

# ---------------------------------------------------------------------------
# The three clusters. A carries every namespace-level defect; B is the
# version laggard; C is the consistency outlier. One file-level rule: a
# defect lives on exactly one cluster, so a scenario red points at one place.
# ---------------------------------------------------------------------------

resource "google_container_cluster" "seeded_a" {
  name     = "${var.cluster_prefix}-a"
  location = var.zone

  remove_default_node_pool = true
  initial_node_count       = 1
  resource_labels          = local.cluster_labels

  # The fleet is standing, but deletion protection stays off: the scheduled
  # re-apply must be able to replace a cluster when a facet change requires
  # it, and the label plus README are the guard against accidental sweeps.
  deletion_protection = false

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  # Workload Identity on, and GKE_METADATA on every pool below (compliance
  # SOP 2.8/2.9): without them any pod could read the metadata server and
  # mint the seeded-fleet-nodes token -- the exact escalation the dedicated
  # minimal SA exists to close. Not a fixture; closed because it costs the
  # fixtures nothing.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # Upgrade-SOP background closure (3.4 no-channel, 3.7 no-window): a and c
  # enroll in REGULAR and take a maintenance window. Unlike seeded-b there
  # is no version fixture here, so auto-upgrade is welcome; enrollment also
  # keeps the drift release-channel facet uniform across the cohort.
  release_channel {
    channel = "REGULAR"
  }

  maintenance_policy {
    daily_maintenance_window {
      start_time = "03:00"
    }
  }

  # Half of the consistency defect: A and B run with master authorized
  # networks ON; C does not (see seeded_c). The drift SOP's severity ladder
  # walks every finding down two steps on a three-cluster cohort (r = 2/3 is
  # below both the 0.90 and 0.80 rungs), so only a base-critical facet
  # survives to a finding -- authorized-networks is one (SOP 4.5), and the
  # logging components facet this stack first used is base-minor and would
  # have been dropped before anyone saw it. The 0.0.0.0/0 block is what
  # keeps the defect access-safe: the facet reads ON from `enabled` plus a
  # non-empty cidrBlocks, the drift SOP explicitly never compares the
  # blocks' contents, and an allow-everything list restricts no eval agent.
  # The compliance audit is a different reader: its 2.10 flags a literal
  # 0.0.0.0/0 here and on seeded-b, and seeded-c's absent block, all at
  # critical. That is a DECLARED background finding, not an oversight --
  # the README's accepted-background table says why it stays open and what
  # closing it would take.
  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = "0.0.0.0/0"
      display_name = "open-for-eval"
    }
  }
}

resource "google_container_node_pool" "seeded_a_default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_a.name
  node_count = 1

  node_config {
    # e2-medium rather than e2-small: cluster A hosts the defect workloads
    # (checkout-gateway, payments-api) alongside system pods, and e2-small's
    # ~1.5 GiB allocatable cannot fit them all.
    machine_type    = "e2-medium"
    disk_size_gb    = 20
    resource_labels = local.fleet_labels
    service_account = google_service_account.fleet_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # trivy GCP-0048, and defence-in-depth beside GKE_METADATA above: the
    # v0.1/v1beta1 metadata endpoints serve unfiltered tokens.
    #
    # Declaring this key costs nothing live. GKE has set it by default since
    # 1.12 and the provider marks node_config.metadata Optional+Computed, so
    # the value written here is the value both projects' state already holds:
    # zero plan diff, verified (4 add / 11 change / 0 destroy, no pool is
    # replaced). The hazard is the other direction -- metadata is ForceNew on
    # any *diff*, so adding a second key to this map, or changing this value,
    # on a live pool REPLACES the pool. That would restart idle-batch-pool's
    # 7-day age gate and cost the cost scenario its activation clock. Edit
    # this map only on a fleet you are willing to rebuild.
    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}

# Defect (cost): a pool running zero non-system pods. The taint is what
# keeps it that way: kube-scheduler PREFERS empty nodes (least-allocated
# scoring), so without it the other defect workloads would land here and
# falsify the fixture. GKE's system daemonsets tolerate custom taints, so
# "zero non-system pods" stays exactly true. The name is the planted noun.
#
# AGE-GATED: the cost SOP's idle-nodepool check refuses "pools created
# < 7 days ago", so this fixture is invisible for its first week and any
# change that RECREATES the pool restarts that clock. Edit in place or not
# at all; the README's activation timeline is the schedule the scenario
# waits on.
resource "google_container_node_pool" "idle_batch_pool" {
  name       = "idle-batch-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_a.name
  node_count = 1

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.fleet_labels
    service_account = google_service_account.fleet_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # trivy GCP-0048. See seeded_a_default above for why this is a zero-diff
    # write and why a second key in this map must never be added to a live
    # pool.
    metadata = {
      disable-legacy-endpoints = "true"
    }

    taint {
      key    = "seeded-role"
      value  = "idle-batch"
      effect = "NO_SCHEDULE"
    }
  }
}

# Defect (capacity): a single-zone pool whose autoscaler is already at its
# maximum, hosting a workload whose HPA wants more than the pool can add
# (defects-a.tf plants the workload and the HPA). The capacity audit must
# name the pool and quantify the gap. Tainted for the same reason as
# idle-batch-pool: only inference-server (which tolerates it) may land here,
# or a stray pod could take the capacity the fixture's math depends on.
resource "google_container_node_pool" "pinned_inference_pool" {
  name       = "pinned-inference-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_a.name
  node_count = 1

  autoscaling {
    min_node_count = 1
    max_node_count = 1
  }

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.fleet_labels
    service_account = google_service_account.fleet_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # trivy GCP-0048. See seeded_a_default above for why this is a zero-diff
    # write and why a second key in this map must never be added to a live
    # pool.
    metadata = {
      disable-legacy-endpoints = "true"
    }
    labels = {
      "seeded-role" = "pinned-inference"
    }

    taint {
      key    = "seeded-role"
      value  = "pinned-inference"
      effect = "NO_SCHEDULE"
    }
  }
}

# Defect (upgrades): held one minor behind the REGULAR channel default --
# and ENROLLED in REGULAR, which is what makes the lag visible at all. The
# upgrade SOP's master-behind check keys every branch off the cluster's
# channel entry in get-server-config: branch (b), the one this defect
# exists to trip, is minor(currentMasterVersion) < minor(channel
# defaultVersion), severity major. A channel-less cluster has no channels[]
# entry, so (b)/(c) cannot evaluate, and branch (a) -- version absent from
# validMasterVersions -- is false by construction here because the pin is
# drawn from that very list. The earlier UNSPECIFIED design hid the defect
# from the audit it was planted for.
#
# What holds the lag under a channel: the maintenance exclusion below, at
# scope NO_MINOR_UPGRADES. Each re-apply stamps a fresh window from now
# (var.exclusion_window_hours, default 90 days) -- timestamp() makes this
# a perpetual in-place diff, ACCEPTED LOUDLY: the scheduled reconcile is
# what rolls the window forward, so the always-present one-line plan change
# is the mechanism working, not drift. Two ways the window ends:
#  - reconciles stop for longer than the window: GKE upgrades the master,
#    the defect self-heals, the upgrade scenario goes red -- that red is
#    the detection;
#  - the held minor reaches its end of life: the API refuses any endTime
#    past EOL (observed live on 1.34: capped at 2027-01-25), the exclusion
#    dies for good, and the same self-heal follows.
# A control plane cannot be downgraded, so recovery either way is
# replacing the cluster -- the derived pin then re-lags against the
# then-current default:
# `tofu apply -replace=google_container_cluster.seeded_b`.
resource "google_container_cluster" "seeded_b" {
  name     = "${var.cluster_prefix}-b"
  location = var.zone

  remove_default_node_pool = true
  initial_node_count       = 1
  resource_labels          = local.cluster_labels
  deletion_protection      = false

  # Assumes the freshest previous-minor patch is offered in REGULAR, which
  # holds in practice (the channel serves ~3 minors); if GKE ever rejects
  # the create, re-derive the pin from the channel's own list.
  min_master_version = local.lagging_version
  release_channel {
    channel = "REGULAR"
  }

  maintenance_policy {
    # A window is required alongside an exclusion; 03:00 UTC keeps patch
    # work clear of eval hours. Minors are excluded below; patches within
    # the lagged minor may roll, which keeps the defect exactly "one minor
    # behind", never "unpatched".
    daily_maintenance_window {
      start_time = "03:00"
    }

    maintenance_exclusion {
      exclusion_name = "hold-the-minor-lag"
      start_time     = timestamp()
      end_time       = timeadd(timestamp(), "${var.exclusion_window_hours}h")
      exclusion_options {
        scope = "NO_MINOR_UPGRADES"
      }
    }
  }

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  # Workload Identity on, and GKE_METADATA on every pool below (compliance
  # SOP 2.8/2.9): without them any pod could read the metadata server and
  # mint the seeded-fleet-nodes token -- the exact escalation the dedicated
  # minimal SA exists to close. Not a fixture; closed because it costs the
  # fixtures nothing.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  # The other peer of the consistency majority: B matches A (authorized
  # networks ON, open block -- see seeded_a for why this is safe), so C is
  # the single outlier on a single facet.
  master_authorized_networks_config {
    cidr_blocks {
      cidr_block   = "0.0.0.0/0"
      display_name = "open-for-eval"
    }
  }

  lifecycle {
    precondition {
      condition     = length(local.lagging_versions) > 0
      error_message = "No valid master version exists one minor behind the REGULAR default; pick the lag manually this cycle."
    }
  }
}

resource "google_container_node_pool" "seeded_b_default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_b.name
  node_count = 1
  version    = local.lagging_version

  # The pool takes the SAME derived pin as the master, and is deliberately
  # not ignore_changes'd: min_master_version carries the control plane
  # forward on every reconcile (see the block above), so a frozen pool
  # would skew against it. Freezing it costs a finding in both directions:
  # at the next REGULAR minor roll the master moves and the pool does not,
  # which is upgrade SOP 3.2 `pool-skew` -- exactly one minor behind with
  # autoUpgrade true, severity minor -- and even without a minor roll the
  # same asymmetry leaks at patch level, because GKE auto-upgrades the pool
  # to the channel's default patch while the pin pushes the master to the
  # freshest patch of the minor, which 3.2 flags as patch-only drift.
  # Either would be an undeclared finding on a fleet whose whole premise is
  # that a correct audit's findings are known in advance. Driving both from
  # local.lagging_version keeps them equal at steady state; the pool
  # depends on the cluster, so terraform updates the master first and the
  # pool follows, which is the only ordering GKE accepts.
  #
  # Channel enrollment requires node auto-upgrade on -- the API rejects
  # false. Between reconciles that auto-upgrade may roll patches within the
  # held minor, and the NO_MINOR_UPGRADES exclusion is what keeps it from
  # crossing into the next one.
  management {
    auto_upgrade = true
  }

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.fleet_labels
    service_account = google_service_account.fleet_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # trivy GCP-0048. See seeded_a_default above for why this is a zero-diff
    # write and why a second key in this map must never be added to a live
    # pool.
    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}

# Defect (consistency): the outlier. A and B run master authorized networks
# ON; C carries no authorized-networks config at all, which the drift SOP
# normalizes to OFF -- absence on one cluster against an ON majority is the
# finding (SOP 4.5, base critical, the severity that survives a
# three-cluster cohort's two ladder steps). Two agree, one differs: the
# reason this fleet is three clusters and not two. Everything else on C
# deliberately matches its peers, so C is an outlier on exactly one facet
# and the split-cluster guard never mistakes it for an uncohorted cluster.
resource "google_container_cluster" "seeded_c" {
  name     = "${var.cluster_prefix}-c"
  location = var.zone

  remove_default_node_pool = true
  initial_node_count       = 1
  resource_labels          = local.cluster_labels
  deletion_protection      = false

  logging_config {
    enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"]
  }

  # Upgrade-SOP background closure, as on seeded-a: REGULAR channel and a
  # window. The consistency defect above is authorized networks alone.
  release_channel {
    channel = "REGULAR"
  }

  maintenance_policy {
    daily_maintenance_window {
      start_time = "03:00"
    }
  }

  # Workload Identity on, and GKE_METADATA on every pool below (compliance
  # SOP 2.8/2.9): without them any pod could read the metadata server and
  # mint the seeded-fleet-nodes token -- the exact escalation the dedicated
  # minimal SA exists to close. Not a fixture; closed because it costs the
  # fixtures nothing.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
}

resource "google_container_node_pool" "seeded_c_default" {
  name       = "default-pool"
  location   = var.zone
  cluster    = google_container_cluster.seeded_c.name
  node_count = 1

  node_config {
    machine_type    = "e2-small"
    disk_size_gb    = 20
    resource_labels = local.fleet_labels
    service_account = google_service_account.fleet_nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]

    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # trivy GCP-0048. See seeded_a_default above for why this is a zero-diff
    # write and why a second key in this map must never be added to a live
    # pool.
    metadata = {
      disable-legacy-endpoints = "true"
    }
  }
}

# Defect (cost): two unattached disks. The prefix is the planted noun the
# waste audit must name; nothing ever mounts them.
#
# AGE-GATED, harder than the pool: the SOP's unattached-disk collector
# filters server-side on `creationTimestamp<-P30D` and flags at AGE >= 30d,
# so these are invisible to the audit for their first THIRTY days, and any
# change that recreates them (name, size, type, zone all force replacement)
# silently restarts that clock. creationTimestamp is server-set and
# immutable -- backdating is impossible, do not try. Label updates are the
# one safe change (in-place, never touches the timestamp); everything else
# here forces replacement and is therefore not worth changing at all.
resource "google_compute_disk" "orphan_pd" {
  count = 2
  name  = "orphan-pd-${count.index + 1}"
  type  = "pd-standard"
  size  = 10
  zone  = var.zone

  labels = local.fleet_labels
}
