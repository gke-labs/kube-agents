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

# GKE-managed Config Sync on seeded-a (#6905), the reconciler the post-merge
# fixture's budget goes through: a fleet membership for the cluster and the
# configmanagement feature turned on for it, with no git source, so no
# RootSync exists. The fixture's RepoSync lives with the fixture.
#
# Off unless the project is listed in var.post_merge_sync_projects. A project
# is listed only once its pool steps are done (docs/ci-pool-projects.md 5.13):
# the reconciler holds roles/gkehub.editor, anthosconfigmanagement.googleapis.com
# is enabled, and the deploy key's secret exists. An unlisted project, a pool
# project or a dev one, plans and applies none of this and reads no secret.
#
# Not a defect, and not a drift facet (fleet_consistency_drift_sop.md section
# 4): a membership changes no field the drift audit compares.

locals {
  post_merge_sync_enabled = contains(var.post_merge_sync_projects, var.project_id)
  # The fleet feature is one per project, and a membership one per cluster.
  fleet_location = "global"
}

resource "google_gke_hub_membership" "seeded_a" {
  count = local.post_merge_sync_enabled ? 1 : 0

  membership_id = google_container_cluster.seeded_a.name
  location      = local.fleet_location
  labels        = local.fleet_labels
  endpoint {
    gke_cluster {
      resource_link = "//container.googleapis.com/${google_container_cluster.seeded_a.id}"
    }
  }
}

resource "google_gke_hub_feature" "configmanagement" {
  count = local.post_merge_sync_enabled ? 1 : 0

  name     = "configmanagement"
  location = local.fleet_location
}

resource "google_gke_hub_feature_membership" "seeded_a_config_sync" {
  count = local.post_merge_sync_enabled ? 1 : 0

  location   = local.fleet_location
  feature    = google_gke_hub_feature.configmanagement[0].name
  membership = google_gke_hub_membership.seeded_a[0].membership_id
  configmanagement {
    management = "MANAGEMENT_AUTOMATIC"
    config_sync {
      enabled = true
    }
  }
}
