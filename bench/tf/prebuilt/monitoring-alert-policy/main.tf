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

# The fixture for bench/tasks/monitoring-alert-policy-read: one Cloud
# Monitoring alert policy in the install's project, with a display name no
# other policy carries. The case asks which alert policies the project has and
# whether each is enabled; only an agent that can run `gcloud monitoring
# policies list` (or `describe`) through the credential broker can name it.
#
# The policy is disabled and names no notification channel, so it never fires
# and never notifies anyone. Its documentation text is written like a real
# policy's, so it neither states the answer nor tells the agent it is a test.
# Its condition watches container restarts against a threshold no workload
# reaches. `teardown: true` in the task destroys it.

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0.0"
    }
  }
}

provider "google" {
  project = var.project_id != "" ? var.project_id : null
}

resource "google_monitoring_alert_policy" "fixture" {
  display_name = var.policy_display_name
  combiner     = "OR"
  enabled      = false

  documentation {
    content   = "Restart storm on the orbital-checkout service. Page the checkout on-call."
    mime_type = "text/markdown"
  }

  # The same identity labels the per-run clusters carry, so a policy that a
  # killed run left behind can be found by label and deleted.
  user_labels = {
    managed-by  = "kube-agents-bench"
    build-id    = var.prow_build_id != "" ? var.prow_build_id : "local"
    pull-number = var.prow_pull_number != "" ? var.prow_pull_number : "none"
  }

  conditions {
    display_name = "container restarts above 1000 in 5 minutes"
    condition_threshold {
      filter          = "resource.type = \"k8s_container\" AND metric.type = \"kubernetes.io/container/restart_count\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1000
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_DELTA"
      }
    }
  }
}

output "policy_name" {
  value = google_monitoring_alert_policy.fixture.name
}

output "cluster_name" {
  value = var.host_cluster_name
}

output "cluster_location" {
  value = var.host_cluster_location
}
